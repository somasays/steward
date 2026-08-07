"""H11 — M1 slice 1's exit criterion, executable (GUARDRAILS.md Tier H, issue #20).

    "register a Postgres source, scan it, and read the assets and columns back
     over the API — with the source connection proven read-only, no credential
     readable anywhere, and a rescan that changes nothing"

as one scenario over the real components: the real FastAPI app, the real
Postgres-backed stores, the real migrations, the real worker loop, and a real
*second* database standing in for a customer's, reachable only through a role
that holds `SELECT`. Nothing is stubbed.

Once shipped, an acceptance scenario runs forever (GUARDRAILS.md H11), so this
is also the regression leash on every claim the slice makes.

    uv run pytest -q -m acceptance
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import pgserver
import psycopg
import pytest
from fastapi.testclient import TestClient
from steward_api.app import create_app
from steward_api.catalog import PostgresCatalogStore
from steward_api.store import PostgresRunStore
from steward_queue import Worker, connect, upgrade_to_head
from steward_queue.db import QueueConnection

pytestmark = pytest.mark.acceptance

POLL_INTERVAL = timedelta(milliseconds=50)
POLL_TIMEOUT = timedelta(seconds=30)
TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled"}
PAGE_SIZE = 2

SOURCE_DATABASE = "acceptance_source"
READER_ROLE = "acceptance_reader"
SECRET_ENV = "STEWARD_ACCEPTANCE_SOURCE_DSN"

INSUFFICIENT_PRIVILEGE = "42501"

FIXTURE_ESTATE: tuple[str, ...] = (
    "CREATE SCHEMA sales",
    "CREATE TABLE sales.orders (id bigint NOT NULL, customer text, total numeric(10,2))",
    "CREATE TABLE sales.customers (id bigint NOT NULL, email text NOT NULL)",
    "CREATE TABLE sales.refunds (id bigint NOT NULL, order_id bigint NOT NULL)",
    "CREATE VIEW sales.recent_orders AS SELECT id, total FROM sales.orders",
)

GRANT_READER: tuple[str, ...] = (
    "GRANT USAGE ON SCHEMA sales TO acceptance_reader",
    "GRANT SELECT ON ALL TABLES IN SCHEMA sales TO acceptance_reader",
    "REVOKE ALL ON SCHEMA public FROM PUBLIC",
)

DROP_REFUNDS = "DROP TABLE sales.refunds"
A_WRITE = "INSERT INTO sales.orders (id, customer, total) VALUES (1, 'nobody', 1.00)"

# What the scan is allowed to have written, read back for the convergence
# comparison. Runs and tasks are excluded on purpose: a second scan is a second
# run by definition, and comparing them would assert that running something
# twice runs it once.
SELECT_CATALOG_STATE = """
SELECT a.schema_name, a.name, a.asset_type, a.lifecycle, a.created_at, a.updated_at,
       c.name, c.data_type, c.ordinal, c.nullable, c.lifecycle, c.updated_at
FROM assets AS a
LEFT JOIN columns AS c ON c.asset_id = a.id
ORDER BY a.schema_name, a.name, c.ordinal
"""
SELECT_CATALOG_AUDIT = """
SELECT action, entity_type, before, after FROM audit_log
WHERE entity_type IN ('source', 'asset', 'column') ORDER BY id
"""
COUNT_REFUNDS_ROWS = "SELECT count(*) FROM assets WHERE name = 'refunds'"
SELECT_SOURCE_ROW = "SELECT * FROM sources"

SOURCE_BODY: dict[str, Any] = {
    "name": "acceptance-warehouse",
    "engine": "postgres",
    "host": "acceptance.internal",
    "database": SOURCE_DATABASE,
    "dsn_secret_ref": f"env:{SECRET_ENV}",
    "include_schemas": ["sales"],
}


@pytest.fixture(scope="session")
def server() -> Iterator[pgserver.PostgresServer]:
    with tempfile.TemporaryDirectory(prefix="steward-m1") as data_dir:
        instance = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            yield instance
        finally:
            instance.cleanup()


@pytest.fixture(scope="session")
def dsn(server: pgserver.PostgresServer) -> str:
    """Steward's own database, migrated by the queue's own migrations."""
    uri: str = server.get_uri()
    upgrade_to_head(uri)
    return uri


@pytest.fixture(scope="session")
def source_admin_dsn(server: pgserver.PostgresServer) -> str:
    """The customer database, and a role that may only read it (I5)."""
    server.psql("DROP DATABASE IF EXISTS acceptance_source")
    server.psql("DROP ROLE IF EXISTS acceptance_reader")
    server.psql("CREATE DATABASE acceptance_source")
    server.psql("CREATE ROLE acceptance_reader LOGIN")
    uri: str = server.get_uri(database=SOURCE_DATABASE)
    with psycopg.connect(uri, autocommit=True) as conn:
        for statement in (*FIXTURE_ESTATE, *GRANT_READER):
            conn.execute(statement)
    return uri


@pytest.fixture(scope="session")
def source_dsn(source_admin_dsn: str) -> str:
    parts = urlsplit(source_admin_dsn)
    host = parts.netloc.split("@")[-1]
    return urlunsplit((parts.scheme, f"{READER_ROLE}@{host}", parts.path, parts.query, parts.fragment))


@pytest.fixture(scope="session", autouse=True)
def secret_store(source_dsn: str) -> Iterator[None]:
    """The deployment's secret store, which for M1 is the environment.

    Set on the process rather than injected, deliberately: the handler the
    worker dispatches to is the *registered* one, built with the default
    `EnvSecretResolver`, and wiring a different resolver here would prove the
    scenario against a handler no deployment runs.
    """
    os.environ[SECRET_ENV] = source_dsn
    yield
    del os.environ[SECRET_ENV]


TRUNCATE_ALL = "TRUNCATE runs, tasks, checkpoints, audit_log, sources CASCADE"
RESET_ESTATE: tuple[str, ...] = ("DROP SCHEMA IF EXISTS sales CASCADE",)


@pytest.fixture(autouse=True)
def clean(dsn: str, source_admin_dsn: str) -> Iterator[None]:
    """Both databases back to their starting state before every scenario.

    Each test below asserts about what a scan *did*, so each has to start from
    a known estate and an empty catalog -- otherwise the test that drops a
    table decides what the next one sees.
    """
    with connect(dsn) as connection:
        connection.execute(TRUNCATE_ALL)
        connection.commit()
    with psycopg.connect(source_admin_dsn, autocommit=True) as connection:
        for statement in (*RESET_ESTATE, *FIXTURE_ESTATE, *GRANT_READER):
            connection.execute(statement)
    yield


@pytest.fixture
def client(dsn: str) -> Iterator[TestClient]:
    app = create_app(PostgresRunStore(dsn), PostgresCatalogStore(dsn))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def conn(dsn: str) -> Iterator[QueueConnection]:
    connection = connect(dsn)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def source_admin(source_admin_dsn: str) -> Iterator[psycopg.Connection[psycopg.rows.TupleRow]]:
    connection = psycopg.connect(source_admin_dsn, autocommit=True)
    try:
        yield connection
    finally:
        connection.close()


def drain(client: TestClient, worker: Worker, run_id: str) -> dict[str, Any]:
    """Run the worker and poll the API until the run stops moving."""
    deadline = time.monotonic() + POLL_TIMEOUT.total_seconds()
    while time.monotonic() < deadline:
        asyncio.run(worker.run_once())
        body: dict[str, Any] = client.get(f"/v1/runs/{run_id}").json()
        if body["status"] in TERMINAL_RUN_STATES:
            return body
        time.sleep(POLL_INTERVAL.total_seconds())
    raise AssertionError(f"run {run_id} never reached a terminal state")


def scan_to_completion(client: TestClient, dsn: str, source_id: str) -> dict[str, Any]:
    accepted = client.post(f"/v1/sources/{source_id}/scan")
    assert accepted.status_code == 202
    return drain(client, Worker(dsn, "m1-acceptance-worker"), accepted.json()["id"])


def catalog_state(conn: QueueConnection) -> list[tuple[Any, ...]]:
    rows = conn.execute(SELECT_CATALOG_STATE).fetchall()
    audit = conn.execute(SELECT_CATALOG_AUDIT).fetchall()
    conn.rollback()
    return rows + audit


def all_assets(client: TestClient, source_id: str) -> list[dict[str, Any]]:
    """Walk `GET /v1/assets` by cursor, exactly as a client must."""
    collected: list[dict[str, Any]] = []
    params: dict[str, Any] = {"source": source_id, "limit": PAGE_SIZE}
    for _ in range(20):  # bounded, so a broken cursor fails instead of hanging
        page = client.get("/v1/assets", params=params).json()
        collected.extend(page["items"])
        if page["next_cursor"] is None:
            return collected
        params = {"source": source_id, "limit": PAGE_SIZE, "cursor": page["next_cursor"]}
    raise AssertionError("pagination did not terminate")


@pytest.fixture
def registered(client: TestClient) -> str:
    created = client.post("/v1/sources", json=SOURCE_BODY)
    assert created.status_code == 201
    source_id: str = created.json()["id"]
    return source_id


def test_a_registered_source_is_scanned_and_readable_over_the_api(
    client: TestClient, dsn: str, registered: str
) -> None:
    """The exit criterion, whole: register, scan, read the catalog back."""
    finished = scan_to_completion(client, dsn, registered)
    assert finished["status"] == "succeeded"
    assert finished["usage"]["steps"] == 1  # I12: one planned task, one step

    assets = all_assets(client, registered)
    assert [asset["fqn"] for asset in assets] == [
        "acceptance_source.sales.customers",
        "acceptance_source.sales.orders",
        "acceptance_source.sales.recent_orders",
        "acceptance_source.sales.refunds",
    ]
    assert {asset["lifecycle"] for asset in assets} == {"active"}
    assert [asset["asset_type"] for asset in assets] == ["table", "table", "view", "table"]

    [orders] = [asset for asset in assets if asset["fqn"].endswith(".orders")]
    detail = client.get(f"/v1/assets/{orders['id']}")
    assert detail.status_code == 200
    assert [(c["name"], c["data_type"], c["nullable"]) for c in detail.json()["columns"]] == [
        ("id", "bigint", False),
        ("customer", "text", True),
        ("total", "numeric(10,2)", True),
    ]


def test_a_second_scan_request_while_one_is_in_flight_returns_that_run(
    client: TestClient, registered: str
) -> None:
    first = client.post(f"/v1/sources/{registered}/scan")
    second = client.post(f"/v1/sources/{registered}/scan")

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["id"] == second.json()["id"]


def test_rescanning_an_unchanged_source_leaves_byte_identical_state(
    client: TestClient, conn: QueueConnection, dsn: str, registered: str
) -> None:
    """I8, the slice's central claim: convergence, not accumulation."""
    scan_to_completion(client, dsn, registered)
    before = catalog_state(conn)
    assert before, "the comparison below would be vacuous against an empty catalog"

    scan_to_completion(client, dsn, registered)

    assert catalog_state(conn) == before  # no new rows, no touched timestamps, no audit churn


def test_a_dropped_table_becomes_missing_and_stays_exactly_one_row(
    client: TestClient,
    conn: QueueConnection,
    dsn: str,
    registered: str,
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
) -> None:
    scan_to_completion(client, dsn, registered)
    source_admin.execute(DROP_REFUNDS)

    scan_to_completion(client, dsn, registered)
    after_drop = catalog_state(conn)

    [refunds] = [a for a in all_assets(client, registered) if a["fqn"].endswith(".refunds")]
    assert refunds["lifecycle"] == "missing"  # marked, not deleted
    assert conn.execute(COUNT_REFUNDS_ROWS).fetchone() == (1,)
    conn.rollback()

    # ...and it does not reappear as a new row, or churn, on the next scan.
    scan_to_completion(client, dsn, registered)
    assert catalog_state(conn) == after_drop
    assert conn.execute(COUNT_REFUNDS_ROWS).fetchone() == (1,)
    conn.rollback()


def test_the_source_connection_is_read_only_at_the_role_level(source_dsn: str) -> None:
    """I5, asserted where it is enforced: in Postgres, not in Python.

    `42501 insufficient_privilege` and not `25006 read_only_sql_transaction` --
    the second would mean a session flag was doing the work, which a
    write-capable role could also pass.
    """
    with psycopg.connect(source_dsn, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
            connection.execute(A_WRITE)
    assert raised.value.sqlstate == INSUFFICIENT_PRIVILEGE


def test_no_credential_is_readable_from_the_database_or_any_response(
    client: TestClient, conn: QueueConnection, dsn: str, registered: str, source_dsn: str
) -> None:
    """N7: the credential exists in exactly one place -- the secret store -- and
    the reference is what everything else holds.

    Checked against the whole `sources` row rather than the columns this test
    happens to know about, so a future column that leaked one would fail here.
    """
    finished = scan_to_completion(client, dsn, registered)

    row = conn.execute(SELECT_SOURCE_ROW).fetchone()
    conn.rollback()
    assert row is not None
    stored = " ".join(str(value) for value in row)
    assert source_dsn not in stored
    assert READER_ROLE not in stored
    assert f"env:{SECRET_ENV}" in stored  # a reference, and only a reference

    served = " ".join(
        response.text
        for response in (
            client.get(f"/v1/runs/{finished['id']}"),
            client.get("/v1/assets", params={"source": registered}),
            client.post("/v1/sources", json=SOURCE_BODY),
        )
    )
    assert source_dsn not in served
    assert READER_ROLE not in served


def test_scanning_a_source_that_was_never_registered_is_a_404(client: TestClient) -> None:
    unknown = UUID(int=7)
    assert client.post(f"/v1/sources/{unknown}/scan").status_code == 404
