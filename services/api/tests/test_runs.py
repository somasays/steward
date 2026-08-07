import re
from uuid import uuid4

from fastapi.testclient import TestClient

PROBLEM_CONTENT_TYPE = "application/problem+json"


def test_create_run_returns_202_with_run_id(client: TestClient) -> None:
    resp = client.post("/v1/runs", json={"goal": "scan_source", "payload": {"source_id": "abc"}})
    assert resp.status_code == 202
    body = resp.json()
    assert body["goal"] == "scan_source"
    assert body["payload"] == {"source_id": "abc"}
    assert body["status"] == "pending"
    assert "id" in body
    assert resp.headers["location"] == f"/v1/runs/{body['id']}"


def test_a_created_run_is_traceable_and_bounded(client: TestClient) -> None:
    # I7 and I12 are visible in the contract, not just in the database: a
    # client can always name the trace, and can always see the caps.
    body = client.post("/v1/runs", json={"goal": "scan_source"}).json()
    assert re.fullmatch(r"[0-9a-f]{32}", body["trace_id"])
    assert body["budget"]["steps"] > 0
    assert body["usage"]["steps"] == 0


def test_get_run_returns_created_run(client: TestClient) -> None:
    created = client.post("/v1/runs", json={"goal": "noop"}).json()

    resp = client.get(f"/v1/runs/{created['id']}")

    assert resp.status_code == 200
    assert resp.json() == created


def test_get_run_missing_is_404_problem_details(client: TestClient) -> None:
    missing_id = uuid4()

    resp = client.get(f"/v1/runs/{missing_id}")

    assert resp.status_code == 404
    assert resp.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = resp.json()
    assert body["status"] == 404
    assert body["title"]
    assert str(missing_id) in body["detail"]


def test_idempotency_key_returns_same_run(client: TestClient) -> None:
    headers = {"Idempotency-Key": "retry-123"}
    first = client.post("/v1/runs", json={"goal": "scan_source"}, headers=headers)
    second = client.post("/v1/runs", json={"goal": "scan_source"}, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert first.json() == second.json()


def test_reusing_an_idempotency_key_for_a_different_request_is_a_409(client: TestClient) -> None:
    # Returning the first run would tell the client its edited goal was
    # queued. Nothing would ever run it, and nothing would say so.
    headers = {"Idempotency-Key": "retry-123"}
    first = client.post("/v1/runs", json={"goal": "scan_source"}, headers=headers)
    second = client.post("/v1/runs", json={"goal": "classify_columns"}, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert second.json()["instance"] == f"/v1/runs/{first.json()['id']}"


def test_a_changed_payload_also_conflicts(client: TestClient) -> None:
    headers = {"Idempotency-Key": "retry-payload"}
    client.post("/v1/runs", json={"goal": "scan_source", "payload": {"source_id": "a"}}, headers=headers)
    second = client.post(
        "/v1/runs", json={"goal": "scan_source", "payload": {"source_id": "b"}}, headers=headers
    )

    assert second.status_code == 409


def test_different_idempotency_keys_create_different_runs(client: TestClient) -> None:
    first = client.post("/v1/runs", json={"goal": "scan_source"}, headers={"Idempotency-Key": "a"})
    second = client.post("/v1/runs", json={"goal": "scan_source"}, headers={"Idempotency-Key": "b"})

    assert first.json()["id"] != second.json()["id"]


def test_no_idempotency_key_creates_distinct_runs(client: TestClient) -> None:
    first = client.post("/v1/runs", json={"goal": "scan_source"})
    second = client.post("/v1/runs", json={"goal": "scan_source"})

    assert first.json()["id"] != second.json()["id"]
