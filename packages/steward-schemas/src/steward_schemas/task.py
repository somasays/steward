"""TaskSpec / TaskResult — the typed seam between the orchestrator and a
worker's bounded agent loop (SPEC.md §3.1, §3.2)."""

from enum import StrEnum
from typing import Any
from uuid import UUID

from steward_schemas._base import SchemaModel
from steward_schemas.budget import RunBudget
from steward_schemas.errors import ProblemDetails


class TaskStatus(StrEnum):
    """Terminal states of a task execution (SPEC.md §3.1 state machine:
    "pending -> claimed -> running -> (succeeded | failed | dead)"). Only the
    terminal outcomes of one execution attempt are represented here; queue
    bookkeeping (`pending`/`claimed`/`running`, `attempts`, `claimed_by`) is
    the task queue's concern, not this result contract's.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


class TaskSpec(SchemaModel):
    """What the orchestrator hands a worker to execute one bounded agent
    loop (SPEC.md §3.2). `payload` is intentionally opaque JSON: task types
    are open-ended (defined by each agent's task registry, M1+), and this
    contract is the generic queue seam they all share — the task-type-specific
    shape is validated by the handler, not here.
    """

    task_id: UUID
    run_id: UUID
    task_type: str
    payload: dict[str, Any]
    budget: RunBudget
    max_attempts: int


class TaskResult(SchemaModel):
    """The typed, terminal output of one task execution (SPEC.md §3.2:
    "a task's terminal output must validate against the task type's result
    schema"). `output` is generic JSON at this layer for the same reason as
    `TaskSpec.payload` — per-task-type result schemas (TableProfile, AssetDoc,
    ...) land with their agents in M1+.
    """

    task_id: UUID
    status: TaskStatus
    usage: RunBudget
    """Resources actually consumed — same shape as the budget it was run
    under (see `RunBudget`), so callers can compare usage to cap directly."""

    output: dict[str, Any] | None = None
    """Present when `status == SUCCEEDED`."""

    error: ProblemDetails | None = None
    """Present when `status != SUCCEEDED`, e.g. a `budget_exceeded` problem."""
