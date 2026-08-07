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
