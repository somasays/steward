"""A minimal asyncio worker loop.

The worker is deliberately thin: claim, dispatch to the registry, record the
outcome. It holds no policy of its own -- retries, backoff and dead-lettering
belong to `queue.fail`, budgets to the run, and business behaviour to handlers
(GUARDRAILS.md §4: "retry/timeout/budget logic duplicated in an agent instead
of the runtime"). There is no LLM here and there is not meant to be one: M0's
worker executes registered handlers only.

Transaction shape, one task at a time:

1. **claim** -- own transaction, committed immediately, so the claim is visible
   to other workers and the row is not locked for the length of the execution.
2. **mark running** -- own transaction, so `running` is observable rather than
   a state that only ever exists inside an uncommitted transaction.
3. **execute + record** -- one transaction: the handler's writes, the terminal
   state and the audit row commit together, or none of them do (I7, I8).

If the worker dies between 1 and 3 the task sits with an expired lease and
`queue.requeue_stale` returns it to `pending` -- a crash costs a retry, never a
task (N1, H3).

psycopg's synchronous connection is used from the async loop: the blocking
queue calls run in `asyncio.to_thread`, and the connection is never touched by
two coroutines at once. Handlers are `async def` (M1+ will call the LLM gateway
from them) and use the same connection directly for their -- short -- writes.
"""

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import timedelta

from steward_schemas import ProblemDetails, RunBudget, TaskResult, TaskStatus

from steward_queue import queue
from steward_queue.backoff import DEFAULT_BASE_DELAY, DEFAULT_FACTOR, DEFAULT_MAX_DELAY
from steward_queue.db import QueueConnection, connect
from steward_queue.models import SYSTEM_ACTOR, Actor, ClaimedTask
from steward_queue.registry import TaskContext, UnknownTaskType, get_handler, registered_types

DEFAULT_POLL_INTERVAL = timedelta(milliseconds=200)

HANDLER_FAILED = "handler raised"
UNKNOWN_TYPE = "no handler registered for task type"
BUDGET_EXCEEDED = "budget_exceeded"


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


class Worker:
    """Claims tasks for the types this process has handlers for and runs them."""

    def __init__(
        self,
        dsn: str,
        worker_id: str,
        *,
        task_types: Sequence[str] | None = None,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        lease: timedelta = queue.DEFAULT_LEASE,
        batch_size: int = 1,
        retry_base_delay: timedelta = DEFAULT_BASE_DELAY,
        retry_factor: float = DEFAULT_FACTOR,
        retry_max_delay: timedelta = DEFAULT_MAX_DELAY,
        actor: Actor | None = None,
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

    async def run_once(self) -> int:
        """Claim and execute one batch. Returns how many tasks were claimed."""
        claimed = await asyncio.to_thread(self._claim)
        for task in claimed:
            await self.execute(task)
        return len(claimed)

    async def reap_stale(self) -> int:
        """Return lease-expired tasks to the queue. Returns how many."""
        return await asyncio.to_thread(self._reap)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set, reaping stale leases whenever idle."""
        while not stop.is_set():
            if await self.run_once():
                continue
            await self.reap_stale()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self._poll_interval.total_seconds())

    # --- synchronous halves, always called through asyncio.to_thread ---

    def _claim(self) -> list[ClaimedTask]:
        with connect(self._dsn) as conn:
            claimed = queue.claim(
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
            recovered = queue.requeue_stale(conn, actor=self._actor)
            conn.commit()
        return len(recovered)

    def _start(self, conn: QueueConnection, task: ClaimedTask) -> None:
        queue.mark_running(
            conn,
            task.spec.task_id,
            lease=self._lease,
            claimed_by=self._worker_id,
            actor=self._actor,
        )
        conn.commit()

    def _succeed(self, conn: QueueConnection, result: TaskResult) -> None:
        queue.complete(conn, result, claimed_by=self._worker_id, actor=self._actor)
        conn.commit()

    def _fail(self, conn: QueueConnection, task: ClaimedTask, error: ProblemDetails) -> None:
        conn.rollback()  # discard the failed attempt's partial writes before recording it
        queue.fail(
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

    # --- execution ---

    async def execute(self, task: ClaimedTask) -> bool:
        """Run one claimed task and record its outcome.

        Returns False when the task was taken back mid-flight -- a reaper
        requeued it because this worker's lease expired while it was working.
        That is not an error: the execution simply becomes an attempt that did
        not get to record itself, and handlers are idempotent so the retry is
        safe (I8). Swallowing it here keeps a stalled worker from crash-looping
        its poll loop.
        """
        conn = await asyncio.to_thread(connect, self._dsn)
        try:
            return await self._execute_on(conn, task)
        except queue.TaskNotClaimable:
            await asyncio.to_thread(conn.rollback)
            return False
        finally:
            await asyncio.to_thread(conn.close)

    async def _execute_on(self, conn: QueueConnection, task: ClaimedTask) -> bool:
        await asyncio.to_thread(self._start, conn, task)
        try:
            registration = get_handler(task.spec.task_type)
        except UnknownTaskType:
            await asyncio.to_thread(self._fail, conn, task, _problem(UNKNOWN_TYPE, task.spec.task_type))
            return True
        ctx = TaskContext(connection=conn, spec=task.spec, attempts=task.attempts)
        try:
            # I12: the wall-clock cap is enforced here, by the runtime, not left
            # to handlers to honour. A handler blocked on a dead connection ends
            # as a typed `budget_exceeded` failure instead of holding this worker
            # slot until the lease expires. Step/token/cost caps belong to the
            # agent loop that spends them (steward-agents, M1) -- M0 has no LLM.
            async with asyncio.timeout(task.spec.budget.wall_clock.total_seconds()):
                result = await registration.fn(ctx)
        except TimeoutError:
            await asyncio.to_thread(self._fail, conn, task, _budget_exceeded(task.spec.budget))
            return True
        except Exception as exc:
            await asyncio.to_thread(
                self._fail, conn, task, _problem(HANDLER_FAILED, f"{type(exc).__name__}: {exc}")
            )
            return True
        if result.status is not TaskStatus.SUCCEEDED:
            error = result.error or _problem(HANDLER_FAILED, result.status.value)
            await asyncio.to_thread(self._fail, conn, task, error)
            return True
        await asyncio.to_thread(self._succeed, conn, result)
        return True
