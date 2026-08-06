"""Real-Postgres fixtures.

The queue's guarantees are Postgres guarantees -- `FOR UPDATE SKIP LOCKED`,
transaction visibility, unique-index conflict handling. A fake would assert
our beliefs about those, not the behaviour, so every integration test here
runs against a real server: `pgserver` ships the Postgres binaries in its
wheel and starts an ephemeral instance for the session, which is what lets
Tier H run on a laptop without Docker (GUARDRAILS.md §1 Tier H).

One server and one migrated schema per session; each test starts from
truncated tables.
"""

import tempfile
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pgserver
import pytest
from steward_queue import connect, create_run, enqueue, upgrade_to_head
from steward_queue.db import QueueConnection
from steward_schemas import RunBudget, TaskSpec

TEST_BUDGET = RunBudget(steps=10, tokens=1000, cost_usd=Decimal("1.500000"), wall_clock=timedelta(minutes=5))

TRUNCATE_ALL = "TRUNCATE runs, tasks, checkpoints, audit_log RESTART IDENTITY CASCADE"


@pytest.fixture(scope="session")
def pg_server() -> Iterator[pgserver.PostgresServer]:
    """An ephemeral Postgres for the whole session."""
    with tempfile.TemporaryDirectory(prefix="stq") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            yield server
        finally:
            server.cleanup()


@pytest.fixture(scope="session")
def dsn(pg_server: pgserver.PostgresServer) -> str:
    """The default database, migrated to head by the package's own migrations."""
    uri: str = pg_server.get_uri()
    upgrade_to_head(uri)
    return uri


@pytest.fixture
def open_conn(dsn: str) -> Iterator[Callable[[], QueueConnection]]:
    """Open extra connections; every one is closed when the test ends.

    Concurrency and crash tests need more than one connection in flight, and a
    crashed one is simply never committed.
    """
    opened: list[QueueConnection] = []

    def factory() -> QueueConnection:
        conn = connect(dsn)
        opened.append(conn)
        return conn

    yield factory
    for conn in opened:
        conn.close()


@pytest.fixture
def conn(dsn: str, open_conn: Callable[[], QueueConnection]) -> Iterator[QueueConnection]:
    """A clean database and one connection on it."""
    connection = open_conn()
    connection.execute(TRUNCATE_ALL)
    connection.commit()
    yield connection
    connection.rollback()


@pytest.fixture
def budget() -> RunBudget:
    return TEST_BUDGET


@pytest.fixture
def run_id(conn: QueueConnection) -> UUID:
    """A committed run to hang tasks off."""
    record = create_run(conn, goal="test", budget=TEST_BUDGET)
    conn.commit()
    return record.id


def make_spec(
    run: UUID,
    *,
    task_type: str = "noop",
    payload: dict[str, object] | None = None,
    max_attempts: int = 3,
    task_id: UUID | None = None,
    budget: RunBudget | None = None,
) -> TaskSpec:
    """A TaskSpec with the fixture budget unless the test needs a tighter one."""
    return TaskSpec(
        task_id=task_id or uuid4(),
        run_id=run,
        task_type=task_type,
        payload=payload if payload is not None else {"echo": "noop"},
        budget=budget or TEST_BUDGET,
        max_attempts=max_attempts,
    )


@pytest.fixture
def spec_factory() -> Callable[..., TaskSpec]:
    return make_spec


@pytest.fixture
def queued(conn: QueueConnection, run_id: UUID) -> Callable[..., TaskSpec]:
    """Enqueue a task and commit, returning its spec."""

    def factory(
        *,
        task_type: str = "noop",
        payload: dict[str, object] | None = None,
        max_attempts: int = 3,
        task_id: UUID | None = None,
        budget: RunBudget | None = None,
    ) -> TaskSpec:
        spec = make_spec(
            run_id,
            task_type=task_type,
            payload=payload,
            max_attempts=max_attempts,
            task_id=task_id,
            budget=budget,
        )
        enqueue(conn, spec)
        conn.commit()
        return spec

    return factory
