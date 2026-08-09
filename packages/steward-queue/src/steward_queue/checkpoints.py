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
