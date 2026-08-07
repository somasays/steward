"""The run aggregate: creation, status, and spend.

**The caller owns the transaction.** Nothing here commits or rolls back, so a
run's status change, its usage totals, and the audit rows that record them
belong to whichever transaction the caller opened -- in practice the same one
as the task transition that caused them (I7, I8).

Run status follows the run's tasks and nothing else moves it, except an
operator cancelling: `start_run` when the first task starts, `rollup_run_status`
when the last one settles. Both are called from the task transitions in
`tasks`, inside their transaction.

SQL lives in `_sql` as static constants (I5).
"""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from steward_schemas import RunBudget, TaskResult
from steward_telemetry import new_trace_id

from steward_queue import _sql
from steward_queue._rows import _budget_from, _budget_params, _require_row
from steward_queue.audit import RUN_ENTITY, write_audit
from steward_queue.db import QueueConnection
from steward_queue.keys import digest
from steward_queue.models import SYSTEM_ACTOR, Actor, RunRecord, RunStatus


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
    write_audit(
        conn,
        actor=actor,
        action="run.created",
        entity_type=RUN_ENTITY,
        entity_id=str(record.id),
        after={"status": record.status.value, "goal": record.goal, "trace_id": record.trace_id},
    )
    return record


def claim_single_flight(conn: QueueConnection, *, goal: str, payload: Mapping[str, Any]) -> RunRecord | None:
    """The run already in flight for this exact goal and payload, or None --
    and, either way, the right to decide, until the caller commits.

    An advisory lock on the hashed (goal, payload) is taken first and released
    when the caller's transaction ends. That is what makes "a scan already in
    flight returns that run" (SPEC.md §8) true under concurrency rather than
    only under a leisurely test: without it two simultaneous requests both read
    "nothing in flight" and both create a run, and the endpoint's idempotency
    would hold only when nobody was in a hurry.

    Generic on purpose. The queue knows goals by name and payload by value and
    nothing else -- it must not learn what a goal *means* (I4) -- so callers
    with a narrower notion of "the same request" pass the payload they mean.
    """
    conn.execute(_sql.LOCK_RUN_ADMISSION, {"key": digest({"goal": goal, "payload": dict(payload)})})
    row = conn.execute(_sql.SELECT_IN_FLIGHT_RUN, {"goal": goal, "payload": Jsonb(dict(payload))}).fetchone()
    return _run_record(row) if row is not None else None


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
    write_audit(
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
    write_audit(
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


def _usage_fields(budget: RunBudget) -> dict[str, Any]:
    """JSON-safe rendering of a `RunBudget` for an audit payload."""
    return {
        "steps": budget.steps,
        "tokens": budget.tokens,
        "cost_usd": str(budget.cost_usd),
        "wall_clock_seconds": budget.wall_clock.total_seconds(),
    }


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
    write_audit(
        conn,
        actor=actor,
        action="run.usage_recorded",
        entity_type=RUN_ENTITY,
        entity_id=str(run_id),
        before=_usage_fields(_budget_from(before[0], before[1], before[2], before[3])),
        after=_usage_fields(_budget_from(before[4], before[5], before[6], before[7]))
        | {"task_id": str(result.task_id)},
    )
