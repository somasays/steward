"""Registration, schema filtering and the assets listing, against a real
database -- the properties here are Postgres properties (unique indexes,
keyset ordering) and a fake would assert our beliefs about them."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from steward_catalog import (
    EnvSecretResolver,
    build_scan_source,
    decode_cursor,
    encode_cursor,
    get_asset,
    get_source,
    list_asset_columns,
    list_assets,
    postgres_inspector,
    register_source,
)
from steward_queue import SYSTEM_ACTOR, QueueConnection, TaskContext, UsageLedger
from steward_schemas import AssetType, SourceCreate, SourceEngine, TaskSpec, TaskStatus

COUNT_SOURCES = "SELECT count(*) FROM sources"
SELECT_SOURCE_AUDIT = "SELECT action, after FROM audit_log WHERE entity_type = 'source' ORDER BY id"

PAGE_SIZE = 2


def register(conn: QueueConnection, create: SourceCreate) -> tuple[UUID, bool]:
    record, created = register_source(conn, create, actor=SYSTEM_ACTOR)
    conn.commit()
    return record.id, created


def run_scan(conn: QueueConnection, spec: TaskSpec, resolver: EnvSecretResolver) -> None:
    handler = build_scan_source(resolver=resolver, inspect=postgres_inspector)
    result = asyncio.run(handler(TaskContext(connection=conn, spec=spec, attempts=1, usage=UsageLedger())))
    conn.commit()
    assert result.status is TaskStatus.SUCCEEDED, result.error


def test_registering_the_same_source_twice_creates_one_row(
    conn: QueueConnection, source_create: SourceCreate
) -> None:
    first_id, first_created = register(conn, source_create)
    second_id, second_created = register(conn, source_create)

    assert (first_created, second_created) == (True, False)
    assert first_id == second_id
    assert conn.execute(COUNT_SOURCES).fetchone() == (1,)
    conn.rollback()


def test_a_replayed_registration_writes_no_second_audit_row(
    conn: QueueConnection, source_create: SourceCreate
) -> None:
    # Nothing was created, so nothing is recorded -- the same rule
    # `steward_queue.create_run` follows on an idempotency replay.
    register(conn, source_create)
    register(conn, source_create)

    rows = conn.execute(SELECT_SOURCE_AUDIT).fetchall()
    conn.rollback()
    assert [row[0] for row in rows] == ["source.registered"]
    assert rows[0][1]["dsn_secret_ref"] == "env:STEWARD_TEST_SOURCE_DSN"  # a reference, not a DSN


def test_a_different_name_does_not_make_a_different_source(
    conn: QueueConnection, source_create: SourceCreate
) -> None:
    """The key is engine + host + database + schema set. A rename is not a new
    catalog, and the first registration's name is kept."""
    first_id, _ = register(conn, source_create)
    second_id, created = register(conn, source_create.model_copy(update={"name": "renamed"}))

    assert (second_id, created) == (first_id, False)
    record = get_source(conn, first_id)
    assert record is not None and record.name == source_create.name


def test_the_schema_filter_is_part_of_the_key(conn: QueueConnection, source_create: SourceCreate) -> None:
    narrower = source_create.model_copy(update={"include_schemas": ("sales",)})

    first_id, _ = register(conn, source_create)
    second_id, created = register(conn, narrower)

    assert created and second_id != first_id


def test_the_key_ignores_the_order_schemas_were_listed_in(
    conn: QueueConnection, source_create: SourceCreate
) -> None:
    reordered = source_create.model_copy(update={"include_schemas": ("staging", "sales", "sales")})

    first_id, _ = register(conn, source_create)
    second_id, created = register(conn, reordered)

    assert (second_id, created) == (first_id, False)


def test_a_source_with_no_allowlist_scans_everything_not_excluded(
    conn: QueueConnection,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    """Denylist mode, which is also the answer to "what happens to a schema
    created after registration": it is scanned, because the filter names what
    to skip rather than what to keep."""
    denylist = SourceCreate(
        name="everything",
        engine=SourceEngine.POSTGRES,
        host="fixture.internal",
        database="fixture_source",
        dsn_secret_ref="env:STEWARD_TEST_SOURCE_DSN",
    )
    source_id, _ = register(conn, denylist)

    run_scan(conn, spec_factory(source_id), resolver)

    schemas = {asset.schema_name for asset in list_assets(conn, source_id=source_id, limit=100)}
    conn.rollback()
    assert schemas == {"sales", "staging"}  # public is empty; the engine's own schemas are excluded


def test_an_allowlist_is_closed(
    conn: QueueConnection,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    only_sales = SourceCreate(
        name="sales-only",
        engine=SourceEngine.POSTGRES,
        host="fixture.internal",
        database="fixture_source",
        dsn_secret_ref="env:STEWARD_TEST_SOURCE_DSN",
        include_schemas=("sales",),
    )
    source_id, _ = register(conn, only_sales)

    run_scan(conn, spec_factory(source_id), resolver)

    schemas = {asset.schema_name for asset in list_assets(conn, source_id=source_id, limit=100)}
    conn.rollback()
    assert schemas == {"sales"}


@pytest.fixture
def catalogued(
    conn: QueueConnection,
    source_create: SourceCreate,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> UUID:
    source_id, _ = register(conn, source_create)
    run_scan(conn, spec_factory(source_id), resolver)
    return source_id


def test_the_listing_pages_through_every_asset_exactly_once(conn: QueueConnection, catalogued: UUID) -> None:
    seen: list[str] = []
    cursor: tuple[str, str, UUID] | None = None
    for _ in range(10):  # bounded, so a broken cursor fails instead of hanging
        page = list_assets(conn, source_id=catalogued, after=cursor, limit=PAGE_SIZE)
        seen.extend(f"{asset.schema_name}.{asset.name}" for asset in page)
        if len(page) < PAGE_SIZE:
            break
        last = page[-1]
        cursor = decode_cursor(encode_cursor(last.schema_name, last.name, last.id))
    conn.rollback()

    assert seen == [
        "sales.customers",
        "sales.orders",
        "sales.recent_orders",
        "staging.raw_events",
    ]


def test_an_asset_carries_its_columns_and_a_fully_qualified_name(
    conn: QueueConnection, catalogued: UUID
) -> None:
    [orders] = [
        asset for asset in list_assets(conn, source_id=catalogued, limit=100) if asset.name == "orders"
    ]

    assert orders.fqn == "fixture_source.sales.orders"
    assert orders.asset_type is AssetType.TABLE
    columns = list_asset_columns(conn, orders.id)
    conn.rollback()
    # Ordinal order, which is the source's own order -- not alphabetical.
    assert [(c.name, c.data_type, c.ordinal, c.nullable) for c in columns] == [
        ("id", "bigint", 1, False),
        ("customer", "text", 2, True),
        ("total", "numeric(10,2)", 3, True),
    ]


def test_an_asset_that_was_never_scanned_is_not_found(conn: QueueConnection) -> None:
    assert get_asset(conn, uuid4()) is None
    assert get_source(conn, uuid4()) is None
    conn.rollback()
