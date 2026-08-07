"""A minimal asyncio worker loop.

The worker is deliberately thin: claim, dispatch to the registry, record the
outcome. It holds no policy of its own -- retries, backoff and dead-lettering
belong to `tasks.fail`, budgets to the run, and business behaviour to handlers
(GUARDRAILS.md §4: "retry/timeout/budget logic duplicated in an agent instead
of the runtime"). There is no LLM here and there is not meant to be one: M0's
worker executes registered handlers only.

Transaction shape, one task at a time:

1. **claim** -- own transaction, committed immediately, so the claim is visible
   to other workers and the row is not locked for the length of the execution.
2. **mark running** -- own transaction, so `running` is observable rather than
   a state that only ever exists inside an uncommitted transaction.
3. **execute + record** -- one transaction on the handler's own connection: the
   handler's writes, the terminal state and the audit row commit together, or
   none of them do (I7, I8).

If the worker dies between 1 and 3 the task sits with an expired lease and
`tasks.requeue_stale` returns it to `pending` -- a crash costs a retry, never a
task (N1, H3).

Which connections a crash-loop can still reach (#56, SPEC.md §13 D7)
----------------------------------------------------------------------
A task opens up to four connections across a poll: `_claim`'s, `_reap`'s,
`execute`'s bookkeeping one, and the handler's own (`execution`). `_claim`
has no task in hand, so a connection failure there is left fatal -- see its
docstring. The other three are covered: `_reap_forever` survives a broken
connection and retries on its own poll interval; `execute` retries its own
connect briefly and, failing that, leaves the attempt to lease recovery
instead of the worker; the handler's is `execution`'s to answer for (#45).

What this module is not
-----------------------
Running a handler is a job of its own and lives in `execution`: the thread it
runs on, the connection it opens there, the deadline the loop holds over it,
and the handoff that decides which of the two contexts records the attempt
(SPEC.md §13, D7). What stays here is the loop -- polling, claiming, the
worker's own bookkeeping connection, and settling whatever the execution
reports. The two meet at exactly three points: a `_Handoff` this module
creates, the `record` callable it hands down, and the `_Executed` it gets back.

Every execution runs inside a task span on the run's trace (I7). The worker
takes a `Tracer`, it does not build one: which backend traces, and whether one
is configured at all, is a wiring decision (`steward_telemetry.tracer_from_env`)
that belongs to the process being started, not to the loop.
"""

import asyncio
import contextlib
import time
from collections.abc import Sequence
from datetime import timedelta

import psycopg
from steward_schemas import ProblemDetails, TaskResult
from steward_telemetry import NoopTracer, Span, SpanOutcome, Tracer

from steward_queue import tasks
from steward_queue.backoff import DEFAULT_BASE_DELAY, DEFAULT_FACTOR, DEFAULT_MAX_DELAY
from steward_queue.db import QueueConnection, connect, set_statement_timeout, terminate_backend
from steward_queue.execution import (
    BUDGET_EXCEEDED,
    DEADLINE_GRACE,
    _budget_exceeded,
    _Executed,
    _Handoff,
    _problem,
    spawn,
    wait,
)
from steward_queue.models import SYSTEM_ACTOR, Actor, ClaimedTask
from steward_queue.registry import HandlerRegistration, UnknownTaskType, get_handler, registered_types

__all__ = ["BUDGET_EXCEEDED", "DEADLINE_GRACE", "Worker"]
"""`BUDGET_EXCEEDED` and `DEADLINE_GRACE` are defined next to the mechanism that
raises them and re-exported here, because this is the module operators, tests
and wiring code address -- the vocabulary of a task's outcome should not move
when the machinery behind it does."""

DEFAULT_POLL_INTERVAL = timedelta(milliseconds=200)

UNKNOWN_TYPE = "no handler registered for task type"
WORKER_STOPPING = "worker stopping"
BOOKKEEPING_CONNECTION_FAILED = "worker could not open its bookkeeping connection"

CONNECT_RETRY_ATTEMPTS = 3
CONNECT_RETRY_DELAY = timedelta(milliseconds=150)
"""Bounded retry for `execute`'s connect, aimed at losing a slot, not exhaustion
(#56, SPEC.md §13 D7). Rationale on `_connect_for_execute`, next to the code."""


class Worker:
    """Claims tasks for the types this process has handlers for and runs them."""

    def __init__(
        self,
        dsn: str,
        worker_id: str,
        *,
        task_types: Sequence[str] | None = None,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        lease: timedelta = tasks.DEFAULT_LEASE,
        batch_size: int = 1,
        retry_base_delay: timedelta = DEFAULT_BASE_DELAY,
        retry_factor: float = DEFAULT_FACTOR,
        retry_max_delay: timedelta = DEFAULT_MAX_DELAY,
        actor: Actor | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._dsn = dsn
        self._worker_id = worker_id
        self._task_types = tuple(task_types) if task_types is not None else registered_types()
        self._poll_interval = poll_interval
        self._lease = lease
        self._batch_size = batch_size
        self._retry_base_delay = retry_base_delay
        self._retry_factor = retry_factor
        self._retry_max_delay = retry_max_delay
        self._actor = actor or Actor(kind=SYSTEM_ACTOR.kind, id=worker_id)
        self._tracer: Tracer = tracer if tracer is not None else NoopTracer()

    async def run_once(self, *, stop: asyncio.Event | None = None) -> int:
        """Claim and execute one batch. Returns how many tasks were claimed."""
        claimed = await asyncio.to_thread(self._claim)
        for task in claimed:
            await self.execute(task, stop=stop)
        return len(claimed)

    async def reap_stale(self) -> int:
        """Return lease-expired tasks to the queue. Returns how many."""
        return await asyncio.to_thread(self._reap)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set, with lease recovery running alongside.

        The reaper is a task of its own rather than a step taken between polls,
        because a worker that is executing something still has to hand other
        workers' expired leases back (N1). That only became possible when
        handlers moved off the event loop: on the loop, a running handler
        starved this coroutine exactly as it starved everything else.
        """
        reaper = asyncio.create_task(self._reap_forever(stop))
        try:
            while not stop.is_set():
                if await self.run_once(stop=stop):
                    continue
                await self._idle(stop)
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper

    async def _reap_forever(self, stop: asyncio.Event) -> None:
        """Reap on every interval, surviving a connection this worker cannot open.

        `_reap`'s connect is a fourth call site with the same exposure as
        `_claim`'s and `execute`'s (#56), but no task in hand to fail and no
        caller reading its result -- this coroutine is created once and only
        ever awaited at shutdown (`run_forever`'s `finally`). Left uncaught, a
        connection failure here does not crash the worker; it silently ends
        the reaper and no lease is recovered until shutdown, which is worse
        than fatal because nothing signals it. The catch costs nothing this
        loop was not already going to pay: the next interval is a bounded
        retry it runs anyway, and every other worker's own reaper covers the
        same stale tasks in the meantime (N1, P4).
        """
        while not stop.is_set():
            with contextlib.suppress(psycopg.OperationalError):
                await self.reap_stale()
            await self._idle(stop)

    async def _idle(self, stop: asyncio.Event) -> None:
        """Wait one poll interval, or until the worker is asked to stop."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), self._poll_interval.total_seconds())

    # --- synchronous halves, always called off the event loop ---

    def _claim(self) -> list[ClaimedTask]:
        """Claim this worker's next batch. A connection failure here is left fatal.

        Unlike `execute`'s connect, there is no task in hand yet -- nothing is
        stranded by exiting, because nothing was claimed. Retrying in-process
        would also duplicate a policy that belongs to the process supervisor
        (systemd, Kubernetes), not the worker, and would hide the failure from
        it: an operator watching restarts sees a crash-loop; one watching a
        worker that silently never claims anything sees nothing at all. It is
        also the misconfiguration canary -- a bad DSN fails here, loudly, on
        the very first poll, rather than becoming a worker that polls forever
        and claims nothing (#56, SPEC.md §13 D7).
        """
        with connect(self._dsn) as conn:
            claimed = tasks.claim(
                conn,
                worker_id=self._worker_id,
                limit=self._batch_size,
                lease=self._lease,
                task_types=self._task_types,
                actor=self._actor,
            )
            conn.commit()
        return claimed

    def _reap(self) -> int:
        with connect(self._dsn) as conn:
            recovered = tasks.requeue_stale(conn, actor=self._actor)
            conn.commit()
        return len(recovered)

    def _lease_for(self, task: ClaimedTask) -> timedelta:
        """A lease long enough to cover the budget it is supervising.

        The lease and the wall-clock budget are two different bounds and the
        reaper only knows about the first, so a task whose budget exceeds the
        worker's lease gets taken back by `requeue_stale` while it is still
        legitimately running -- and then re-executed concurrently, with the
        original executor's `complete` failing `TaskNotClaimable` and its
        writes discarded. Retried under the same lease it fails identically,
        so the task can exhaust `max_attempts` and dead-letter without ever
        succeeding, entirely inside the budget the API published for it.

        Taking the larger of the two makes the budget the effective bound
        again: the runtime cancels a task for exceeding its cap (I12), never
        for outliving a lease that was shorter than the cap.
        """
        return max(self._lease, task.spec.budget.wall_clock)

    def _start(self, conn: QueueConnection, task: ClaimedTask) -> None:
        tasks.mark_running(
            conn,
            task.spec.task_id,
            lease=self._lease_for(task),
            claimed_by=self._worker_id,
            actor=self._actor,
        )
        conn.commit()

    def _record(self, conn: QueueConnection, task: ClaimedTask, outcome: TaskResult | ProblemDetails) -> None:
        """Write an attempt's terminal state on the connection it is given.

        The connection is an argument because the two contexts that can reach
        this point hold different ones: the handler thread records on its own,
        together with the handler's writes, and the loop records on the
        worker's when it has had to end an attempt itself.
        """
        if isinstance(outcome, ProblemDetails):
            self._fail(conn, task, outcome)
        else:
            self._succeed(conn, outcome)

    def _succeed(self, conn: QueueConnection, result: TaskResult) -> None:
        set_statement_timeout(conn, self._lease)  # bookkeeping runs under the lease, not the task's budget
        tasks.complete(conn, result, claimed_by=self._worker_id, actor=self._actor)
        conn.commit()

    def _fail(self, conn: QueueConnection, task: ClaimedTask, error: ProblemDetails) -> None:
        conn.rollback()  # discard the failed attempt's partial writes before recording it
        set_statement_timeout(conn, self._lease)
        tasks.fail(
            conn,
            task.spec.task_id,
            error,
            base_delay=self._retry_base_delay,
            factor=self._retry_factor,
            max_delay=self._retry_max_delay,
            claimed_by=self._worker_id,
            actor=self._actor,
        )
        conn.commit()

    def _abandon(self, conn: QueueConnection, handoff: _Handoff) -> None:
        """Dispose of the session of a handler this worker has stopped waiting for.

        The thread cannot be killed -- Python has no such operation -- but its
        database session can, and that is what matters: it is holding a
        transaction that will never commit, and the row locks in it are the
        ones this worker is about to need to record the outcome. Called only
        after winning the handoff, so a thread that is already writing its own
        outcome is never interrupted mid-transaction.

        Sent over the worker's own connection against a published pid, so this
        never becomes a second reference to the handler's connection.
        """
        backend_pid = handoff.backend_pid()
        if backend_pid is not None:
            terminate_backend(conn, backend_pid)

    # --- execution ---

    async def execute(self, task: ClaimedTask, *, stop: asyncio.Event | None = None) -> bool:
        """Run one claimed task and record its outcome.

        Returns False when the task was taken back mid-flight -- a reaper
        requeued it because this worker's lease expired while it was working.
        That is not an error: the execution simply becomes an attempt that did
        not get to record itself, and handlers are idempotent so the retry is
        safe (I8). Swallowing it here keeps a stalled worker from crash-looping
        its poll loop.

        The connection opened here is the worker's own -- `mark_running`, the
        session it ends at the deadline, and the terminal state the loop writes
        when it has to end an attempt itself. The handler never sees it; it gets one of its
        own, on its own thread (`execution`, D7). This one is opened under
        the lease, the bound on the worker's bookkeeping, and stays there.

        `stop` is honoured while a handler runs: a worker asked to shut down
        leaves the attempt to its lease rather than waiting out the budget, so
        SIGTERM latency is a poll interval instead of a task duration (N1).

        A task IS claimed by the time this runs, unlike `_claim`, which has
        none to answer for -- so a connection this worker cannot open here is
        not left to kill the worker (#56, SPEC.md §13 D7). `_connect_for_execute`
        retries briefly; if the pool stays out of room past that, the attempt
        is left `running` for `requeue_stale` rather than recorded, because
        there is no connection left to record it on.
        """
        conn = await asyncio.to_thread(self._connect_for_execute)
        if conn is None:
            self._span_unopened(task)
            return True
        try:
            with self._tracer.task_span(
                trace_id=task.trace_id,
                run_id=task.spec.run_id,
                task_id=task.spec.task_id,
                task_type=task.spec.task_type,
            ) as span:
                return await self._execute_on(conn, task, span, stop)
        except tasks.TaskNotClaimable:
            await asyncio.to_thread(conn.rollback)
            return False
        finally:
            await asyncio.to_thread(conn.close)

    def _connect_for_execute(self) -> QueueConnection | None:
        """Open the worker's bookkeeping connection, tolerating a transient refusal.

        A task is already claimed by the time `execute` calls this, so a
        connection failure here has a task-scoped answer that `_claim` does
        not have (SPEC.md §13, D7): leave the attempt where lease recovery
        finds it instead of taking the worker down over a task it already
        owns. The retry is aimed at the shape #56 found still reachable after
        #45 -- losing a race for the last slot in the pool, not genuine
        `max_connections` exhaustion -- so it stays short: a few attempts a
        few hundred milliseconds apart, a fraction of a poll interval and
        nowhere near a task's lease.

        Always called off the event loop (`execute` runs it via `to_thread`),
        so the retry's `time.sleep` blocks only this thread. `None` tells the
        caller the pool stayed out of room past the bound: `requeue_stale`
        reclaims the task at lease expiry, the same "recorded by neither
        context" outcome #53 already accepts, reached from a different seam.
        """
        for attempt in range(CONNECT_RETRY_ATTEMPTS):
            try:
                return connect(self._dsn, statement_timeout=self._lease)
            except psycopg.OperationalError:
                if attempt == CONNECT_RETRY_ATTEMPTS - 1:
                    return None
                time.sleep(CONNECT_RETRY_DELAY.total_seconds())
        return None  # pragma: no cover -- unreachable; the loop always returns before falling off

    def _span_unopened(self, task: ClaimedTask) -> None:
        """Mark a span for an attempt that never got a connection to record on.

        The task row is untouched here: `execute`'s connect failure leaves the
        attempt exactly where a thread that took the handoff and then failed to
        write also leaves it (#53) -- unrecorded, still owned, and reclaimed by
        `requeue_stale` at lease expiry. Nothing else can write a terminal state
        without a connection to write it on.
        """
        with self._tracer.task_span(
            trace_id=task.trace_id,
            run_id=task.spec.run_id,
            task_id=task.spec.task_id,
            task_type=task.spec.task_type,
        ) as span:
            span.record(SpanOutcome.ERROR, BOOKKEEPING_CONNECTION_FAILED)

    async def _execute_on(
        self, conn: QueueConnection, task: ClaimedTask, span: Span, stop: asyncio.Event | None
    ) -> bool:
        await asyncio.to_thread(self._start, conn, task)
        try:
            registration = get_handler(task.spec.task_type)
        except UnknownTaskType:
            problem = _problem(UNKNOWN_TYPE, task.spec.task_type)
            await self._record_failure(conn, task, problem, span)
            return True

        handoff = _Handoff()
        finished = self._spawn(task, registration, handoff)
        deadline = task.spec.budget.wall_clock + DEADLINE_GRACE
        started = time.monotonic()
        await wait(finished, deadline, stop)

        if finished.done():
            return await self._settle(conn, task, handoff, finished.result(), span)
        stopping = time.monotonic() - started < deadline.total_seconds()
        if not handoff.take():
            # The thread claimed the recording in the moment between the wait
            # returning and this line; its outcome is the authoritative one.
            return await self._settle(conn, task, handoff, await finished, span)
        await asyncio.to_thread(self._abandon, conn, handoff)
        if stopping:
            # `stop` fired before the cap: the budget is intact, so this is not
            # a budget failure. The attempt is left `running` for a reaper to
            # requeue -- exactly N1's model, at the cost of one re-executed
            # idempotent attempt (I8). Taking the handoff first is what makes
            # that true rather than aspirational: without it the thread would
            # go on to commit a terminal state for an attempt this worker has
            # already walked away from, and for a handler nothing is bounding
            # any more.
            span.record(SpanOutcome.ERROR, WORKER_STOPPING)
            return True
        await self._record_failure(conn, task, _budget_exceeded(task.spec.budget), span)
        return True

    def _spawn(
        self, task: ClaimedTask, registration: HandlerRegistration, handoff: _Handoff
    ) -> asyncio.Future[_Executed]:
        """Hand one claimed task to `execution`, with this worker's way of recording it."""
        return spawn(
            dsn=self._dsn,
            task=task,
            registration=registration,
            handoff=handoff,
            record=self._record,
        )

    async def _settle(
        self,
        conn: QueueConnection,
        task: ClaimedTask,
        handoff: _Handoff,
        executed: _Executed,
        span: Span,
    ) -> bool:
        """Close out an execution the handler thread has finished with.

        Usually there is nothing to write: the thread recorded the outcome in
        the same transaction as the handler's writes, and this only marks the
        span. The exception is an execution that fell over before it could
        record itself -- the connection it needed, or the write itself (#45).
        The failure is this worker's to persist then, on its own connection,
        and it asks the handoff first so a thread that did get there is never
        double-counted.

        A lost claim is re-raised rather than reported: `execute` already knows
        how to answer one, and letting it travel as an exception is also what
        puts it on the span.
        """
        if executed.lost_claim:
            raise tasks.TaskNotClaimable("the task was taken back mid-execution")
        if executed.error is None:
            span.record(SpanOutcome.OK)
            return True
        if not executed.recorded and handoff.take():
            await asyncio.to_thread(self._fail, conn, task, executed.error)
        span.record(SpanOutcome.ERROR, executed.error.title)
        return True

    async def _record_failure(
        self, conn: QueueConnection, task: ClaimedTask, error: ProblemDetails, span: Span
    ) -> None:
        """Persist a failed attempt and mark its span.

        The span is recorded explicitly because these failures are return
        values, not raises -- the tracer's own exception handling would never
        see them, and an unmarked span would read as a success.
        """
        await asyncio.to_thread(self._fail, conn, task, error)
        span.record(SpanOutcome.ERROR, error.title)
