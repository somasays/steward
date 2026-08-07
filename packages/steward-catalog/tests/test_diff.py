"""Convergence, decided without a database.

`plan_convergence` is the whole of the rescan guarantee: if it returns an empty
plan the repository executes no statement, so nothing can move. These tests
pin every branch of that decision at the level where it is a function of two
values -- the database-backed proof that the function is wired up correctly is
`test_convergence.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from steward_catalog import CatalogState, DiscoveredAsset, DiscoveredColumn, plan_convergence
from steward_catalog.models import AssetRecord, ColumnRecord
from steward_schemas import AssetLifecycle, AssetType

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
SOURCE_ID = UUID("22222222-2222-2222-2222-222222222222")
WORKSPACE = UUID(int=0)

ID_COLUMN = DiscoveredColumn(name="id", data_type="bigint", ordinal=1, nullable=False)
EMAIL_COLUMN = DiscoveredColumn(name="email", data_type="text", ordinal=2, nullable=True)


def observed_orders(*columns: DiscoveredColumn) -> DiscoveredAsset:
    return DiscoveredAsset(schema_name="sales", name="orders", asset_type=AssetType.TABLE, columns=columns)


def stored_asset(
    asset_id: UUID,
    *,
    asset_type: AssetType = AssetType.TABLE,
    lifecycle: AssetLifecycle = AssetLifecycle.ACTIVE,
) -> AssetRecord:
    return AssetRecord(
        id=asset_id,
        workspace_id=WORKSPACE,
        source_id=SOURCE_ID,
        database="analytics",
        schema_name="sales",
        name="orders",
        asset_type=asset_type,
        lifecycle=lifecycle,
        created_at=NOW,
        updated_at=NOW,
    )


def stored_column(
    column_id: UUID,
    asset_id: UUID,
    facts: DiscoveredColumn,
    *,
    lifecycle: AssetLifecycle = AssetLifecycle.ACTIVE,
) -> ColumnRecord:
    return ColumnRecord(
        id=column_id,
        workspace_id=WORKSPACE,
        asset_id=asset_id,
        name=facts.name,
        data_type=facts.data_type,
        ordinal=facts.ordinal,
        nullable=facts.nullable,
        lifecycle=lifecycle,
        created_at=NOW,
        updated_at=NOW,
    )


def state_with(asset: AssetRecord, *columns: ColumnRecord) -> CatalogState:
    return CatalogState(
        assets={(asset.schema_name, asset.name): asset},
        columns={asset.id: {c.name: c for c in columns}},
    )


EMPTY = CatalogState(assets={}, columns={})


def test_an_empty_catalog_inserts_everything_it_observes() -> None:
    plan = plan_convergence(EMPTY, (observed_orders(ID_COLUMN, EMAIL_COLUMN),))

    [insert] = plan.insert_assets
    assert (insert.schema_name, insert.name, insert.asset_type) == ("sales", "orders", AssetType.TABLE)
    assert insert.columns == (ID_COLUMN, EMAIL_COLUMN)
    # Columns of a brand-new asset ride with it: there is no id to hang them off
    # until the asset row exists.
    assert plan.insert_columns == ()
    assert not plan.is_empty()


def test_rescanning_an_unchanged_catalog_plans_nothing() -> None:
    """The whole convergence guarantee, at the level it is decided."""
    asset_id = uuid4()
    state = state_with(
        stored_asset(asset_id),
        stored_column(uuid4(), asset_id, ID_COLUMN),
        stored_column(uuid4(), asset_id, EMAIL_COLUMN),
    )

    plan = plan_convergence(state, (observed_orders(ID_COLUMN, EMAIL_COLUMN),))

    assert plan.is_empty()


def test_a_new_column_on_a_known_asset_is_inserted_alone() -> None:
    asset_id = uuid4()
    state = state_with(stored_asset(asset_id), stored_column(uuid4(), asset_id, ID_COLUMN))

    plan = plan_convergence(state, (observed_orders(ID_COLUMN, EMAIL_COLUMN),))

    assert plan.insert_assets == ()
    [insert] = plan.insert_columns
    assert (insert.asset_id, insert.column) == (asset_id, EMAIL_COLUMN)


def test_a_retyped_column_is_updated_and_carries_its_before_state() -> None:
    asset_id, column_id = uuid4(), uuid4()
    state = state_with(stored_asset(asset_id), stored_column(column_id, asset_id, ID_COLUMN))
    widened = DiscoveredColumn(name="id", data_type="numeric", ordinal=1, nullable=False)

    plan = plan_convergence(state, (observed_orders(widened),))

    [update] = plan.update_columns
    assert update.column_id == column_id
    assert update.before == ID_COLUMN  # I7: the audit row needs both sides
    assert update.after == widened


def test_a_table_that_became_a_view_is_updated_not_replaced() -> None:
    asset_id = uuid4()
    state = state_with(stored_asset(asset_id), stored_column(uuid4(), asset_id, ID_COLUMN))
    as_view = DiscoveredAsset(
        schema_name="sales", name="orders", asset_type=AssetType.VIEW, columns=(ID_COLUMN,)
    )

    plan = plan_convergence(state, (as_view,))

    [update] = plan.update_assets
    assert (update.asset_id, update.before_asset_type, update.asset_type) == (
        asset_id,
        AssetType.TABLE,
        AssetType.VIEW,
    )
    assert plan.insert_assets == ()


def test_a_dropped_table_is_marked_missing_with_its_columns() -> None:
    asset_id, column_id = uuid4(), uuid4()
    state = state_with(stored_asset(asset_id), stored_column(column_id, asset_id, ID_COLUMN))

    plan = plan_convergence(state, ())

    assert [op.asset_id for op in plan.missing_assets] == [asset_id]
    assert [op.column_id for op in plan.missing_columns] == [column_id]


def test_a_table_that_is_already_missing_is_not_touched_again() -> None:
    """Why a dropped table stays exactly one row across any number of rescans:
    the second scan has nothing to write, so nothing moves -- not the row, not
    `updated_at`, not the audit log."""
    asset_id = uuid4()
    state = state_with(
        stored_asset(asset_id, lifecycle=AssetLifecycle.MISSING),
        stored_column(uuid4(), asset_id, ID_COLUMN, lifecycle=AssetLifecycle.MISSING),
    )

    assert plan_convergence(state, ()).is_empty()


def test_a_table_that_came_back_returns_to_active_on_the_same_row() -> None:
    asset_id, column_id = uuid4(), uuid4()
    state = state_with(
        stored_asset(asset_id, lifecycle=AssetLifecycle.MISSING),
        stored_column(column_id, asset_id, ID_COLUMN, lifecycle=AssetLifecycle.MISSING),
    )

    plan = plan_convergence(state, (observed_orders(ID_COLUMN),))

    assert plan.insert_assets == ()  # not a new row
    [update] = plan.update_assets
    assert (update.asset_id, update.lifecycle) == (asset_id, AssetLifecycle.ACTIVE)
    [column_update] = plan.update_columns
    assert (column_update.column_id, column_update.lifecycle) == (column_id, AssetLifecycle.ACTIVE)


def test_missing_assets_are_planned_in_a_stable_order() -> None:
    """Audit rows land in plan order, so two scans of the same change have to
    produce the same ledger -- dict insertion order would not guarantee that."""
    first, second = uuid4(), uuid4()
    a = stored_asset(first)
    b = stored_asset(second)
    state = CatalogState(
        assets={
            ("sales", "zeta"): a.model_copy(update={"name": "zeta"}),
            ("sales", "alpha"): b.model_copy(update={"name": "alpha"}),
        },
        columns={},
    )

    plan = plan_convergence(state, ())

    assert [op.name for op in plan.missing_assets] == ["alpha", "zeta"]
