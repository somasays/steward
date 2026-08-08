"""Catalog persistence: sources, assets, columns — and their audit rows.

Two rules hold for every function here, both inherited deliberately from
`steward_queue`:

* **The caller owns the transaction.** Nothing commits or rolls back. A scan is
  therefore all-or-nothing with the task that ran it: the worker's transaction
  carries the catalog writes, the task's terminal state and every audit row, so
  a scan that dies halfway leaves the catalog exactly as it was (I8).
* **A mutation and its audit row are one write.** `steward_queue.write_audit`
  runs on the same connection, between the mutation and the caller's commit
  (I7). The writer is imported rather than re-implemented: a second opinion
  about what an audit row looks like is the drift I7 exists to prevent.

Writes are applied from a `ConvergencePlan` computed *before* any of them, so a
handler never reads its own side effects to decide what to do next
(GUARDRAILS.md §4). The plan is empty when nothing upstream changed, and an
empty plan executes no statement -- which is what makes a rescan byte-identical
rather than merely equivalent.

SQL lives in `_sql` as static constants (I5).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from steward_queue import Actor, QueueConnection, write_audit
from steward_schemas import AssetLifecycle, AssetType, SourceCreate, SourceEngine

from steward_catalog import _sql
from steward_catalog.diff import (
    CatalogState,
    ConvergencePlan,
    InsertAsset,
    MarkAssetMissing,
    MarkColumnMissing,
    UpdateAsset,
    UpdateColumn,
)
from steward_catalog.models import (
    WORKSPACE_ID,
    AssetRecord,
    ColumnRecord,
    DiscoveredColumn,
    SchemaFilter,
    SourceKey,
    SourceRecord,
)
from steward_catalog.profiles import PROFILE_ENTITY

__all__ = [
    "ASSET_ENTITY",
    "CATALOG_ENTITIES",
    "COLUMN_ENTITY",
    "SOURCE_ENTITY",
    "apply_plan",
    "get_asset",
    "get_source",
    "list_asset_columns",
    "list_assets",
    "load_state",
    "register_source",
]

SOURCE_ENTITY = "source"
ASSET_ENTITY = "asset"
COLUMN_ENTITY = "column"

CATALOG_ENTITIES: tuple[str, ...] = (SOURCE_ENTITY, ASSET_ENTITY, COLUMN_ENTITY, PROFILE_ENTITY)
"""The `audit_log.entity_type` values catalog mutations write.

Named here so a test can ask "what did this scan record" without hardcoding
strings that the writer could drift away from. `profile` (issue #49) is
included by importing the constant from the module that writes it rather than
by restating it, so a harness sweeping catalog audit rows picks up a new entity
type when it appears, not when someone remembers this tuple.
"""


def _source_record(row: Sequence[Any]) -> SourceRecord:
    return SourceRecord(
        id=row[0],
        workspace_id=row[1],
        name=row[2],
        key=SourceKey(
            engine=SourceEngine(row[3]),
            host=row[4],
            database=row[5],
            schemas=SchemaFilter(include=tuple(row[6]), exclude=tuple(row[7])),
        ),
        dsn_secret_ref=row[8],
        scan_schedule=row[9],
        created_at=row[10],
        updated_at=row[11],
    )


def _asset_record(row: Sequence[Any]) -> AssetRecord:
    return AssetRecord(
        id=row[0],
        workspace_id=row[1],
        source_id=row[2],
        database=row[3],
        schema_name=row[4],
        name=row[5],
        asset_type=AssetType(row[6]),
        lifecycle=AssetLifecycle(row[7]),
        created_at=row[8],
        updated_at=row[9],
    )


def _column_record(row: Sequence[Any]) -> ColumnRecord:
    return ColumnRecord(
        id=row[0],
        workspace_id=row[1],
        asset_id=row[2],
        name=row[3],
        data_type=row[4],
        ordinal=row[5],
        nullable=row[6],
        lifecycle=AssetLifecycle(row[7]),
        created_at=row[8],
        updated_at=row[9],
    )


def _key_params(key: SourceKey) -> dict[str, Any]:
    return {
        "workspace_id": WORKSPACE_ID,
        "engine": key.engine.value,
        "host": key.host,
        "database_name": key.database,
        "include_schemas": list(key.schemas.include),
        "exclude_schemas": list(key.schemas.exclude),
    }


def register_source(
    conn: QueueConnection, create: SourceCreate, *, actor: Actor
) -> tuple[SourceRecord, bool]:
    """Register `create`, or return the source its natural key already names.

    Returns the record and whether this call created it. Idempotency is the
    unique index's, not this function's: a concurrent duplicate loses the
    `INSERT` and reads the winner's row back, so two simultaneous registrations
    of one database converge on one source rather than racing (I8).

    A second registration deliberately does **not** rewrite `name`, the secret
    reference or the schedule. They are properties of the source, and letting
    whichever caller re-posted last silently redefine them would make
    registration a stealth update. Changing them is a future `PATCH`, with its
    own audit row.
    """
    params = _key_params(SourceKey.of(create)) | {
        "id": uuid4(),
        "name": create.name,
        "dsn_secret_ref": create.dsn_secret_ref,
        "scan_schedule": create.scan_schedule,
    }
    row = conn.execute(_sql.INSERT_SOURCE, params).fetchone()
    if row is None:
        existing = conn.execute(_sql.SELECT_SOURCE_BY_KEY, params).fetchone()
        if existing is None:  # pragma: no cover -- unreachable unless the key drifts
            raise RuntimeError("source key conflicted without an existing row")
        return _source_record(existing), False
    record = _source_record(row)
    write_audit(
        conn,
        actor=actor,
        action="source.registered",
        entity_type=SOURCE_ENTITY,
        entity_id=str(record.id),
        after={
            "name": record.name,
            "engine": record.key.engine.value,
            "host": record.key.host,
            "database": record.key.database,
            "include_schemas": list(record.key.schemas.include),
            "exclude_schemas": list(record.key.schemas.exclude),
            "dsn_secret_ref": record.dsn_secret_ref,
        },
    )
    return record, True


def get_source(conn: QueueConnection, source_id: UUID) -> SourceRecord | None:
    row = conn.execute(_sql.SELECT_SOURCE, {"id": source_id}).fetchone()
    return _source_record(row) if row is not None else None


def load_state(conn: QueueConnection, source_id: UUID) -> CatalogState:
    """The catalog as stored for one source -- the diff's "before" side."""
    assets = [_asset_record(row) for row in conn.execute(_sql.SELECT_SOURCE_ASSETS, {"source_id": source_id})]
    columns: dict[UUID, dict[str, ColumnRecord]] = {asset.id: {} for asset in assets}
    for row in conn.execute(_sql.SELECT_SOURCE_COLUMNS, {"source_id": source_id}):
        column = _column_record(row)
        columns.setdefault(column.asset_id, {})[column.name] = column
    return CatalogState(assets={(a.schema_name, a.name): a for a in assets}, columns=columns)


def _column_facts(column: DiscoveredColumn) -> dict[str, Any]:
    return {
        "name": column.name,
        "data_type": column.data_type,
        "ordinal": column.ordinal,
        "nullable": column.nullable,
    }


def _insert_column(conn: QueueConnection, asset_id: UUID, column: DiscoveredColumn, *, actor: Actor) -> None:
    column_id = uuid4()
    conn.execute(
        _sql.INSERT_COLUMN,
        {"id": column_id, "workspace_id": WORKSPACE_ID, "asset_id": asset_id} | _column_facts(column),
    )
    write_audit(
        conn,
        actor=actor,
        action="column.discovered",
        entity_type=COLUMN_ENTITY,
        entity_id=str(column_id),
        after=_column_facts(column) | {"asset_id": str(asset_id), "lifecycle": AssetLifecycle.ACTIVE.value},
    )


def _apply_insert_asset(conn: QueueConnection, source_id: UUID, op: InsertAsset, *, actor: Actor) -> None:
    asset_id = uuid4()
    conn.execute(
        _sql.INSERT_ASSET,
        {
            "id": asset_id,
            "workspace_id": WORKSPACE_ID,
            "source_id": source_id,
            "schema_name": op.schema_name,
            "name": op.name,
            "asset_type": op.asset_type.value,
        },
    )
    write_audit(
        conn,
        actor=actor,
        action="asset.discovered",
        entity_type=ASSET_ENTITY,
        entity_id=str(asset_id),
        after={
            "source_id": str(source_id),
            "schema_name": op.schema_name,
            "name": op.name,
            "asset_type": op.asset_type.value,
            "lifecycle": AssetLifecycle.ACTIVE.value,
        },
    )
    for column in op.columns:
        _insert_column(conn, asset_id, column, actor=actor)


def _apply_update_asset(conn: QueueConnection, op: UpdateAsset, *, actor: Actor) -> None:
    conn.execute(
        _sql.UPDATE_ASSET,
        {"id": op.asset_id, "asset_type": op.asset_type.value, "lifecycle": op.lifecycle.value},
    )
    write_audit(
        conn,
        actor=actor,
        action="asset.changed",
        entity_type=ASSET_ENTITY,
        entity_id=str(op.asset_id),
        before={"asset_type": op.before_asset_type.value, "lifecycle": op.before_lifecycle.value},
        after={"asset_type": op.asset_type.value, "lifecycle": op.lifecycle.value},
    )


def _apply_missing_asset(conn: QueueConnection, op: MarkAssetMissing, *, actor: Actor) -> None:
    conn.execute(_sql.MARK_ASSET_MISSING, {"id": op.asset_id})
    write_audit(
        conn,
        actor=actor,
        action="asset.missing",
        entity_type=ASSET_ENTITY,
        entity_id=str(op.asset_id),
        before={"lifecycle": AssetLifecycle.ACTIVE.value},
        after={
            "lifecycle": AssetLifecycle.MISSING.value,
            "schema_name": op.schema_name,
            "name": op.name,
        },
    )


def _apply_update_column(conn: QueueConnection, op: UpdateColumn, *, actor: Actor) -> None:
    conn.execute(
        _sql.UPDATE_COLUMN,
        {"id": op.column_id, "lifecycle": op.lifecycle.value} | _column_facts(op.after),
    )
    write_audit(
        conn,
        actor=actor,
        action="column.changed",
        entity_type=COLUMN_ENTITY,
        entity_id=str(op.column_id),
        before=_column_facts(op.before) | {"lifecycle": op.before_lifecycle.value},
        after=_column_facts(op.after) | {"lifecycle": op.lifecycle.value},
    )


def _apply_missing_column(conn: QueueConnection, op: MarkColumnMissing, *, actor: Actor) -> None:
    conn.execute(_sql.MARK_COLUMN_MISSING, {"id": op.column_id})
    write_audit(
        conn,
        actor=actor,
        action="column.missing",
        entity_type=COLUMN_ENTITY,
        entity_id=str(op.column_id),
        before={"lifecycle": AssetLifecycle.ACTIVE.value},
        after={"lifecycle": AssetLifecycle.MISSING.value, "asset_id": str(op.asset_id), "name": op.name},
    )


def apply_plan(conn: QueueConnection, source_id: UUID, plan: ConvergencePlan, *, actor: Actor) -> None:
    """Execute a plan inside the caller's transaction. An empty plan writes
    nothing at all -- not a row, not a timestamp, not an audit entry."""
    for insert_asset in plan.insert_assets:
        _apply_insert_asset(conn, source_id, insert_asset, actor=actor)
    for update_asset in plan.update_assets:
        _apply_update_asset(conn, update_asset, actor=actor)
    for missing_asset in plan.missing_assets:
        _apply_missing_asset(conn, missing_asset, actor=actor)
    for insert_column in plan.insert_columns:
        _insert_column(conn, insert_column.asset_id, insert_column.column, actor=actor)
    for update_column in plan.update_columns:
        _apply_update_column(conn, update_column, actor=actor)
    for missing_column in plan.missing_columns:
        _apply_missing_column(conn, missing_column, actor=actor)


def list_assets(
    conn: QueueConnection,
    *,
    source_id: UUID | None = None,
    after: tuple[str, str, UUID] | None = None,
    limit: int,
) -> list[AssetRecord]:
    """One page of assets in `(schema, name, id)` order, after `after`."""
    rows = conn.execute(
        _sql.SELECT_ASSETS_PAGE,
        {
            "source_id": source_id,
            "after_schema": after[0] if after is not None else None,
            "after_name": after[1] if after is not None else None,
            "after_id": after[2] if after is not None else None,
            "limit": limit,
        },
    ).fetchall()
    return [_asset_record(row) for row in rows]


def get_asset(conn: QueueConnection, asset_id: UUID) -> AssetRecord | None:
    row = conn.execute(_sql.SELECT_ASSET, {"id": asset_id}).fetchone()
    return _asset_record(row) if row is not None else None


def list_asset_columns(conn: QueueConnection, asset_id: UUID) -> list[ColumnRecord]:
    rows = conn.execute(_sql.SELECT_ASSET_COLUMNS, {"asset_id": asset_id}).fetchall()
    return [_column_record(row) for row in rows]
