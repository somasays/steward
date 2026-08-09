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

`Handoff` is what makes "*at most* one of them records" true rather than
likely, and what makes an abandoned thread harmless: having lost the handoff it
never touches the task row, and its session is gone by then anyway, so its
writes are discarded and the attempt the worker recorded stands. At most, not
exactly -- see below and SPEC.md §13 D7.

The three caps nobody can enforce from outside
----------------------------------------------
Wall-clock is bounded above by machinery: the thread's timeout, the driver's,
and the loop's deadline. Steps, tokens and cost are not observable from here at
all -- they are counted inside the handler and *reported* on its `TaskResult`.
So they are enforced where they become visible: a succeeded result whose usage
exceeds the task's own budget is turned into a `budget_exceeded` failure
(`_overspent`), and its usage is therefore never rolled up onto the run. That
is what keeps `runs.used_*` inside `runs.budget_*` once a plan's per-task caps
are reserved against the run's (issue #48) -- the reservation bounds what tasks
may spend, this bounds what they may *report*, and the run's totals are the sum
of the second (I12, N6).

What the thread never does is raise at the loop
-----------------------------------------------
Everything that can go wrong here -- the handler, the connection it needs
before the handler can run at all, the statement that records the outcome -- is
one task's failure and is reported as an `Executed`, never as an exception on
the future. The loop reads that future from inside its poll loop, so an
exception on it is a worker killed by one task: #45, reached in practice by a
handler connection that could not be opened once #42 doubled the connections a
task holds. **Nothing travels.** Not `CancelledError` (#55), and since #63 not
`SystemExit` or `KeyboardInterrupt` either.

The fatal set is empty here because a thread has nothing to say about a
process. `SystemExit` on a non-main thread ends that thread under Python's own
threading semantics and nothing else, so escalating it to a worker exit
manufactured a process death the interpreter would not perform -- and any
dependency with an argparse/click-style fail-fast path calls `sys.exit()`,
which made it the #55 shape through the one door #55 left named.
`KeyboardInterrupt` cannot be *delivered* here at all: the interpreter raises
it on the main thread, so one arriving on a handler thread can only be an
explicit raise by the code running there, which is a task's bug wearing a
shutdown's name.

A real shutdown never used this door. `services/workers` wires SIGINT and
SIGTERM to the loop's stop event, which `wait` below already honours: the loop
takes the handoff, drops the handler's session, and leaves the attempt to its
lease (N1). Where `add_signal_handler` is unavailable, a `KeyboardInterrupt`
still lands on the main thread -- where the loop runs -- and ends the process
from there. Handler threads are daemons, so an abandoned one cannot hold the
process open either way.

What that costs is stated plainly in SPEC.md §13 D7 and unchanged in kind by
#63: `MemoryError` and an fd-exhausted `OSError` are one task's failure here, so
a degraded worker consumes attempts instead of exiting for a fresh pod. #63 adds
the same trade for two more classes nobody has seen a worker recover from.

An outcome the thread could not write reaches the loop as `recorded=False`, and
the handoff decides whether the loop may write it instead. That covers a thread
that fell over before taking the handoff. It deliberately does not cover a
thread that took the handoff and *then* failed to write: the loop cannot know
that thread is not still inside a commit, so it writes nothing, and the attempt
keeps its lease until `requeue_stale` returns it (N1). Zero contexts record that
attempt -- which is why the property here is "at most one", never "exactly one"
(#53, SPEC.md §13 D7).
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
from steward_queue.usage import UsageLedger

__all__ = [
    "BUDGET_EXCEEDED",
    "DEADLINE_GRACE",
    "EXECUTION_FAILED",
    "HANDLER_FAILED",
    "Executed",
    "Handoff",
    "RecordOutcome",
    "budget_exceeded",
    "problem",
    "spawn",
    "wait",
]
"""The seam `worker` runs an execution through, and nothing else (I3, #58).

`spawn` and `wait` are the entry points; `Handoff`, `Executed` and
`RecordOutcome` are the types their signatures carry -- contracts between two
modules, not internals, which is how `_Handoff` and `_Executed` read while
`worker` imported them anyway. The titles and `DEADLINE_GRACE` are re-exported
by `worker`, the module tests and operators address.

Everything else stays underscored because none of it crosses a boundary:
`_run_handler`, `_classify`, `_bounded`, the `_WallClockExpired` it raises.
"""

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

type RecordOutcome = Callable[
    [QueueConnection, ClaimedTask, TaskResult | ProblemDetails, RunBudget], None
]
"""Write an attempt's terminal state on the connection it is handed.

The handler thread records through this on its *own* connection, so the
handler's writes and the terminal state commit together (I7, I8); the loop
records through the same callable on the worker's connection when it has to end
an attempt itself. Where the outcome is written is therefore an argument, and
the queue bookkeeping behind it -- retry policy, actor, lease -- stays in
`worker` instead of being reimplemented here.
"""

# The fourth argument is what this attempt's `UsageLedger` says it spent, and it
# is the charge *only* when the outcome is a `ProblemDetails` -- a failure with
# no result behind it. A `TaskResult` reports its own usage and is charged that,
# so exactly one number reaches the run per attempt and a handler that both
# debits and reports is not billed twice (SPEC.md §13 D12).


def problem(title: str, detail: str) -> ProblemDetails:
    """A typed failure record for the `last_error` column (SPEC.md §8)."""
    return ProblemDetails(type="urn:steward:task-failed", title=title, status=500, detail=detail)


def budget_exceeded(budget: RunBudget, detail: str | None = None) -> ProblemDetails:
    """The typed, visible failure I12 requires when a hard cap is hit.

    Carries the budget it blew as an RFC 9457 extension member (which is why
    this goes through `model_validate` rather than the constructor), so an
    operator reading the `last_error` column sees the cap, not just the symptom.

    `detail` defaults to the wall-clock overrun because that is the cap the
    runtime itself enforces; the other three dimensions are reported by the
    handler and checked in `_overspent`, which passes its own.
    """
    return ProblemDetails.model_validate(
        {
            "type": "urn:steward:budget-exceeded",
            "title": BUDGET_EXCEEDED,
            "status": 504,
            "detail": detail or f"wall-clock budget of {budget.wall_clock} exhausted",
            "budget": budget.model_dump(mode="json"),
        }
    )


def _overspent(result: TaskResult, budget: RunBudget) -> ProblemDetails | None:
    """The failure a task that outspent its own cap ends on, or None (I12).

    The other half of "budgets are hard" (SPEC.md §3.2). Wall-clock is bounded
    by the runtime, which does not need the handler's cooperation; steps,
    tokens and cost are *reported* by the handler, and a report is not a bound
    until something compares it to the cap. This is that comparison, and it
    runs on the one path every task's result takes.

    Without it, run-level reservation (issue #48) would bound only what tasks
    are *allowed* to spend, leaving `runs.used_*` -- the sum of what they say
    they spent -- free to exceed the run's budget one task at a time. With it,
    a succeeded result that overran becomes a `budget_exceeded` failure whose
    usage is never rolled up, so the run's totals stay inside its caps by
    construction rather than by handlers being well-behaved.

    In-loop enforcement (stopping an agent at the step that would cross the
    cap, rather than failing the task that already did) lands with the M1 agent
    loop and is what H4's step/token/cost half measures; this is the outer
    fence, and it holds whatever the loop inside does.
    """
    overrun = result.usage.over(budget)
    if not overrun:
        return None
    return budget_exceeded(budget, f"reported usage exceeded the task budget: {', '.join(overrun)}")


class Handoff:
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
class Executed:
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


def _consume(finished: asyncio.Future[Executed]) -> None:
    """Retrieve an abandoned execution's outcome so asyncio does not log it.

    A handler the loop stopped waiting for still finishes and still resolves
    its future, and a future that resolves to an exception nobody read prints
    a warning at garbage-collection time. The outcome is genuinely uninteresting
    by then -- the attempt has already been recorded -- so it is read and dropped.

    Since #63 the thread cannot *set* an exception -- `_execute_in_thread`
    calls `set_result` on both branches -- so what is left to read is the
    cancellation of the wrapped future itself, which `cancelled()` guards.
    Kept rather than deleted because the class of warning it suppresses is a
    property of `wrap_future`, not of what this module currently raises.
    """
    if not finished.cancelled():
        finished.exception()


class _WallClockExpired(Exception):
    """The cap's own timeout, told apart from every other `TimeoutError`.

    Since 3.11 `socket.timeout`, `asyncio.TimeoutError` and the `OSError`
    subclass are one class, so a customer database's `connect_timeout` firing
    five seconds into a half-hour budget arrives at `_classify` as the same type
    an overrun does. Raised only by `_bounded`, and only when the cap it set is
    the thing that fired, so the type is evidence rather than a guess (#57).

    An `Exception` rather than a `BaseException` subclass: it is a fault like
    any other and nothing on the way to `_classify` reads it as more than that.
    It was also the load-bearing choice while `_run_handler` answered only for
    `Exception` -- a sentinel outside that was #55 with a different name -- and
    is now merely the accurate one (#63).
    """


async def _bounded(coro: Awaitable[TaskResult], budget: timedelta) -> TaskResult:
    """Run a handler under its wall-clock cap, on the thread's own event loop.

    This is the cheap half of enforcement and it only reaches handlers that
    await. The expensive half -- a handler that never yields -- is the loop's
    deadline, which does not depend on the handler cooperating at all.

    A `TimeoutError` leaving here is re-raised as itself unless `asyncio.timeout`
    reports that it expired -- the handler's own timeouts are the handler's, and
    only the cap's is the cap's.
    """
    cap = asyncio.timeout(budget.total_seconds())
    try:
        async with cap:
            return await coro
    except TimeoutError as exc:
        if not cap.expired():
            raise
        raise _WallClockExpired(f"wall-clock cap of {budget} reached") from exc


def spawn(
    *,
    dsn: str,
    task: ClaimedTask,
    registration: HandlerRegistration,
    handoff: Handoff,
    record: RecordOutcome,
    usage: UsageLedger,
) -> asyncio.Future[Executed]:
    """Start the handler on a thread of its own; return its awaitable result.

    A plain daemon thread, not `asyncio.to_thread`'s shared executor: a
    handler the loop has abandoned goes on occupying its thread, and in a
    pool that thread is one the worker also needs for its own bookkeeping
    calls. Daemon, so an abandoned handler cannot keep the process alive.

    `usage` is created by the caller rather than here because the caller is the
    one that still needs it after this thread is beyond reach: abandoning a
    handler at its cap means never seeing its result, and the ledger is then the
    only surviving account of what it spent.
    """
    future: concurrent.futures.Future[Executed] = concurrent.futures.Future()
    threading.Thread(
        target=_execute_in_thread,
        args=(dsn, task, registration, handoff, record, usage, future),
        name=f"steward-task-{task.spec.task_id}",
        daemon=True,
    ).start()
    awaitable = asyncio.wrap_future(future)
    awaitable.add_done_callback(_consume)
    return awaitable


async def wait(finished: asyncio.Future[Executed], deadline: timedelta, stop: asyncio.Event | None) -> None:
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
    handoff: Handoff,
    record: RecordOutcome,
    usage: UsageLedger,
    future: concurrent.futures.Future[Executed],
) -> None:
    """Thread entry point. Never raises, and never fails the worker.

    `_run_handler` already answers for the handler itself. What this catches is
    everything around it: the connection opened before the handler is called,
    the context built from it, the statement that records the outcome, the
    close on the way out. Those raised straight through the future and out of
    the poll loop until #45 -- one task's transient `OperationalError` ending a
    worker and leaving its task `running` for a budget-length lease.

    The exception becomes this task's typed failure instead, unrecorded, for
    the loop to settle. Nothing is excepted from that any more, which is what
    `except BaseException` with no branch above it says: this frame runs on a
    thread, and a thread cannot end a process (#63). `SystemExit` and
    `KeyboardInterrupt` were named here as "the process ending" until they were
    read as what they are on a thread -- the argument is in SPEC.md §13 D7 and
    in this module's docstring.
    """
    try:
        future.set_result(_run_handler(dsn, task, registration, handoff, record, usage))
    except BaseException as exc:
        detail = f"{type(exc).__name__}: {exc}"
        failure = Executed(error=problem(EXECUTION_FAILED, detail), lost_claim=False, recorded=False)
        future.set_result(failure)


def _run_handler(
    dsn: str,
    task: ClaimedTask,
    registration: HandlerRegistration,
    handoff: Handoff,
    record: RecordOutcome,
    usage: UsageLedger,
) -> Executed:
    """The whole execution, on this thread, through a connection of its own.

    The connection is a local of this frame and is closed before it returns,
    so no other context ever holds a reference to it -- that is the whole
    of the thread-safety argument, and it is structural.

    `asyncio.run` gives the handler an event loop of its own. Handlers stay
    `async def` (M1+ awaits the LLM gateway from them) and a handler that
    blocks now blocks only this thread. That loop is also why a `CancelledError`
    out of `asyncio.run` is caught here with everything else: it was raised on a
    loop nobody outside this frame can reach, so it is one handler's bug and not
    a signal about the worker (#55).

    The catch is `BaseException` for the same reason, one level out: what the
    handler raised is the handler's, whatever it inherits from. A dependency
    that calls `sys.exit()` on a fail-fast path, or one whose fatal-error class
    sits outside `Exception`, is a bug in the code this task chose to run --
    `handler raised`, per `_classify`. Narrower, it fell through to
    `_execute_in_thread` and was titled `execution failed`, which sends the
    operator to the connection when the fault is in the task (#63).
    """
    budget = task.spec.budget.wall_clock
    conn = connect(dsn, statement_timeout=budget)
    try:
        handoff.publish(conn.info.backend_pid)
        ctx = TaskContext(
            connection=conn,
            spec=task.spec,
            attempts=task.attempts,
            trace_id=task.trace_id,
            usage=usage,
        )
        started = time.monotonic()
        try:
            result = asyncio.run(_bounded(registration.fn(ctx), budget))
        except BaseException as exc:
            outcome: TaskResult | ProblemDetails = _classify(
                exc, task.spec.budget, time.monotonic() - started
            )
        else:
            outcome = _settled(result, task.spec.budget)
        return _record_in_thread(conn, task, handoff, record, outcome, usage.total())
    finally:
        # Whatever was not committed above never happened. Suppressed
        # because an abandoned handler's session has already been ended by
        # the worker, and a rollback on a dead connection is then the
        # correct outcome arriving as an exception.
        with contextlib.suppress(psycopg.Error):
            conn.rollback()
        conn.close()


def _settled(result: TaskResult, budget: RunBudget) -> TaskResult | ProblemDetails:
    """What a handler's returned result actually settles as.

    A handler answers with a `TaskResult` whichever way its work went, so three
    outcomes come out of one value: a failure it named itself, a failure it
    reported without naming, and a success -- which is only a success if what
    it says it spent fits the cap it was given (`_overspent`).
    """
    if result.status is not TaskStatus.SUCCEEDED:
        if result.error is None:
            return result.model_copy(update={"error": problem(HANDLER_FAILED, result.status.value)})
        return result
    return _overspent(result, budget) or result


def _classify(exc: BaseException, budget: RunBudget, elapsed: float) -> ProblemDetails:
    """Name the failure a handler ended on -- overrun, or genuine fault.

    Two shapes are the cap, and each is recognised by what it can offer.
    `_WallClockExpired` is the in-band one: `_bounded` raises it only when the
    timeout *it* set fired, so the type is proof. A driver-blocked handler has
    no such signal -- its overrun arrives as the connection's `QueryCanceled`,
    which is what a cancelled statement always looks like -- so it is recognised
    by the clock, and reporting it as "handler raised" would hide the one
    failure mode I12 exists to make visible.

    What is deliberately *not* here is `isinstance(exc, TimeoutError)`. Since
    3.11 that is also `socket.timeout`, so it filed an unreachable customer
    database as `budget_exceeded` against a cap nothing had approached: an
    operator sent to the budget instead of the host, and an H4 assertion a
    non-budget failure could satisfy (#57).
    """
    if isinstance(exc, _WallClockExpired) or elapsed >= budget.wall_clock.total_seconds():
        return budget_exceeded(budget)
    return problem(HANDLER_FAILED, f"{type(exc).__name__}: {exc}")


def _record_in_thread(
    conn: QueueConnection,
    task: ClaimedTask,
    handoff: Handoff,
    record: RecordOutcome,
    outcome: TaskResult | ProblemDetails,
    spent: RunBudget,
) -> Executed:
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
    error = (
        outcome
        if isinstance(outcome, ProblemDetails)
        else outcome.error if outcome.status is not TaskStatus.SUCCEEDED else None
    )
    if not handoff.take():
        return Executed(error=error, lost_claim=False, recorded=False)
    try:
        record(conn, task, outcome, spent)
    except tasks.TaskNotClaimable:
        return Executed(error=error, lost_claim=True, recorded=False)
    return Executed(error=error, lost_claim=False, recorded=True)
