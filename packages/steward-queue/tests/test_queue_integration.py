"""Integration tests against a real Postgres.

Every assertion here is about observable state -- rows, states, audit trail --
not about how the queue got there.
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from steward_queue import (
    RunStatus,
    TaskNotClaimable,
    TaskState,
    bind_idempotency_key,
    claim,
    claim_single_flight,
    complete,
    create_run,
    enqueue,
    fail,
    get_run,
    get_task,
    mark_running,
    requeue_stale,
    rollup_run_status,
    set_run_status,
    write_checkpoint,
)
from steward_queue.db import QueueConnection
from steward_schemas import ProblemDetails, RunBudget, TaskResult, TaskSpec, TaskStatus
from steward_telemetry import new_trace_id

SELECT_AUDIT_ACTIONS = "SELECT action FROM audit_log ORDER BY id"
COUNT_TASKS = "SELECT count(*) FROM tasks"
COUNT_RUNS = "SELECT count(*) FROM runs"
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

    def test_a_run_always_has_a_trace_id(self, conn: QueueConnection, budget: RunBudget) -> None:
        # I7: no caller has to remember to pass one, and none can opt out.
        created = create_run(conn, goal="scan", budget=budget)
        conn.commit()
        assert created.trace_id == new_trace_id(seed=str(created.id))

    def test_the_payload_is_stored_on_the_run(self, conn: QueueConnection, budget: RunBudget) -> None:
        created = create_run(conn, goal="scan", budget=budget, payload={"source_id": "abc"})
        conn.commit()
        fetched = get_run(conn, created.id)
        assert fetched is not None and fetched.payload == {"source_id": "abc"}

    def test_replaying_an_idempotency_key_returns_the_first_run(
        self, conn: QueueConnection, budget: RunBudget
    ) -> None:
        first = create_run(conn, goal="scan", budget=budget, idempotency_key="retry-1")
        conn.commit()
        second = create_run(conn, goal="scan", budget=budget, idempotency_key="retry-1")
        conn.commit()
        assert second == first
        assert scalar(conn, COUNT_RUNS) == 1
        assert audit_actions(conn) == ["run.created"]  # nothing was created the second time

    def test_different_idempotency_keys_create_different_runs(
        self, conn: QueueConnection, budget: RunBudget
    ) -> None:
        first = create_run(conn, goal="scan", budget=budget, idempotency_key="a")
        second = create_run(conn, goal="scan", budget=budget, idempotency_key="b")
        conn.commit()
        assert first.id != second.id

    def test_runs_without_a_key_never_collide(self, conn: QueueConnection, budget: RunBudget) -> None:
        # The unique index is partial for exactly this reason: NULL keys must
        # not be "the same key".
        for _ in range(3):
            create_run(conn, goal="scan", budget=budget)
        conn.commit()
        assert scalar(conn, COUNT_RUNS) == 3


class TestRunStatusRollup:
    """The run's status follows its tasks, decided in their transactions."""

    def test_a_run_starts_running_when_its_first_task_does(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued()
        claim(conn, worker_id="w1")
        mark_running(conn, spec.task_id)
        conn.commit()
        run = get_run(conn, run_id)
        assert run is not None and run.status is RunStatus.RUNNING

    def test_a_second_task_starting_does_not_re_announce_running(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        first = queued(payload={"n": 1})
        second = queued(payload={"n": 2})
        claim(conn, worker_id="w1", limit=2)
        mark_running(conn, first.task_id)
        mark_running(conn, second.task_id)
        conn.commit()
        assert audit_actions(conn).count("run.status_changed") == 1

    def test_a_run_stays_running_while_any_task_is_outstanding(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        first = queued(payload={"n": 1})
        queued(payload={"n": 2})
        claim(conn, worker_id="w1", limit=2)
        mark_running(conn, first.task_id)
        complete(conn, succeed(first))
        conn.commit()
        run = get_run(conn, run_id)
        assert run is not None and run.status is RunStatus.RUNNING

    def test_a_run_succeeds_when_its_last_task_succeeds(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        specs = [queued(payload={"n": n}) for n in range(2)]
        claim(conn, worker_id="w1", limit=2)
        for spec in specs:
            complete(conn, succeed(spec))
        conn.commit()
        run = get_run(conn, run_id)
        assert run is not None and run.status is RunStatus.SUCCEEDED

    def test_one_lost_task_fails_the_whole_run(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        good = queued(payload={"n": 1})
        bad = queued(payload={"n": 2}, max_attempts=1)
        claim(conn, worker_id="w1", limit=2)
        complete(conn, succeed(good))
        fail(conn, bad.task_id, boom())
        conn.commit()
        run = get_run(conn, run_id)
        assert run is not None and run.status is RunStatus.FAILED

    def test_a_scheduled_retry_leaves_the_run_in_flight(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(max_attempts=3)
        claim(conn, worker_id="w1")
        assert fail(conn, spec.task_id, boom(), base_delay=NO_BACKOFF) is TaskState.PENDING
        conn.commit()
        run = get_run(conn, run_id)
        assert run is not None and run.status is not RunStatus.FAILED

    def test_a_terminal_run_is_not_moved_again(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        # Re-running the rollup must be a no-op, so a replayed transition
        # cannot rewrite the outcome of a finished run.
        spec = queued()
        claim(conn, worker_id="w1")
        complete(conn, succeed(spec))
        conn.commit()
        assert rollup_run_status(conn, run_id) is None
        conn.commit()
        assert audit_actions(conn).count("run.status_changed") == 1

    def test_a_cancelled_run_is_never_rolled_up(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        # Cancellation is an operator decision; finishing the work it left
        # behind must not quietly relabel the run as successful.
        spec = queued()
        claim(conn, worker_id="w1")
        set_run_status(conn, run_id, RunStatus.CANCELLED)
        complete(conn, succeed(spec))
        conn.commit()
        run = get_run(conn, run_id)
        assert run is not None and run.status is RunStatus.CANCELLED

    def test_a_dead_lettered_lease_settles_its_run(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        # The path with no worker left to do the rollup: the reaper must do it,
        # or a run whose last task died with its worker sits `running` forever.
        queued(max_attempts=1)
        claim(conn, worker_id="crashed", lease=EXPIRED_LEASE)
        conn.commit()
        [(_, state)] = requeue_stale(conn)
        conn.commit()
        assert state is TaskState.DEAD
        run = get_run(conn, run_id)
        assert run is not None and run.status is RunStatus.FAILED

    def test_a_requeued_lease_leaves_the_run_alone(
        self, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        queued(max_attempts=3)
        claim(conn, worker_id="crashed", lease=EXPIRED_LEASE)
        conn.commit()
        [(_, state)] = requeue_stale(conn)
        conn.commit()
        assert state is TaskState.PENDING
        run = get_run(conn, run_id)
        assert run is not None and run.status is not RunStatus.FAILED

    @pytest.mark.parametrize("first_succeeds", [True, False])
    def test_two_workers_settling_the_last_two_tasks_still_finish_the_run(
        self,
        conn: QueueConnection,
        open_conn: Callable[[], QueueConnection],
        run_id: UUID,
        queued: Callable[..., TaskSpec],
        first_succeeds: bool,
    ) -> None:
        """The race that has no recovery path if it is lost.

        Two workers finish the last two tasks of a run at the same moment. Each
        writes its own task's terminal state and then asks "is anything still
        outstanding?" -- and each can only see the other's answer once the other
        has committed. Whoever gets the run lock second must therefore evaluate
        that question *after* the wait, not against the snapshot it started
        with, or both decline and the run is stranded non-terminal with no
        sweeper to notice (`rollup_run_status` deliberately has no fallback).

        Both orderings are covered because they take different code paths:
        `complete` touches the run row before the rollup (usage), `fail` does
        not, so the failing/failing pair is the one with nothing else to
        serialise it.
        """
        first = queued(payload={"n": 1}, max_attempts=1)
        second = queued(payload={"n": 2}, max_attempts=1)

        worker_a = open_conn()
        worker_b = open_conn()
        claim(worker_a, worker_id="a", limit=2)
        worker_a.commit()

        # A settles its task and holds the run lock, uncommitted.
        if first_succeeds:
            complete(worker_a, succeed(first))
        else:
            fail(worker_a, first.task_id, boom())

        # B settles the other one concurrently; its rollup blocks on A's lock.
        settled = threading.Event()

        def settle_second() -> None:
            fail(worker_b, second.task_id, boom())
            worker_b.commit()
            settled.set()

        thread = threading.Thread(target=settle_second)
        thread.start()
        try:
            assert not settled.wait(timeout=0.5)  # blocked, as designed
            worker_a.commit()
            assert settled.wait(timeout=20), "second worker never got the run lock"
        finally:
            thread.join(timeout=20)

        run = get_run(conn, run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED  # one task died, so the run did
        assert audit_actions(conn).count("run.status_changed") == 1


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
        assert run.status is RunStatus.SUCCEEDED
        assert run.usage.steps == 3
        assert run.usage.tokens == 42
        assert run.usage.cost_usd == Decimal("0.010000")
        assert run.usage.wall_clock == timedelta(seconds=2)

        assert audit_actions(conn) == [
            "run.created",
            "task.enqueued",
            "task.claimed",
            "task.started",
            "run.status_changed",  # pending -> running, when the task started
            "task.succeeded",
            "run.usage_recorded",
            "run.status_changed",  # running -> succeeded, when the last task settled
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


class TestSingleFlightAdmission:
    """`claim_single_flight` -- the primitive behind "a scan already in flight
    returns that run" (SPEC.md §8). Generic: goal name plus payload value, with
    no idea what either means (I4)."""

    def test_nothing_in_flight_yields_none(self, conn: QueueConnection) -> None:
        assert claim_single_flight(conn, goal="scan_source", payload={"source_id": "a"}) is None
        conn.rollback()

    def test_a_pending_run_for_the_same_payload_is_returned(
        self, conn: QueueConnection, budget: RunBudget
    ) -> None:
        payload = {"source_id": "a"}
        created = create_run(conn, goal="scan_source", payload=payload, budget=budget)
        conn.commit()

        found = claim_single_flight(conn, goal="scan_source", payload=payload)
        conn.rollback()
        assert found is not None and found.id == created.id

    def test_a_different_payload_is_a_different_flight(
        self, conn: QueueConnection, budget: RunBudget
    ) -> None:
        create_run(conn, goal="scan_source", payload={"source_id": "a"}, budget=budget)
        conn.commit()

        assert claim_single_flight(conn, goal="scan_source", payload={"source_id": "b"}) is None
        conn.rollback()

    def test_a_finished_run_is_not_in_flight(self, conn: QueueConnection, budget: RunBudget) -> None:
        # Otherwise a source could never be rescanned: the first scan would
        # keep answering for every request that followed it.
        payload = {"source_id": "a"}
        created = create_run(conn, goal="scan_source", payload=payload, budget=budget)
        set_run_status(conn, created.id, RunStatus.SUCCEEDED)
        conn.commit()

        assert claim_single_flight(conn, goal="scan_source", payload=payload) is None
        conn.rollback()

    def test_the_lock_serialises_two_simultaneous_admissions(
        self, conn: QueueConnection, open_conn: Callable[[], QueueConnection], budget: RunBudget
    ) -> None:
        """The reason the lock exists. Without it both callers read "nothing in
        flight" and both create a run, and the endpoint is idempotent only when
        nobody is in a hurry."""
        payload = {"source_id": "contended"}
        first = open_conn()
        assert claim_single_flight(first, goal="scan_source", payload=payload) is None
        created = create_run(first, goal="scan_source", payload=payload, budget=budget)

        second = open_conn()
        seen: list[UUID | None] = []

        def contend() -> None:
            found = claim_single_flight(second, goal="scan_source", payload=payload)
            seen.append(found.id if found is not None else None)
            second.rollback()

        waiter = threading.Thread(target=contend)
        waiter.start()
        waiter.join(timeout=0.5)
        assert waiter.is_alive()  # blocked on the admission lock, as designed

        first.commit()
        waiter.join(timeout=5)
        assert seen == [created.id]
        assert scalar(conn, COUNT_RUNS) == 1


class TestBindIdempotencyKey:
    """`bind_idempotency_key` -- the primitive that closes issue #44: a run
    `claim_single_flight` found already has no INSERT for `ON CONFLICT` to
    arbitrate, so binding a retry's key onto it needs its own path, with the
    same guarantees `create_run`'s idempotency handling has."""

    def test_binds_an_unbound_run(self, conn: QueueConnection, budget: RunBudget) -> None:
        created = create_run(conn, goal="scan_source", payload={"source_id": "a"}, budget=budget)
        conn.commit()

        bound = bind_idempotency_key(conn, created.id, "retry-1")
        conn.commit()

        assert bound.id == created.id
        assert bound.idempotency_key == "retry-1"
        fetched = get_run(conn, created.id)
        assert fetched is not None and fetched.idempotency_key == "retry-1"
        assert audit_actions(conn) == ["run.created", "run.idempotency_key_bound"]

    def test_a_second_bind_of_the_same_key_is_a_no_op(
        self, conn: QueueConnection, budget: RunBudget
    ) -> None:
        created = create_run(conn, goal="scan_source", payload={"source_id": "a"}, budget=budget)
        conn.commit()
        bind_idempotency_key(conn, created.id, "retry-1")
        conn.commit()

        again = bind_idempotency_key(conn, created.id, "retry-1")
        conn.commit()

        assert again.id == created.id
        assert again.idempotency_key == "retry-1"
        # No second mutation: the audit trail has exactly one bind, not two.
        assert audit_actions(conn) == ["run.created", "run.idempotency_key_bound"]

    def test_a_key_bound_elsewhere_is_not_moved(self, conn: QueueConnection, budget: RunBudget) -> None:
        owner = create_run(
            conn, goal="scan_source", payload={"source_id": "a"}, budget=budget, idempotency_key="k"
        )
        other = create_run(conn, goal="scan_source", payload={"source_id": "b"}, budget=budget)
        conn.commit()

        result = bind_idempotency_key(conn, other.id, "k")
        conn.commit()

        # The caller gets the key's actual owner back, not `other` -- exactly
        # `create_run`'s own conflict shape, so the caller can compare payloads
        # and decide 409 the same way regardless of which path it took.
        assert result.id == owner.id
        assert result.payload == owner.payload
        fetched = get_run(conn, other.id)
        assert fetched is not None and fetched.idempotency_key is None
        assert audit_actions(conn) == ["run.created", "run.created"]  # no bind was recorded

    def test_a_bound_key_then_replayed_through_create_run_finds_the_same_run(
        self, conn: QueueConnection, budget: RunBudget
    ) -> None:
        """The bridge the bug broke: a key bound by `bind_idempotency_key`
        (the single-flight path) must be exactly as visible to a later
        `create_run` replay (the no-longer-in-flight path) as one bound by
        `create_run` itself -- one unique index, one source of truth."""
        created = create_run(conn, goal="scan_source", payload={"source_id": "a"}, budget=budget)
        conn.commit()
        bind_idempotency_key(conn, created.id, "retry-1")
        conn.commit()

        replayed = create_run(
            conn, goal="scan_source", payload={"source_id": "a"}, budget=budget, idempotency_key="retry-1"
        )
        conn.commit()

        assert replayed.id == created.id
        assert scalar(conn, COUNT_RUNS) == 1  # no second run was created

    def test_the_lock_serialises_two_simultaneous_binds_of_the_same_key(
        self, conn: QueueConnection, open_conn: Callable[[], QueueConnection], budget: RunBudget
    ) -> None:
        """Without a lock over the key's own namespace, two runs racing to
        bind the same key could both see "unbound" and one loses the write to
        a raw unique-index violation instead of the typed conflict callers
        expect."""
        first_conn = open_conn()
        first = create_run(first_conn, goal="scan_source", payload={"source_id": "a"}, budget=budget)
        first_conn.commit()
        second_conn = open_conn()
        second = create_run(second_conn, goal="scan_source", payload={"source_id": "b"}, budget=budget)
        second_conn.commit()

        holder = open_conn()
        bind_idempotency_key(holder, first.id, "contended")

        seen: list[UUID] = []

        def contend() -> None:
            bound = bind_idempotency_key(second_conn, second.id, "contended")
            seen.append(bound.id)
            second_conn.commit()

        waiter = threading.Thread(target=contend)
        waiter.start()
        waiter.join(timeout=0.5)
        assert waiter.is_alive()  # blocked on the key's lock, as designed

        holder.commit()
        waiter.join(timeout=5)
        assert seen == [first.id]  # the key stayed with whoever bound it first
        fetched = get_run(conn, second.id)
        assert fetched is not None and fetched.idempotency_key is None
