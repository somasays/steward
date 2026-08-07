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

Two connections, two contexts, no sharing (SPEC.md §13, D7)
-----------------------------------------------------------
A handler runs on a **thread of its own**, through a connection **it opens and
closes on that thread**. The event loop keeps a second connection for the
worker's own bookkeeping: `mark_running`, the terminate it sends at the deadline,
and the terminal state it writes when it has to end an attempt itself.

Nothing but an `int` -- the handler backend's pid -- ever crosses between the
two. The loop cannot reach the handler's connection object, so the race
`asyncio.to_thread` alone would introduce (the loop rolling a connection back
while the handler thread is still executing statements on it, on a driver whose
connections are not thread-safe) is not merely unlikely here, it is unreachable.
What the loop does instead, once it owns the attempt, is have Postgres end the
handler's *session* by pid -- which drops the transaction it will never commit,
and the row locks the worker is about to need.

That also fixes the enforcement hole this replaces: handlers are `async def`
but their work is blocking driver calls, so an `asyncio.timeout` around the
call had no await point to cancel at and never fired. The cap rested on
`statement_timeout`s each set to the *full* budget, which a handler making two
slow calls spends twice over -- and while it ran, the loop was blocked, so
SIGTERM latency equalled the task duration and `reap_stale` could not run (I12,
N1).

Who ends an overrun
-------------------
The handler thread gets first refusal: it runs the coroutine under an in-thread
`asyncio.timeout(cap)` and its connection carries `statement_timeout = cap`, so
an awaiting handler and a driver-blocked one both come back at the cap and
record their own `budget_exceeded` atomically with whatever they wrote.

The loop is the backstop for a thread that comes back from neither -- one
blocked in Python, which no timeout can reach and no thread can be killed out
of. `DEADLINE_GRACE` past the cap the loop stops waiting, ends the handler's
session, and records `budget_exceeded` itself. The margin it guarantees is
therefore `DEADLINE_GRACE` plus one terminate round trip plus one bookkeeping
transaction, *independent of what the handler is doing*, because nothing on
that path waits on the handler thread.

`_Handoff` is what makes "exactly one of them records" true rather than likely,
and what makes an abandoned thread harmless: having lost the handoff it never
touches the task row, and its session is gone by then anyway, so its writes are
discarded and the attempt the worker recorded stands.

Every execution runs inside a task span on the run's trace (I7). The worker
takes a `Tracer`, it does not build one: which backend traces, and whether one
is configured at all, is a wiring decision (`steward_telemetry.tracer_from_env`)
that belongs to the process being started, not to the loop.
"""

import asyncio
import concurrent.futures
import contextlib
import threading
import time
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg
from steward_schemas import ProblemDetails, RunBudget, TaskResult, TaskStatus
from steward_telemetry import NoopTracer, Span, SpanOutcome, Tracer

from steward_queue import tasks
from steward_queue.backoff import DEFAULT_BASE_DELAY, DEFAULT_FACTOR, DEFAULT_MAX_DELAY
from steward_queue.db import QueueConnection, connect, set_statement_timeout, terminate_backend
from steward_queue.models import SYSTEM_ACTOR, Actor, ClaimedTask
from steward_queue.registry import (
    HandlerRegistration,
    TaskContext,
    UnknownTaskType,
    get_handler,
    registered_types,
)

DEFAULT_POLL_INTERVAL = timedelta(milliseconds=200)

DEADLINE_GRACE = timedelta(milliseconds=500)
"""How long past the cap the loop lets the handler thread record its own overrun.

The thread's outcome is the better one -- it commits the terminal state in the
same transaction as the handler's writes -- so it is worth a short wait. It is
a fixed constant rather than a fraction of the budget because it bounds
*bookkeeping*, not work: a 20 ms budget and a 30 minute one need the same few
round trips to write a failure row.
"""

HANDLER_FAILED = "handler raised"
UNKNOWN_TYPE = "no handler registered for task type"
BUDGET_EXCEEDED = "budget_exceeded"
WORKER_STOPPING = "worker stopping"


def _problem(title: str, detail: str) -> ProblemDetails:
    """A typed failure record for the `last_error` column (SPEC.md §8)."""
    return ProblemDetails(type="urn:steward:task-failed", title=title, status=500, detail=detail)


def _budget_exceeded(budget: RunBudget) -> ProblemDetails:
    """The typed, visible failure I12 requires when a hard cap is hit.

    Carries the budget it blew as an RFC 9457 extension member (which is why
    this goes through `model_validate` rather than the constructor), so an
    operator reading the `last_error` column sees the cap, not just the symptom.
    """
    return ProblemDetails.model_validate(
        {
            "type": "urn:steward:budget-exceeded",
            "title": BUDGET_EXCEEDED,
            "status": 504,
            "detail": f"wall-clock budget of {budget.wall_clock} exhausted",
            "budget": budget.model_dump(mode="json"),
        }
    )


class _Handoff:
    """Which of an attempt's two contexts gets to record its outcome -- once.

    Both the handler thread and the event loop can reach the point of writing a
    terminal state for the same attempt, and they must not both do it: two
    `fail`/`complete` calls would count the attempt twice, or record an outcome
    for a task another worker has since claimed. `take` hands the role to
    whichever asks first and refuses everyone after, so the loser is left with
    one instruction -- roll back and touch nothing.

    `backend_pid` is the *only* thing the thread publishes to the loop. An
    integer cannot be used to run a statement, which is precisely why the loop
    ends the handler's session through its own connection instead of holding a
    reference to the handler's.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._taken = False
        self._backend_pid: int | None = None

    def take(self) -> bool:
        """Claim the right to record this attempt. True for exactly one caller."""
        with self._lock:
            if self._taken:
                return False
            self._taken = True
            return True

    def publish(self, backend_pid: int) -> None:
        """Announce the backend the handler's connection is running on."""
        with self._lock:
            self._backend_pid = backend_pid

    def backend_pid(self) -> int | None:
        """The handler's backend pid, or None if it never opened a connection."""
        with self._lock:
            return self._backend_pid


@dataclass(frozen=True, slots=True)
class _Executed:
    """What the handler thread did, reported back to the loop that owns the span.

    `error` is the typed failure it settled on (None on success) and is carried
    even when the thread did not get to record it, so the span still says how
    the work ended.
    """

    error: ProblemDetails | None
    lost_claim: bool


def _consume(finished: asyncio.Future[_Executed]) -> None:
    """Retrieve an abandoned execution's outcome so asyncio does not log it.

    A handler the loop stopped waiting for still finishes and still resolves
    its future, and a future that resolves to an exception nobody read prints
    a warning at garbage-collection time. The outcome is genuinely uninteresting
    by then -- the attempt has already been recorded -- so it is read and dropped.
    """
    if not finished.cancelled():
        finished.exception()


async def _bounded(coro: Awaitable[TaskResult], budget: timedelta) -> TaskResult:
    """Run a handler under its wall-clock cap, on the thread's own event loop.

    This is the cheap half of enforcement and it only reaches handlers that
    await. The expensive half -- a handler that never yields -- is the loop's
    deadline, which does not depend on the handler cooperating at all.
    """
    async with asyncio.timeout(budget.total_seconds()):
        return await coro


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
        while not stop.is_set():
            await self.reap_stale()
            await self._idle(stop)

    async def _idle(self, stop: asyncio.Event) -> None:
        """Wait one poll interval, or until the worker is asked to stop."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), self._poll_interval.total_seconds())

    # --- synchronous halves, always called off the event loop ---

    def _claim(self) -> list[ClaimedTask]:
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
        own, on its own thread (module docstring, D7). This one is opened under
        the lease, the bound on the worker's bookkeeping, and stays there.

        `stop` is honoured while a handler runs: a worker asked to shut down
        leaves the attempt to its lease rather than waiting out the budget, so
        SIGTERM latency is a poll interval instead of a task duration (N1).
        """
        conn = await asyncio.to_thread(connect, self._dsn, statement_timeout=self._lease)
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
        await self._wait(finished, deadline, stop)

        if finished.done():
            return self._settle(finished.result(), span)
        stopping = time.monotonic() - started < deadline.total_seconds()
        if not handoff.take():
            # The thread claimed the recording in the moment between the wait
            # returning and this line; its outcome is the authoritative one.
            return self._settle(await finished, span)
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
        """Start the handler on a thread of its own; return its awaitable result.

        A plain daemon thread, not `asyncio.to_thread`'s shared executor: a
        handler the loop has abandoned goes on occupying its thread, and in a
        pool that thread is one the worker also needs for its own bookkeeping
        calls. Daemon, so an abandoned handler cannot keep the process alive.
        """
        future: concurrent.futures.Future[_Executed] = concurrent.futures.Future()
        threading.Thread(
            target=self._execute_in_thread,
            args=(task, registration, handoff, future),
            name=f"steward-task-{task.spec.task_id}",
            daemon=True,
        ).start()
        awaitable = asyncio.wrap_future(future)
        awaitable.add_done_callback(_consume)
        return awaitable

    async def _wait(
        self, finished: asyncio.Future[_Executed], deadline: timedelta, stop: asyncio.Event | None
    ) -> None:
        """Wait for the handler, the deadline, or a shutdown -- whichever is first.

        `asyncio.wait` rather than `wait_for`, because a timeout here must not
        cancel anything: the thread owns a live transaction and ends it itself.
        """
        waiting: list[asyncio.Future[Any]] = [finished]
        stopping = None if stop is None else asyncio.ensure_future(stop.wait())
        if stopping is not None:
            waiting.append(stopping)
        try:
            await asyncio.wait(waiting, timeout=deadline.total_seconds(), return_when=asyncio.FIRST_COMPLETED)
        finally:
            if stopping is not None:
                stopping.cancel()

    def _execute_in_thread(
        self,
        task: ClaimedTask,
        registration: HandlerRegistration,
        handoff: _Handoff,
        future: concurrent.futures.Future[_Executed],
    ) -> None:
        """Thread entry point. Never raises: the loop must not wait on a lost future."""
        try:
            future.set_result(self._run_handler(task, registration, handoff))
        except BaseException as exc:
            future.set_exception(exc)

    def _run_handler(
        self, task: ClaimedTask, registration: HandlerRegistration, handoff: _Handoff
    ) -> _Executed:
        """The whole execution, on this thread, through a connection of its own.

        The connection is a local of this frame and is closed before it returns,
        so no other context ever holds a reference to it -- that is the whole
        of the thread-safety argument, and it is structural.

        `asyncio.run` gives the handler an event loop of its own. Handlers stay
        `async def` (M1+ awaits the LLM gateway from them) and a handler that
        blocks now blocks only this thread.
        """
        budget = task.spec.budget.wall_clock
        conn = connect(self._dsn, statement_timeout=budget)
        try:
            handoff.publish(conn.info.backend_pid)
            ctx = TaskContext(connection=conn, spec=task.spec, attempts=task.attempts)
            started = time.monotonic()
            try:
                result = asyncio.run(_bounded(registration.fn(ctx), budget))
            except Exception as exc:
                outcome: TaskResult | ProblemDetails = self._classify(
                    exc, task.spec.budget, time.monotonic() - started
                )
            else:
                outcome = (
                    result
                    if result.status is TaskStatus.SUCCEEDED
                    else result.error or _problem(HANDLER_FAILED, result.status.value)
                )
            return self._record_in_thread(conn, task, handoff, outcome)
        finally:
            # Whatever was not committed above never happened. Suppressed
            # because an abandoned handler's session has already been ended by
            # the worker, and a rollback on a dead connection is then the
            # correct outcome arriving as an exception.
            with contextlib.suppress(psycopg.Error):
                conn.rollback()
            conn.close()

    def _classify(self, exc: Exception, budget: RunBudget, elapsed: float) -> ProblemDetails:
        """Name the failure a handler ended on -- overrun, or genuine fault.

        Anything raised at or past the cap is the cap: a driver-blocked handler
        surfaces its overrun as the connection's `QueryCanceled`, and reporting
        that as "handler raised" would hide the one failure mode I12 exists to
        make visible. Below the cap the exception is the handler's own.
        """
        if isinstance(exc, TimeoutError) or elapsed >= budget.wall_clock.total_seconds():
            return _budget_exceeded(budget)
        return _problem(HANDLER_FAILED, f"{type(exc).__name__}: {exc}")

    def _record_in_thread(
        self,
        conn: QueueConnection,
        task: ClaimedTask,
        handoff: _Handoff,
        outcome: TaskResult | ProblemDetails,
    ) -> _Executed:
        """Write this attempt's terminal state -- if the loop has not already.

        Losing the handoff means the loop gave up on this thread and recorded
        the attempt itself. The only correct move is then to write nothing at
        all: the caller's `finally` rolls the handler's transaction back, so an
        abandoned execution leaves no trace of itself anywhere -- not in the
        catalog it was writing, and not on the task row.
        """
        error = outcome if isinstance(outcome, ProblemDetails) else None
        if not handoff.take():
            return _Executed(error=error, lost_claim=False)
        try:
            if isinstance(outcome, ProblemDetails):
                self._fail(conn, task, outcome)
            else:
                self._succeed(conn, outcome)
        except tasks.TaskNotClaimable:
            return _Executed(error=error, lost_claim=True)
        return _Executed(error=error, lost_claim=False)

    def _settle(self, executed: _Executed, span: Span) -> bool:
        """Mark the span for an execution the handler thread recorded itself.

        A lost claim is re-raised rather than reported: `execute` already knows
        how to answer one, and letting it travel as an exception is also what
        puts it on the span.
        """
        if executed.lost_claim:
            raise tasks.TaskNotClaimable("the task was taken back mid-execution")
        if executed.error is not None:
            span.record(SpanOutcome.ERROR, executed.error.title)
        else:
            span.record(SpanOutcome.OK)
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
