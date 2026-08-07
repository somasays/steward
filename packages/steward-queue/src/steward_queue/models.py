"""Queue-local contracts.

`steward-schemas` owns what crosses the orchestrator/worker seam (`TaskSpec`,
`TaskResult`, `RunBudget`, `ProblemDetails`, `RunStatus`). This module owns the
bookkeeping vocabulary that only the queue has an opinion about: the SPEC.md
§3.1 task state machine, the audit actor, and the row projections the queue
hands back to callers.

`RunStatus` is re-exported rather than redefined: the API (`steward_schemas.run`)
and the queue must agree on what state a run is in, and two enums with the same
members are two things that can drift (I3).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from steward_schemas import RunBudget, RunStatus, TaskSpec

__all__ = [
    "SYSTEM_ACTOR",
    "Actor",
    "ActorKind",
    "ClaimedTask",
    "QueueModel",
    "RunRecord",
    "RunStatus",
    "TaskRecord",
    "TaskState",
]


class QueueModel(BaseModel):
    """Frozen, closed base for this package's row projections.

    Same discipline as `steward_schemas._base.SchemaModel`, restated here
    rather than imported: that base is another package's private module, and
    these models are queue-internal, not published contracts (I3/I4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskState(StrEnum):
    """Queue state machine (SPEC.md §3.1).

    `pending -> claimed -> running -> (succeeded | failed | dead)`. A retryable
    failure with attempts left returns the row to `pending` with a backoff
    rather than resting in `failed`: `failed` is where a non-retryable failure
    lands, `dead` is where a task goes once its attempts are spent.
    """

    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


class ActorKind(StrEnum):
    """Who caused a mutation (ARCHITECTURE.md §5 I7, SPEC.md §7 `audit_log`)."""

    HUMAN = "human"
    AGENT = "agent"
    POLICY = "policy"
    SYSTEM = "system"


class Actor(QueueModel):
    """The attributed cause of a mutation, recorded on every audit row."""

    kind: ActorKind
    id: str


SYSTEM_ACTOR = Actor(kind=ActorKind.SYSTEM, id="steward-queue")


class RunRecord(QueueModel):
    """A `runs` row -- the authoritative state of a run (I1).

    Budget and usage share `RunBudget`'s shape (I12): the cap a run was
    admitted under, and what it has consumed so far. `trace_id` is not optional
    because the column is not nullable: a run that cannot be traced back to a
    trace does not exist (I7). What the API publishes of this is
    `steward_schemas.RunResponse` -- a projection, so how the row is stored and
    what the contract promises can evolve separately (I3).
    """

    id: UUID
    goal: str
    payload: dict[str, Any]
    status: RunStatus
    budget: RunBudget
    usage: RunBudget
    trace_id: str
    idempotency_key: str | None
    created_at: datetime
    updated_at: datetime


class TaskRecord(QueueModel):
    """A `tasks` row, projected for observability and tests."""

    id: UUID
    run_id: UUID
    task_type: str
    state: TaskState
    attempts: int
    max_attempts: int
    dedup_key: str
    claimed_by: str | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    available_at: datetime


class ClaimedTask(QueueModel):
    """What `claim()` hands a worker: the typed spec, the claim facts the
    worker needs to honour its lease, and the run's trace id.

    `trace_id` rides along rather than being looked up because the worker needs
    it on every execution to put its task span on the run's trace (I7), and a
    per-task round trip for a value the claim already had in hand is a query
    the design does not need. It is deliberately not on `TaskSpec`: that is a
    published contract about *what to execute*, and where the execution is
    observed is not part of it.
    """

    spec: TaskSpec
    attempts: int
    claimed_by: str
    lease_expires_at: datetime
    trace_id: str
