"""The run contracts: the command that starts a run, and the API's view of one
(SPEC.md §7, §8).

These are two different things and the distinction is load-bearing. A run's
authoritative state is its `runs` row, projected by `steward_queue.RunRecord` --
budget, usage, trace id, timestamps, the lot. `Run` is what the API *publishes*
of that state: a projection built from the record (`steward_api.store`), versioned
and compatibility-checked (S6) independently of how the row happens to be stored.
Letting one model be both would tie the published contract to the schema and
make every storage change a breaking API change (I3, N9).

`RunCreate` is the external command; `Run` is the external view. Nothing here
knows about tasks or transactions.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field

from steward_schemas._base import SchemaModel
from steward_schemas.budget import RunBudget


class RunStatus(StrEnum):
    """Lifecycle of a run (SPEC.md §7).

    A run is `PENDING` until a worker starts its first task, `RUNNING` while
    any task is in flight, and terminal once every task is terminal: `FAILED`
    if any task failed or dead-lettered, `SUCCEEDED` otherwise. `CANCELLED` is
    operator-driven and is never entered by the rollup.

    The queue's `tasks.state` machine is a different, finer thing (claimed,
    leases, attempts); these five are the run-level states the API publishes.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunCreate(SchemaModel):
    """`POST /v1/runs` request body: the goal to execute and its parameters.

    `goal` names a well-known goal (e.g. "scan_source", SPEC.md §3.1);
    validated by the orchestrator, not this generic API-facing contract --
    mirrors `TaskSpec.payload` (`steward_schemas.task`) for the same reason:
    per-goal parameter shapes land with the goals themselves.
    """

    goal: str
    payload: dict[str, Any] = Field(default_factory=dict)


class Run(SchemaModel):
    """The API's projection of a run (SPEC.md §8: "status, task tree, cost,
    trace link").

    `trace_id` is always populated by the server -- `runs.trace_id` is `NOT
    NULL` and generated whether or not a tracing backend is configured (I7) --
    and `budget`/`usage` likewise, so a caller can see how close a run is to
    its caps without a second request (I12). They are nullable *in the
    contract* because this model was published without them and S6 classifies
    a newly-required property as a breaking change; tightening them belongs
    with the contract-versioning mechanism, not with a field addition. The task
    tree lands with the orchestrator in M1.
    """

    id: UUID
    goal: str
    payload: dict[str, Any]
    status: RunStatus
    trace_id: str | None = None
    budget: RunBudget | None = None
    usage: RunBudget | None = None
    created_at: datetime
    updated_at: datetime
