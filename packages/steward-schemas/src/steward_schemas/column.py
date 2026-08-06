"""Column — a per-asset column (SPEC.md §7)."""

from datetime import datetime
from uuid import UUID

from steward_schemas._base import SchemaModel


class Column(SchemaModel):
    """A column of an `Asset`. `data_type` is the source engine's own type
    name (e.g. "varchar", "numeric(10,2)") — engines disagree on type
    systems, so this is deliberately a string rather than a closed enum.
    """

    id: UUID
    workspace_id: UUID
    asset_id: UUID
    name: str
    data_type: str
    ordinal: int
    nullable: bool
    created_at: datetime
    updated_at: datetime
