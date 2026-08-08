"""Unit tests: the pieces that hold without a database.

Backoff policy, the dedup natural key, the registry contract's bookkeeping,
and the Alembic URL binding.
"""

import threading
import time
from collections.abc import Callable
from datetime import timedelta

import pytest
from steward_queue import NOOP_TASK_TYPE, REGISTRY, dedup_key_for, registered_types, retry_delay
from steward_queue.backoff import DEFAULT_MAX_DELAY
from steward_queue.db import MIN_STATEMENT_TIMEOUT_MS, statement_timeout_ms
from steward_queue.execution import Handoff
from steward_queue.handlers import noop
from steward_queue.migrate import sqlalchemy_url
from steward_queue.registry import UnknownTaskType, get_handler, task_handler

THREADS = 8


def wait_for(condition: Callable[[], bool], *, within: float = 5.0) -> bool:
    """Poll `condition` until it holds. Returns whether it did."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


class TestStatementTimeout:
    """Translating a wall-clock budget into the server-side cap that makes it
    enforceable (I12)."""

    def test_a_budget_becomes_milliseconds(self) -> None:
        assert statement_timeout_ms(timedelta(seconds=2.5)) == 2500

    @pytest.mark.parametrize("budget", [timedelta(0), timedelta(seconds=-5), timedelta(microseconds=1)])
    def test_a_spent_budget_never_becomes_postgres_no_timeout(self, budget: timedelta) -> None:
        # Postgres reads 0 as "unlimited", which is the exact inverse of what a
        # zero or negative wall-clock budget means: already exhausted, fail now.
        assert statement_timeout_ms(budget) == MIN_STATEMENT_TIMEOUT_MS
        assert statement_timeout_ms(budget) > 0

    def test_sub_millisecond_precision_truncates_rather_than_disables(self) -> None:
        assert statement_timeout_ms(timedelta(milliseconds=1.9)) == 1


class TestBackoff:
    def test_first_attempt_waits_the_base_delay(self) -> None:
        assert retry_delay(1, base=timedelta(seconds=2)) == timedelta(seconds=2)

    def test_delay_doubles_per_attempt(self) -> None:
        base = timedelta(seconds=1)
        assert [retry_delay(n, base=base) for n in (1, 2, 3, 4)] == [
            timedelta(seconds=1),
            timedelta(seconds=2),
            timedelta(seconds=4),
            timedelta(seconds=8),
        ]

    def test_delay_is_clamped(self) -> None:
        assert retry_delay(50) == DEFAULT_MAX_DELAY

    def test_an_absurd_attempt_count_does_not_overflow(self) -> None:
        # The failure path must never raise: a closed-form power would.
        assert retry_delay(10_000) == DEFAULT_MAX_DELAY

    def test_unrecorded_attempt_gets_the_base_delay(self) -> None:
        # The failure path must never raise on bookkeeping it did not expect.
        assert retry_delay(0) == retry_delay(1)

    def test_a_zero_base_disables_backoff(self) -> None:
        assert retry_delay(5, base=timedelta(0)) == timedelta(0)

    def test_a_non_growing_factor_is_flat(self) -> None:
        assert retry_delay(5, base=timedelta(seconds=3), factor=1.0) == timedelta(seconds=3)

    def test_a_base_beyond_the_cap_is_capped(self) -> None:
        assert retry_delay(1, base=timedelta(hours=1), cap=timedelta(minutes=1)) == timedelta(minutes=1)


class TestDedupKey:
    def test_key_is_stable_across_key_order(self) -> None:
        assert dedup_key_for("t", {"a": 1, "b": 2}) == dedup_key_for("t", {"b": 2, "a": 1})

    def test_key_separates_task_types(self) -> None:
        assert dedup_key_for("a", {"x": 1}) != dedup_key_for("b", {"x": 1})

    def test_key_separates_payloads(self) -> None:
        assert dedup_key_for("t", {"x": 1}) != dedup_key_for("t", {"x": 2})


class TestRegistry:
    def test_noop_is_registered(self) -> None:
        assert NOOP_TASK_TYPE in REGISTRY
        assert REGISTRY[NOOP_TASK_TYPE].fn is noop

    def test_every_registration_carries_a_sample_payload(self) -> None:
        # Registry contract clause 3 -- without it H1 has nothing to run.
        assert all(r.sample_payload is not None for r in REGISTRY.values())

    def test_registered_types_are_sorted(self) -> None:
        assert registered_types() == tuple(sorted(REGISTRY))

    def test_duplicate_registration_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="already registered"):
            task_handler(NOOP_TASK_TYPE, sample_payload={})(noop)

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(UnknownTaskType):
            get_handler("no-such-task-type")


class TestSqlalchemyUrl:
    @pytest.mark.parametrize(
        "dsn",
        ["postgresql://u@h/db", "postgres://u@h/db", "postgresql+psycopg://u@h/db"],
    )
    def test_every_accepted_form_binds_psycopg(self, dsn: str) -> None:
        assert sqlalchemy_url(dsn).startswith("postgresql+psycopg://")

    def test_query_parameters_survive(self) -> None:
        assert sqlalchemy_url("postgresql://u@/db?host=/tmp/sock").endswith("/db?host=/tmp/sock")

    def test_non_postgres_dsn_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a PostgreSQL DSN"):
            sqlalchemy_url("mysql://u@h/db")


class TestHandoff:
    """Exactly one context records an attempt (execution.py, SPEC.md D7).

    The handler thread and the event loop both reach the point of writing a
    terminal state for the same attempt. Two of them writing would count the
    attempt twice, or record an outcome for a task another worker has since
    taken over -- so the property is not "rarely both", it is "never both".
    """

    def test_only_the_first_caller_takes_it(self) -> None:
        handoff = Handoff()
        assert handoff.take() is True
        assert handoff.take() is False

    def test_only_one_of_many_threads_takes_it(self) -> None:
        handoff = Handoff()
        start = threading.Barrier(THREADS)
        winners: list[bool] = []
        lock = threading.Lock()

        def contend() -> None:
            start.wait()
            taken = handoff.take()
            with lock:
                winners.append(taken)

        threads = [threading.Thread(target=contend) for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert winners.count(True) == 1

    def test_a_backend_that_was_never_published_is_none(self) -> None:
        # The loop asks before the thread has opened its connection: there is
        # nothing to end, and that is not an error.
        assert Handoff().backend_pid() is None

    def test_a_published_backend_is_readable_from_another_thread(self) -> None:
        handoff = Handoff()
        threading.Thread(target=lambda: handoff.publish(4242)).start()
        assert wait_for(lambda: handoff.backend_pid() == 4242)
