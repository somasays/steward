"""RFC 9457 problem-details shape (SPEC.md §8, issue #4): every error path,
including FastAPI's default validation-error handling, returns the same
`steward_schemas.ProblemDetails` shape as `application/problem+json`."""

import logging
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from steward_api.app import create_app
from steward_schemas import Run, RunCreate

PROBLEM_CONTENT_TYPE = "application/problem+json"

_SECRET_DETAIL = "wrong number of turnips in the silo: 42"


class _ExplodingStore:
    """A `RunStore` whose every method raises with detail no client should
    ever see -- issue #39's "unexpected server error" case, decoupled from
    any specific planner bug so the catch-all handler is proven generic
    rather than special-cased to `EmptyRunPlan`/`DisallowedTaskType`."""

    async def create_run(self, spec: RunCreate, idempotency_key: str | None) -> Run:
        raise RuntimeError(_SECRET_DETAIL)

    async def get_run(self, run_id: UUID) -> Run | None:
        raise RuntimeError(_SECRET_DETAIL)


def test_validation_error_is_problem_details_shape(client: TestClient) -> None:
    resp = client.post("/v1/runs", json={"payload": {}})  # missing required "goal"

    assert resp.status_code == 422
    assert resp.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = resp.json()
    assert body["status"] == 422
    assert body["title"] == "Request validation failed"
    assert body["instance"] == "/v1/runs"
    assert isinstance(body["errors"], list)
    assert body["errors"]
    assert any(err["loc"] == ["body", "goal"] for err in body["errors"])


def test_malformed_run_id_is_problem_details_shape(client: TestClient) -> None:
    resp = client.get("/v1/runs/not-a-uuid")

    assert resp.status_code == 422
    assert resp.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = resp.json()
    assert body["status"] == 422
    assert "type" in body


def test_unknown_route_is_problem_details_shape(client: TestClient) -> None:
    resp = client.get("/v1/does-not-exist")

    assert resp.status_code == 404
    assert resp.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = resp.json()
    assert body["status"] == 404
    assert body["title"]


def test_an_unexpected_server_error_is_sanitized_problem_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Issue #39: a programming error that escapes a route handler used to
    # reach Starlette's default 500 handling -- undocumented, and not
    # `application/problem+json`. It is now caught and reported generically,
    # with nothing of the underlying exception in the body -- the detail is
    # not dropped, it goes to the server log instead.
    with caplog.at_level(logging.ERROR, logger="steward_api.problem_details"):
        with TestClient(create_app(_ExplodingStore()), raise_server_exceptions=False) as client:
            resp = client.post("/v1/runs", json={"goal": "noop"})

    assert resp.status_code == 500
    assert resp.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = resp.json()
    assert body["status"] == 500
    assert body["type"] == "urn:steward:internal-error"
    assert body["title"]
    assert _SECRET_DETAIL not in resp.text
    assert "RuntimeError" not in resp.text
    assert body.get("detail") is None

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert _SECRET_DETAIL in caplog.text


def test_a_validation_error_never_reflects_the_submitted_value(client: TestClient) -> None:
    """N7: a rejected field must not become a mirror.

    Pydantic's error details carry `input`, so echoing them verbatim would put
    whatever the client sent -- including a credential posted into the wrong
    field -- in the response body, its logs, and any body-recording proxy.
    """
    rejected = client.post("/v1/runs", json={"goal": 12345, "payload": {"secret": "hunter2"}})

    assert rejected.status_code == 422
    assert "hunter2" not in rejected.text
    assert "12345" not in rejected.text
    # ...while still telling the client which field was wrong.
    assert any(error["loc"] == ["body", "goal"] for error in rejected.json()["errors"])
    assert all(set(error) == {"type", "loc", "msg"} for error in rejected.json()["errors"])
