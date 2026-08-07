"""The worker loop against a real Postgres.

The handlers registered here are test scaffolding, but they are registered the
same way production handlers are -- so the H1 harness picks them up too, and
each one has to satisfy the registry contract on its own.
"""

import asyncio
import contextlib
import itertools
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import psycopg
import pytest
from steward_queue import (
    NOOP_TASK_TYPE,
    REGISTRY,
    RunStatus,
    TaskState,
    Worker,
    claim,
    connect,
    get_run,
    get_task,
    requeue_stale,
    task_handler,
    write_checkpoint,
)
from steward_queue.db import QueueConnection
from steward_queue.execution import EXECUTION_FAILED, HANDLER_FAILED
from steward_queue.registry import TaskContext
from steward_queue.worker import (
    BOOKKEEPING_CONNECTION_FAILED,
    BUDGET_EXCEEDED,
    CONNECT_RETRY_ATTEMPTS,
    DEADLINE_GRACE,
)
from steward_schemas import ProblemDetails, RunBudget, TaskResult, TaskSpec, TaskStatus
from steward_telemetry import Span, SpanOutcome

NO_BACKOFF = timedelta(0)
EXPIRED_LEASE = timedelta(seconds=-1)
NO_USAGE = RunBudget(steps=1, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(0))

EXPLODES = "test.explodes_on_demand"
OVERSPENDS = "test.overspends_its_budget"
REPORTS_FAILURE = "test.reports_failure"
SLEEPS = "test.sleeps"
BLOCKS_IN_THE_DRIVER = "test.blocks_in_the_driver"
BLOCKS_THE_INTERPRETER = "test.blocks_the_interpreter"
SPENDS_THE_CAP_TWICE = "test.spends_the_cap_twice"
SWALLOWS_A_DATABASE_ERROR = "test.swallows_a_database_error"
LEAKS_CANCELLATION = "test.leaks_cancellation"
CANNOT_REACH_ITS_SOURCE = "test.cannot_reach_its_source"
UNREGISTERED = "test.unregistered"
REPORTS_ITS_LEASE = "test.reports_its_lease"

TINY_WALL_CLOCK = RunBudget(steps=1, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(milliseconds=20))
SHORT_WALL_CLOCK = RunBudget(steps=1, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(milliseconds=250))
ONE_SECOND_WALL_CLOCK = RunBudget(steps=1, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(seconds=1))
TWO_SECOND_WALL_CLOCK = RunBudget(steps=1, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(seconds=2))

SELECT_TASK_RESULT = "SELECT result FROM tasks WHERE id = %s"
SELECT_TASK_LAST_ERROR = "SELECT last_error FROM tasks WHERE id = %s"
SELECT_CHECKPOINTS = "SELECT step, state FROM checkpoints WHERE task_id = %s ORDER BY step"
SELECT_AUDIT_ACTIONS = "SELECT action FROM audit_log WHERE entity_id = %s ORDER BY id"
SELECT_PG_SLEEP = "SELECT pg_sleep(%(seconds)s)"
SELECT_DIVIDE_BY_ZERO = "SELECT 1 / 0"
EXPIRE_LEASE = "UPDATE tasks SET lease_expires_at = now() - interval '1 second' WHERE id = %s"
SELECT_BACKEND_STATE = "SELECT state FROM pg_stat_activity WHERE pid = %(pid)s"
SELECT_LEASE_HEADROOM_SECONDS = """
SELECT EXTRACT(EPOCH FROM (lease_expires_at - now())) FROM tasks WHERE id = %(id)s
"""


@task_handler(REPORTS_ITS_LEASE, sample_payload={})
async def reports_its_lease(ctx: TaskContext) -> TaskResult:
    """Reports how much lease its own task row has left, while it runs.

    The worker commits `mark_running` before calling a handler, so by the time
    this executes the lease it was granted is readable from the row -- which
    makes the worker's lease decision observable state rather than something a
    test has to reach into a private method for. Idempotent: it reads, it never
    writes, and the value is excluded from the result so H1's byte-identical
    comparison is not made unfalsifiable by a moving clock.
    """
    row = ctx.connection.execute(SELECT_LEASE_HEADROOM_SECONDS, {"id": ctx.spec.task_id}).fetchone()
    _observed_lease_headroom.append(float(row[0]) if row is not None and row[0] is not None else 0.0)
    return TaskResult(task_id=ctx.spec.task_id, status=TaskStatus.SUCCEEDED, usage=NO_USAGE)


_observed_lease_headroom: list[float] = []


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


@task_handler(SLEEPS, sample_payload={"seconds": 0.0})
async def sleeps(ctx: TaskContext) -> TaskResult:
    """Burns as much wall clock as the payload asks for, and writes nothing.

    Payload-driven so the sample payload (zero seconds) stays instant and
    idempotent under H1, while a budget test can ask for more time than the
    task's `RunBudget.wall_clock` allows.
    """
    await asyncio.sleep(float(ctx.spec.payload["seconds"]))
    return TaskResult(task_id=ctx.spec.task_id, status=TaskStatus.SUCCEEDED, usage=NO_USAGE)


@task_handler(BLOCKS_IN_THE_DRIVER, sample_payload={"seconds": 0.0})
async def blocks_in_the_driver(ctx: TaskContext) -> TaskResult:
    """Blocks inside psycopg -- the case `asyncio.timeout` cannot reach.

    `async def` with no await: the call sits in a blocking C function, so the
    event loop never gets control back to cancel it. Only a server-side
    statement timeout can end this. Payload-driven, so the sample payload
    (zero seconds) stays instant and idempotent under H1.
    """
    ctx.connection.execute(SELECT_PG_SLEEP, {"seconds": float(ctx.spec.payload["seconds"])})
    return TaskResult(task_id=ctx.spec.task_id, status=TaskStatus.SUCCEEDED, usage=NO_USAGE)


@dataclass(frozen=True)
class HandlerSighting:
    """Where a handler ran: which thread, and which Postgres backend it held."""

    thread_ident: int
    backend_pid: int


_sightings: dict[UUID, HandlerSighting] = {}
"""Sightings by task id, so a test reads its own and never another's.

Keyed rather than appended because this handler is also executed by H1 off the
registry: a list would make every test that reads it depend on what ran before.
"""


@task_handler(BLOCKS_THE_INTERPRETER, sample_payload={"seconds": 0.0})
async def blocks_the_interpreter(ctx: TaskContext) -> TaskResult:
    """Writes, then blocks in Python with an open transaction, then writes again.

    The case neither timeout can reach: `asyncio.timeout` needs an await point
    and `statement_timeout` needs a running statement, and `time.sleep` offers
    neither. Only the worker's own deadline can end this task, and only by
    leaving the thread behind -- which is why the checkpoints bracket the sleep,
    so a test can check whether an abandoned handler's writes ever land.

    It reports where it ran so the test can observe the two contexts directly.
    Payload-driven, so the sample payload (zero seconds) is instant, and both
    checkpoints are upserted at fixed steps, so it is idempotent under H1.
    """
    write_checkpoint(ctx.connection, ctx.spec.task_id, step=0, state={"phase": "before"})
    _sightings[ctx.spec.task_id] = HandlerSighting(threading.get_ident(), ctx.connection.info.backend_pid)
    time.sleep(float(ctx.spec.payload["seconds"]))
    write_checkpoint(ctx.connection, ctx.spec.task_id, step=1, state={"phase": "after"})
    return TaskResult(task_id=ctx.spec.task_id, status=TaskStatus.SUCCEEDED, usage=NO_USAGE)


@task_handler(SPENDS_THE_CAP_TWICE, sample_payload={"seconds": 0.0})
async def spends_the_cap_twice(ctx: TaskContext) -> TaskResult:
    """Runs a slow statement on a second connection, then one on its own.

    The shape `scan_source` has in production: a customer's database read
    through a connection whose `statement_timeout` is the *whole* budget,
    followed by local work under a second `statement_timeout` that is also the
    whole budget. Two caps, one task (#42). Payload-driven -- the sample
    payload names no second database and sleeps for zero seconds, so it stays
    instant and writes nothing.
    """
    seconds = float(ctx.spec.payload["seconds"])
    dsn = ctx.spec.payload.get("dsn")
    if dsn is not None:
        other = connect(str(dsn), statement_timeout=ctx.spec.budget.wall_clock)
        try:
            with contextlib.suppress(psycopg.Error):
                other.execute(SELECT_PG_SLEEP, {"seconds": seconds})
        finally:
            other.close()
    ctx.connection.execute(SELECT_PG_SLEEP, {"seconds": seconds})
    return TaskResult(task_id=ctx.spec.task_id, status=TaskStatus.SUCCEEDED, usage=NO_USAGE)


@task_handler(SWALLOWS_A_DATABASE_ERROR, sample_payload={"swallow": False})
async def swallows_a_database_error(ctx: TaskContext) -> TaskResult:
    """Aborts its transaction, hides the error, and reports success anyway.

    The realistic way the recording write fails *after* the executor has taken
    the handoff: Postgres refuses every further statement on an aborted
    transaction, so the `complete` the thread is about to run raises
    `InFailedSqlTransaction` -- not `TaskNotClaimable`, so it is not the
    already-answered lost-claim case. Nothing is mocked; a handler that
    suppresses a driver error and returns `SUCCEEDED` is a shape production
    code reaches on its own.

    Payload-driven, so the sample payload swallows nothing and stays an
    instant, idempotent no-op under H1.
    """
    if ctx.spec.payload.get("swallow"):
        with contextlib.suppress(psycopg.Error):
            ctx.connection.execute(SELECT_DIVIDE_BY_ZERO)
    return TaskResult(task_id=ctx.spec.task_id, status=TaskStatus.SUCCEEDED, usage=NO_USAGE)


@task_handler(LEAKS_CANCELLATION, sample_payload={"leak": False})
async def leaks_cancellation(ctx: TaskContext) -> TaskResult:
    """Lets a bare `CancelledError` escape -- the routine bug behind #55.

    Awaiting a task that has been cancelled raises `CancelledError` in the
    awaiter without cancelling it, which is exactly what an inner `wait_for` or
    `TaskGroup` around the LLM gateway leaks when a handler neither absorbs it
    nor re-raises it as its own error. It is raised on the handler thread's own
    event loop, so it says nothing about the worker's.

    Payload-driven, so the sample payload is an instant, idempotent no-op that
    writes nothing under H1.
    """
    if ctx.spec.payload.get("leak"):
        inner = asyncio.create_task(asyncio.sleep(30))
        inner.cancel()
        await inner
    return TaskResult(task_id=ctx.spec.task_id, status=TaskStatus.SUCCEEDED, usage=NO_USAGE)


@task_handler(CANNOT_REACH_ITS_SOURCE, sample_payload={"unreachable": False})
async def cannot_reach_its_source(ctx: TaskContext) -> TaskResult:
    """Fails the way an unreachable customer database fails: a `TimeoutError`.

    Since 3.11 `socket.timeout` *is* `TimeoutError`, so a `connect_timeout`
    firing seconds into a long budget reaches the runtime as the same class an
    overrun does (#57). Raised directly rather than by dialling a black-holed
    address, so the test asserts the classification and not the network.

    Payload-driven, so the sample payload reaches nothing and stays an instant,
    idempotent no-op under H1.
    """
    if ctx.spec.payload.get("unreachable"):
        raise TimeoutError("connection to customer-db timed out")
    return TaskResult(task_id=ctx.spec.task_id, status=TaskStatus.SUCCEEDED, usage=NO_USAGE)


@task_handler(OVERSPENDS, sample_payload={"steps": 1})
async def overspends_its_budget(ctx: TaskContext) -> TaskResult:
    """Succeeds while reporting whatever usage the payload names.

    Steps, tokens and cost are counted inside a handler and reported here;
    nothing outside can observe them, so a handler is free to report more than
    it was given. Payload-driven, so the sample payload (one step) is inside
    every fixture budget and idempotent under H1, while a budget test can ask
    for an overrun.
    """
    return TaskResult(
        task_id=ctx.spec.task_id,
        status=TaskStatus.SUCCEEDED,
        usage=RunBudget(
            steps=int(ctx.spec.payload["steps"]),
            tokens=0,
            cost_usd=Decimal("0"),
            wall_clock=timedelta(0),
        ),
    )


@task_handler(REPORTS_FAILURE, sample_payload={})
async def reports_failure(ctx: TaskContext) -> TaskResult:
    """Returns a typed failure instead of raising -- the other failure path."""
    return TaskResult(
        task_id=ctx.spec.task_id,
        status=TaskStatus.FAILED,
        usage=NO_USAGE,
        error=ProblemDetails(type="urn:steward:test", title="declined", status=422),
    )


@dataclass
class RecordedSpan:
    """One span the worker opened, and how it said the work ended."""

    trace_id: str
    task_id: UUID
    task_type: str
    outcome: SpanOutcome | None = None
    detail: str | None = None

    def record(self, outcome: SpanOutcome, detail: str | None = None) -> None:
        self.outcome, self.detail = outcome, detail


class RecordingTracer:
    """A `Tracer` that keeps its spans instead of exporting them.

    Spans are the observable output of tracing, so asserting on them is
    asserting on emitted events -- not on how the worker happened to call its
    collaborator.
    """

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def run_span(self, *, trace_id: str, run_id: UUID, goal: str) -> Iterator[Span]:
        raise NotImplementedError("the worker executes tasks; runs are opened by their creator")
        yield  # pragma: no cover -- unreachable, kept so the signature is a generator

    @contextmanager
    def task_span(self, *, trace_id: str, run_id: UUID, task_id: UUID, task_type: str) -> Iterator[Span]:
        span = RecordedSpan(trace_id=trace_id, task_id=task_id, task_type=task_type)
        self.spans.append(span)
        yield span


def audit_actions(conn: QueueConnection, task_id: UUID) -> list[str]:
    return [row[0] for row in conn.execute(SELECT_AUDIT_ACTIONS, (str(task_id),)).fetchall()]


def backend_state(conn: QueueConnection, backend_pid: int) -> str | None:
    """What Postgres says the handler's session is doing, or None once it is gone."""
    row = conn.execute(SELECT_BACKEND_STATE, {"pid": backend_pid}).fetchone()
    conn.commit()  # end the read transaction so the next read sees fresh state
    return None if row is None else str(row[0])


def checkpoints(conn: QueueConnection, task_id: UUID) -> list[tuple[int, object]]:
    rows = conn.execute(SELECT_CHECKPOINTS, (task_id,)).fetchall()
    conn.commit()
    return [(row[0], row[1]) for row in rows]


def state_of(conn: QueueConnection, task_id: UUID) -> TaskState | None:
    task = get_task(conn, task_id)
    conn.commit()
    return None if task is None else task.state


async def until(condition: Callable[[], bool], *, within: float = 20.0) -> float:
    """Poll `condition` until it holds; return how long that took."""
    started = time.monotonic()
    while time.monotonic() - started < within:
        if condition():
            return time.monotonic() - started
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition did not hold within {within}s")


async def drain(worker: Worker) -> None:
    while await worker.run_once():
        pass


def break_handler_connections(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, *, times: int = 1
) -> None:
    """Make the next `times` handler connections fail to open.

    The seam is the connect the handler thread makes for itself, so the
    worker's own bookkeeping connection is left working -- which is the case
    #45 describes: the second connection a task opens, after `mark_running` has
    already committed, against a server that has no capacity left for it.
    """
    attempts = itertools.count()

    def failing(dsn: str, *, statement_timeout: timedelta | None = None) -> QueueConnection:
        if next(attempts) < times:
            raise error
        return connect(dsn, statement_timeout=statement_timeout)

    monkeypatch.setattr("steward_queue.execution.connect", failing)


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

    async def test_a_handler_that_outruns_its_wall_clock_budget_is_terminated(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        # I12: the cap is enforced by the runtime and the failure is typed and
        # visible -- the task does not sit holding a worker until its lease dies.
        spec = queued(
            task_type=SLEEPS,
            payload={"seconds": 30.0},
            budget=TINY_WALL_CLOCK,
            max_attempts=1,
        )
        assert await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once() == 1

        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.DEAD
        row = conn.execute(SELECT_TASK_LAST_ERROR, (spec.task_id,)).fetchone()
        assert row is not None
        assert row[0]["title"] == BUDGET_EXCEEDED
        assert row[0]["budget"]["steps"] == 1  # the cap travels with the problem

    async def test_a_handler_blocked_in_the_driver_is_interrupted(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        # The hole `asyncio.timeout` alone leaves open (#3 review): a handler
        # blocked in psycopg has no await point to cancel at, so the budget has
        # to be enforced server-side or it is not enforced at all. What comes
        # back from the driver is a `QueryCanceled`, and reporting *that* would
        # file an overrun under "handler raised" -- so the runtime names it for
        # what it is, which is what I12 asks of a hard cap (#42).
        spec = queued(
            task_type=BLOCKS_IN_THE_DRIVER,
            payload={"seconds": 30.0},
            budget=SHORT_WALL_CLOCK,
            max_attempts=1,
        )
        async with asyncio.timeout(20):  # the assertion is that this never fires
            assert await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once() == 1

        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.DEAD
        row = conn.execute(SELECT_TASK_LAST_ERROR, (spec.task_id,)).fetchone()
        assert row is not None
        assert row[0]["title"] == BUDGET_EXCEEDED

    async def test_recording_an_outcome_is_not_bound_by_the_handlers_budget(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        # A 20ms budget must still be able to write its own failure: the
        # connection widens back to the lease before the worker records.
        spec = queued(
            task_type=BLOCKS_IN_THE_DRIVER,
            payload={"seconds": 30.0},
            budget=TINY_WALL_CLOCK,
            max_attempts=1,
        )
        assert await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once() == 1
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.DEAD

    async def test_a_handler_within_its_budget_is_left_alone(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=SLEEPS, payload={"seconds": 0.0})
        await Worker(dsn, "w1").run_once()
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.SUCCEEDED

    async def test_unknown_task_type_fails_the_task(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=UNREGISTERED, payload={"n": 3}, max_attempts=1)
        worker = Worker(dsn, "w1", task_types=[UNREGISTERED], retry_base_delay=NO_BACKOFF)
        assert await worker.run_once() == 1
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.DEAD


class TestExecutionFailures:
    """#45: a task the worker cannot even start costs that task, not the worker.

    The failure this is written against happens before the handler is called at
    all -- the connection it runs through cannot be opened -- so no handler can
    be responsible for catching it. Until this it travelled out of the future
    the poll loop reads and killed the process, leaving the task `running` for
    the length of its lease; N1 got it back, but a worker that dies on a
    transient connection error is not operable.
    """

    async def test_a_handler_connection_that_cannot_be_opened_fails_the_task(
        self,
        dsn: str,
        conn: QueueConnection,
        queued: Callable[..., TaskSpec],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        broken = queued(task_type=NOOP_TASK_TYPE, payload={"n": 900}, max_attempts=1)
        break_handler_connections(monkeypatch, psycopg.OperationalError("too many connections"))

        stop = asyncio.Event()
        worker = Worker(dsn, "w1", poll_interval=timedelta(milliseconds=20), retry_base_delay=NO_BACKOFF)
        loop_task = asyncio.create_task(worker.run_forever(stop))
        try:
            await until(lambda: state_of(conn, broken.task_id) is TaskState.DEAD)

            # Enqueued only now, so claiming it proves the loop is still
            # polling rather than that it had two tasks in one batch.
            healthy = queued(task_type=NOOP_TASK_TYPE, payload={"n": 901})
            await until(lambda: state_of(conn, healthy.task_id) is TaskState.SUCCEEDED)
        finally:
            stop.set()
            await asyncio.wait_for(loop_task, timeout=5)

        row = conn.execute(SELECT_TASK_LAST_ERROR, (broken.task_id,)).fetchone()
        conn.commit()
        assert row is not None
        assert row[0]["title"] == EXECUTION_FAILED  # not the handler's fault, and named as such
        assert row[0]["detail"].startswith("OperationalError")

    async def test_a_shutdown_on_the_handler_thread_still_stops_the_worker(
        self,
        dsn: str,
        conn: QueueConnection,
        queued: Callable[..., TaskSpec],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The other half of the distinction: not every raise is a task's fault.

        `KeyboardInterrupt` and `SystemExit` mean the process is ending, so they
        travel out of the worker instead of being filed as a failed task. The
        attempt is left where lease recovery can find it (N1).
        """
        spec = queued(task_type=NOOP_TASK_TYPE, payload={"n": 902})
        break_handler_connections(monkeypatch, KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            await Worker(dsn, "w1").run_once()

        assert state_of(conn, spec.task_id) is TaskState.RUNNING

    async def test_an_executor_that_cannot_persist_leaves_the_task_to_lease_recovery(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        """Where "recorded exactly once" stops (#53, SPEC §13 D7).

        The handoff is taken *before* the write, so a write that then fails is
        an attempt nobody records: the loop has lost the handoff and must not
        write a terminal state the thread that beat it to it may still be
        committing. What is left is N1's own mechanism -- the attempt keeps its
        lease, and `requeue_stale` hands it back when that lease expires.
        """
        spec = queued(task_type=SWALLOWS_A_DATABASE_ERROR, payload={"swallow": True}, max_attempts=3)

        assert await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once() == 1

        # Not failed, not succeeded, and not dead: unrecorded, and still owned.
        assert state_of(conn, spec.task_id) is TaskState.RUNNING
        row = conn.execute(SELECT_TASK_LAST_ERROR, (spec.task_id,)).fetchone()
        conn.commit()
        assert row is not None and row[0] is None  # the loop wrote nothing at all

        conn.execute(EXPIRE_LEASE, (spec.task_id,))  # the lease this attempt was left to
        conn.commit()
        assert requeue_stale(conn) == [(spec.task_id, TaskState.PENDING)]
        conn.commit()
        assert state_of(conn, spec.task_id) is TaskState.PENDING


def break_worker_connections(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, *, times: int | None = None
) -> None:
    """Make `execute`'s own bookkeeping connect fail -- not `_claim`'s or `_reap`'s.

    #45's regression test (`break_handler_connections` above) only reaches
    `execution.connect`, the handler's own -- its docstring says so. #56 is
    that a task opens two connections *before* the handler ever runs: `_claim`
    and `execute`'s bookkeeping one, both fatal until this. `_claim` and
    `_reap` call `connect(dsn)` with no `statement_timeout`; `execute` always
    passes one (`self._lease`). That is the one thing to key on, since nothing
    else about the call differs.

    `times=None` fails every attempt, exercising exhaustion past the retry
    bound; a small int lets a later attempt through, exercising the retry
    succeeding.
    """
    attempts = itertools.count()

    def failing(dsn: str, *, statement_timeout: timedelta | None = None) -> QueueConnection:
        if statement_timeout is not None and (times is None or next(attempts) < times):
            raise error
        return connect(dsn, statement_timeout=statement_timeout)

    monkeypatch.setattr("steward_queue.worker.connect", failing)


class TestWorkerConnectionFailures:
    """#56: the worker's own connections can fail too, not just the handler's.

    A task opens up to four connections across a poll -- `_claim`'s, `_reap`'s,
    `execute`'s bookkeeping one, and the handler's (#45 covers the last). #45's
    regression test forced the failure at `execution.connect` only, leaving
    `worker.connect` working, which is a narrower case than genuine
    `max_connections` exhaustion: that kills the worker at `_claim` or
    `execute`, before the handler's connection is ever reached. These force it
    at `worker.connect` instead.
    """

    async def test_a_bookkeeping_connection_that_never_opens_leaves_the_task_to_lease_recovery(
        self,
        dsn: str,
        conn: QueueConnection,
        queued: Callable[..., TaskSpec],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`execute`'s connect can fail after `_claim` has already committed the claim.

        Unlike `_claim` -- which has no task in hand and is left to exit
        (SPEC.md §13 D7) -- a task IS claimed here, so the worker must not die
        over it. Every retry fails, so `_start`/`mark_running` never runs and
        the row is left `claimed` rather than `running` -- it never got that
        far. The bounded retry gives up rather than blocking the loop, and
        with no connection left to write on, the attempt is left exactly where
        #53's "recorded by neither context" case leaves one: unrecorded, still
        owned, reclaimed by `requeue_stale` at lease expiry.
        """
        spec = queued(task_type=NOOP_TASK_TYPE, payload={"n": 950})
        [claimed] = claim(conn, worker_id="w1")
        conn.commit()
        break_worker_connections(monkeypatch, psycopg.OperationalError("too many connections"))

        tracer = RecordingTracer()
        worker = Worker(dsn, "w1", tracer=tracer)
        assert await worker.execute(claimed) is True  # handled, not raised

        assert state_of(conn, spec.task_id) is TaskState.CLAIMED
        row = conn.execute(SELECT_TASK_LAST_ERROR, (spec.task_id,)).fetchone()
        conn.commit()
        assert row is not None and row[0] is None  # nothing was ever written

        [span] = tracer.spans
        assert span.outcome is SpanOutcome.ERROR
        assert span.detail == BOOKKEEPING_CONNECTION_FAILED

        conn.execute(EXPIRE_LEASE, (spec.task_id,))  # the lease this attempt was left to
        conn.commit()
        assert requeue_stale(conn) == [(spec.task_id, TaskState.PENDING)]
        conn.commit()
        assert state_of(conn, spec.task_id) is TaskState.PENDING

    async def test_a_bookkeeping_connection_that_opens_on_retry_still_runs_the_task(
        self,
        dsn: str,
        conn: QueueConnection,
        queued: Callable[..., TaskSpec],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proves the retry loop itself: losing a race for one slot is not exhaustion.

        Without this, the retry in `_connect_for_execute` is dead code the
        exhaustion test above never walks past the first attempt.
        """
        assert CONNECT_RETRY_ATTEMPTS > 1  # otherwise this test asserts nothing about a retry
        spec = queued(task_type=NOOP_TASK_TYPE, payload={"n": 951})
        [claimed] = claim(conn, worker_id="w1")
        conn.commit()
        break_worker_connections(
            monkeypatch, psycopg.OperationalError("too many connections"), times=CONNECT_RETRY_ATTEMPTS - 1
        )

        assert await Worker(dsn, "w1").execute(claimed) is True

        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.SUCCEEDED

    async def test_connection_exhaustion_does_not_crash_the_poll_loop(
        self,
        dsn: str,
        conn: QueueConnection,
        queued: Callable[..., TaskSpec],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The loop-survival half of #56, mirroring #45's own shape.

        `_claim` succeeds (it is untouched by `break_worker_connections`), so
        the task reaches `execute` and only then finds every connection
        refused -- exhausting the retry -- while the loop keeps polling and a
        second, healthy task still completes.
        """
        broken = queued(task_type=NOOP_TASK_TYPE, payload={"n": 952}, max_attempts=1)
        break_worker_connections(
            monkeypatch, psycopg.OperationalError("too many connections"), times=CONNECT_RETRY_ATTEMPTS
        )

        stop = asyncio.Event()
        worker = Worker(dsn, "w1", poll_interval=timedelta(milliseconds=20), retry_base_delay=NO_BACKOFF)
        loop_task = asyncio.create_task(worker.run_forever(stop))
        try:
            await until(lambda: state_of(conn, broken.task_id) is TaskState.CLAIMED)

            # Enqueued only now, so claiming and succeeding it proves the loop
            # is still polling, not that it had two tasks in one batch.
            healthy = queued(task_type=NOOP_TASK_TYPE, payload={"n": 953})
            await until(lambda: state_of(conn, healthy.task_id) is TaskState.SUCCEEDED)
        finally:
            stop.set()
            await asyncio.wait_for(loop_task, timeout=5)

    async def test_the_reaper_survives_a_connection_it_cannot_open(
        self,
        dsn: str,
        conn: QueueConnection,
        queued: Callable[..., TaskSpec],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`_reap`'s connect is a fourth call site with the same exposure (#56).

        `_claim` and `_reap` are indistinguishable by call signature -- neither
        passes `statement_timeout` -- so `break_worker_connections` (keyed on
        that) cannot target one without the other. This runs `_reap_forever`
        directly instead of the full loop, so `_claim` is never called at all
        and a plain, unconditional connect failure only ever hits `_reap`: the
        assertion is that the reaper outlives a connection it cannot open and
        still reclaims a stale lease once one succeeds.
        """
        spec = queued(task_type=NOOP_TASK_TYPE, payload={"n": 954})
        claim(conn, worker_id="ghost", lease=EXPIRED_LEASE)
        conn.commit()

        attempts = itertools.count()

        def flaky(dsn_: str, *, statement_timeout: timedelta | None = None) -> QueueConnection:
            if next(attempts) < 2:
                raise psycopg.OperationalError("too many connections")
            return connect(dsn_, statement_timeout=statement_timeout)

        monkeypatch.setattr("steward_queue.worker.connect", flaky)

        worker = Worker(dsn, "w1", poll_interval=timedelta(milliseconds=20))
        stop = asyncio.Event()
        reaper_task = asyncio.create_task(worker._reap_forever(stop))
        try:
            await until(lambda: state_of(conn, spec.task_id) is TaskState.PENDING)
        finally:
            stop.set()
            await asyncio.wait_for(reaper_task, timeout=5)


class TestCancellationIsTaskScoped:
    """#55: a handler's `CancelledError` is one task's bug, not the process ending.

    The handler runs `asyncio.run` on an event loop of its own, so cancellation
    escaping it carries no information about the worker's loop. Grouped with
    `SystemExit` and `KeyboardInterrupt` it travelled out of the future the poll
    loop reads and killed the worker -- the #45 shape through the one door #45
    left open, and reachable by any handler that awaits.
    """

    async def test_a_handler_leaking_cancellation_fails_that_task_and_leaves_the_worker_polling(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        leaking = queued(task_type=LEAKS_CANCELLATION, payload={"leak": True}, max_attempts=1)

        stop = asyncio.Event()
        worker = Worker(dsn, "w1", poll_interval=timedelta(milliseconds=20), retry_base_delay=NO_BACKOFF)
        loop_task = asyncio.create_task(worker.run_forever(stop))
        try:
            await until(lambda: state_of(conn, leaking.task_id) is TaskState.DEAD)

            # Enqueued only now, so claiming it proves the loop is still
            # polling rather than that it had two tasks in one batch.
            healthy = queued(task_type=NOOP_TASK_TYPE, payload={"n": 904})
            await until(lambda: state_of(conn, healthy.task_id) is TaskState.SUCCEEDED)
        finally:
            stop.set()
            await asyncio.wait_for(loop_task, timeout=5)

        row = conn.execute(SELECT_TASK_LAST_ERROR, (leaking.task_id,)).fetchone()
        conn.commit()
        assert row is not None
        assert row[0]["title"] == HANDLER_FAILED  # the handler's own bug, named as such
        assert row[0]["detail"].startswith("CancelledError")


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


class TestTracing:
    """I7's tracing half: every execution is a span on its run's trace."""

    async def test_a_successful_execution_opens_a_span_on_the_runs_trace(
        self, dsn: str, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=NOOP_TASK_TYPE, payload={"n": 300})
        run = get_run(conn, run_id)
        assert run is not None

        tracer = RecordingTracer()
        assert await Worker(dsn, "w1", tracer=tracer).run_once() == 1

        [span] = tracer.spans
        assert span.trace_id == run.trace_id  # the id stored on the row, not a fresh one
        assert span.task_id == spec.task_id
        assert span.task_type == NOOP_TASK_TYPE
        assert span.outcome is SpanOutcome.OK

    async def test_a_typed_failure_is_recorded_on_the_span(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        # A handler that returns a failure never raises, so nothing but an
        # explicit record can stop the span reading as a success.
        queued(task_type=REPORTS_FAILURE, payload={"n": 301}, max_attempts=1)
        tracer = RecordingTracer()
        await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF, tracer=tracer).run_once()

        [span] = tracer.spans
        assert span.outcome is SpanOutcome.ERROR
        assert span.detail == "declined"

    async def test_a_budget_failure_is_recorded_on_the_span(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        queued(task_type=SLEEPS, payload={"seconds": 30.0}, budget=TINY_WALL_CLOCK, max_attempts=1)
        tracer = RecordingTracer()
        await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF, tracer=tracer).run_once()

        [span] = tracer.spans
        assert span.outcome is SpanOutcome.ERROR
        assert span.detail == BUDGET_EXCEEDED

    async def test_every_attempt_gets_its_own_span(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        queued(task_type=EXPLODES, payload={"boom": True}, max_attempts=2)
        tracer = RecordingTracer()
        worker = Worker(dsn, "w1", retry_base_delay=NO_BACKOFF, tracer=tracer)
        await worker.run_once()
        await worker.run_once()

        assert [span.outcome for span in tracer.spans] == [SpanOutcome.ERROR, SpanOutcome.ERROR]

    async def test_a_worker_without_a_tracer_still_executes(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        # Graceful degradation: no tracer configured is not a degraded system.
        spec = queued(task_type=NOOP_TASK_TYPE, payload={"n": 302})
        assert await Worker(dsn, "w1").run_once() == 1
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.SUCCEEDED


class TestRunRollup:
    async def test_a_finished_run_reads_succeeded(
        self, dsn: str, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        queued(task_type=NOOP_TASK_TYPE, payload={"n": 400})
        await Worker(dsn, "w1").run_once()
        run = get_run(conn, run_id)
        assert run is not None and run.status is RunStatus.SUCCEEDED

    async def test_a_run_whose_task_dead_letters_reads_failed(
        self, dsn: str, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        queued(task_type=EXPLODES, payload={"boom": True}, max_attempts=1)
        await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once()
        run = get_run(conn, run_id)
        assert run is not None and run.status is RunStatus.FAILED

    async def test_concurrent_workers_settle_a_run_exactly_once(
        self, dsn: str, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        # The race the run lock exists for: several workers finishing the last
        # tasks of one run at the same moment.
        for n in range(8):
            queued(task_type=NOOP_TASK_TYPE, payload={"n": 500 + n})
        await asyncio.gather(*(drain(Worker(dsn, f"w{n}")) for n in range(4)))

        run = get_run(conn, run_id)
        assert run is not None and run.status is RunStatus.SUCCEEDED
        transitions = conn.execute(SELECT_AUDIT_ACTIONS, (str(run_id),)).fetchall()
        assert [row[0] for row in transitions].count("run.status_changed") == 2


@pytest.mark.parametrize(
    "task_type",
    [
        EXPLODES,
        OVERSPENDS,
        REPORTS_FAILURE,
        BLOCKS_IN_THE_DRIVER,
        BLOCKS_THE_INTERPRETER,
        SPENDS_THE_CAP_TWICE,
        SWALLOWS_A_DATABASE_ERROR,
        LEAKS_CANCELLATION,
        CANNOT_REACH_ITS_SOURCE,
    ],
)
def test_scaffolding_handlers_are_registered(task_type: str) -> None:
    assert task_type in REGISTRY


class TestReportedUsageIsCapped:
    """I12's other three dimensions, at the seam where they become visible (#48).

    Wall-clock is bounded by machinery the handler cannot influence. Steps,
    tokens and cost are counted *inside* the handler and reported on its
    result, so the only place they can be enforced is where that report is
    read. Without this check, run-level reservation would bound what tasks are
    allowed to spend while `runs.used_*` -- the sum of what they say they spent
    -- stayed free to exceed the run's budget one task at a time.
    """

    async def test_a_result_reporting_more_than_its_budget_is_a_budget_failure(
        self, dsn: str, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=OVERSPENDS, payload={"steps": 99}, max_attempts=1)

        assert await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once() == 1

        assert state_of(conn, spec.task_id) is TaskState.DEAD
        row = conn.execute(SELECT_TASK_LAST_ERROR, (spec.task_id,)).fetchone()
        assert row is not None
        assert row[0]["title"] == BUDGET_EXCEEDED
        assert "steps" in row[0]["detail"]  # which cap, not just that one blew
        run = get_run(conn, run_id)
        assert run is not None and run.usage.steps == 0  # nothing overspent was rolled up

    async def test_a_result_within_its_budget_is_rolled_up_unchanged(
        self, dsn: str, conn: QueueConnection, run_id: UUID, queued: Callable[..., TaskSpec]
    ) -> None:
        # The check must bind the overrun and nothing else: the same handler
        # reporting a figure inside its cap succeeds and its usage counts.
        queued(task_type=OVERSPENDS, payload={"steps": 2}, max_attempts=1)

        assert await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once() == 1

        run = get_run(conn, run_id)
        assert run is not None and run.usage.steps == 2
        assert run.usage.over(run.budget) == ()


class TestWallClockIsOneCap:
    """H4's wall-clock half: the published cap is the bound, whatever the handler does.

    Before #42 it was neither. `asyncio.timeout` wrapped handlers that never
    await, so it never fired; what was left were `statement_timeout`s each set
    to the *whole* budget, on each of the two connections a scan uses. A
    handler blocking in Python was unbounded; a handler making two slow calls
    cost two caps.
    """

    async def test_a_handler_spending_the_cap_twice_is_still_capped_once(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(
            task_type=SPENDS_THE_CAP_TWICE,
            payload={"seconds": 30.0, "dsn": dsn},
            budget=TWO_SECOND_WALL_CLOCK,
            max_attempts=1,
        )
        started = time.monotonic()
        assert await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once() == 1
        elapsed = time.monotonic() - started

        cap = TWO_SECOND_WALL_CLOCK.wall_clock.total_seconds()
        assert cap <= elapsed < 2 * cap, f"took {elapsed:.2f}s against a {cap:.0f}s cap"
        assert state_of(conn, spec.task_id) is TaskState.DEAD
        row = conn.execute(SELECT_TASK_LAST_ERROR, (spec.task_id,)).fetchone()
        assert row is not None
        assert row[0]["title"] == BUDGET_EXCEEDED

    async def test_a_handler_blocking_the_interpreter_is_terminated_near_its_cap(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        """The margin, measured: cap + grace + one bookkeeping transaction.

        The handler holds the interpreter for six times its budget and nothing
        can make it stop, so the bound has to come from the worker not waiting.
        """
        cap = ONE_SECOND_WALL_CLOCK.wall_clock.total_seconds()
        spec = queued(
            task_type=BLOCKS_THE_INTERPRETER,
            payload={"seconds": 6.0},
            budget=ONE_SECOND_WALL_CLOCK,
            max_attempts=1,
        )
        started = time.monotonic()
        assert await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once() == 1
        elapsed = time.monotonic() - started

        margin = elapsed - cap
        assert margin < 1.0, f"margin was {margin:.2f}s over a {cap:.0f}s cap"
        assert margin >= DEADLINE_GRACE.total_seconds()  # the thread was given its chance first
        assert state_of(conn, spec.task_id) is TaskState.DEAD
        row = conn.execute(SELECT_TASK_LAST_ERROR, (spec.task_id,)).fetchone()
        assert row is not None
        assert row[0]["title"] == BUDGET_EXCEEDED
        assert row[0]["budget"]["wall_clock"] == "PT1S"  # the cap travels with the problem
        conn.commit()

    async def test_the_handler_and_the_worker_never_share_a_connection(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        """The safety property, observed rather than asserted.

        Mid-flight, the handler holds an *open transaction* containing its
        first checkpoint, on a backend of its own, on a thread that is not the
        event loop's -- while the worker has already committed `running` to the
        same task row. One psycopg connection cannot do both: a shared
        connection would have committed or discarded the handler's write along
        with the worker's. That the two are independent is what the old
        `asyncio.to_thread` shape could not offer, and it is why the worker can
        end the attempt without ever touching what the handler is using.

        Afterwards the handler's session is gone: the worker had Postgres end
        it, so an abandoned handler is not merely expected not to write -- it
        cannot.
        """
        spec = queued(
            task_type=BLOCKS_THE_INTERPRETER,
            payload={"seconds": 6.0},
            budget=ONE_SECOND_WALL_CLOCK,
            max_attempts=1,
        )
        execution = asyncio.create_task(Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once())

        await until(lambda: spec.task_id in _sightings)
        sighting = _sightings[spec.task_id]
        assert sighting.thread_ident != threading.get_ident()  # off the loop, which stayed free
        assert backend_state(conn, sighting.backend_pid) == "idle in transaction"
        assert state_of(conn, spec.task_id) is TaskState.RUNNING  # committed by the other session
        assert checkpoints(conn, spec.task_id) == []  # while the handler's write is still pending

        assert await execution == 1
        assert backend_state(conn, sighting.backend_pid) is None
        assert checkpoints(conn, spec.task_id) == []
        task = get_task(conn, spec.task_id)
        conn.commit()
        assert task is not None
        assert task.state is TaskState.DEAD and task.attempts == 1


class TestBudgetIsNotEveryTimeout:
    """H4's other edge: `budget_exceeded` must mean the budget (#57).

    The three shapes above assert the cap fires when it should. This asserts
    what the cap must *not* claim -- a timeout from somewhere else, well inside
    a budget nothing has approached. While every `TimeoutError` classified as an
    overrun, H4's assertion was satisfiable by an unreachable host, which is the
    fitness function passing for the wrong reason that GUARDRAILS §3 exists to
    prevent -- and the operator reading `last_error` was sent to the budget
    instead of to the host.
    """

    async def test_a_connect_timeout_inside_the_budget_is_not_a_budget_failure(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        # The fixture budget is five minutes; this fails in milliseconds.
        spec = queued(
            task_type=CANNOT_REACH_ITS_SOURCE,
            payload={"unreachable": True},
            max_attempts=1,
        )
        started = time.monotonic()
        assert await Worker(dsn, "w1", retry_base_delay=NO_BACKOFF).run_once() == 1
        elapsed = time.monotonic() - started

        cap = spec.budget.wall_clock.total_seconds()
        assert elapsed < cap / 10, f"took {elapsed:.2f}s, too near the {cap:.0f}s cap to prove anything"
        assert state_of(conn, spec.task_id) is TaskState.DEAD
        row = conn.execute(SELECT_TASK_LAST_ERROR, (spec.task_id,)).fetchone()
        conn.commit()
        assert row is not None
        assert row[0]["title"] == HANDLER_FAILED  # the host, not the cap
        assert row[0]["detail"].startswith("TimeoutError")
        assert "budget" not in row[0]  # and the cap does not travel with it


class TestWorkerStaysResponsive:
    """N1: an executing worker is still a working worker.

    A handler used to run on the event loop, so for its whole duration the
    worker could neither reap another worker's expired leases nor notice a
    SIGTERM. Both are now bounded by a poll interval instead of a task.
    """

    async def test_expired_leases_are_reaped_while_a_handler_runs(
        self,
        dsn: str,
        conn: QueueConnection,
        open_conn: Callable[[], QueueConnection],
        queued: Callable[..., TaskSpec],
    ) -> None:
        stale = queued(task_type=NOOP_TASK_TYPE, payload={"n": 700})
        crashed = open_conn()
        claim(crashed, worker_id="crashed", lease=EXPIRED_LEASE, task_types=[NOOP_TASK_TYPE])
        crashed.commit()
        crashed.close()  # the crash: claimed, never executed, lease already expired

        blocking = queued(task_type=BLOCKS_THE_INTERPRETER, payload={"seconds": 6.0})
        stop = asyncio.Event()
        worker = Worker(dsn, "w1", poll_interval=timedelta(milliseconds=50))
        loop_task = asyncio.create_task(worker.run_forever(stop))
        try:
            await until(lambda: state_of(conn, blocking.task_id) is TaskState.RUNNING)
            await until(lambda: state_of(conn, stale.task_id) is TaskState.PENDING, within=5.0)
            assert state_of(conn, blocking.task_id) is TaskState.RUNNING  # still working on it
        finally:
            stop.set()
            await asyncio.wait_for(loop_task, timeout=5)

    async def test_shutdown_does_not_wait_out_the_handler(
        self, dsn: str, conn: QueueConnection, queued: Callable[..., TaskSpec]
    ) -> None:
        spec = queued(task_type=BLOCKS_THE_INTERPRETER, payload={"seconds": 6.0})
        stop = asyncio.Event()
        worker = Worker(dsn, "w1", poll_interval=timedelta(milliseconds=50))
        loop_task = asyncio.create_task(worker.run_forever(stop))

        await until(lambda: state_of(conn, spec.task_id) is TaskState.RUNNING)
        stop.set()
        started = time.monotonic()
        await asyncio.wait_for(loop_task, timeout=5)
        assert time.monotonic() - started < 1.0  # not the handler's remaining seconds

        # The attempt is left where a reaper can find it (N1) rather than
        # failed: nothing about it exceeded a budget. "Abandoned" has to mean
        # abandoned, though -- the worker takes the handoff and drops the
        # handler's session on the way out, so the thread it leaves behind
        # cannot come back later and settle a task nobody is supervising.
        assert state_of(conn, spec.task_id) is TaskState.RUNNING
        sighting = _sightings[spec.task_id]
        assert backend_state(conn, sighting.backend_pid) is None
        await asyncio.sleep(0.5)
        assert state_of(conn, spec.task_id) is TaskState.RUNNING
        assert checkpoints(conn, spec.task_id) == []


class TestLeaseCoversBudget:
    """A lease must outlive the budget it supervises (I12, N1).

    The reaper only knows about leases. A task whose wall-clock budget exceeds
    the worker's lease would be handed back by `requeue_stale` while it is
    still legitimately inside its cap, then executed a second time
    concurrently -- and the first executor's `complete` rejected as
    `TaskNotClaimable`, discarding its writes. Retried under the same lease it
    fails identically, so the task can exhaust `max_attempts` and dead-letter
    without ever having exceeded the budget the API published for it.
    """

    async def test_a_budget_longer_than_the_lease_extends_the_lease(
        self, dsn: str, queued: Callable[..., TaskSpec]
    ) -> None:
        _observed_lease_headroom.clear()
        generous = RunBudget(steps=1, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(minutes=30))
        queued(task_type=REPORTS_ITS_LEASE, payload={}, budget=generous)

        await Worker(dsn, "lease-worker", lease=timedelta(minutes=5)).run_once()

        [headroom] = _observed_lease_headroom
        assert headroom > timedelta(minutes=5).total_seconds()

    async def test_a_budget_inside_the_lease_leaves_the_lease_alone(
        self, dsn: str, queued: Callable[..., TaskSpec]
    ) -> None:
        _observed_lease_headroom.clear()
        tight = RunBudget(steps=1, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(seconds=30))
        queued(task_type=REPORTS_ITS_LEASE, payload={}, budget=tight)

        await Worker(dsn, "lease-worker", lease=timedelta(minutes=5)).run_once()

        [headroom] = _observed_lease_headroom
        assert timedelta(minutes=4).total_seconds() < headroom <= timedelta(minutes=5).total_seconds()
