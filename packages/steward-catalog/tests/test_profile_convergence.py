"""I8 for profiling: profiling twice leaves byte-identical state (#49).

The same claim `test_convergence.py` makes for a scan, asserted the same way
and for the same reason: `profiles` is append-only, so a profile that wrote a
version per run would turn "what did this table look like in March" into
"which of these four hundred identical rows was March".

"Byte-identical" is scoped and visible: the `profiles` rows (id excluded, since
it is generated) and every audit row for a catalog entity. Runs and tasks are
excluded because profiling twice is two runs by definition.

Marked `invariants` so it runs in the Tier H sweep alongside H1, which
exercises the same handler with the registry's sample payload -- a payload that
names no catalogued asset. The success path is leashed here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from steward_catalog import (
    EnvSecretResolver,
    build_profile_asset,
    build_scan_source,
    postgres_inspector,
    postgres_profiler,
    register_source,
)
from steward_catalog.repository import CATALOG_ENTITIES
from steward_queue import SYSTEM_ACTOR, QueueConnection, TaskContext, UsageLedger
from steward_schemas import SourceCreate, TaskSpec, TaskStatus


def _ctx(conn: QueueConnection, spec: TaskSpec, attempts: int = 1) -> TaskContext:
    """A handler context for a test: a trace to hang spans on, and a fresh
    per-attempt usage ledger (`steward_queue.usage`)."""
    return TaskContext(
        connection=conn,
        spec=spec,
        attempts=attempts,
        claimed_by="w-test",
        trace_id="trace-test",
        usage=UsageLedger(),
    )


pytestmark = pytest.mark.invariants

SELECT_PROFILES = """
SELECT asset_id, version, digest, profile FROM profiles ORDER BY asset_id, version
"""
SELECT_CATALOG_AUDIT = """
SELECT actor_kind, action, entity_type, before, after
FROM audit_log WHERE entity_type = ANY (%(entities)s) ORDER BY id
"""
SELECT_ASSET_ID = """
SELECT id FROM assets WHERE schema_name = %(schema)s AND name = %(name)s
"""

ADD_CUSTOMER = "INSERT INTO sales.customers (id, email, card) VALUES (3, 'grace@example.com', NULL)"


def snapshot(conn: QueueConnection) -> dict[str, list[tuple[Any, ...]]]:
    """Everything profiling is allowed to have written, as comparable values."""
    state = {
        "profiles": conn.execute(SELECT_PROFILES).fetchall(),
        "audit": conn.execute(SELECT_CATALOG_AUDIT, {"entities": list(CATALOG_ENTITIES)}).fetchall(),
    }
    conn.rollback()
    return state


def profile(conn: QueueConnection, spec: TaskSpec, resolver: EnvSecretResolver) -> Any:
    """Run the real handler in the caller's transaction, as a worker would."""
    handler = build_profile_asset(resolver=resolver, profiler=postgres_profiler)
    result = asyncio.run(handler(_ctx(conn, spec, 1)))
    conn.commit()
    return result


def profile_spec(spec_factory: Callable[[UUID], TaskSpec], asset_id: UUID) -> TaskSpec:
    """A `profile_asset` spec on a committed run of the right task type."""
    spec = spec_factory(asset_id)
    return spec.model_copy(update={"task_type": "profile_asset", "payload": {"asset_id": str(asset_id)}})


@pytest.fixture
def catalogued(
    conn: QueueConnection,
    source_create: SourceCreate,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> UUID:
    """A registered, scanned source -- so there are assets to profile."""
    source, _ = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()
    handler = build_scan_source(resolver=resolver, inspect=postgres_inspector)
    ctx = _ctx(conn, spec_factory(source.id), 1)
    result = asyncio.run(handler(ctx))
    conn.commit()
    assert result.status is TaskStatus.SUCCEEDED, result.error
    return source.id


@pytest.fixture
def customers_id(conn: QueueConnection, catalogued: UUID) -> UUID:
    row = conn.execute(SELECT_ASSET_ID, {"schema": "sales", "name": "customers"}).fetchone()
    conn.rollback()
    assert row is not None
    asset_id: UUID = row[0]
    return asset_id


def test_profiling_persists_a_version_and_its_audit_row(
    conn: QueueConnection,
    customers_id: UUID,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    result = profile(conn, profile_spec(spec_factory, customers_id), resolver)

    assert result.status is TaskStatus.SUCCEEDED, result.error
    assert result.output == {
        "asset_id": str(customers_id),
        "columns": 3,
        "row_count": 4,
        "version": 1,
        "changed": True,
    }
    state = snapshot(conn)
    [(asset_id, version, _digest, stored)] = state["profiles"]
    assert (asset_id, version) == (customers_id, 1)
    assert [column["name"] for column in stored["columns"]] == ["id", "email", "card"]
    # I7: the profile row and its audit entry were written together.
    profile_audit = [row for row in state["audit"] if row[2] == "profile"]
    assert [row[1] for row in profile_audit] == ["profile.recorded"]
    assert profile_audit[0][4]["version"] == 1


def test_profiling_twice_with_no_change_is_byte_identical(
    conn: QueueConnection,
    customers_id: UUID,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    profile(conn, profile_spec(spec_factory, customers_id), resolver)
    before = snapshot(conn)

    result = profile(conn, profile_spec(spec_factory, customers_id), resolver)

    assert result.status is TaskStatus.SUCCEEDED
    assert result.output is not None and result.output["changed"] is False
    assert result.output["version"] == 1  # the version that already stood
    assert snapshot(conn) == before  # no second version, no audit churn


def test_changed_data_appends_a_version_and_keeps_the_old_one(
    conn: QueueConnection,
    customers_id: UUID,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
) -> None:
    profile(conn, profile_spec(spec_factory, customers_id), resolver)
    first = snapshot(conn)["profiles"]

    source_admin.execute(ADD_CUSTOMER)
    result = profile(conn, profile_spec(spec_factory, customers_id), resolver)

    assert result.output is not None and result.output == {
        "asset_id": str(customers_id),
        "columns": 3,
        "row_count": 5,
        "version": 2,
        "changed": True,
    }
    profiles = snapshot(conn)["profiles"]
    assert [row[1] for row in profiles] == [1, 2]
    assert profiles[0] == first[0]  # version 1 was not rewritten
    assert profiles[0][2] != profiles[1][2]  # ...and the digests differ


def test_an_uncatalogued_asset_fails_typed_and_writes_nothing(
    conn: QueueConnection,
    catalogued: UUID,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    before = snapshot(conn)

    result = profile(conn, profile_spec(spec_factory, uuid4()), resolver)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:unknown-asset"
    assert snapshot(conn) == before


def test_a_missing_asset_is_refused_before_a_connection_is_opened(
    conn: QueueConnection,
    catalogued: UUID,
    customers_id: UUID,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
) -> None:
    """A dropped table keeps its row with `lifecycle = missing` (issue #20).
    Profiling it would fail in the driver; failing here names the real cause."""
    source_admin.execute("DROP TABLE sales.customers CASCADE")
    scan = build_scan_source(resolver=resolver, inspect=postgres_inspector)
    ctx = _ctx(conn, spec_factory(catalogued), 1)
    asyncio.run(scan(ctx))
    conn.commit()

    result = profile(conn, profile_spec(spec_factory, customers_id), resolver)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:asset-not-active"
    assert snapshot(conn)["profiles"] == []


def test_a_table_that_vanished_since_the_last_scan_fails_without_leaking_the_driver(
    conn: QueueConnection,
    customers_id: UUID,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
) -> None:
    """The window the check above cannot close: dropped upstream, not yet
    rescanned. The failure is typed and its detail is ours -- the driver's
    message is logged with its SQLSTATE and never returned (N7)."""
    source_admin.execute("DROP TABLE sales.customers CASCADE")

    result = profile(conn, profile_spec(spec_factory, customers_id), resolver)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:asset-unprofilable"
    assert "sales.customers" not in (result.error.detail or "")
    assert snapshot(conn)["profiles"] == []
