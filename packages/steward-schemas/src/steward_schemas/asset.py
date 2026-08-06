"""Asset — a table or view discovered in a source (SPEC.md §7)."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from steward_schemas._base import SchemaModel


class AssetType(StrEnum):
    TABLE = "table"
    VIEW = "view"


class AssetLifecycle(StrEnum):
    """SPEC.md §7: "lifecycle: active|missing|deprecated"."""

    ACTIVE = "active"
    MISSING = "missing"
    DEPRECATED = "deprecated"


class Asset(SchemaModel):
    """A table or view discovered by scanning a `Source`."""

    id: UUID
    workspace_id: UUID
    source_id: UUID
    fqn: str
    """Fully qualified name, e.g. "analytics.public.orders"."""

    asset_type: AssetType
    lifecycle: AssetLifecycle
    created_at: datetime
    updated_at: datetime
