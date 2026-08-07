"""Catalog-local contracts: what a scan observes, and what the rows look like.

`steward-schemas` owns what crosses the API boundary (`Source`, `Asset`,
`Column`, `SourceCreate`, `AssetPage`, `AssetDetail`). This module owns the two
vocabularies only the catalog has an opinion about:

* **Observations** (`DiscoveredAsset`, `DiscoveredColumn`) — what an inspector
  read out of a customer database. Deliberately identity-free: an observation
  is a fact about the upstream schema, not a row, and giving it an id would
  invite a scanner to invent one.
* **Records** (`SourceRecord`, `AssetRecord`, `ColumnRecord`) — projections of
  the rows Steward stores, the same relationship `steward_queue.RunRecord` has
  to `steward_schemas.Run`. The API projects records onto contracts, so storage
  can change without that being an API change (I3, N9).

`SchemaFilter` sits between the two: it is the part of a source's natural key
that says *which* subset of a database this registration describes, canonical
(sorted, deduplicated) so that two registrations naming the same schemas in a
different order are the same source.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from steward_schemas import AssetLifecycle, AssetType, SourceCreate, SourceEngine

__all__ = [
    "WORKSPACE_ID",
    "AssetRecord",
    "CatalogModel",
    "ColumnRecord",
    "DiscoveredAsset",
    "DiscoveredColumn",
    "SchemaFilter",
    "SourceKey",
    "SourceRecord",
]

WORKSPACE_ID = UUID(int=0)
"""The single workspace this deployment has (SPEC.md §1: "not multi-tenant
(v1)").

Every root entity carries `workspace_id` from the first migration so tenancy is
a lookup change rather than a schema rewrite; until there is an API that can
create a second workspace, inventing one per source would be inventing an
identifier nothing can resolve. A named constant makes the assumption greppable
on the day it stops being true.
"""


class CatalogModel(BaseModel):
    """Frozen, closed base for this package's models.

    Same discipline as `steward_schemas._base.SchemaModel`, restated rather
    than imported for the same reason `steward_queue.models.QueueModel` restates
    it: that base is another package's private module, and these are
    package-internal models, not published contracts (I3/I4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class SchemaFilter(CatalogModel):
    """Which schemas of a database a source covers.

    Empty `include` means "everything not excluded", so schemas created
    upstream after registration are picked up by the next scan. A non-empty
    `include` is a closed allowlist and new schemas are never scanned until it
    changes -- asking for an allowlist is asking for that.

    Both tuples are canonical (`of()` sorts and deduplicates) because the pair
    is part of the source's natural key: `["sales", "public"]` and
    `["public", "sales", "public"]` describe one source, and a key that
    disagreed would let the same database be registered twice.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @staticmethod
    def of(include: tuple[str, ...], exclude: tuple[str, ...]) -> SchemaFilter:
        """A canonical filter from whatever order a client sent."""
        return SchemaFilter(include=tuple(sorted(set(include))), exclude=tuple(sorted(set(exclude))))


class SourceKey(CatalogModel):
    """A source's natural key (issue #20): engine + host + database + schema set.

    Registration is idempotent on this, not on `name`: renaming a registration
    must not create a second source, and two names for one database subset
    would be two catalogs of the same tables.
    """

    engine: SourceEngine
    host: str
    database: str
    schemas: SchemaFilter

    @staticmethod
    def of(create: SourceCreate) -> SourceKey:
        """The key a `POST /v1/sources` body identifies."""
        return SourceKey(
            engine=create.engine,
            host=create.host,
            database=create.database,
            schemas=SchemaFilter.of(create.include_schemas, create.exclude_schemas),
        )


class DiscoveredColumn(CatalogModel):
    """A column as the source reports it, with no Steward identity attached."""

    name: str
    data_type: str
    ordinal: int
    nullable: bool


class DiscoveredAsset(CatalogModel):
    """A table or view as the source reports it, with its columns.

    `columns` is ordered by the source's own ordinals, and the whole
    observation is comparable by value -- which is what lets the diff decide
    "nothing changed" without consulting the database twice.
    """

    schema_name: str
    name: str
    asset_type: AssetType
    columns: tuple[DiscoveredColumn, ...] = ()


class SourceRecord(CatalogModel):
    """A `sources` row. Holds a secret *reference*; never a credential (N7)."""

    id: UUID
    workspace_id: UUID
    name: str
    key: SourceKey
    dsn_secret_ref: str
    scan_schedule: str | None
    created_at: datetime
    updated_at: datetime


class AssetRecord(CatalogModel):
    """An `assets` row, plus the source facts the published `fqn` needs."""

    id: UUID
    workspace_id: UUID
    source_id: UUID
    database: str
    schema_name: str
    name: str
    asset_type: AssetType
    lifecycle: AssetLifecycle
    created_at: datetime
    updated_at: datetime

    @property
    def fqn(self) -> str:
        """`database.schema.name` -- the shape `steward_schemas.Asset` documents."""
        return f"{self.database}.{self.schema_name}.{self.name}"


class ColumnRecord(CatalogModel):
    """A `columns` row."""

    id: UUID
    workspace_id: UUID
    asset_id: UUID
    name: str
    data_type: str
    ordinal: int
    nullable: bool
    lifecycle: AssetLifecycle
    created_at: datetime
    updated_at: datetime
