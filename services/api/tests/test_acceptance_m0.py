"""H11 — M0's exit criterion, executable (GUARDRAILS.md Tier H, SPEC.md §12).

    "a no-op run flows API -> queue -> worker -> done, with a Langfuse trace"

as one scenario over the real components: the real FastAPI app, the real
Postgres-backed store, the real migrations, the real worker loop. Nothing is
stubbed except the tracer, and only because asserting on a span means catching
it -- the trace id being asserted is the one the API generated and the database
stored, and the worker reads it back from the run row like any other worker.

The Langfuse-credentialled path is not exercised here, deliberately: this
scenario must run on a laptop and in CI with no accounts and no network (that
is the point of the graceful-degradation design), and the vendor adapter has
its own tests in `packages/steward-telemetry`.

    uv run pytest -q -m acceptance
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import pgserver
import pytest
from fastapi.testclient import TestClient
from steward_api.app import create_app
from steward_api.store import PostgresRunStore
from steward_queue import NOOP_TASK_TYPE, TaskState, Worker, connect, get_task, upgrade_to_head
from steward_queue.db import QueueConnection
from steward_telemetry import NoopTracer

pytestmark = pytest.mark.acceptance

TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

POLL_INTERVAL = timedelta(milliseconds=50)
POLL_TIMEOUT = timedelta(seconds=30)

TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled"}

SELECT_RUN_TASKS = "SELECT id FROM tasks WHERE run_id = %(run_id)s"
SELECT_AUDIT_TRAIL = """
SELECT action FROM audit_log
WHERE entity_id = %(run)s OR entity_id IN (SELECT id::text FROM tasks WHERE run_id = %(run_id)s)
ORDER BY id
"""

EXPECTED_LIFECYCLE = [
    "run.created",
    "task.enqueued",
    "task.claimed",
    "task.started",
    "run.status_changed",  # pending -> running
    "task.succeeded",
    "run.usage_recorded",
    "run.status_changed",  # running -> succeeded
]


@pytest.fixture(scope="session")
def dsn() -> Iterator[str]:
    """An ephemeral Postgres migrated by the queue's own migrations.

    pgserver ships the server binaries, so Tier H runs without Docker
    (GUARDRAILS.md §1) -- the acceptance scenario has no setup a reader has to
    reproduce by hand.
    """
    with tempfile.TemporaryDirectory(prefix="steward-acceptance") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            uri: str = server.get_uri()
            upgrade_to_head(uri)
            yield uri
        finally:
            server.cleanup()


@pytest.fixture
def client(dsn: str) -> Iterator[TestClient]:
    """The real app, wired to the queue-backed store."""
    store = PostgresRunStore(dsn, tracer=NoopTracer())
    with TestClient(create_app(store)) as test_client:
        yield test_client


@pytest.fixture
def conn(dsn: str) -> Iterator[QueueConnection]:
    """A connection for reading back what the system persisted."""
    connection = connect(dsn)
    try:
        yield connection
    finally:
        connection.close()


def drain_until_terminal(client: TestClient, worker: Worker, run_id: str) -> dict[str, Any]:
    """Run the worker and poll the API until the run stops moving.

    Polling `GET /v1/runs/{id}` rather than the database is the point: the exit
    criterion is what a client can observe, and a client only has the API. The
    worker is stepped rather than left running in a thread so a failure here is
    a readable assertion instead of a hang.
    """
    deadline = time.monotonic() + POLL_TIMEOUT.total_seconds()
    while time.monotonic() < deadline:
        asyncio.run(worker.run_once())
        body: dict[str, Any] = client.get(f"/v1/runs/{run_id}").json()
        if body["status"] in TERMINAL_RUN_STATES:
            return body
        time.sleep(POLL_INTERVAL.total_seconds())
    raise AssertionError(f"run {run_id} never reached a terminal state")


def test_a_noop_run_flows_api_to_queue_to_worker_to_done(
    client: TestClient, conn: QueueConnection, dsn: str
) -> None:
    accepted = client.post("/v1/runs", json={"goal": "noop", "payload": {"echo": "m0"}})
    assert accepted.status_code == 202
    created = accepted.json()
    run_id = created["id"]
    assert created["status"] == "pending"
    assert TRACE_ID_RE.match(created["trace_id"])

    # I8: the 202 means the run row and its task committed together. A client
    # that got this response is guaranteed there is work queued for it.
    [(task_id,)] = conn.execute(SELECT_RUN_TASKS, {"run_id": UUID(run_id)}).fetchall()
    conn.rollback()

    finished = drain_until_terminal(client, Worker(dsn, "acceptance-worker"), run_id)

    assert finished["status"] == "succeeded"
    assert finished["trace_id"] == created["trace_id"]  # one trace for the whole run
    assert finished["usage"]["steps"] == 1
    assert finished["payload"] == {"echo": "m0"}

    task = get_task(conn, task_id)
    assert task is not None
    assert task.task_type == NOOP_TASK_TYPE
    assert task.state is TaskState.SUCCEEDED
    assert task.attempts == 1  # claimed once, executed once

    # I7: every state change on the way wrote its audit row, in the transaction
    # that made the change.
    rows = conn.execute(SELECT_AUDIT_TRAIL, {"run": run_id, "run_id": UUID(run_id)}).fetchall()
    conn.rollback()
    assert [row[0] for row in rows] == EXPECTED_LIFECYCLE


def test_a_replayed_idempotency_key_does_not_start_a_second_run(
    client: TestClient, conn: QueueConnection
) -> None:
    headers = {"Idempotency-Key": "acceptance-replay"}
    first = client.post("/v1/runs", json={"goal": "noop"}, headers=headers).json()
    second = client.post("/v1/runs", json={"goal": "noop"}, headers=headers).json()

    assert first == second
    tasks = conn.execute(SELECT_RUN_TASKS, {"run_id": UUID(first["id"])}).fetchall()
    conn.rollback()
    assert len(tasks) == 1


def test_a_run_the_api_never_created_is_a_404(client: TestClient) -> None:
    missing = "00000000-0000-0000-0000-0000000000ff"
    assert client.get(f"/v1/runs/{missing}").status_code == 404
