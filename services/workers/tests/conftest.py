"""Real-Postgres fixtures for the worker service's own tests.

The same ephemeral-server approach the queue's tests use (GUARDRAILS §1 Tier H):
these assertions are about a worker, a real transaction and a real checkpoint
row surviving a restart, and a fake would assert our beliefs about those rather
than the behaviour.
"""

import tempfile
from collections.abc import Iterator

import pgserver
import pytest
from steward_queue import connect, upgrade_to_head
from steward_queue.db import QueueConnection

TRUNCATE_ALL = "TRUNCATE runs, tasks, checkpoints, audit_log RESTART IDENTITY CASCADE"


@pytest.fixture(scope="session")
def pg_server() -> Iterator[pgserver.PostgresServer]:
    with tempfile.TemporaryDirectory(prefix="stw") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            yield server
        finally:
            server.cleanup()


@pytest.fixture(scope="session")
def dsn(pg_server: pgserver.PostgresServer) -> str:
    uri: str = pg_server.get_uri()
    upgrade_to_head(uri)
    return uri


@pytest.fixture
def conn(dsn: str) -> Iterator[QueueConnection]:
    """A connection whose tables start empty."""
    connection = connect(dsn)
    try:
        connection.execute(TRUNCATE_ALL)
        connection.commit()
        yield connection
    finally:
        connection.close()
