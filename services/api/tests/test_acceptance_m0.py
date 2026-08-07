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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pgserver
import pytest
from fastapi.testclient import TestClient
from steward_api.app import create_app
from steward_api.store import REPLAYED_DETAIL, PostgresRunStore
from steward_queue import NOOP_TASK_TYPE, TaskState, Worker, connect, get_task, upgrade_to_head
from steward_queue.db import QueueConnection
from steward_schemas import RunCreate
from steward_telemetry import Span, SpanOutcome, new_trace_id

pytestmark = pytest.mark.acceptance

TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

UNREACHABLE_DSN = "postgresql://steward@127.0.0.1:1/steward"
"""A DSN nothing listens on: the cheapest way to make creation fail for real."""

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


@dataclass
class RecordedSpan:
    """A span the store opened, and how it said the work ended."""

    trace_id: str
    run_id: UUID
    goal: str
    outcome: SpanOutcome | None = None
    detail: str | None = None

    def record(self, outcome: SpanOutcome, detail: str | None = None) -> None:
        if self.outcome is None:  # first outcome wins, as in the Langfuse span
            self.outcome, self.detail = outcome, detail


class RecordingTracer:
    """A `Tracer` that keeps its spans, so the scenario can assert on them.

    Honours the same exit rule the `Span` contract states and `LangfuseTracer`
    implements: a block that raises ends its span `ERROR`, a clean one ends it
    `OK`, and an outcome the caller already recorded wins. Without that this
    tracer would agree with the real one only on the happy path.
    """

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def run_span(self, *, trace_id: str, run_id: UUID, goal: str) -> Iterator[Span]:
        span = RecordedSpan(trace_id=trace_id, run_id=run_id, goal=goal)
        self.spans.append(span)
        try:
            yield span
        except Exception as exc:
            span.record(SpanOutcome.ERROR, f"{type(exc).__name__}: {exc}")
            raise
        span.record(SpanOutcome.OK)

    @contextmanager
    def task_span(self, *, trace_id: str, run_id: UUID, task_id: UUID, task_type: str) -> Iterator[Span]:
        raise NotImplementedError("the API creates runs; tasks are executed by workers")
        yield  # pragma: no cover -- unreachable, kept so the signature is a generator


@pytest.fixture
def tracer() -> RecordingTracer:
    return RecordingTracer()


@pytest.fixture
def client(dsn: str, tracer: RecordingTracer) -> Iterator[TestClient]:
    """The real app, wired to the queue-backed store."""
    store = PostgresRunStore(dsn, tracer=tracer)
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
    client: TestClient, conn: QueueConnection, dsn: str, tracer: RecordingTracer
) -> None:
    accepted = client.post("/v1/runs", json={"goal": "noop", "payload": {"echo": "m0"}})
    assert accepted.status_code == 202
    created = accepted.json()
    run_id = created["id"]
    assert created["status"] == "pending"
    assert TRACE_ID_RE.match(created["trace_id"])

    # I7: creating the run opened the trace, on the id the row now carries.
    [span] = tracer.spans
    assert span.trace_id == created["trace_id"]
    assert span.run_id == UUID(run_id)
    assert span.goal == "noop"

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
    client: TestClient, conn: QueueConnection, tracer: RecordingTracer
) -> None:
    headers = {"Idempotency-Key": "acceptance-replay"}
    first = client.post("/v1/runs", json={"goal": "noop"}, headers=headers).json()
    second = client.post("/v1/runs", json={"goal": "noop"}, headers=headers).json()

    assert first == second
    tasks = conn.execute(SELECT_RUN_TASKS, {"run_id": UUID(first["id"])}).fetchall()
    conn.rollback()
    assert len(tasks) == 1
    # The replay is traced as a replay rather than as a second creation...
    assert [span.detail for span in tracer.spans] == [None, REPLAYED_DETAIL]
    # ...and on the original run's trace. The replay generated a run id and a
    # trace id that the transaction then discarded; a span carrying those would
    # sit on a trace no run points at, and the original run's trace would show
    # nothing of the retry (#27).
    assert [span.run_id for span in tracer.spans] == [UUID(first["id"])] * 2
    assert [span.trace_id for span in tracer.spans] == [first["trace_id"]] * 2


def test_a_creation_that_fails_is_still_traced(tracer: RecordingTracer) -> None:
    """I7: a failed creation is a trace with an error, not a missing trace.

    Nothing was persisted, so the span carries the identity the attempt
    generated -- the only one that ever named the work -- which is checked here
    by its derivation, since no response exists to read the run id from.
    """
    store = PostgresRunStore(UNREACHABLE_DSN, tracer=tracer)

    with pytest.raises(Exception, match="connection"):
        asyncio.run(store.create_run(RunCreate(goal="noop"), None))

    [span] = tracer.spans
    assert span.outcome is SpanOutcome.ERROR
    assert span.detail is not None
    assert span.goal == "noop"
    assert span.trace_id == new_trace_id(seed=str(span.run_id))


def test_reusing_a_key_for_a_different_request_is_a_409(client: TestClient, conn: QueueConnection) -> None:
    headers = {"Idempotency-Key": "acceptance-conflict"}
    first = client.post("/v1/runs", json={"goal": "noop"}, headers=headers)
    second = client.post("/v1/runs", json={"goal": "noop", "payload": {"echo": "x"}}, headers=headers)

    assert second.status_code == 409
    tasks = conn.execute(SELECT_RUN_TASKS, {"run_id": UUID(first.json()["id"])}).fetchall()
    conn.rollback()
    assert len(tasks) == 1  # the conflicting request queued nothing


def test_a_run_the_api_never_created_is_a_404(client: TestClient) -> None:
    missing = "00000000-0000-0000-0000-0000000000ff"
    assert client.get(f"/v1/runs/{missing}").status_code == 404
