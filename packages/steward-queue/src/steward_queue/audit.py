"""The audit write every mutation makes.

A state mutation and its audit row are one write. `write_audit` runs on the
mutation's own connection, between the mutation and the caller's commit, so I7
holds by construction rather than by reviewer attention -- there is no audit
row that can survive a rolled-back mutation, and no mutation that can commit
without one.

It is public because `audit_log` is the whole system's ledger, not this
package's: the catalog (issue #20) mutates `sources`/`assets`/`columns` in the
same database and must write the same rows the same way. A second
implementation of this INSERT elsewhere would be a second opinion about what an
audit row is, which is exactly the drift I7 exists to prevent -- so the writer
is shared and the caller supplies the entity vocabulary.

SQL lives in `_sql` as static constants (I5).
"""

from collections.abc import Mapping
from typing import Any

from psycopg.types.json import Jsonb

from steward_queue import _sql
from steward_queue.db import QueueConnection
from steward_queue.models import Actor

RUN_ENTITY = "run"
TASK_ENTITY = "task"


def write_audit(
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
