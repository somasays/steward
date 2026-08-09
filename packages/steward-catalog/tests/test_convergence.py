"""I8 for the catalog: scanning twice leaves byte-identical state.

This is the M1 slice's central claim and it is asserted end to end over the
real handler, a real Steward database and a real source database -- not over
the diff function, which `test_diff.py` covers, and not over a mock.

"Byte-identical" is scoped, deliberately and visibly: the catalog rows
(`sources`, `assets`, `columns`) *including their `updated_at`*, plus every
audit row for a catalog entity. Runs and tasks are excluded because a second
scan is a second run by definition -- comparing them would be asserting that
executing something twice executes it once.

Marked `invariants` so it runs in the same Tier H sweep as H1. H1 itself
exercises this handler with the registry's sample payload, which names no
registered source; the success path is leashed here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest
from steward_catalog import (
    EnvSecretResolver,
    build_scan_source,
    postgres_inspector,
    register_source,
)
from steward_catalog.repository import CATALOG_ENTITIES
from steward_queue import SYSTEM_ACTOR, QueueConnection, TaskContext, UsageLedger
from steward_schemas import SourceCreate, TaskSpec, TaskStatus

pytestmark = pytest.mark.invariants

SELECT_ASSETS = """
SELECT id, workspace_id, source_id, schema_name, name, asset_type, lifecycle, created_at, updated_at
FROM assets ORDER BY schema_name, name
"""
SELECT_COLUMNS = """
SELECT id, asset_id, name, data_type, ordinal, nullable, lifecycle, created_at, updated_at
FROM columns ORDER BY asset_id, name
"""
SELECT_CATALOG_AUDIT = """
SELECT actor_kind, action, entity_type, entity_id, before, after
FROM audit_log WHERE entity_type = ANY (%(entities)s) ORDER BY id
"""
COUNT_ASSETS_NAMED = "SELECT count(*) FROM assets WHERE schema_name = %(schema)s AND name = %(name)s"
# Upstream changes the tests make. `CASCADE` takes `sales.recent_orders` with
# the table it selects from, which is why two assets go missing below.
DROP_ORDERS = "DROP TABLE sales.orders CASCADE"
RECREATE_ORDERS = "CREATE TABLE sales.orders (id bigint NOT NULL, customer text, total numeric(10,2))"
REGRANT_ORDERS = "GRANT SELECT ON sales.orders TO steward_reader"
SELECT_ORDERS_LIFECYCLE = """
SELECT id, lifecycle FROM assets WHERE schema_name = 'sales' AND name = 'orders'
"""


def snapshot(conn: QueueConnection) -> dict[str, list[tuple[Any, ...]]]:
    """Everything a scan is allowed to have written, as comparable values."""
    state = {
        "assets": conn.execute(SELECT_ASSETS).fetchall(),
        "columns": conn.execute(SELECT_COLUMNS).fetchall(),
        "audit": conn.execute(SELECT_CATALOG_AUDIT, {"entities": list(CATALOG_ENTITIES)}).fetchall(),
    }
    conn.rollback()
    return state


def scan(conn: QueueConnection, spec: TaskSpec, resolver: EnvSecretResolver) -> Any:
    """Run the real handler in the caller's transaction, as a worker would."""
    handler = build_scan_source(resolver=resolver, inspect=postgres_inspector)
    result = asyncio.run(handler(TaskContext(connection=conn, spec=spec, attempts=1, usage=UsageLedger())))
    conn.commit()
    return result


@pytest.fixture
def scanned(
    conn: QueueConnection,
    source_create: SourceCreate,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> UUID:
    """A registered source, scanned once."""
    source, created = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()
    assert created
    result = scan(conn, spec_factory(source.id), resolver)
    assert result.status is TaskStatus.SUCCEEDED, result.error
    return source.id


def test_a_scan_persists_the_estate_it_observed(conn: QueueConnection, scanned: UUID) -> None:
    state = snapshot(conn)

    assert [(row[3], row[4], row[5]) for row in state["assets"]] == [
        ("sales", "customers", "table"),
        ("sales", "orders", "table"),
        ("sales", "recent_orders", "view"),
        ("staging", "raw_events", "table"),
    ]
    assert {(row[2], row[3]) for row in state["columns"]} >= {
        ("id", "bigint"),
        ("email", "text"),
        ("total", "numeric(10,2)"),
    }
    # I7: the rows and their audit entries were written together.
    assert {row[1] for row in state["audit"]} == {
        "source.registered",
        "asset.discovered",
        "column.discovered",
    }


def test_scanning_twice_with_no_upstream_change_is_byte_identical(
    conn: QueueConnection,
    scanned: UUID,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    before = snapshot(conn)

    result = scan(conn, spec_factory(scanned), resolver)

    assert result.status is TaskStatus.SUCCEEDED
    assert result.output is not None and result.output["changed"] is False
    assert snapshot(conn) == before  # no new rows, no touched timestamps, no audit churn


def test_a_dropped_table_becomes_missing_and_stays_exactly_one_row(
    conn: QueueConnection,
    scanned: UUID,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
) -> None:
    source_admin.execute(DROP_ORDERS)

    scan(conn, spec_factory(scanned), resolver)
    after_drop = snapshot(conn)

    [(asset_id, lifecycle)] = conn.execute(SELECT_ORDERS_LIFECYCLE).fetchall()
    conn.rollback()
    assert lifecycle == "missing"
    assert conn.execute(COUNT_ASSETS_NAMED, {"schema": "sales", "name": "orders"}).fetchone() == (1,)
    conn.rollback()
    assert "asset.missing" in {row[1] for row in after_drop["audit"]}

    # ...and rescanning a source that is still missing the table writes nothing:
    # the row does not reappear, does not duplicate, and does not churn.
    scan(conn, spec_factory(scanned), resolver)
    assert snapshot(conn) == after_drop

    # ...and when it comes back it is the same row, returned to active.
    source_admin.execute(RECREATE_ORDERS)
    source_admin.execute(REGRANT_ORDERS)
    scan(conn, spec_factory(scanned), resolver)
    assert conn.execute(SELECT_ORDERS_LIFECYCLE).fetchall() == [(asset_id, "active")]
    conn.rollback()


def test_the_recreated_view_is_dropped_with_its_table_and_returns(
    conn: QueueConnection,
    scanned: UUID,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
) -> None:
    """`DROP TABLE ... CASCADE` takes the view with it: two assets go missing,
    and the second scan is still a no-op."""
    source_admin.execute(DROP_ORDERS)
    scan(conn, spec_factory(scanned), resolver)

    lifecycles = {(row[3], row[4]): row[6] for row in snapshot(conn)["assets"]}
    assert lifecycles[("sales", "orders")] == "missing"
    assert lifecycles[("sales", "recent_orders")] == "missing"
    assert lifecycles[("sales", "customers")] == "active"

    source_admin.execute(RECREATE_ORDERS)
    source_admin.execute(REGRANT_ORDERS)
    scan(conn, spec_factory(scanned), resolver)
