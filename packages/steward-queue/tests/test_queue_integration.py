"""Integration tests against a real Postgres.

Every assertion here is about observable state -- rows, states, audit trail --
not about how the queue got there.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from steward_queue import (
    RunStatus,
    TaskNotClaimable,
    TaskState,
    claim,
    complete,
    create_run,
    enqueue,
    fail,
    get_run,
    get_task,
    mark_running,
    requeue_stale,
    set_run_status,
    write_checkpoint,
)
from steward_queue.db import QueueConnection
from steward_schemas import ProblemDetails, RunBudget, TaskResult, TaskSpec, TaskStatus

SELECT_AUDIT_ACTIONS = "SELECT action FROM audit_log ORDER BY id"
COUNT_TASKS = "SELECT count(*) FROM tasks"
SELECT_TASK_LAST_ERROR = "SELECT last_error FROM tasks WHERE id = %s"
SELECT_AUDIT_BEFORE = "SELECT before FROM audit_log WHERE action = %s AND entity_id = %s"
SELECT_AUDIT_AFTER = "SELECT after FROM audit_log WHERE action = %s AND entity_id = %s"
SELECT_CHECKPOINT_STATE = "SELECT state FROM checkpoints WHERE task_id = %s AND step = %s"

NO_BACKOFF = timedelta(0)
EXPIRED_LEASE = timedelta(seconds=-1)


def audit_actions(conn: QueueConnection) -> list[str]:
    return [row[0] for row in conn.execute(SELECT_AUDIT_ACTIONS).fetchall()]


def scalar(conn: QueueConnection, sql: str, *params: object) -> object:
    row = conn.execute(sql, params if params else None).fetchone()
    assert row is not None
    return row[0]


def succeed(spec: TaskSpec, *, steps: int = 1) -> TaskResult:
    return TaskResult(
        task_id=spec.task_id,
        status=TaskStatus.SUCCEEDED,
        usage=RunBudget(
            steps=steps, tokens=42, cost_usd=Decimal("0.010000"), wall_clock=timedelta(seconds=2)
        ),
        output={"ok": True},
    )


def boom() -> ProblemDetails:
    return ProblemDetails(type="urn:steward:test", title="boom", status=500)


class TestRuns:
    def test_run_carries_its_budget(self, conn: QueueConnection, budget: RunBudget) -> None:
        created = create_run(conn, goal="scan", budget=budget, trace_id="trace-1")
        conn.commit()
        fetched = get_run(conn, created.id)
        assert fetched is not None
        assert fetched.budget == budget
        assert fetched.usage == RunBudget(steps=0, tokens=0, cost_usd=Decimal(0), wall_clock=timedelta(0))
        assert fetched.trace_id == "trace-1"
        assert fetched.status is RunStatus.PENDING

    def test_missing_run_is_none(self, conn: QueueConnection) -> None:
        assert get_run(conn, uuid4()) is None

    def test_status_change_is_audited(self, conn: QueueConnection, run_id: UUID) -> None:
        set_run_status(conn, run_id, RunStatus.RUNNING)
        conn.commit()
        fetched = get_run(conn, run_id)
        assert fetched is not None and fetched.status is RunStatus.RUNNING
        assert audit_actions(conn) == ["run.created", "run.status_changed"]

    def test_status_change_on_missing_run_raises(self, conn: QueueConnection) -> None:
        with pytest.raises(LookupError):
            set_run_status(conn, uuid4(), RunStatus.RUNNING)


class TestEnqueue:
    def test_enqueue_is_invisible_until_the_caller_commits(
        self,
        conn: QueueConnection,
        open_conn: Callable[[], QueueConnection],
        run_id: UUID,
        spec_factory: Callable[..., TaskSpec],
    ) -> None:
        # I8: the task belongs to the transaction that created it.
        enqueue(conn, spec_factory(run_id))
        other = open_conn()
        assert scalar(other, COUNT_TASKS) == 0
        conn.commit()
        assert scalar(other, COUNT_TASKS) == 1

    def test_enqueue_and_its_audit_row_roll_back_together(
        self, conn: QueueConnection, run_id: UUID, spec_factory: Callable[..., TaskSpec]
    ) -> None:
        # I7: the audit row is not a separate write that can survive on its own.
        enqueue(conn, spec_factory(run_id))
        conn.rollback()
        assert scalar(conn, COUNT_TASKS) == 0
        assert "task.enqueued" not in audit_actions(conn)

    def test_identical_payloads_dedup_within_a_run(
        self, conn: QueueConnection, run_id: UUID, spec_factory: Callable[..., TaskSpec]
    ) -> None:
        first = spec_factory(run_id, payload={"table": "public.orders"})
        second = spec_factory(run_id, payload={"table": "public.orders"})
        first_id = enqueue(conn, first)
        second_id = enqueue(conn, second)
        conn.commit()
        assert first_id == second_id == first.task_id
        assert scalar(conn, COUNT_TASKS) == 1
        assert audit_actions(conn).count("task.enqueued") == 1

    def test_explicit_dedup_key_allows_lookalike_tasks(
        self, conn: QueueConnection, run_id: UUID, spec_factory: Callable[..., TaskSpec]
    ) -> None:
        payload = {"table": "public.orders"}
        enqueue(conn, spec_factory(run_id, payload=payload), dedup_key="shard-1")
        enqueue(conn, spec_factory(run_id, payload=payload), dedup_key="shard-2")
        conn.commit()
        assert scalar(conn, COUNT_TASKS) == 2

    def test_scheduled_tasks_are_not_claimable_yet(
        self, conn: QueueConnection, run_id: UUID, spec_factory: Callable[..., TaskSpec]
    ) -> None:
        enqueue(
            conn,
            spec_factory(run_id),
            available_at=datetime.now(UTC) + timedelta(hours=1),
        )
        conn.commit()
        assert claim(conn, worker_id="w1") == []


class TestLifecycle:
    def test_enqueue_claim_succeed(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued()
        [claimed] = claim(conn, worker_id="w1")
        conn.commit()
        assert claimed.spec == spec
        assert claimed.attempts == 1
        assert claimed.claimed_by == "w1"

        mark_running(conn, spec.task_id)
        complete(conn, succeed(spec, steps=3))
        conn.commit()

        task = get_task(conn, spec.task_id)
        assert task is not None
        assert task.state is TaskState.SUCCEEDED
        assert task.finished_at is not None
        assert task.lease_expires_at is None

        run = get_run(conn, run_id)
        assert run is not None
        assert run.usage.steps == 3
        assert run.usage.tokens == 42
        assert run.usage.cost_usd == Decimal("0.010000")
        assert run.usage.wall_clock == timedelta(seconds=2)

        assert audit_actions(conn) == [
            "run.created",
            "task.enqueued",
            "task.claimed",
            "task.started",
            "task.succeeded",
            "run.usage_recorded",
        ]

    def test_run_spend_is_audited_as_a_mutation_of_the_run(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        # I7: "how did this run reach its cap" needs a row per increment, on the
        # run entity -- not one row about the task that caused it.
        spec = queued()
        claim(conn, worker_id="w1")
        complete(conn, succeed(spec, steps=3))
        conn.commit()
        before = scalar(conn, SELECT_AUDIT_BEFORE, "run.usage_recorded", str(run_id))
        after = scalar(conn, SELECT_AUDIT_AFTER, "run.usage_recorded", str(run_id))
        assert isinstance(before, dict) and isinstance(after, dict)
        assert before["steps"] == 0
        assert after["steps"] == 3
        assert after["task_id"] == str(spec.task_id)

    def test_the_audit_row_records_the_state_it_actually_replaced(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        # Completing straight from `claimed` (no `mark_running`) must not be
        # audited as if it had replaced `running`.
        spec = queued()
        claim(conn, worker_id="w1")
        complete(conn, succeed(spec))
        conn.commit()
        before = scalar(conn, SELECT_AUDIT_BEFORE, "task.succeeded", str(spec.task_id))
        assert before == {"state": TaskState.CLAIMED.value}

    def test_claim_only_returns_requested_task_types(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        queued(task_type="profile_table", payload={"n": 1})
        wanted = queued(task_type="noop", payload={"n": 2})
        [claimed] = claim(conn, worker_id="w1", task_types=["noop"])
        assert claimed.spec.task_id == wanted.task_id

    def test_claim_returns_nothing_when_the_queue_is_empty(self, conn: QueueConnection) -> None:
        assert claim(conn, worker_id="w1") == []

    def test_completing_an_unclaimed_task_is_refused(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued()
        with pytest.raises(TaskNotClaimable):
            complete(conn, succeed(spec))

    def test_starting_an_unclaimed_task_is_refused(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued()
        with pytest.raises(TaskNotClaimable):
            mark_running(conn, spec.task_id)

    def test_failing_an_unclaimed_task_is_refused(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued()
        with pytest.raises(TaskNotClaimable):
            fail(conn, spec.task_id, boom())

    def test_failing_a_missing_task_raises(self, conn: QueueConnection) -> None:
        with pytest.raises(LookupError):
            fail(conn, uuid4(), boom())

    def test_missing_task_is_none(self, conn: QueueConnection) -> None:
        assert get_task(conn, uuid4()) is None


class TestRetryAndDeadLetter:
    def test_failure_reschedules_into_the_future(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued()
        claim(conn, worker_id="w1")
        landed = fail(conn, spec.task_id, boom())
        conn.commit()
        assert landed is TaskState.PENDING

        task = get_task(conn, spec.task_id)
        assert task is not None
        assert task.state is TaskState.PENDING
        assert task.claimed_by is None
        assert task.available_at > datetime.now(UTC)
        # A rescheduled task is not claimable until its backoff elapses.
        assert claim(conn, worker_id="w1") == []

    def test_attempts_are_spent_then_the_task_dies(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(max_attempts=2)

        [first] = claim(conn, worker_id="w1")
        assert first.attempts == 1
        mark_running(conn, spec.task_id)
        assert fail(conn, spec.task_id, boom(), base_delay=NO_BACKOFF) is TaskState.PENDING
        conn.commit()

        [second] = claim(conn, worker_id="w2")
        assert second.attempts == 2
        mark_running(conn, spec.task_id)
        assert fail(conn, spec.task_id, boom(), base_delay=NO_BACKOFF) is TaskState.DEAD
        conn.commit()

        task = get_task(conn, spec.task_id)
        assert task is not None
        assert task.state is TaskState.DEAD
        assert task.attempts == 2
        assert task.finished_at is not None
        assert claim(conn, worker_id="w3") == []

        last_error = scalar(conn, SELECT_TASK_LAST_ERROR, spec.task_id)
        assert isinstance(last_error, dict)
        assert last_error["title"] == "boom"

        actions = audit_actions(conn)
        assert actions.count("task.retry_scheduled") == 1
        assert actions.count("task.dead") == 1

    def test_non_retryable_failure_is_terminal_immediately(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(max_attempts=5)
        claim(conn, worker_id="w1")
        assert fail(conn, spec.task_id, boom(), retryable=False) is TaskState.FAILED
        conn.commit()
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.FAILED
        assert "task.failed" in audit_actions(conn)


class TestFencing:
    def test_a_stale_worker_cannot_move_a_task_someone_else_now_holds(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        """The claim is fenced by worker id, not just by state.

        Worker "a" stalls past its lease, a reaper requeues the task, worker "b"
        claims it. Without the fence, a's late complete/fail would pass the
        state check and stomp b's claim while b is still executing it.
        """
        spec = queued()
        claim(conn, worker_id="a", lease=EXPIRED_LEASE)
        conn.commit()
        requeue_stale(conn)
        conn.commit()
        claim(conn, worker_id="b")
        conn.commit()

        with pytest.raises(TaskNotClaimable):
            mark_running(conn, spec.task_id, claimed_by="a")
        with pytest.raises(TaskNotClaimable):
            complete(conn, succeed(spec), claimed_by="a")
        with pytest.raises(TaskNotClaimable):
            fail(conn, spec.task_id, boom(), claimed_by="a")

        # The holder is unaffected and can still finish it.
        complete(conn, succeed(spec), claimed_by="b")
        conn.commit()
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.SUCCEEDED

    def test_an_unfenced_call_still_works_for_a_single_owner(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        # Orchestrator-side callers that are not workers may omit the token.
        spec = queued()
        claim(conn, worker_id="a")
        complete(conn, succeed(spec))
        conn.commit()
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.SUCCEEDED


class TestLeaseRecovery:
    def test_expired_lease_returns_the_task_to_pending(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued()
        claim(conn, worker_id="crashed", lease=EXPIRED_LEASE)
        conn.commit()

        recovered = requeue_stale(conn)
        conn.commit()
        assert recovered == [(spec.task_id, TaskState.PENDING)]

        task = get_task(conn, spec.task_id)
        assert task is not None
        assert task.state is TaskState.PENDING
        assert task.attempts == 1  # the crashed attempt is remembered, not forgotten
        assert task.claimed_by is None
        assert claim(conn, worker_id="w2")[0].attempts == 2

    def test_expired_lease_on_a_spent_task_dead_letters_it(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(max_attempts=1)
        claim(conn, worker_id="crashed", lease=EXPIRED_LEASE)
        conn.commit()
        assert requeue_stale(conn) == [(spec.task_id, TaskState.DEAD)]
        conn.commit()
        assert "task.lease_expired" in audit_actions(conn)

    def test_live_leases_are_left_alone(self, conn: QueueConnection, queued: Callable[..., TaskSpec]) -> None:
        queued()
        claim(conn, worker_id="w1", lease=timedelta(minutes=5))
        conn.commit()
        assert requeue_stale(conn) == []


class TestSkipLocked:
    def test_concurrent_claims_are_disjoint(
        self,
        conn: QueueConnection,
        open_conn: Callable[[], QueueConnection],
        queued: Callable[..., TaskSpec],
    ) -> None:
        """The SKIP LOCKED proof: two workers claiming at the same time.

        Worker A's claim transaction is still open -- its rows are locked -- when
        worker B claims. B skips the locked rows instead of blocking on them, so
        the two sets are disjoint and no task is handed to two handlers.
        """
        specs = [queued(payload={"n": n}) for n in range(4)]

        worker_a = open_conn()
        worker_b = open_conn()
        claimed_a = claim(worker_a, worker_id="a", limit=2)
        claimed_b = claim(worker_b, worker_id="b", limit=2)  # A has not committed yet
        worker_a.commit()
        worker_b.commit()

        ids_a = {c.spec.task_id for c in claimed_a}
        ids_b = {c.spec.task_id for c in claimed_b}
        assert len(ids_a) == len(ids_b) == 2
        assert ids_a.isdisjoint(ids_b)
        assert ids_a | ids_b == {s.task_id for s in specs}

        for spec in specs:
            task = get_task(conn, spec.task_id)
            assert task is not None
            assert task.state is TaskState.CLAIMED
            assert task.attempts == 1  # claimed exactly once, by exactly one worker

    def test_a_claimed_task_is_not_offered_again(
        self, conn: QueueConnection, open_conn: Callable[[], QueueConnection], queued: Callable[..., TaskSpec]
    ) -> None:
        queued()
        claim(conn, worker_id="a")
        conn.commit()
        assert claim(open_conn(), worker_id="b") == []


class TestCheckpoints:
    def test_checkpoint_upsert_overwrites_the_step(
        self, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued()
        write_checkpoint(conn, spec.task_id, step=0, state={"messages": 1})
        write_checkpoint(conn, spec.task_id, step=0, state={"messages": 2})
        conn.commit()
        rows = conn.execute(SELECT_CHECKPOINT_STATE, (spec.task_id, 0)).fetchall()
        assert [row[0] for row in rows] == [{"messages": 2}]
