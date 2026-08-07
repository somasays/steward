"""What `POST /v1/runs` now refuses, and what refusing costs (issue #19).

`goal` used to be a free string: any value was accepted, a run row was written,
and a `noop` task was enqueued for it whatever the client had asked for. The
goal registry moved that decision in front of the database, so these tests
assert both halves -- the response a client gets, and the absence of a run.

The rejection path runs against a real Postgres because "no run row is created"
is a claim about the database; the in-memory store could only ever show that
the store's own dict stayed empty.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator

import pgserver
import pytest
from fastapi.testclient import TestClient
from steward_api.app import create_app
from steward_api.store import PostgresRunStore
from steward_orchestration import NOOP_BUDGET
from steward_queue import connect, upgrade_to_head
from steward_queue.db import QueueConnection

PROBLEM_CONTENT_TYPE = "application/problem+json"

COUNT_RUNS = "SELECT count(*) FROM runs"
COUNT_TASKS = "SELECT count(*) FROM tasks"


@pytest.fixture(scope="session")
def dsn() -> Iterator[str]:
    with tempfile.TemporaryDirectory(prefix="steward-admission") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            uri: str = server.get_uri()
            upgrade_to_head(uri)
            yield uri
        finally:
            server.cleanup()


@pytest.fixture
def conn(dsn: str) -> Iterator[QueueConnection]:
    connection = connect(dsn)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def db_client(dsn: str) -> Iterator[TestClient]:
    with TestClient(create_app(PostgresRunStore(dsn))) as test_client:
        yield test_client


def _counts(conn: QueueConnection) -> tuple[int, int]:
    runs = conn.execute(COUNT_RUNS).fetchone()
    tasks = conn.execute(COUNT_TASKS).fetchone()
    conn.rollback()
    assert runs is not None and tasks is not None
    return runs[0], tasks[0]


def test_an_unknown_goal_is_rejected_with_problem_details(client: TestClient) -> None:
    resp = client.post("/v1/runs", json={"goal": "not_a_goal"})

    assert resp.status_code == 422
    assert resp.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = resp.json()
    assert body["type"] == "urn:steward:unknown-goal"
    assert "not_a_goal" in body["detail"]


def test_a_payload_the_goals_schema_rejects_says_which_field(client: TestClient) -> None:
    resp = client.post("/v1/runs", json={"goal": "noop", "payload": {"echo": 7}})

    assert resp.status_code == 422
    body = resp.json()
    assert body["type"] == "urn:steward:invalid-goal-payload"
    assert [error["loc"] for error in body["errors"]] == [["echo"]]


def test_a_parameter_the_goal_does_not_have_is_rejected(client: TestClient) -> None:
    # Previously accepted and silently ignored: the run was created, and the
    # payload the client thought it had sent never reached anything.
    resp = client.post("/v1/runs", json={"goal": "noop", "payload": {"eco": "typo"}})

    assert resp.status_code == 422
    assert resp.json()["type"] == "urn:steward:invalid-goal-payload"


def test_a_registered_goal_with_a_valid_payload_is_still_accepted(client: TestClient) -> None:
    resp = client.post("/v1/runs", json={"goal": "noop", "payload": {"echo": "ok"}})

    assert resp.status_code == 202


def test_the_budget_a_run_is_admitted_under_comes_from_its_goal(client: TestClient) -> None:
    body = client.post("/v1/runs", json={"goal": "noop"}).json()

    assert body["budget"]["steps"] == NOOP_BUDGET.steps
    assert body["budget"]["tokens"] == NOOP_BUDGET.tokens


def test_a_rejected_request_creates_no_run_and_no_task(db_client: TestClient, conn: QueueConnection) -> None:
    before = _counts(conn)

    unknown = db_client.post("/v1/runs", json={"goal": "not_a_goal"})
    invalid = db_client.post("/v1/runs", json={"goal": "noop", "payload": {"echo": []}})

    assert (unknown.status_code, invalid.status_code) == (422, 422)
    assert _counts(conn) == before


def test_an_admitted_request_creates_the_run_and_its_planned_task(
    db_client: TestClient, conn: QueueConnection
) -> None:
    runs, tasks = _counts(conn)

    accepted = db_client.post("/v1/runs", json={"goal": "noop", "payload": {"echo": "planned"}})

    assert accepted.status_code == 202
    assert _counts(conn) == (runs + 1, tasks + 1)


def test_an_idempotency_key_does_not_rescue_an_invalid_request(
    db_client: TestClient, conn: QueueConnection
) -> None:
    # Admission happens before the key is looked at, so replaying a good key
    # with a bad body is a 422 about the body, not a 409 about the key -- and
    # it still writes nothing.
    headers = {"Idempotency-Key": "admission-replay"}
    db_client.post("/v1/runs", json={"goal": "noop"}, headers=headers)
    before = _counts(conn)

    resp = db_client.post("/v1/runs", json={"goal": "nope"}, headers=headers)

    assert resp.status_code == 422
    assert _counts(conn) == before
