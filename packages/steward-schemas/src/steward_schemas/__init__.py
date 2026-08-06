"""steward-schemas: Pydantic contracts shared across Steward packages and services.

Pydantic and the standard library only (I4) — this package is importable by
everything and depends on nothing steward-owned.

`CONTRACTS` names every published contract (I3): a versioned, typed model
that crosses a package/service/API boundary. S6 (contract compatibility,
issue #7) snapshots each entry's JSON Schema and fails the build on a
breaking change.
"""

from pydantic import BaseModel

from steward_schemas.agent import AgentSpec
from steward_schemas.asset import Asset, AssetLifecycle, AssetType
from steward_schemas.budget import RunBudget
from steward_schemas.column import Column
from steward_schemas.errors import ProblemDetails
from steward_schemas.source import Source, SourceEngine
from steward_schemas.task import TaskResult, TaskSpec, TaskStatus

__all__ = [
    "CONTRACTS",
    "AgentSpec",
    "Asset",
    "AssetLifecycle",
    "AssetType",
    "Column",
    "ProblemDetails",
    "RunBudget",
    "Source",
    "SourceEngine",
    "TaskResult",
    "TaskSpec",
    "TaskStatus",
]

CONTRACTS: dict[str, type[BaseModel]] = {
    "source": Source,
    "asset": Asset,
    "column": Column,
    "task_spec": TaskSpec,
    "task_result": TaskResult,
    "run_budget": RunBudget,
    "agent_spec": AgentSpec,
    "problem_details": ProblemDetails,
}
