"""Two real databases, because a scan is a conversation between two.

`steward_dsn` is Steward's own system of record, migrated by the queue's
migrations. `source_dsn` is a *separate* database standing in for a customer's,
reachable only through a role that holds `SELECT` and nothing else -- which is
what lets the read-only proof assert a privilege error from Postgres rather
than a guard in Python (I5).

Both live on one ephemeral `pgserver` instance: it ships the Postgres binaries,
so Tier H runs on a laptop and in CI without Docker (GUARDRAILS.md §1).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pgserver
import psycopg
import pytest
from steward_catalog import EnvSecretResolver, Secret
from steward_queue import QueueConnection, connect, create_run, upgrade_to_head
from steward_schemas import RunBudget, SourceCreate, SourceEngine, TaskSpec

SOURCE_DATABASE = "fixture_source"
READER_ROLE = "steward_reader"
SOURCE_SECRET_ENV = "STEWARD_TEST_SOURCE_DSN"
SOURCE_SECRET_REF = "env:STEWARD_TEST_SOURCE_DSN"

SCAN_BUDGET = RunBudget(steps=4, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(seconds=30))

TRUNCATE_CATALOG = "TRUNCATE runs, tasks, checkpoints, audit_log, sources CASCADE"

# The canaries H7 hunts for (GUARDRAILS.md Tier H, issue #49). They are planted
# in the fixture data below and the harness asserts that none of them appears in
# a profile row, an audit row, a log line, a span payload -- or anywhere else in
# Steward's database. Each is a distinctive token that occurs nowhere else in
# the repository, so a match is evidence of a leak rather than a coincidence.
CANARY_EMAIL = "canary.7f3a91d2@steward-canary.test"
CANARY_CARD = "4539578763621486"
CANARY_SECRET = "STEWARD-CANARY-TOKEN-9c4e17b6d05f"

CANARY_AFTER_LAST_DOT = "case@2026.CANARY-DIAGNOSIS-4b81f7ac"
"""A canary whose payload sits *after the last dot*.

The other three are shaped like the values whoever wrote the masker was
picturing, and that is exactly why they missed the leak this one exists for:
`_mask_email` interpolated the TLD verbatim, and every canary above has `.test`
or no dot at all as its tail, so the harness watched the leak happen and
reported green. A canary is only evidence for the shapes it takes -- so this
one takes the shape a notes or reference column produces by accident: no
whitespace, one `@`, a dot, and something confidential behind it.
"""

CANARIES: tuple[str, ...] = (CANARY_EMAIL, CANARY_CARD, CANARY_SECRET, CANARY_AFTER_LAST_DOT)

CANARY_TAIL = CANARY_AFTER_LAST_DOT.rpartition(".")[2]
"""The payload alone -- `CANARY-DIAGNOSIS-4b81f7ac`. Swept for separately,
because a mask that published only the tail would leave the full string absent
and every assertion green."""

# The fixture estate. Two schemas so filtering has something to filter, a view
# so `asset_type` has two values, and a nullable column so `nullable` does.
FIXTURE_ESTATE: tuple[str, ...] = (
    "CREATE SCHEMA sales",
    "CREATE SCHEMA staging",
    "CREATE TABLE sales.orders (id bigint NOT NULL, customer text, total numeric(10,2))",
    "CREATE TABLE sales.customers (id bigint NOT NULL, email text NOT NULL, card text)",
    "CREATE VIEW sales.recent_orders AS SELECT id, total FROM sales.orders",
    "CREATE TABLE staging.raw_events (id bigint NOT NULL, body text)",
)

# Rows, because profiling has nothing to say about an empty table (issue #49).
# Scanning is metadata-only and is unaffected by them. `customers` carries both
# ordinary values and the canaries, so H7's assertions are made about a table
# that was really profiled rather than one that happened to be skipped.
#
# The canaries are bound as parameters rather than interpolated: this is a test
# fixture, but an f-string here would be string-built SQL and S3 (ruff S608)
# does not have a "but it is only a test" clause -- suppressing it would be the
# first pragma in this package (I5).
FIXTURE_DATA: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "INSERT INTO sales.orders (id, customer, total) VALUES "
        "(1, 'ada', 10.50), (2, 'grace', 10.50), (3, NULL, 99.99)",
        {},
    ),
    (
        "INSERT INTO sales.customers (id, email, card) VALUES "
        "(1, 'ada@example.com', NULL), (2, %(email)s, %(card)s)",
        {"email": CANARY_EMAIL, "card": CANARY_CARD},
    ),
    (
        "INSERT INTO staging.raw_events (id, body) VALUES "
        "(1, %(secret)s), (2, 'ordinary event'), (3, %(tail)s)",
        {"secret": CANARY_SECRET, "tail": CANARY_AFTER_LAST_DOT},
    ),
)

GRANT_READER: tuple[str, ...] = (
    "GRANT USAGE ON SCHEMA sales, staging TO steward_reader",
    "GRANT SELECT ON ALL TABLES IN SCHEMA sales, staging TO steward_reader",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA sales, staging GRANT SELECT ON TABLES TO steward_reader",
    # PUBLIC keeps CREATE on `public` before Postgres 15; revoking it makes the
    # write proof version-independent.
    "REVOKE ALL ON SCHEMA public FROM PUBLIC",
)


def build_estate(conn: psycopg.Connection[psycopg.rows.TupleRow]) -> None:
    """Create the fixture estate, fill it, and grant the reader its access.

    One function so the session fixture and the per-test teardown build the
    same estate; a test that dropped a table would otherwise decide what the
    next test sees.
    """
    for statement in FIXTURE_ESTATE:
        conn.execute(statement)
    for statement, params in FIXTURE_DATA:
        conn.execute(statement, params or None)
    for statement in GRANT_READER:
        conn.execute(statement)


@pytest.fixture(scope="session")
def canaries() -> tuple[str, ...]:
    """The planted secrets, as a fixture rather than an import.

    Tests run under `--import-mode=importlib`, so a test module cannot import
    this one; a fixture is how a conftest constant reaches a test here.
    """
    return CANARIES


@pytest.fixture(scope="session")
def canary_email() -> str:
    return CANARY_EMAIL


@pytest.fixture(scope="session")
def canary_card() -> str:
    return CANARY_CARD


@pytest.fixture(scope="session")
def canary_secret() -> str:
    return CANARY_SECRET


@pytest.fixture(scope="session")
def canary_tail() -> str:
    """The payload behind the last dot, swept for on its own."""
    return CANARY_TAIL


@pytest.fixture(scope="session")
def pg_server() -> Iterator[pgserver.PostgresServer]:
    with tempfile.TemporaryDirectory(prefix="stc") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            yield server
        finally:
            server.cleanup()


@pytest.fixture(scope="session")
def steward_dsn(pg_server: pgserver.PostgresServer) -> str:
    """Steward's system of record, at head."""
    uri: str = pg_server.get_uri()
    upgrade_to_head(uri)
    return uri


@pytest.fixture(scope="session")
def source_admin_dsn(pg_server: pgserver.PostgresServer) -> str:
    """The fixture source database, as an owner who may change it."""
    pg_server.psql("DROP DATABASE IF EXISTS fixture_source")
    pg_server.psql("DROP ROLE IF EXISTS steward_reader")
    pg_server.psql("CREATE DATABASE fixture_source")
    pg_server.psql("CREATE ROLE steward_reader LOGIN")
    uri: str = pg_server.get_uri(database=SOURCE_DATABASE)
    with psycopg.connect(uri, autocommit=True) as conn:
        build_estate(conn)
    return uri


def as_role(dsn: str, role: str) -> str:
    """The same DSN, connecting as `role`. Used to build the reader's DSN."""
    parts = urlsplit(dsn)
    host = parts.netloc.split("@")[-1]
    return urlunsplit((parts.scheme, f"{role}@{host}", parts.path, parts.query, parts.fragment))


@pytest.fixture(scope="session")
def source_dsn(source_admin_dsn: str) -> str:
    """The fixture source as Steward sees it: the read-only role's DSN."""
    return as_role(source_admin_dsn, READER_ROLE)


@pytest.fixture(scope="session")
def source_secret(source_dsn: str) -> Secret:
    return Secret(source_dsn)


@pytest.fixture
def resolver(source_dsn: str) -> EnvSecretResolver:
    """A resolver whose environment holds the fixture source's DSN.

    Injected rather than exported into `os.environ`, so tests running in one
    session cannot see each other's credentials.
    """
    return EnvSecretResolver(environ={SOURCE_SECRET_ENV: source_dsn})


RESET_ESTATE: tuple[str, ...] = (
    "DROP SCHEMA IF EXISTS sales CASCADE",
    "DROP SCHEMA IF EXISTS staging CASCADE",
)


@pytest.fixture
def source_admin(source_admin_dsn: str) -> Iterator[psycopg.Connection[psycopg.rows.TupleRow]]:
    """An owner connection for mutating the fixture estate mid-test.

    The estate is rebuilt on teardown rather than shared: a test that drops a
    table would otherwise decide what the next test sees, and "did this scan
    converge" is not a question to answer against a moving fixture.
    """
    conn = psycopg.connect(source_admin_dsn, autocommit=True)
    try:
        yield conn
        for statement in RESET_ESTATE:
            conn.execute(statement)
        build_estate(conn)
    finally:
        conn.close()


@pytest.fixture
def conn(steward_dsn: str) -> Iterator[QueueConnection]:
    """A clean catalog and one connection on it."""
    connection = connect(steward_dsn)
    connection.execute(TRUNCATE_CATALOG)
    connection.commit()
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def make_source_create(
    *,
    name: str = "fixture-warehouse",
    host: str = "fixture.internal",
    database: str = SOURCE_DATABASE,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = ("information_schema", "pg_catalog"),
) -> SourceCreate:
    return SourceCreate(
        name=name,
        engine=SourceEngine.POSTGRES,
        host=host,
        database=database,
        dsn_secret_ref=SOURCE_SECRET_REF,
        include_schemas=include,
        exclude_schemas=exclude,
    )


@pytest.fixture
def source_create() -> SourceCreate:
    """A registration that covers the fixture estate's two schemas only."""
    return make_source_create(include=("sales", "staging"))


@pytest.fixture
def spec_factory(conn: QueueConnection) -> Callable[[UUID], TaskSpec]:
    """A `scan_source` spec on a committed run, so the handler has a real task."""

    def factory(source_id: UUID) -> TaskSpec:
        run = create_run(conn, goal="scan_source", budget=SCAN_BUDGET)
        conn.commit()
        return TaskSpec(
            task_id=uuid4(),
            run_id=run.id,
            task_type="scan_source",
            payload={"source_id": str(source_id)},
            budget=SCAN_BUDGET,
            max_attempts=3,
        )

    return factory
