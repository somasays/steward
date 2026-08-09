"""The task aggregate: enqueue, claim, and the transitions that end a task.

Two rules hold for every function here and are the reason the module exists:

* **The caller owns the transaction.** Nothing commits or rolls back. Enqueue
  is therefore transactional with whatever domain change motivated it (I8) --
  there is no code path that can publish a task for a state change that never
  committed, or lose one that did.
* **A state mutation and its audit row are one write.** `audit.write_audit` runs on
  the same connection, between the mutation and the caller's commit, so I7
  holds by construction rather than by reviewer attention.

A task settling is also, sometimes, its run settling: the terminal transitions
below call `runs.rollup_run_status` in their own transaction, so there is never
a window in which every task of a run is finished but the run still reads
`running`.

Fencing is opt-in, and that is deliberate. `mark_running`, `complete` and
`fail` take a `claimed_by` token; passing the worker id that holds the claim
makes the transition apply only while that worker still holds it, and passing
`None` disables the check. Workers always pass their id -- the unfenced form
exists for administrative callers (a replay tool, a migration) that legitimately
act on a task no worker holds, and choosing it is a caller's explicit decision
rather than a default anyone can drift into.

SQL lives in `_sql` as static constants (I5).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from steward_schemas import ProblemDetails, RunBudget, TaskResult, TaskSpec, TaskStatus

from steward_queue import _sql
from steward_queue._rows import budget_from, budget_params, require_row
from steward_queue.audit import TASK_ENTITY, write_audit
from steward_queue.backoff import DEFAULT_BASE_DELAY, DEFAULT_FACTOR, DEFAULT_MAX_DELAY, retry_delay
from steward_queue.db import QueueConnection
from steward_queue.keys import digest
from steward_queue.models import (
    SYSTEM_ACTOR,
    Actor,
    ClaimedTask,
    TaskRecord,
    TaskState,
)
from steward_queue.runs import record_usage, rollup_run_status, start_run

DEFAULT_LEASE = timedelta(minutes=5)
"""How long a claim is honoured before `requeue_stale` may take it back."""


class TaskNotClaimable(RuntimeError):
    """A terminal transition was attempted on a task that no worker holds."""


def dedup_key_for(task_type: str, payload: Mapping[str, Any]) -> str:
    """The natural key that makes enqueue idempotent within a run.

    A task is identified by what it will do -- its type and its payload -- so
    re-planning a run, or retrying the transaction that enqueued it, converges
    on one row instead of a duplicate. Callers that genuinely want two
    identical-looking tasks in one run pass an explicit `dedup_key`.
    """
    return digest({"task_type": task_type, "payload": dict(payload)})


def enqueue(
    conn: QueueConnection,
    spec: TaskSpec,
    *,
    dedup_key: str | None = None,
    available_at: datetime | None = None,
    actor: Actor = SYSTEM_ACTOR,
) -> UUID:
    """Enqueue a task **inside the caller's transaction** (I8).

    Returns the id of the task now queued -- `spec.task_id` for a new row, or
    the id of the existing row when `dedup_key` already exists in the run. The
    caller commits; until it does, the task does not exist for any worker.

    On a dedup hit the queued task keeps the payload it was enqueued with and
    this spec's payload is discarded. With the derived key that is a tautology
    (the payload is what the key is computed from); with an explicit
    `dedup_key` it is the caller asserting "these are the same task", so
    passing one with a payload that differs in a way that matters is a caller
    bug this function cannot detect.
    """
    key = dedup_key if dedup_key is not None else dedup_key_for(spec.task_type, spec.payload)
    params: dict[str, Any] = {
        "id": spec.task_id,
        "run_id": spec.run_id,
        "task_type": spec.task_type,
        "payload": Jsonb(dict(spec.payload)),
        "max_attempts": spec.max_attempts,
        "dedup_key": key,
        "available_at": available_at,
        **budget_params(spec.budget),
    }
    inserted = conn.execute(_sql.INSERT_TASK, params).fetchone()
    if inserted is None:
        existing = conn.execute(
            _sql.SELECT_TASK_ID_BY_DEDUP, {"run_id": spec.run_id, "dedup_key": key}
        ).fetchone()
        deduped_id: UUID = require_row(existing, "dedup conflict without an existing row")[0]
        return deduped_id
    write_audit(
        conn,
        actor=actor,
        action="task.enqueued",
        entity_type=TASK_ENTITY,
        entity_id=str(spec.task_id),
        after={
            "state": TaskState.PENDING.value,
            "task_type": spec.task_type,
            "run_id": str(spec.run_id),
            "dedup_key": key,
        },
    )
    new_id: UUID = inserted[0]
    return new_id


def claim(
    conn: QueueConnection,
    *,
    worker_id: str,
    limit: int = 1,
    lease: timedelta = DEFAULT_LEASE,
    task_types: Sequence[str] | None = None,
    actor: Actor = SYSTEM_ACTOR,
) -> list[ClaimedTask]:
    """Claim up to `limit` due tasks with `SELECT ... FOR UPDATE SKIP LOCKED`.

    Rows locked by another in-flight claim are skipped, not waited on, so two
    workers running this concurrently get disjoint sets -- exactly-once
    claiming, at-least-once execution (SPEC.md §3.1, D2). The claim is visible
    to other workers only once the caller commits.
    """
    rows = conn.execute(
        _sql.CLAIM_TASKS,
        {
            "worker_id": worker_id,
            "limit": limit,
            "lease": lease,
            "task_types": list(task_types) if task_types is not None else None,
        },
    ).fetchall()
    claimed: list[ClaimedTask] = []
    for row in rows:
        spec = TaskSpec(
            task_id=row[0],
            run_id=row[1],
            task_type=row[2],
            payload=row[3],
            budget=budget_from(row[6], row[7], row[8], row[9]),
            max_attempts=row[5],
        )
        claimed.append(
            ClaimedTask(
                spec=spec,
                attempts=row[4],
                claimed_by=row[10],
                lease_expires_at=row[11],
                trace_id=row[12],
            )
        )
        write_audit(
            conn,
            actor=actor,
            action="task.claimed",
            entity_type=TASK_ENTITY,
            entity_id=str(spec.task_id),
            before={"state": TaskState.PENDING.value},
            after={
                "state": TaskState.CLAIMED.value,
                "claimed_by": worker_id,
                "attempts": row[4],
            },
        )
    return claimed


def mark_running(
    conn: QueueConnection,
    task_id: UUID,
    *,
    lease: timedelta = DEFAULT_LEASE,
    claimed_by: str | None = None,
    actor: Actor = SYSTEM_ACTOR,
) -> None:
    """Move a claimed task to `running` and extend its lease.

    The run it belongs to moves `pending -> running` in the same transaction,
    so a run is observably in flight from the moment its first task is.

    `claimed_by` is a fencing token (see `complete`): pass the worker id that
    claimed the task so a stalled worker cannot move a task a reaper has since
    handed to someone else.
    """
    row = conn.execute(
        _sql.MARK_RUNNING, {"id": task_id, "lease": lease, "claimed_by": claimed_by}
    ).fetchone()
    if row is None:
        raise TaskNotClaimable(f"task {task_id} is not claimed by {claimed_by or 'any worker'}")
    write_audit(
        conn,
        actor=actor,
        action="task.started",
        entity_type=TASK_ENTITY,
        entity_id=str(task_id),
        before={"state": TaskState.CLAIMED.value},
        after={"state": TaskState.RUNNING.value},
    )
    start_run(conn, row[1], actor=actor)


def complete(
    conn: QueueConnection,
    result: TaskResult,
    *,
    claimed_by: str | None = None,
    actor: Actor = SYSTEM_ACTOR,
) -> None:
    """Record a successful execution and roll its usage and outcome up onto the run.

    Called on the same connection the handler wrote through, so the handler's
    effects, the terminal state, the run's cost/token totals, the run's own
    terminal status and every audit row are one commit (I7, I12).

    `claimed_by` is a fencing token. Pass the id of the worker that claimed the
    task and the transition applies only while that worker still holds it; a
    worker whose lease expired mid-execution then gets `TaskNotClaimable`
    instead of silently overwriting the outcome of the worker that took over.
    """
    row = conn.execute(
        _sql.COMPLETE_TASK,
        {
            "id": result.task_id,
            "result": Jsonb(result.model_dump(mode="json")),
            "claimed_by": claimed_by,
        },
    ).fetchone()
    if row is None:
        raise TaskNotClaimable(f"task {result.task_id} is not held by {claimed_by or 'any worker'}")
    run_id: UUID = row[0]
    previous = TaskState(row[1])
    write_audit(
        conn,
        actor=actor,
        action="task.succeeded",
        entity_type=TASK_ENTITY,
        entity_id=str(result.task_id),
        before={"state": previous.value},
        after={"state": TaskState.SUCCEEDED.value},
    )
    record_usage(conn, run_id, result, actor=actor)
    rollup_run_status(conn, run_id, actor=actor)


def fail(
    conn: QueueConnection,
    task_id: UUID,
    error: ProblemDetails,
    *,
    usage: RunBudget | None = None,
    retryable: bool = True,
    base_delay: timedelta = DEFAULT_BASE_DELAY,
    factor: float = DEFAULT_FACTOR,
    max_delay: timedelta = DEFAULT_MAX_DELAY,
    claimed_by: str | None = None,
    actor: Actor = SYSTEM_ACTOR,
) -> TaskState:
    """Record a failed execution; reschedule it or dead-letter it.

    A retryable failure with attempts left returns the task to `pending` at
    `now() + retry_delay(attempts)`. The attempt that spends `max_attempts`
    goes to `dead`; a non-retryable failure goes straight to `failed`. Returns
    the state the task landed in. A terminal landing settles the run's status
    in the same transaction; a scheduled retry does not, because the run is
    still in flight.

    `claimed_by` is the same fencing token `complete` takes; the row is read
    `FOR UPDATE` first, so checking the holder here is as strong as checking it
    in the `UPDATE` predicate.
    """
    row = conn.execute(_sql.SELECT_TASK_ATTEMPTS_FOR_UPDATE, {"id": task_id}).fetchone()
    if row is None:
        raise LookupError(f"no such task: {task_id}")
    state, attempts, max_attempts, holder = TaskState(row[0]), row[1], row[2], row[3]
    if state not in (TaskState.CLAIMED, TaskState.RUNNING):
        raise TaskNotClaimable(f"task {task_id} is not claimed or running")
    if claimed_by is not None and holder != claimed_by:
        raise TaskNotClaimable(f"task {task_id} is held by {holder!r}, not {claimed_by!r}")

    error_json = Jsonb(error.model_dump(mode="json"))
    if retryable and attempts < max_attempts:
        delay = retry_delay(attempts, base=base_delay, factor=factor, cap=max_delay)
        outcome = conn.execute(_sql.RETRY_TASK, {"id": task_id, "delay": delay, "error": error_json})
        landed, action = TaskState.PENDING, "task.retry_scheduled"
        after: dict[str, Any] = {
            "state": landed.value,
            "attempts": attempts,
            "retry_in_seconds": delay.total_seconds(),
        }
    else:
        landed = TaskState.DEAD if retryable else TaskState.FAILED
        action = "task.dead" if retryable else "task.failed"
        outcome = conn.execute(
            _sql.TERMINATE_TASK, {"id": task_id, "state": landed.value, "error": error_json}
        )
        after = {"state": landed.value, "attempts": attempts}
    run_id: UUID = require_row(outcome.fetchone(), "task transition returned no row")[0]
    if usage is not None:
        record_usage(
            conn,
            run_id,
            TaskResult(task_id=task_id, status=TaskStatus.FAILED, usage=usage, error=error),
            actor=actor,
        )
    write_audit(
        conn,
        actor=actor,
        action=action,
        entity_type=TASK_ENTITY,
        entity_id=str(task_id),
        before={"state": state.value, "attempts": attempts},
        after=after | {"error": error.title},
    )
    if landed is not TaskState.PENDING:
        rollup_run_status(conn, run_id, actor=actor)
    return landed


def requeue_stale(
    conn: QueueConnection,
    *,
    actor: Actor = SYSTEM_ACTOR,
) -> list[tuple[UUID, TaskState]]:
    """Reclaim tasks whose lease expired -- the crash-recovery path (N1, H3).

    A worker that dies after claiming leaves a `claimed`/`running` row nobody
    is executing. Here it returns to `pending` (or to `dead` if its attempts
    are spent), so a crash costs a retry, never a task.

    Dead-lettering here is a terminal transition like any other, so the runs it
    finishes off are settled in the same transaction -- otherwise a run whose
    last task died with its worker would sit `running` forever, which is the
    one failure mode a status rollup exists to prevent. The affected runs are
    locked in id order so two reapers running concurrently cannot deadlock.
    """
    rows = conn.execute(_sql.REQUEUE_STALE).fetchall()
    recovered: list[tuple[UUID, TaskState]] = []
    finished: set[UUID] = set()
    for row in rows:
        task_id, run_id, state = row[0], row[1], TaskState(row[2])
        recovered.append((task_id, state))
        if state is TaskState.DEAD:
            finished.add(run_id)
        write_audit(
            conn,
            actor=actor,
            action="task.lease_expired",
            entity_type=TASK_ENTITY,
            entity_id=str(task_id),
            after={"state": state.value},
        )
    for run_id in sorted(finished):
        rollup_run_status(conn, run_id, actor=actor)
    return recovered


def get_task(conn: QueueConnection, task_id: UUID) -> TaskRecord | None:
    row = conn.execute(_sql.SELECT_TASK, {"id": task_id}).fetchone()
    if row is None:
        return None
    return TaskRecord(
        id=row[0],
        run_id=row[1],
        task_type=row[2],
        state=TaskState(row[3]),
        attempts=row[4],
        max_attempts=row[5],
        dedup_key=row[6],
        claimed_by=row[7],
        claimed_at=row[8],
        lease_expires_at=row[9],
        started_at=row[10],
        finished_at=row[11],
        available_at=row[12],
    )
