"""The queue operations.

Two rules hold for every function here and are the reason the module exists:

* **The caller owns the transaction.** Nothing commits or rolls back. Enqueue
  is therefore transactional with whatever domain change motivated it (I8) --
  there is no code path that can publish a task for a state change that never
  committed, or lose one that did.
* **A state mutation and its audit row are one write.** `_audit` runs on the
  same connection, between the mutation and the caller's commit, so I7 holds
  by construction rather than by reviewer attention.

Fencing is opt-in, and that is deliberate. `mark_running`, `complete` and
`fail` take a `claimed_by` token; passing the worker id that holds the claim
makes the transition apply only while that worker still holds it, and passing
`None` disables the check. Workers always pass their id -- the unfenced form
exists for administrative callers (a replay tool, a migration) that legitimately
act on a task no worker holds, and choosing it is a caller's explicit decision
rather than a default anyone can drift into.

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
from steward_telemetry import new_trace_id

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
        payload=row[2],
        status=RunStatus(row[3]),
        budget=_budget_from(row[4], row[5], row[6], row[7]),
        usage=_budget_from(row[8], row[9], row[10], row[11]),
        trace_id=row[12],
        idempotency_key=row[13],
        created_at=row[14],
        updated_at=row[15],
    )


def create_run(
    conn: QueueConnection,
    *,
    goal: str,
    budget: RunBudget,
    payload: Mapping[str, Any] | None = None,
    run_id: UUID | None = None,
    trace_id: str | None = None,
    idempotency_key: str | None = None,
    status: RunStatus = RunStatus.PENDING,
    actor: Actor = SYSTEM_ACTOR,
) -> RunRecord:
    """Create a run, or return the one an earlier call with the same
    `idempotency_key` created.

    `budget` is required, has no default, and is stored on the row: a run
    without hard caps cannot exist (I12). `trace_id` defaults to one derived
    from the run id, so an untraced run is likewise unrepresentable (I7) --
    deriving rather than randomising means a retried creation transaction lands
    on the same trace instead of scattering the run across two.

    With an `idempotency_key`, a replay returns the existing record unchanged
    and writes no audit row: nothing was created, so nothing is recorded. The
    caller can tell the two apart by comparing `run_id` to the returned id.
    """
    identifier = run_id or uuid4()
    params: dict[str, Any] = {
        "id": identifier,
        "goal": goal,
        "payload": Jsonb(dict(payload or {})),
        "status": status.value,
        "trace_id": trace_id if trace_id is not None else new_trace_id(seed=str(identifier)),
        "idempotency_key": idempotency_key,
        **_budget_params(budget),
    }
    row = conn.execute(_sql.INSERT_RUN, params).fetchone()
    if row is None:
        existing = conn.execute(
            _sql.SELECT_RUN_BY_IDEMPOTENCY_KEY, {"idempotency_key": idempotency_key}
        ).fetchone()
        return _run_record(_require_row(existing, "idempotency conflict without an existing row"))
    record = _run_record(row)
    _audit(
        conn,
        actor=actor,
        action="run.created",
        entity_type=RUN_ENTITY,
        entity_id=str(record.id),
        after={"status": record.status.value, "goal": record.goal, "trace_id": record.trace_id},
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
    row = conn.execute(_sql.UPDATE_RUN_STATUS, {"id": run_id, "status": status.value}).fetchone()
    if row is None:
        raise LookupError(f"no such run: {run_id}")
    _audit(
        conn,
        actor=actor,
        action="run.status_changed",
        entity_type=RUN_ENTITY,
        entity_id=str(run_id),
        before={"status": row[0]},
        after={"status": status.value},
    )


def _record_run_status(
    conn: QueueConnection,
    run_id: UUID,
    before: str,
    after: str,
    *,
    actor: Actor,
) -> None:
    _audit(
        conn,
        actor=actor,
        action="run.status_changed",
        entity_type=RUN_ENTITY,
        entity_id=str(run_id),
        before={"status": before},
        after={"status": after},
    )


def start_run(conn: QueueConnection, run_id: UUID, *, actor: Actor = SYSTEM_ACTOR) -> bool:
    """Move a `pending` run to `running`. Returns whether it moved.

    Called when a task of the run starts, so "running" means what an operator
    expects it to mean -- work is in flight -- rather than being a state the
    orchestrator sets hopefully at creation time. Idempotent by predicate: the
    second and later tasks of a run find it already running and do nothing.
    """
    row = conn.execute(_sql.START_RUN, {"id": run_id}).fetchone()
    if row is None:
        return False
    _record_run_status(conn, run_id, RunStatus.PENDING.value, RunStatus.RUNNING.value, actor=actor)
    return True


def rollup_run_status(
    conn: QueueConnection, run_id: UUID, *, actor: Actor = SYSTEM_ACTOR
) -> RunStatus | None:
    """Settle a run's status once none of its tasks are outstanding.

    Returns the status the run landed in, or None if it is still in flight or
    was already terminal.

    This lives here, called from the terminal task transitions, rather than in
    a sweeper: the run reaching its terminal state *is* part of the task
    reaching its terminal state, and running both in one transaction means
    there is no window in which every task is finished but the run still reads
    `running` -- no interval for a client to poll into, and no reconciliation
    job whose failure would strand runs (I7, I8). The cost is one locking
    statement per terminal task; the alternative is an eventually-consistent
    run status, which is the wrong tradeoff for the resource the API publishes.

    The lock is taken in its own statement, before the one that counts tasks,
    and that ordering is the whole correctness argument -- see `_sql.LOCK_RUN`.
    Callers must therefore have already written their own task's terminal state
    in this transaction, or they are voting with a row nobody else can see.
    """
    conn.execute(_sql.LOCK_RUN, {"id": run_id})
    row = conn.execute(_sql.ROLLUP_RUN, {"id": run_id}).fetchone()
    if row is None:
        return None
    landed = RunStatus(row[1])
    _record_run_status(conn, run_id, row[0], landed.value, actor=actor)
    return landed


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
        claimed.append(
            ClaimedTask(
                spec=spec,
                attempts=row[4],
                claimed_by=row[10],
                lease_expires_at=row[11],
                trace_id=row[12],
            )
        )
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
    _audit(
        conn,
        actor=actor,
        action="task.started",
        entity_type=TASK_ENTITY,
        entity_id=str(task_id),
        before={"state": TaskState.CLAIMED.value},
        after={"state": TaskState.RUNNING.value},
    )
    start_run(conn, row[1], actor=actor)


def _usage_fields(budget: RunBudget) -> dict[str, Any]:
    """JSON-safe rendering of a `RunBudget` for an audit payload."""
    return {
        "steps": budget.steps,
        "tokens": budget.tokens,
        "cost_usd": str(budget.cost_usd),
        "wall_clock_seconds": budget.wall_clock.total_seconds(),
    }


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
    _audit(
        conn,
        actor=actor,
        action="task.succeeded",
        entity_type=TASK_ENTITY,
        entity_id=str(result.task_id),
        before={"state": previous.value},
        after={"state": TaskState.SUCCEEDED.value},
    )
    _record_usage(conn, run_id, result, actor=actor)
    rollup_run_status(conn, run_id, actor=actor)


def _record_usage(conn: QueueConnection, run_id: UUID, result: TaskResult, *, actor: Actor) -> None:
    """Add a task's usage to its run's totals, audited on the run entity (I7).

    The run's spend is a mutation in its own right -- a reviewer asking "how
    did this run reach its cap" needs a row per increment, not one row about
    the task that caused it.
    """
    row = conn.execute(
        _sql.ADD_RUN_USAGE,
        {
            "id": run_id,
            "steps": result.usage.steps,
            "tokens": result.usage.tokens,
            "cost_usd": result.usage.cost_usd,
            "wall_clock": result.usage.wall_clock,
        },
    ).fetchone()
    before = _require_row(row, "run usage update returned no row")
    _audit(
        conn,
        actor=actor,
        action="run.usage_recorded",
        entity_type=RUN_ENTITY,
        entity_id=str(run_id),
        before=_usage_fields(_budget_from(before[0], before[1], before[2], before[3])),
        after=_usage_fields(_budget_from(before[4], before[5], before[6], before[7]))
        | {"task_id": str(result.task_id)},
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
    run_id: UUID = _require_row(outcome.fetchone(), "task transition returned no row")[0]
    _audit(
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
        _audit(
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
