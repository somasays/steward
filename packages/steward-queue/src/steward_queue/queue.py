"""The queue operations.

Two rules hold for every function here and are the reason the module exists:

* **The caller owns the transaction.** Nothing commits or rolls back. Enqueue
  is therefore transactional with whatever domain change motivated it (I8) --
  there is no code path that can publish a task for a state change that never
  committed, or lose one that did.
* **A state mutation and its audit row are one write.** `_audit` runs on the
  same connection, between the mutation and the caller's commit, so I7 holds
  by construction rather than by reviewer attention.

SQL lives in `_sql` as static constants (I5).
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from steward_schemas import ProblemDetails, RunBudget, TaskResult, TaskSpec

from steward_queue import _sql
from steward_queue.backoff import DEFAULT_BASE_DELAY, DEFAULT_FACTOR, DEFAULT_MAX_DELAY, retry_delay
from steward_queue.db import QueueConnection
from steward_queue.models import (
    SYSTEM_ACTOR,
    Actor,
    ClaimedTask,
    RunRecord,
    RunStatus,
    TaskRecord,
    TaskState,
)

DEFAULT_LEASE = timedelta(minutes=5)
"""How long a claim is honoured before `requeue_stale` may take it back."""

RUN_ENTITY = "run"
TASK_ENTITY = "task"


class TaskNotClaimable(RuntimeError):
    """A terminal transition was attempted on a task that no worker holds."""


def _require_row(row: Sequence[Any] | None, what: str) -> Sequence[Any]:
    """Narrow a `RETURNING` result that the statement guarantees exists."""
    if row is None:  # pragma: no cover -- unreachable unless the schema drifts
        raise RuntimeError(what)
    return row


def _audit(
    conn: QueueConnection,
    *,
    actor: Actor,
    action: str,
    entity_type: str,
    entity_id: str,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
) -> None:
    """Record a mutation. Always called on the mutation's own connection (I7)."""
    conn.execute(
        _sql.INSERT_AUDIT,
        {
            "actor_kind": actor.kind.value,
            "actor_id": actor.id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "before": Jsonb(dict(before)) if before is not None else None,
            "after": Jsonb(dict(after)) if after is not None else None,
        },
    )


def dedup_key_for(task_type: str, payload: Mapping[str, Any]) -> str:
    """The natural key that makes enqueue idempotent within a run.

    A task is identified by what it will do -- its type and its payload -- so
    re-planning a run, or retrying the transaction that enqueued it, converges
    on one row instead of a duplicate. Callers that genuinely want two
    identical-looking tasks in one run pass an explicit `dedup_key`.
    """
    canonical = json.dumps(
        {"task_type": task_type, "payload": dict(payload)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _budget_params(budget: RunBudget) -> dict[str, Any]:
    return {
        "budget_steps": budget.steps,
        "budget_tokens": budget.tokens,
        "budget_cost_usd": budget.cost_usd,
        "budget_wall_clock": budget.wall_clock,
    }


def _budget_from(steps: int, tokens: int, cost_usd: Decimal, wall_clock: timedelta) -> RunBudget:
    return RunBudget(steps=steps, tokens=tokens, cost_usd=cost_usd, wall_clock=wall_clock)


def _run_record(row: Sequence[Any]) -> RunRecord:
    return RunRecord(
        id=row[0],
        goal=row[1],
        status=RunStatus(row[2]),
        budget=_budget_from(row[3], row[4], row[5], row[6]),
        usage=_budget_from(row[7], row[8], row[9], row[10]),
        trace_id=row[11],
        created_at=row[12],
        updated_at=row[13],
    )


def create_run(
    conn: QueueConnection,
    *,
    goal: str,
    budget: RunBudget,
    run_id: UUID | None = None,
    trace_id: str | None = None,
    status: RunStatus = RunStatus.PENDING,
    actor: Actor = SYSTEM_ACTOR,
) -> RunRecord:
    """Create a run. `budget` is required, has no default, and is stored on the
    row: a run without hard caps cannot exist (I12)."""
    params: dict[str, Any] = {
        "id": run_id or uuid4(),
        "goal": goal,
        "status": status.value,
        "trace_id": trace_id,
        **_budget_params(budget),
    }
    row = conn.execute(_sql.INSERT_RUN, params).fetchone()
    record = _run_record(_require_row(row, "INSERT INTO runs returned no row"))
    _audit(
        conn,
        actor=actor,
        action="run.created",
        entity_type=RUN_ENTITY,
        entity_id=str(record.id),
        after={"status": record.status.value, "goal": record.goal},
    )
    return record


def get_run(conn: QueueConnection, run_id: UUID) -> RunRecord | None:
    row = conn.execute(_sql.SELECT_RUN, {"id": run_id}).fetchone()
    return _run_record(row) if row is not None else None


def set_run_status(
    conn: QueueConnection,
    run_id: UUID,
    status: RunStatus,
    *,
    actor: Actor = SYSTEM_ACTOR,
) -> None:
    before = get_run(conn, run_id)
    if before is None:
        raise LookupError(f"no such run: {run_id}")
    conn.execute(_sql.UPDATE_RUN_STATUS, {"id": run_id, "status": status.value})
    _audit(
        conn,
        actor=actor,
        action="run.status_changed",
        entity_type=RUN_ENTITY,
        entity_id=str(run_id),
        before={"status": before.status.value},
        after={"status": status.value},
    )


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
        **_budget_params(spec.budget),
    }
    inserted = conn.execute(_sql.INSERT_TASK, params).fetchone()
    if inserted is None:
        existing = conn.execute(
            _sql.SELECT_TASK_ID_BY_DEDUP, {"run_id": spec.run_id, "dedup_key": key}
        ).fetchone()
        deduped_id: UUID = _require_row(existing, "dedup conflict without an existing row")[0]
        return deduped_id
    _audit(
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
            budget=_budget_from(row[6], row[7], row[8], row[9]),
            max_attempts=row[5],
        )
        claimed.append(ClaimedTask(spec=spec, attempts=row[4], claimed_by=row[10], lease_expires_at=row[11]))
        _audit(
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
    actor: Actor = SYSTEM_ACTOR,
) -> None:
    """Move a claimed task to `running` and extend its lease."""
    row = conn.execute(_sql.MARK_RUNNING, {"id": task_id, "lease": lease}).fetchone()
    if row is None:
        raise TaskNotClaimable(f"task {task_id} is not in state claimed")
    _audit(
        conn,
        actor=actor,
        action="task.started",
        entity_type=TASK_ENTITY,
        entity_id=str(task_id),
        before={"state": TaskState.CLAIMED.value},
        after={"state": TaskState.RUNNING.value},
    )


def complete(
    conn: QueueConnection,
    result: TaskResult,
    *,
    actor: Actor = SYSTEM_ACTOR,
) -> None:
    """Record a successful execution and roll its usage up onto the run.

    Called on the same connection the handler wrote through, so the handler's
    effects, the terminal state, the run's cost/token totals and the audit row
    are one commit (I7, I12).
    """
    row = conn.execute(
        _sql.COMPLETE_TASK,
        {"id": result.task_id, "result": Jsonb(result.model_dump(mode="json"))},
    ).fetchone()
    if row is None:
        raise TaskNotClaimable(f"task {result.task_id} is not claimed or running")
    run_id: UUID = row[0]
    conn.execute(
        _sql.ADD_RUN_USAGE,
        {
            "id": run_id,
            "steps": result.usage.steps,
            "tokens": result.usage.tokens,
            "cost_usd": result.usage.cost_usd,
            "wall_clock": result.usage.wall_clock,
        },
    )
    _audit(
        conn,
        actor=actor,
        action="task.succeeded",
        entity_type=TASK_ENTITY,
        entity_id=str(result.task_id),
        before={"state": TaskState.RUNNING.value},
        after={"state": TaskState.SUCCEEDED.value},
    )


def fail(
    conn: QueueConnection,
    task_id: UUID,
    error: ProblemDetails,
    *,
    retryable: bool = True,
    base_delay: timedelta = DEFAULT_BASE_DELAY,
    factor: float = DEFAULT_FACTOR,
    max_delay: timedelta = DEFAULT_MAX_DELAY,
    actor: Actor = SYSTEM_ACTOR,
) -> TaskState:
    """Record a failed execution; reschedule it or dead-letter it.

    A retryable failure with attempts left returns the task to `pending` at
    `now() + retry_delay(attempts)`. The attempt that spends `max_attempts`
    goes to `dead`; a non-retryable failure goes straight to `failed`. Returns
    the state the task landed in.
    """
    row = conn.execute(_sql.SELECT_TASK_ATTEMPTS_FOR_UPDATE, {"id": task_id}).fetchone()
    if row is None:
        raise LookupError(f"no such task: {task_id}")
    state, attempts, max_attempts = TaskState(row[0]), row[1], row[2]
    if state not in (TaskState.CLAIMED, TaskState.RUNNING):
        raise TaskNotClaimable(f"task {task_id} is not claimed or running")

    error_json = Jsonb(error.model_dump(mode="json"))
    if retryable and attempts < max_attempts:
        delay = retry_delay(attempts, base=base_delay, factor=factor, cap=max_delay)
        conn.execute(_sql.RETRY_TASK, {"id": task_id, "delay": delay, "error": error_json})
        landed, action = TaskState.PENDING, "task.retry_scheduled"
        after: dict[str, Any] = {
            "state": landed.value,
            "attempts": attempts,
            "retry_in_seconds": delay.total_seconds(),
        }
    else:
        landed = TaskState.DEAD if retryable else TaskState.FAILED
        action = "task.dead" if retryable else "task.failed"
        conn.execute(_sql.TERMINATE_TASK, {"id": task_id, "state": landed.value, "error": error_json})
        after = {"state": landed.value, "attempts": attempts}
    _audit(
        conn,
        actor=actor,
        action=action,
        entity_type=TASK_ENTITY,
        entity_id=str(task_id),
        before={"state": state.value, "attempts": attempts},
        after=after | {"error": error.title},
    )
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
    """
    rows = conn.execute(_sql.REQUEUE_STALE).fetchall()
    recovered: list[tuple[UUID, TaskState]] = []
    for row in rows:
        task_id, state = row[0], TaskState(row[1])
        recovered.append((task_id, state))
        _audit(
            conn,
            actor=actor,
            action="task.lease_expired",
            entity_type=TASK_ENTITY,
            entity_id=str(task_id),
            after={"state": state.value},
        )
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


def write_checkpoint(
    conn: QueueConnection,
    task_id: UUID,
    *,
    step: int,
    state: Mapping[str, Any],
) -> None:
    """Persist agent state after a step (SPEC.md §3.2, N1).

    An upsert on `(task_id, step)`: re-executing a step overwrites its snapshot
    instead of accumulating one row per attempt. That is what lets a handler
    satisfy the registry's idempotence clause without reading its own writes.
    """
    conn.execute(_sql.UPSERT_CHECKPOINT, {"task_id": task_id, "step": step, "state": Jsonb(dict(state))})
