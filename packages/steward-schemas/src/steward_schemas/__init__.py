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
from steward_schemas.catalog import AssetDetail, AssetPage
from steward_schemas.column import Column
from steward_schemas.errors import ProblemDetails
from steward_schemas.profile import (
    ColumnProfile,
    MaskedSample,
    SemanticType,
    TableProfile,
    ValueFrequency,
)
from steward_schemas.run import Run, RunCreate, RunStatus
from steward_schemas.source import (
    DEFAULT_EXCLUDED_SCHEMAS,
    SECRET_REF_PATTERN,
    Source,
    SourceCreate,
    SourceEngine,
)
from steward_schemas.task import TaskResult, TaskSpec, TaskStatus

__all__ = [
    "CONTRACTS",
    "DEFAULT_EXCLUDED_SCHEMAS",
    "SECRET_REF_PATTERN",
    "AgentSpec",
    "Asset",
    "AssetDetail",
    "AssetLifecycle",
    "AssetPage",
    "AssetType",
    "Column",
    "ColumnProfile",
    "MaskedSample",
    "ProblemDetails",
    "RunBudget",
    "RunCreate",
    "Run",
    "RunStatus",
    "SemanticType",
    "Source",
    "SourceCreate",
    "SourceEngine",
    "TableProfile",
    "TaskResult",
    "TaskSpec",
    "TaskStatus",
    "ValueFrequency",
]

CONTRACTS: dict[str, type[BaseModel]] = {
    "source": Source,
    "source_create": SourceCreate,
    "asset": Asset,
    "asset_detail": AssetDetail,
    "asset_page": AssetPage,
    "column": Column,
    "masked_sample": MaskedSample,
    "value_frequency": ValueFrequency,
    "column_profile": ColumnProfile,
    "table_profile": TableProfile,
    "task_spec": TaskSpec,
    "task_result": TaskResult,
    "run_budget": RunBudget,
    "agent_spec": AgentSpec,
    "problem_details": ProblemDetails,
    "run_create": RunCreate,
    "run": Run,
}
