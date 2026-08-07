"""Running one claimed task under its wall-clock cap (SPEC.md §13, D7).

This is the mechanism half of the worker: a thread, a connection of its own, a
deadline, and the arbitration that decides who gets to record the attempt. The
polling loop, the claim and the worker's own bookkeeping live in `worker`,
which owns this module rather than the other way round.

Two connections, two contexts, no sharing
-----------------------------------------
A handler runs on a **thread of its own**, through a connection **it opens and
closes on that thread**. The worker keeps a second connection on the event loop
for its own bookkeeping: `mark_running`, the terminate it sends at the deadline,
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

What the thread never does is raise at the loop
-----------------------------------------------
Everything that can go wrong here -- the handler, the connection it needs
before the handler can run at all, the statement that records the outcome -- is
one task's failure and is reported as an `_Executed`, never as an exception on
the future. The loop reads that future from inside its poll loop, so an
exception on it is a worker killed by one task: #45, reached in practice by a
handler connection that could not be opened once #42 doubled the connections a
task holds. Only a `BaseException` that is not an `Exception` still travels,
because `SystemExit`, `KeyboardInterrupt` and cancellation are the process
ending rather than a task failing.

An outcome the thread could not write reaches the loop as `recorded=False`, and
the handoff decides whether the loop may write it instead -- so "exactly one
context records this attempt" survives the failure of the one that usually does.
"""

import asyncio
import concurrent.futures
import contextlib
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg
from steward_schemas import ProblemDetails, RunBudget, TaskResult, TaskStatus

from steward_queue import tasks
from steward_queue.db import QueueConnection, connect
from steward_queue.models import ClaimedTask
from steward_queue.registry import HandlerRegistration, TaskContext

DEADLINE_GRACE = timedelta(milliseconds=500)
"""How long past the cap the loop lets the handler thread record its own overrun.

The thread's outcome is the better one -- it commits the terminal state in the
same transaction as the handler's writes -- so it is worth a short wait. It is
a fixed constant rather than a fraction of the budget because it bounds
*bookkeeping*, not work: a 20 ms budget and a 30 minute one need the same few
round trips to write a failure row.
"""

HANDLER_FAILED = "handler raised"
EXECUTION_FAILED = "execution failed"
"""The title for a failure that was not the handler's: the machinery around it.

Kept distinct from `HANDLER_FAILED` because the two send an operator to
different places. "handler raised" is a bug in the task's own code; "execution
failed" is the worker unable to run it at all -- typically the connection the
handler needs, which is the shape #45 was reported as.
"""

BUDGET_EXCEEDED = "budget_exceeded"

type RecordOutcome = Callable[[QueueConnection, ClaimedTask, TaskResult | ProblemDetails], None]
"""Write an attempt's terminal state on the connection it is handed.

The handler thread records through this on its *own* connection, so the
handler's writes and the terminal state commit together (I7, I8); the loop
records through the same callable on the worker's connection when it has to end
an attempt itself. Where the outcome is written is therefore an argument, and
the queue bookkeeping behind it -- retry policy, actor, lease -- stays in
`worker` instead of being reimplemented here.
"""


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
    the work ended. `recorded` says whether the terminal state reached the
    database: false leaves the task unsettled, and the loop -- if the handoff
    is still there to be taken -- writes the failure itself.
    """

    error: ProblemDetails | None
    lost_claim: bool
    recorded: bool


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


def spawn(
    *,
    dsn: str,
    task: ClaimedTask,
    registration: HandlerRegistration,
    handoff: _Handoff,
    record: RecordOutcome,
) -> asyncio.Future[_Executed]:
    """Start the handler on a thread of its own; return its awaitable result.

    A plain daemon thread, not `asyncio.to_thread`'s shared executor: a
    handler the loop has abandoned goes on occupying its thread, and in a
    pool that thread is one the worker also needs for its own bookkeeping
    calls. Daemon, so an abandoned handler cannot keep the process alive.
    """
    future: concurrent.futures.Future[_Executed] = concurrent.futures.Future()
    threading.Thread(
        target=_execute_in_thread,
        args=(dsn, task, registration, handoff, record, future),
        name=f"steward-task-{task.spec.task_id}",
        daemon=True,
    ).start()
    awaitable = asyncio.wrap_future(future)
    awaitable.add_done_callback(_consume)
    return awaitable


async def wait(finished: asyncio.Future[_Executed], deadline: timedelta, stop: asyncio.Event | None) -> None:
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
    dsn: str,
    task: ClaimedTask,
    registration: HandlerRegistration,
    handoff: _Handoff,
    record: RecordOutcome,
    future: concurrent.futures.Future[_Executed],
) -> None:
    """Thread entry point. Never raises, and never fails the worker.

    `_run_handler` already answers for the handler itself. What this catches is
    everything around it: the connection opened before the handler is called,
    the context built from it, the statement that records the outcome, the
    close on the way out. Those raised straight through the future and out of
    the poll loop until #45 -- one task's transient `OperationalError` ending a
    worker and leaving its task `running` for a budget-length lease.

    The exception becomes this task's typed failure instead, unrecorded, for
    the loop to settle. A `BaseException` that is not an `Exception` still
    travels: the process is ending, and dressing that up as a failed task would
    hide a shutdown as a data point.
    """
    try:
        future.set_result(_run_handler(dsn, task, registration, handoff, record))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        failure = _Executed(error=_problem(EXECUTION_FAILED, detail), lost_claim=False, recorded=False)
        future.set_result(failure)
    except BaseException as exc:
        future.set_exception(exc)


def _run_handler(
    dsn: str,
    task: ClaimedTask,
    registration: HandlerRegistration,
    handoff: _Handoff,
    record: RecordOutcome,
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
    conn = connect(dsn, statement_timeout=budget)
    try:
        handoff.publish(conn.info.backend_pid)
        ctx = TaskContext(connection=conn, spec=task.spec, attempts=task.attempts)
        started = time.monotonic()
        try:
            result = asyncio.run(_bounded(registration.fn(ctx), budget))
        except Exception as exc:
            outcome: TaskResult | ProblemDetails = _classify(
                exc, task.spec.budget, time.monotonic() - started
            )
        else:
            outcome = (
                result
                if result.status is TaskStatus.SUCCEEDED
                else result.error or _problem(HANDLER_FAILED, result.status.value)
            )
        return _record_in_thread(conn, task, handoff, record, outcome)
    finally:
        # Whatever was not committed above never happened. Suppressed
        # because an abandoned handler's session has already been ended by
        # the worker, and a rollback on a dead connection is then the
        # correct outcome arriving as an exception.
        with contextlib.suppress(psycopg.Error):
            conn.rollback()
        conn.close()


def _classify(exc: Exception, budget: RunBudget, elapsed: float) -> ProblemDetails:
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
    conn: QueueConnection,
    task: ClaimedTask,
    handoff: _Handoff,
    record: RecordOutcome,
    outcome: TaskResult | ProblemDetails,
) -> _Executed:
    """Write this attempt's terminal state -- if the loop has not already.

    Losing the handoff means the loop gave up on this thread and recorded
    the attempt itself. The only correct move is then to write nothing at
    all: the caller's `finally` rolls the handler's transaction back, so an
    abandoned execution leaves no trace of itself anywhere -- not in the
    catalog it was writing, and not on the task row.

    Anything else `record` raises leaves this thread holding a handoff it
    cannot use, and travels on: the loop cannot write the outcome either (the
    handoff is gone, and this thread might still commit), so the attempt is
    left to its lease and `requeue_stale` returns it (N1).
    """
    error = outcome if isinstance(outcome, ProblemDetails) else None
    if not handoff.take():
        return _Executed(error=error, lost_claim=False, recorded=False)
    try:
        record(conn, task, outcome)
    except tasks.TaskNotClaimable:
        return _Executed(error=error, lost_claim=True, recorded=False)
    return _Executed(error=error, lost_claim=False, recorded=True)
