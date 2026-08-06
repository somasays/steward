"""Unit tests: the pieces that hold without a database.

Backoff policy, the dedup natural key, the registry contract's bookkeeping,
and the Alembic URL binding.
"""

from datetime import timedelta

import pytest
from steward_queue import NOOP_TASK_TYPE, REGISTRY, dedup_key_for, registered_types, retry_delay
from steward_queue.backoff import DEFAULT_MAX_DELAY
from steward_queue.handlers import noop
from steward_queue.migrate import sqlalchemy_url
from steward_queue.registry import UnknownTaskType, get_handler, task_handler


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
