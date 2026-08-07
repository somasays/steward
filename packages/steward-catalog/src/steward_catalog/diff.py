"""What a scan should change — decided as a pure function.

`plan_convergence(current, observed)` takes the catalog as stored and the
catalog as observed and returns the writes that reconcile them. It touches no
connection, mints no identity and reads no clock, which buys three things:

* **Rescan convergence is testable without a database.** A second scan of an
  unchanged source produces an empty plan, and that is an assertion about a
  function, not about I/O timing (I8).
* **"No write" is representable.** An empty plan means the repository executes
  no statement, so no `updated_at` moves and no audit row appears. That is what
  makes "scanning twice leaves byte-identical state" true rather than
  approximately true -- an upsert that touched every row every scan would
  satisfy every other requirement here and still fail it.
* **A handler cannot read its own side effects to decide what to do**
  (GUARDRAILS.md §4): the decision is a function of two inputs that were both
  read before any write happened.

Lifecycle, not deletion. A table that vanished upstream becomes `missing` and
keeps its row and its id; if it comes back, the same row returns to `active`.
A row that is already `missing` and still absent produces no write at all --
which is why a dropped table stays exactly one row across any number of
rescans.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from steward_schemas import AssetLifecycle, AssetType

from steward_catalog.models import AssetRecord, ColumnRecord, DiscoveredAsset, DiscoveredColumn

__all__ = [
    "AssetKey",
    "CatalogState",
    "ConvergencePlan",
    "InsertAsset",
    "InsertColumn",
    "MarkAssetMissing",
    "MarkColumnMissing",
    "UpdateAsset",
    "UpdateColumn",
    "plan_convergence",
]

type AssetKey = tuple[str, str]
"""(schema, name) -- an asset's natural key within its source."""


@dataclass(frozen=True, slots=True)
class CatalogState:
    """The catalog as stored for one source: its assets, and their columns."""

    assets: dict[AssetKey, AssetRecord]
    columns: dict[UUID, dict[str, ColumnRecord]]


@dataclass(frozen=True, slots=True)
class InsertAsset:
    """An asset the catalog has never seen. Its columns are inserted with it,
    once the row it hangs them off has an id."""

    schema_name: str
    name: str
    asset_type: AssetType
    columns: tuple[DiscoveredColumn, ...]


@dataclass(frozen=True, slots=True)
class UpdateAsset:
    """An asset whose stored facts no longer match the source.

    Carries both sides because the audit row does: "what did this look like
    before" is the question an audit log exists to answer (I7).
    """

    asset_id: UUID
    schema_name: str
    name: str
    asset_type: AssetType
    lifecycle: AssetLifecycle
    before_asset_type: AssetType
    before_lifecycle: AssetLifecycle


@dataclass(frozen=True, slots=True)
class MarkAssetMissing:
    """An asset that is stored `active` and no longer exists upstream."""

    asset_id: UUID
    schema_name: str
    name: str


@dataclass(frozen=True, slots=True)
class InsertColumn:
    """A column on an asset that already exists."""

    asset_id: UUID
    column: DiscoveredColumn


@dataclass(frozen=True, slots=True)
class UpdateColumn:
    """A column whose type, position, nullability or lifecycle drifted."""

    column_id: UUID
    asset_id: UUID
    after: DiscoveredColumn
    lifecycle: AssetLifecycle
    before: DiscoveredColumn
    before_lifecycle: AssetLifecycle


@dataclass(frozen=True, slots=True)
class MarkColumnMissing:
    """A column that is stored `active` and no longer exists upstream."""

    column_id: UUID
    asset_id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class ConvergencePlan:
    """Every write a scan needs, and nothing else. Empty means "no change"."""

    insert_assets: tuple[InsertAsset, ...] = ()
    update_assets: tuple[UpdateAsset, ...] = ()
    missing_assets: tuple[MarkAssetMissing, ...] = ()
    insert_columns: tuple[InsertColumn, ...] = ()
    update_columns: tuple[UpdateColumn, ...] = ()
    missing_columns: tuple[MarkColumnMissing, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.insert_assets
            or self.update_assets
            or self.missing_assets
            or self.insert_columns
            or self.update_columns
            or self.missing_columns
        )


@dataclass(slots=True)
class _Accumulator:
    """Mutable scratch for the walk below; frozen into a `ConvergencePlan`."""

    insert_assets: list[InsertAsset]
    update_assets: list[UpdateAsset]
    missing_assets: list[MarkAssetMissing]
    insert_columns: list[InsertColumn]
    update_columns: list[UpdateColumn]
    missing_columns: list[MarkColumnMissing]


def _facts(column: ColumnRecord) -> DiscoveredColumn:
    return DiscoveredColumn(
        name=column.name,
        data_type=column.data_type,
        ordinal=column.ordinal,
        nullable=column.nullable,
    )


def _plan_columns(
    asset_id: UUID,
    stored: dict[str, ColumnRecord],
    observed: tuple[DiscoveredColumn, ...],
    plan: _Accumulator,
) -> None:
    seen: set[str] = set()
    for column in observed:
        seen.add(column.name)
        record = stored.get(column.name)
        if record is None:
            plan.insert_columns.append(InsertColumn(asset_id=asset_id, column=column))
        elif _facts(record) != column or record.lifecycle is not AssetLifecycle.ACTIVE:
            plan.update_columns.append(
                UpdateColumn(
                    column_id=record.id,
                    asset_id=asset_id,
                    after=column,
                    lifecycle=AssetLifecycle.ACTIVE,
                    before=_facts(record),
                    before_lifecycle=record.lifecycle,
                )
            )
    for name, record in sorted(stored.items()):
        if name not in seen and record.lifecycle is AssetLifecycle.ACTIVE:
            plan.missing_columns.append(MarkColumnMissing(column_id=record.id, asset_id=asset_id, name=name))


def plan_convergence(current: CatalogState, observed: tuple[DiscoveredAsset, ...]) -> ConvergencePlan:
    """The writes that make `current` equal `observed`, in a stable order.

    Stable order matters beyond tidiness: it is the order the audit rows land
    in, so two scans of the same change produce the same ledger. `observed`
    arrives sorted from the inspector; the missing-asset sweep is sorted here
    because a dict's iteration order is an artefact of insertion, not of the
    catalog.
    """
    plan = _Accumulator([], [], [], [], [], [])
    seen: set[AssetKey] = set()
    for asset in observed:
        key: AssetKey = (asset.schema_name, asset.name)
        seen.add(key)
        record = current.assets.get(key)
        if record is None:
            plan.insert_assets.append(
                InsertAsset(
                    schema_name=asset.schema_name,
                    name=asset.name,
                    asset_type=asset.asset_type,
                    columns=asset.columns,
                )
            )
            continue
        if record.asset_type is not asset.asset_type or record.lifecycle is not AssetLifecycle.ACTIVE:
            plan.update_assets.append(
                UpdateAsset(
                    asset_id=record.id,
                    schema_name=asset.schema_name,
                    name=asset.name,
                    asset_type=asset.asset_type,
                    lifecycle=AssetLifecycle.ACTIVE,
                    before_asset_type=record.asset_type,
                    before_lifecycle=record.lifecycle,
                )
            )
        _plan_columns(record.id, current.columns.get(record.id, {}), asset.columns, plan)

    for key in sorted(current.assets.keys() - seen):
        record = current.assets[key]
        if record.lifecycle is not AssetLifecycle.ACTIVE:
            continue  # already marked: a dropped table stays exactly one row, forever
        plan.missing_assets.append(MarkAssetMissing(asset_id=record.id, schema_name=key[0], name=key[1]))
        # Its columns went with it. Marking them in the same plan keeps the two
        # levels of the catalog telling one story, and costs nothing on the next
        # scan: they are already `missing`, so nothing is written again.
        _plan_columns(record.id, current.columns.get(record.id, {}), (), plan)

    return ConvergencePlan(
        insert_assets=tuple(plan.insert_assets),
        update_assets=tuple(plan.update_assets),
        missing_assets=tuple(plan.missing_assets),
        insert_columns=tuple(plan.insert_columns),
        update_columns=tuple(plan.update_columns),
        missing_columns=tuple(plan.missing_columns),
    )
