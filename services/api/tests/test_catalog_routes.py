"""The catalog HTTP layer, against the in-memory store.

Status codes, headers, cursors and rejections -- everything the routes decide,
which is deliberately not very much. The behaviour below the seam is proved
against real databases in `packages/steward-catalog` and end to end in
`test_acceptance_m1.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

SOURCE_BODY: dict[str, Any] = {
    "name": "warehouse",
    "engine": "postgres",
    "host": "warehouse.internal",
    "database": "analytics",
    "dsn_secret_ref": "env:STEWARD_SOURCE_DSN_WAREHOUSE",
    "include_schemas": ["public", "sales"],
}


def test_registering_a_source_is_201_then_200(client: TestClient) -> None:
    first = client.post("/v1/sources", json=SOURCE_BODY)
    second = client.post("/v1/sources", json=SOURCE_BODY)

    assert first.status_code == 201
    assert second.status_code == 200  # idempotent on the natural key, and says so
    assert first.json() == second.json()
    assert first.headers["Location"] == f"/v1/sources/{first.json()['id']}"


def test_a_registration_response_carries_a_reference_and_no_credential(client: TestClient) -> None:
    body = client.post("/v1/sources", json=SOURCE_BODY).json()

    assert body["dsn_secret_ref"] == SOURCE_BODY["dsn_secret_ref"]
    assert "://" not in body["dsn_secret_ref"]
    assert not any("password" in key or "dsn" == key for key in body)


def test_posting_a_dsn_instead_of_a_reference_is_rejected_at_the_boundary(
    client: TestClient,
) -> None:
    """N7, and the reason the constraint is on the *contract* and not only on
    the column.

    Without it the request reaches the INSERT, Postgres rejects it, and the
    `CheckViolation` it raises quotes the failing row -- password included --
    into the API's error log on its way to a sanitized 500. Rejecting at the
    boundary means the credential never leaves the request.
    """
    rejected = client.post(
        "/v1/sources",
        json={**SOURCE_BODY, "dsn_secret_ref": "postgresql://steward:hunter2@db.internal:5432/analytics"},
    )

    assert rejected.status_code == 422
    assert "hunter2" not in rejected.text  # the rejection does not echo the value back


def test_a_source_body_must_name_an_engine_steward_supports(client: TestClient) -> None:
    rejected = client.post("/v1/sources", json={**SOURCE_BODY, "engine": "sqlite"})

    assert rejected.status_code == 422
    assert rejected.headers["content-type"].startswith("application/problem+json")


def test_an_unknown_field_is_rejected_rather_than_dropped(client: TestClient) -> None:
    # `SourceCreate` forbids extras (I3): a misspelled parameter must not be
    # silently ignored, because the source it registers would be the wrong one.
    assert client.post("/v1/sources", json={**SOURCE_BODY, "schemas": ["public"]}).status_code == 422


def test_scanning_a_source_is_202_and_idempotent_while_in_flight(client: TestClient) -> None:
    source_id = client.post("/v1/sources", json=SOURCE_BODY).json()["id"]

    first = client.post(f"/v1/sources/{source_id}/scan")
    second = client.post(f"/v1/sources/{source_id}/scan")

    assert first.status_code == 202
    assert first.json() == second.json()  # the second request returns the first run
    assert first.json()["goal"] == "scan_source"
    assert first.json()["payload"] == {"source_id": source_id}
    assert first.headers["Location"] == f"/v1/runs/{first.json()['id']}"


def test_a_scan_run_is_created_with_the_goals_budget(client: TestClient) -> None:
    source_id = client.post("/v1/sources", json=SOURCE_BODY).json()["id"]

    run = client.post(f"/v1/sources/{source_id}/scan").json()

    assert run["budget"]["steps"] == 1  # I12: one planned task, one step
    assert run["trace_id"]  # I7: traceable from creation


def test_scanning_a_source_that_was_never_registered_is_a_404(client: TestClient) -> None:
    missing = uuid4()

    rejected = client.post(f"/v1/sources/{missing}/scan")

    assert rejected.status_code == 404
    assert rejected.json()["type"] == "urn:steward:not-found"


def test_listing_assets_returns_a_page(client: TestClient) -> None:
    listed = client.get("/v1/assets")

    assert listed.status_code == 200
    assert listed.json() == {"items": [], "next_cursor": None}


def test_a_cursor_this_api_never_issued_is_a_400(client: TestClient) -> None:
    rejected = client.get("/v1/assets", params={"cursor": "not-a-cursor!"})

    assert rejected.status_code == 400
    assert rejected.json()["type"] == "urn:steward:bad-request"


def test_the_page_size_is_bounded(client: TestClient) -> None:
    # An unbounded `limit` is a client-controlled full-table read.
    assert client.get("/v1/assets", params={"limit": 1000}).status_code == 422
    assert client.get("/v1/assets", params={"limit": 0}).status_code == 422


def test_an_asset_that_does_not_exist_is_a_404(client: TestClient) -> None:
    rejected = client.get(f"/v1/assets/{uuid4()}")

    assert rejected.status_code == 404
    assert rejected.headers["content-type"].startswith("application/problem+json")
