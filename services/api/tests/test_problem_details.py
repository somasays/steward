"""RFC 9457 problem-details shape (SPEC.md §8, issue #4): every error path,
including FastAPI's default validation-error handling, returns the same
`steward_schemas.ProblemDetails` shape as `application/problem+json`."""

from fastapi.testclient import TestClient

PROBLEM_CONTENT_TYPE = "application/problem+json"


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
