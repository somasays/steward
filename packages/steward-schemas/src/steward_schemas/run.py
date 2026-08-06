"""Run -- the resource `POST /v1/runs` creates and `GET /v1/runs/{id}`
returns (SPEC.md §7, §8; issue #4).

M0 ships the API skeleton only: creating a run here does not expand a task
DAG yet (the orchestrator/queue-backed store lands in issue #5) -- every run
is created straight into `PENDING`. `cost`/token totals and the Langfuse
trace id (SPEC.md §7's `runs` row) are added once a run actually executes;
this contract is deliberately narrow today and grows additively.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field

from steward_schemas._base import SchemaModel


class RunStatus(StrEnum):
    """Lifecycle of a `Run` (SPEC.md §7). The M0 in-memory store only ever
    produces `PENDING`; the remaining states are driven by the orchestrator
    once a run is actually executed (issue #5 onward)."""

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
    """An agent run record (SPEC.md §7: "runs -- agent run records (goal,
    status, cost, token totals, langfuse trace id)")."""

    id: UUID
    goal: str
    payload: dict[str, Any]
    status: RunStatus
    created_at: datetime
    updated_at: datetime
