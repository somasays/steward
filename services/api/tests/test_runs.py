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


def test_different_idempotency_keys_create_different_runs(client: TestClient) -> None:
    first = client.post("/v1/runs", json={"goal": "scan_source"}, headers={"Idempotency-Key": "a"})
    second = client.post("/v1/runs", json={"goal": "scan_source"}, headers={"Idempotency-Key": "b"})

    assert first.json()["id"] != second.json()["id"]


def test_no_idempotency_key_creates_distinct_runs(client: TestClient) -> None:
    first = client.post("/v1/runs", json={"goal": "scan_source"})
    second = client.post("/v1/runs", json={"goal": "scan_source"})

    assert first.json()["id"] != second.json()["id"]
