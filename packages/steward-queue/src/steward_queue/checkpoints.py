"""Agent state persisted between steps.

The caller owns the transaction here too: a checkpoint is written on the
handler's own connection, so it commits with the step that produced it or not
at all -- a checkpoint for work that was rolled back would be worse than none
(N1).

SQL lives in `_sql` as static constants (I5).
"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from steward_queue import _sql
from steward_queue.db import QueueConnection


class StaleClaim(RuntimeError):
    """This attempt no longer holds the task it is writing about.

    Raised by `guard_claim`, and the answer to it is always the same: stop. A
    handler whose task was reaped and re-claimed is working on behalf of nobody,
    and anything it writes -- a checkpoint a live attempt would resume from, a
    usage row charged to a run -- is worse than nothing.
    """


def guard_claim(conn: QueueConnection, task_id: UUID, *, claimed_by: str, attempts: int) -> None:
    """Refuse to write unless this attempt still holds this task.

    Takes the row `FOR UPDATE`, so the check and whatever the caller writes next
    are one atomic decision: a reaper cannot re-claim the task between them.
    Both halves of the fence matter -- `claimed_by` catches a different worker,
    and `attempts` catches *this* worker's later attempt at the same task, which
    shares the id (SPEC.md §13 D7).
    """
    row = conn.execute(_sql.SELECT_CLAIM_FOR_UPDATE, {"id": task_id}).fetchone()
    if row is None:
        raise StaleClaim(f"no such task: {task_id}")
    holder, current_attempt, state = row[0], row[1], row[2]
    if holder != claimed_by or current_attempt != attempts:
        raise StaleClaim(
            f"task {task_id} is attempt {current_attempt} held by {holder!r}, "
            f"not attempt {attempts} held by {claimed_by!r}"
        )
    if state not in ("claimed", "running"):
        raise StaleClaim(f"task {task_id} is {state}, so this attempt is over")


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


def latest_checkpoint(conn: QueueConnection, task_id: UUID) -> Mapping[str, Any] | None:
    """The most recent step's state for `task_id`, or None if it has none.

    The counterpart to `write_checkpoint`, and the reason resume is possible at
    all: a re-executed attempt reads the furthest step that committed rather
    than starting over. Only the latest is returned because that is what resume
    needs -- the earlier rows are the audit trail of how it got there.
    """
    row = conn.execute(_sql.SELECT_LATEST_CHECKPOINT, {"task_id": task_id}).fetchone()
    if row is None:
        return None
    state: Mapping[str, Any] = row[1]
    return state
