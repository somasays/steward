"""The worker loop against a real Postgres.

The handlers registered here are test scaffolding, but they are registered the
same way production handlers are -- so the H1 harness picks them up too, and
each one has to satisfy the registry contract on its own.
"""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from steward_queue import (
    NOOP_TASK_TYPE,
    REGISTRY,
    TaskState,
    Worker,
    claim,
    get_run,
    get_task,
    requeue_stale,
    task_handler,
    write_checkpoint,
)
from steward_queue.db import QueueConnection
from steward_queue.registry import TaskContext
from steward_schemas import ProblemDetails, RunBudget, TaskResult, TaskSpec, TaskStatus

NO_BACKOFF = timedelta(0)
EXPIRED_LEASE = timedelta(seconds=-1)
NO_USAGE = RunBudget(steps=1, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(0))

EXPLODES = "test.explodes_on_demand"
REPORTS_FAILURE = "test.reports_failure"
UNREGISTERED = "test.unregistered"

SELECT_TASK_RESULT = "SELECT result FROM tasks WHERE id = %s"
SELECT_CHECKPOINTS = "SELECT step, state FROM checkpoints WHERE task_id = %s ORDER BY step"
SELECT_AUDIT_ACTIONS = "SELECT action FROM audit_log WHERE entity_id = %s ORDER BY id"


@task_handler(EXPLODES, sample_payload={"boom": False})
async def explodes_on_demand(ctx: TaskContext) -> TaskResult:
    """Raises when the payload says so; otherwise an idempotent no-op.

    The failure is payload-driven rather than unconditional so this handler
    still honours the registry's idempotence clause under H1.
    """
    if ctx.spec.payload.get("boom"):
        raise RuntimeError("handler exploded")
    write_checkpoint(ctx.connection, ctx.spec.task_id, step=0, state={"boom": False})
    return TaskResult(task_id=ctx.spec.task_id, status=TaskStatus.SUCCEEDED, usage=NO_USAGE)


@task_handler(REPORTS_FAILURE, sample_payload={})
async def reports_failure(ctx: TaskContext) -> TaskResult:
    """Returns a typed failure instead of raising -- the other failure path."""
    return TaskResult(
        task_id=ctx.spec.task_id,
        status=TaskStatus.FAILED,
        usage=NO_USAGE,
        error=ProblemDetails(type="urn:steward:test", title="declined", status=422),
    )


def audit_actions(conn: QueueConnection, task_id: UUID) -> list[str]:
    return [row[0] for row in conn.execute(SELECT_AUDIT_ACTIONS, (str(task_id),)).fetchall()]


async def drain(worker: Worker) -> None:
    while await worker.run_once():
        pass


class TestHappyPath:
    async def test_noop_task_runs_to_success(
        self, dsn: str, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=NOOP_TASK_TYPE, payload={"echo": "hello"})
        assert await Worker(dsn, "w1").run_once() == 1

        task = get_task(conn, spec.task_id)
        assert task is not None
        assert task.state is TaskState.SUCCEEDED
        assert task.claimed_by == "w1"
        assert task.started_at is not None

        row = conn.execute(SELECT_TASK_RESULT, (spec.task_id,)).fetchone()
        assert row is not None
        assert row[0]["output"] == {"echo": "hello"}

        checkpoints = conn.execute(SELECT_CHECKPOINTS, (spec.task_id,)).fetchall()
        assert [(r[0], r[1]) for r in checkpoints] == [(0, {"echo": "hello"})]

        run = get_run(conn, run_id)
        assert run is not None and run.usage.steps == 1

        assert audit_actions(conn, spec.task_id) == [
            "task.enqueued",
            "task.claimed",
            "task.started",
            "task.succeeded",
        ]

    async def test_idle_worker_reports_no_work(self, dsn: str, conn: QueueConnection) -> None:
        assert await Worker(dsn, "w1").run_once() == 0

    async def test_worker_only_claims_types_it_handles(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=UNREGISTERED, payload={"n": 1})
        assert await Worker(dsn, "w1").run_once() == 0
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.PENDING


class TestFailurePaths:
    async def test_raising_handler_retries_then_dead_letters(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=EXPLODES, payload={"boom": True}, max_attempts=2)
        worker = Worker(dsn, "w1", retry_base_delay=NO_BACKOFF)

        assert await worker.run_once() == 1
        task = get_task(conn, spec.task_id)
        assert task is not None
        assert task.state is TaskState.PENDING
        assert task.attempts == 1

        assert await worker.run_once() == 1
        task = get_task(conn, spec.task_id)
        assert task is not None
        assert task.state is TaskState.DEAD
        assert task.attempts == 2

        actions = audit_actions(conn, spec.task_id)
        assert actions.count("task.retry_scheduled") == 1
        assert actions.count("task.dead") == 1

    async def test_failed_attempt_leaves_no_partial_writes(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        # The handler checkpoints before raising; the rollback must take it with it.
        spec = queued(task_type=EXPLODES, payload={"boom": True, "n": 1}, max_attempts=1)
        await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once()
        assert conn.execute(SELECT_CHECKPOINTS, (spec.task_id,)).fetchall() == []

    async def test_typed_failure_result_is_recorded(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=REPORTS_FAILURE, payload={"n": 2}, max_attempts=1)
        await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once()
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.DEAD
        assert "task.dead" in audit_actions(conn, spec.task_id)

    async def test_unknown_task_type_fails_the_task(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=UNREGISTERED, payload={"n": 3}, max_attempts=1)
        worker = Worker(dsn, "w1", task_types=[UNREGISTERED], retry_base_delay=NO_BACKOFF)
        assert await worker.run_once() == 1
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.DEAD


class TestConcurrency:
    async def test_two_workers_never_execute_a_task_twice(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        specs = [queued(task_type=NOOP_TASK_TYPE, payload={"n": n}) for n in range(8)]
        await asyncio.gather(drain(Worker(dsn, "a")), drain(Worker(dsn, "b")))

        for spec in specs:
            task = get_task(conn, spec.task_id)
            assert task is not None
            assert task.state is TaskState.SUCCEEDED
            assert task.attempts == 1  # claimed once, executed once
            assert task.claimed_by in {"a", "b"}


class TestLostClaim:
    async def test_a_task_reaped_before_the_worker_starts_is_skipped(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        """A stalled worker whose lease expired must not crash its poll loop."""
        spec = queued(task_type=NOOP_TASK_TYPE, payload={"n": 200})
        [claimed] = claim(conn, worker_id="stalled", lease=EXPIRED_LEASE)
        conn.commit()
        requeue_stale(conn)  # a reaper takes it back while the worker is stalled
        conn.commit()

        assert await Worker(dsn, "stalled").execute(claimed) is False
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.PENDING


class TestLoop:
    async def test_run_forever_drains_and_stops(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=NOOP_TASK_TYPE, payload={"n": 99})
        stop = asyncio.Event()
        worker = Worker(dsn, "w1", poll_interval=timedelta(milliseconds=10))
        loop_task = asyncio.create_task(worker.run_forever(stop))

        async with asyncio.timeout(20):
            while True:
                task = get_task(conn, spec.task_id)
                conn.commit()  # end the read transaction so the next read sees fresh state
                if task is not None and task.state is TaskState.SUCCEEDED:
                    break
                await asyncio.sleep(0.02)

        stop.set()
        await asyncio.wait_for(loop_task, timeout=5)

    async def test_reaping_returns_a_crashed_workers_task(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=NOOP_TASK_TYPE, payload={"n": 100})
        claim(conn, worker_id="crashed", lease=EXPIRED_LEASE)
        conn.commit()

        assert await Worker(dsn, "w1").reap_stale() == 1
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.PENDING


@pytest.mark.parametrize("task_type", [EXPLODES, REPORTS_FAILURE])
def test_scaffolding_handlers_are_registered(task_type: str) -> None:
    assert task_type in REGISTRY
