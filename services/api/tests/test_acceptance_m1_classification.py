"""H11 — M1 slice 3b's exit criterion, executable (GUARDRAILS.md Tier H, issue #50).

    "profile → Classifier Agent → pending review → approval → published version",
     end to end

as one scenario over the real components: the real FastAPI app, the real
Postgres-backed stores, the real migrations, the real worker loop claiming from
the real queue, the real scan and profile handlers reading a real *second*
database through a role that holds only `SELECT`, and the real
`classify_asset` handler with its real evidence resolution and its real
append-only review lifecycle.

**One thing is a stub, and it is the model.** `ColumnClassifier` is bound to a
deterministic answer built from the request the handler hands it. That is the
seam D15 exists for, and stubbing it here is deliberate rather than a shortcut:
a scenario that called a gateway would assert a model's willingness to answer
the same way twice, and would make this file — which runs on every commit
forever — depend on credentials and a network. What the real gateway does is
proven separately by the live smoke test issue #50 asks for; what *this* proves
is everything between the API and the published classification.

The stub is still made to earn its keep. It never states the columns it
classifies: it reads them off the request, so the proposal covers whatever the
fixture estate really has, and the citations it emits are resolved against the
profile the *real* profiler wrote. A stub that named columns would pass while
the handler fed it nothing.

    uv run pytest -q -m acceptance
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pgserver
import psycopg
import pytest
from fastapi.testclient import TestClient
from steward_api.app import create_app
from steward_api.catalog import PostgresCatalogStore
from steward_api.store import PostgresRunStore
from steward_catalog import (
    CLASSIFIER,
    ClassificationRequest,
    ClassificationRun,
    ProposedClassification,
)
from steward_queue import Worker, connect, registered_types, upgrade_to_head
from steward_queue.db import QueueConnection
from steward_schemas import (
    ColumnClassification,
    EvidenceKind,
    EvidenceRef,
    SensitivityLabel,
)

pytestmark = pytest.mark.acceptance

POLL_INTERVAL = timedelta(milliseconds=50)
POLL_TIMEOUT = timedelta(seconds=60)
TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled"}

SOURCE_DATABASE = "classification_source"
READER_ROLE = "classification_reader"
SECRET_ENV = "STEWARD_CLASSIFICATION_SOURCE_DSN"

PROMPT_VERSION = "acceptance-classify@v1"
MODEL_ALIAS = "steward-classify"

# One table, three columns, and the sensitive one is sensitive for a reason a
# citation can point at: it is *named* `email`. The other two are `none`, which
# needs no evidence — so the proposal exercises both halves of the contract.
FIXTURE_ESTATE: tuple[str, ...] = (
    "CREATE SCHEMA sales",
    "CREATE TABLE sales.customers (id bigint NOT NULL, email text NOT NULL, city text)",
    "INSERT INTO sales.customers VALUES (1, 'ada@example.com', 'London')",
    "INSERT INTO sales.customers VALUES (2, 'grace@example.com', 'Baltimore')",
)

GRANT_READER: tuple[str, ...] = (
    "GRANT USAGE ON SCHEMA sales TO classification_reader",
    "GRANT SELECT ON ALL TABLES IN SCHEMA sales TO classification_reader",
)

ADD_COLUMN = "ALTER TABLE sales.customers ADD COLUMN phone text"

SOURCE_BODY: dict[str, Any] = {
    "name": "classification-warehouse",
    "engine": "postgres",
    "host": "classification.internal",
    "database": SOURCE_DATABASE,
    "dsn_secret_ref": f"env:{SECRET_ENV}",
    "include_schemas": ["sales"],
}

COUNT_REVIEWS = "SELECT count(*) FROM classification_reviews"
SELECT_REVIEW_AUDIT = (
    "SELECT action, before, after FROM audit_log "
    "WHERE entity_type = 'classification_proposal' ORDER BY id"
)
TRUNCATE_ALL = (
    "TRUNCATE runs, tasks, checkpoints, audit_log, sources, classification_reviews CASCADE"
)
RESET_ESTATE: tuple[str, ...] = ("DROP SCHEMA IF EXISTS sales CASCADE",)


class AcceptanceClassifier:
    """A `ColumnClassifier` whose answer is derived from the question.

    Every column of the request is classified, because the handler refuses a
    proposal that does not cover exactly the profiled columns — and, more to the
    point, because an asset that reads as classified with a column nobody
    assessed is the defect that guard exists for.

    Nothing here is written down. The column names come from the profile the
    real profiler produced, and the citation's locator is the column's own name
    as that profile stores it, so the evidence is resolved by the real resolver
    against the real profile. Hard-coding either would let this pass while the
    handler passed the classifier an empty request.
    """

    def __init__(self) -> None:
        self.requests: list[ClassificationRequest] = []
        self.runs: list[ClassificationRun] = []

    async def classify(
        self, run: ClassificationRun, request: ClassificationRequest
    ) -> ProposedClassification:
        self.requests.append(request)
        self.runs.append(run)
        return ProposedClassification(
            columns=tuple(
                self._column(column.name, request.profile_version)
                for column in request.profile.columns
            ),
            prompt_version=PROMPT_VERSION,
            model_alias=MODEL_ALIAS,
        )

    @staticmethod
    def _column(name: str, profile_version: int) -> ColumnClassification:
        if name != "email":
            return ColumnClassification(
                column_name=name,
                labels=(SensitivityLabel.NONE,),
                confidence=Decimal("0.99"),
            )
        return ColumnClassification(
            column_name=name,
            labels=(SensitivityLabel.PII,),
            confidence=Decimal("0.95"),
            evidence=(
                EvidenceRef(
                    profile_version=profile_version,
                    column_name=name,
                    kind=EvidenceKind.COLUMN_NAME,
                    locator=name,
                    detail="the column is named 'email', which holds direct contact identifiers",
                ),
            ),
        )


@pytest.fixture(scope="session")
def server() -> Iterator[pgserver.PostgresServer]:
    with tempfile.TemporaryDirectory(prefix="steward-m1-classify") as data_dir:
        instance = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            yield instance
        finally:
            instance.cleanup()


@pytest.fixture(scope="session")
def dsn(server: pgserver.PostgresServer) -> str:
    """Steward's own database, migrated by the queue's own migrations."""
    uri: str = server.get_uri()
    upgrade_to_head(uri)
    return uri


@pytest.fixture(scope="session")
def source_admin_dsn(server: pgserver.PostgresServer) -> str:
    """The customer database, and a role that may only read it (I5)."""
    server.psql(f"DROP DATABASE IF EXISTS {SOURCE_DATABASE}")
    server.psql(f"DROP ROLE IF EXISTS {READER_ROLE}")
    server.psql(f"CREATE DATABASE {SOURCE_DATABASE}")
    server.psql(f"CREATE ROLE {READER_ROLE} LOGIN")
    uri: str = server.get_uri(database=SOURCE_DATABASE)
    with psycopg.connect(uri, autocommit=True) as conn:
        for statement in (*FIXTURE_ESTATE, *GRANT_READER):
            conn.execute(statement)
    return uri


@pytest.fixture(scope="session")
def source_dsn(source_admin_dsn: str) -> str:
    parts = urlsplit(source_admin_dsn)
    host = parts.netloc.split("@")[-1]
    return urlunsplit((parts.scheme, f"{READER_ROLE}@{host}", parts.path, parts.query, parts.fragment))


@pytest.fixture(scope="session", autouse=True)
def secret_store(source_dsn: str) -> Iterator[None]:
    """The deployment's secret store, which for M1 is the environment.

    Set on the process rather than injected: the handlers the worker dispatches
    to are the *registered* ones, built with the default `EnvSecretResolver`, and
    wiring a different resolver here would prove the scenario against handlers no
    deployment runs.
    """
    os.environ[SECRET_ENV] = source_dsn
    yield
    del os.environ[SECRET_ENV]


@pytest.fixture
def classifier() -> Iterator[AcceptanceClassifier]:
    """Bind the capability for the duration of one scenario.

    Through `CLASSIFIER.overridden`, which is what the module's own
    single-assignment provider offers a test — `provide_classifier` refuses a
    second call by design, so a per-test binding has to restore rather than
    accumulate.
    """
    stub = AcceptanceClassifier()
    with CLASSIFIER.overridden(stub):
        yield stub


@pytest.fixture(autouse=True)
def clean(dsn: str, source_admin_dsn: str) -> Iterator[None]:
    """Both databases back to their starting state before every scenario."""
    with connect(dsn) as connection:
        connection.execute(TRUNCATE_ALL)
        connection.commit()
    with psycopg.connect(source_admin_dsn, autocommit=True) as connection:
        for statement in (*RESET_ESTATE, *FIXTURE_ESTATE, *GRANT_READER):
            connection.execute(statement)
    yield


@pytest.fixture
def client(dsn: str) -> Iterator[TestClient]:
    app = create_app(PostgresRunStore(dsn), PostgresCatalogStore(dsn))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def conn(dsn: str) -> Iterator[QueueConnection]:
    connection = connect(dsn)
    try:
        yield connection
    finally:
        connection.close()


def drain(client: TestClient, dsn: str, run_id: str) -> dict[str, Any]:
    """Run a worker and poll the API until the run stops moving.

    The worker claims every registered type, `classify_asset` included, because
    the classifier fixture bound the capability. That a worker *without* one
    narrows its claim list instead is the composition root's rule, and it is
    proven where that root lives (`services/workers/tests/test_worker_capabilities.py`,
    PROOFS row 111) rather than here — this service's tests may not import that
    one (I4: services do not import each other), and an import in a test tree is
    invisible to S1 because import-linter scans `src/` only.
    """
    worker = Worker(dsn, "m1-classification-worker", task_types=registered_types())
    deadline = time.monotonic() + POLL_TIMEOUT.total_seconds()
    while time.monotonic() < deadline:
        asyncio.run(worker.run_once())
        body: dict[str, Any] = client.get(f"/v1/runs/{run_id}").json()
        if body["status"] in TERMINAL_RUN_STATES:
            return body
        time.sleep(POLL_INTERVAL.total_seconds())
    raise AssertionError(f"run {run_id} never reached a terminal state")


def start(client: TestClient, dsn: str, goal: str, payload: dict[str, Any]) -> dict[str, Any]:
    """`POST /v1/runs`, then run it to completion. The whole path, every time."""
    accepted = client.post("/v1/runs", json={"goal": goal, "payload": payload})
    assert accepted.status_code == 202, accepted.text
    finished = drain(client, dsn, accepted.json()["id"])
    assert finished["status"] == "succeeded", finished
    return finished


@pytest.fixture
def asset_id(client: TestClient, dsn: str) -> str:
    """`sales.customers`, registered, scanned and profiled through the API.

    Not inserted: every row this scenario later classifies was written by the
    real scan and the real profiler reading the real source over a read-only
    role.
    """
    created = client.post("/v1/sources", json=SOURCE_BODY)
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    scan = client.post(f"/v1/sources/{source_id}/scan")
    assert scan.status_code == 202
    assert drain(client, dsn, scan.json()["id"])["status"] == "succeeded"

    assets = client.get("/v1/assets", params={"source": source_id}).json()["items"]
    [customers] = [a for a in assets if a["fqn"].endswith(".customers")]
    identifier: str = customers["id"]

    start(client, dsn, "profile_asset", {"asset_id": identifier})
    return identifier


@pytest.fixture
def proposal(
    client: TestClient, dsn: str, asset_id: str, classifier: AcceptanceClassifier
) -> dict[str, Any]:
    """One classification of that asset, through the queue, awaiting review."""
    start(client, dsn, "classify_asset", {"asset_id": asset_id, "profile_version": 1})
    history = client.get(f"/v1/assets/{asset_id}/classifications").json()["items"]
    assert len(history) == 1, history
    return dict(history[0])


def test_the_exit_criterion_whole(
    client: TestClient,
    dsn: str,
    asset_id: str,
    classifier: AcceptanceClassifier,
) -> None:
    """Profile → agent → pending review → approval → published version.

    Read as one story, because that is what the milestone claims: a client
    starts a classification, a worker runs the agent, the result waits for a
    person, the person approves it, and only then does the asset have a
    published classification.
    """
    run = start(client, dsn, "classify_asset", {"asset_id": asset_id, "profile_version": 1})
    # I12: the run carries `classify_asset`'s cap, the first in the registry with
    # tokens and cost on it. Usage is *not* asserted to be non-zero here and the
    # distinction is worth stating: the handler reports nothing spent because the
    # model call's spend is the bound classifier's to charge, inside its own
    # checkpoint transaction — and this classifier is a stub that reaches no
    # model, so zero is the true answer rather than a missing measurement. What
    # a real gateway charges is H4's subject and the live smoke test's.
    assert run["budget"]["steps"] == 6
    assert run["budget"]["tokens"] == 120_000
    assert run["usage"]["steps"] == 0

    # The agent ran, and what it was given was the profile of the real table.
    [request] = classifier.requests
    assert [column.name for column in request.profile.columns] == ["id", "email", "city"]
    assert request.profile.row_count == 2

    # Its result is waiting for a person, and the asset has no answer yet.
    [pending] = client.get(f"/v1/assets/{asset_id}/classifications").json()["items"]
    assert pending["status"] == "pending_review"
    assert pending["version"] == 1
    unpublished = client.get(f"/v1/assets/{asset_id}/classification")
    assert unpublished.status_code == 404
    assert "no approved classification" in unpublished.json()["detail"]

    # A reviewer reads it: labels, confidence, evidence, provenance, no history.
    detail = client.get(f"/v1/reviews/{pending['id']}").json()
    assert detail["reviews"] == []
    labelled = {c["column_name"]: c for c in detail["classification"]["proposal"]["columns"]}
    assert set(labelled) == {"id", "email", "city"}, "every profiled column must be assessed"
    assert labelled["email"]["labels"] == ["pii"]
    assert labelled["city"]["labels"] == ["none"]
    [citation] = labelled["email"]["evidence"]
    assert (citation["kind"], citation["locator"], citation["profile_version"]) == (
        "column_name",
        "email",
        1,
    )
    assert detail["classification"]["proposal"]["prompt_version"] == PROMPT_VERSION
    assert detail["classification"]["proposal"]["model_alias"] == MODEL_ALIAS
    assert detail["classification"]["trace_id"] == run["trace_id"]

    # The person approves.
    approved = client.post(
        f"/v1/reviews/{pending['id']}:approve",
        json={"reason": "the citation resolves to the profiled column"},
        headers={"Idempotency-Key": "acceptance-approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    # Only now does the asset have a published classification, and it is that one.
    published = client.get(f"/v1/assets/{asset_id}/classification")
    assert published.status_code == 200
    assert published.json()["id"] == pending["id"]
    assert published.json()["status"] == "approved"

    # And who decided it, when, and why is on the record.
    reviewed = client.get(f"/v1/reviews/{pending['id']}").json()
    [decision] = reviewed["reviews"]
    assert decision["outcome"] == "approved"
    assert decision["actor_kind"] == "human"
    assert decision["actor_id"] == "api"
    assert decision["reason"] == "the citation resolves to the profiled column"
    assert decision["policy_id"] is None
    assert decision["decided_at"] is not None


def test_an_unreviewed_proposal_is_never_the_assets_answer(
    client: TestClient, asset_id: str, proposal: dict[str, Any]
) -> None:
    """The gate, isolated (SPEC §3.3).

    Stated separately from the story above because it is the property the whole
    lifecycle exists to keep: there is no request that returns an unreviewed
    classification as an asset's published one. The proposal is readable — it is
    in the history and at its own review URL — and the asset still answers 404.
    """
    assert proposal["status"] == "pending_review"
    assert client.get(f"/v1/reviews/{proposal['id']}").status_code == 200
    assert client.get(f"/v1/assets/{asset_id}/classification").status_code == 404


def test_a_rejected_proposal_is_not_published_either(
    client: TestClient, asset_id: str, proposal: dict[str, Any]
) -> None:
    """Rejection records the decision and publishes nothing."""
    rejected = client.post(
        f"/v1/reviews/{proposal['id']}:reject",
        json={"reason": "the confidence does not justify the label"},
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert client.get(f"/v1/assets/{asset_id}/classification").status_code == 404
    [decision] = client.get(f"/v1/reviews/{proposal['id']}").json()["reviews"]
    assert decision["outcome"] == "rejected"
    # The version is kept, not deleted: an append-only table is how "why is this
    # column not labelled PII" stays answerable.
    [version] = client.get(f"/v1/assets/{asset_id}/classifications").json()["items"]
    assert version["status"] == "rejected"


def test_a_decided_proposal_cannot_be_decided_again(
    client: TestClient, proposal: dict[str, Any]
) -> None:
    """The second decision is refused with a type a client can act on."""
    first = client.post(f"/v1/reviews/{proposal['id']}:approve", json={"reason": "correct"})
    assert first.status_code == 200

    second = client.post(f"/v1/reviews/{proposal['id']}:reject", json={"reason": "on reflection"})

    assert second.status_code == 409
    assert second.json()["type"] == "urn:steward:proposal-not-pending"
    assert client.get(f"/v1/assets/{proposal['asset_id']}/classification").json()["status"] == (
        "approved"
    )


def test_replaying_a_decision_under_its_key_records_one_review(
    client: TestClient, conn: QueueConnection, proposal: dict[str, Any]
) -> None:
    """A retried approval is the same approval, not a second one.

    Counted in the review *table* rather than in the response, because the
    response would look identical either way — the failure this catches is two
    audit rows for one governance decision.
    """
    headers = {"Idempotency-Key": "acceptance-replay"}
    body = {"reason": "the citation resolves"}

    first = client.post(f"/v1/reviews/{proposal['id']}:approve", json=body, headers=headers)
    second = client.post(f"/v1/reviews/{proposal['id']}:approve", json=body, headers=headers)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json()
    assert conn.execute(COUNT_REVIEWS).fetchone() == (1,)
    conn.rollback()


def test_the_same_key_cannot_carry_the_opposite_decision(
    client: TestClient, conn: QueueConnection, proposal: dict[str, Any]
) -> None:
    """Approve and reject share one key index; conflating them publishes.

    Without this refusal the rejection would return the *approved* record and a
    reviewer would read 200 while nothing they asked for had happened.
    """
    headers = {"Idempotency-Key": "acceptance-opposite"}
    client.post(f"/v1/reviews/{proposal['id']}:approve", json={"reason": "yes"}, headers=headers)

    reversed_decision = client.post(
        f"/v1/reviews/{proposal['id']}:reject", json={"reason": "yes"}, headers=headers
    )

    assert reversed_decision.status_code == 409
    assert reversed_decision.json()["type"] == "urn:steward:idempotency-key-reused"
    assert conn.execute(COUNT_REVIEWS).fetchone() == (1,)
    conn.rollback()


def test_a_new_profile_supersedes_the_published_version_in_one_action(
    client: TestClient,
    dsn: str,
    conn: QueueConnection,
    asset_id: str,
    proposal: dict[str, Any],
    source_admin_dsn: str,
) -> None:
    """Re-profiling and re-classifying produces a new version that replaces the
    old one on approval — and the asset is never left with none.

    The supersession is the point (SPEC §13 D14): approving version 2 demotes
    version 1 in the same transaction, so there is no window in which the asset
    has no published classification and no second operator action required to
    leave one.
    """
    approved_first = client.post(
        f"/v1/reviews/{proposal['id']}:approve", json={"reason": "correct for now"}
    )
    assert approved_first.status_code == 200

    with psycopg.connect(source_admin_dsn, autocommit=True) as source:
        source.execute(ADD_COLUMN)
    start(client, dsn, "scan_source", {"source_id": _source_of(client, asset_id)})
    start(client, dsn, "profile_asset", {"asset_id": asset_id})
    start(client, dsn, "classify_asset", {"asset_id": asset_id, "profile_version": 2})

    versions = client.get(f"/v1/assets/{asset_id}/classifications").json()["items"]
    assert [(v["version"], v["status"]) for v in versions] == [
        (2, "pending_review"),
        (1, "approved"),
    ]
    # The published version is still the old one until someone decides.
    assert client.get(f"/v1/assets/{asset_id}/classification").json()["version"] == 1

    newest = versions[0]
    assert client.post(
        f"/v1/reviews/{newest['id']}:approve", json={"reason": "covers the new column"}
    ).status_code == 200

    published = client.get(f"/v1/assets/{asset_id}/classification").json()
    assert published["version"] == 2
    assert {c["column_name"] for c in published["proposal"]["columns"]} == {
        "id",
        "email",
        "city",
        "phone",
    }
    # One approved row, not two: the incumbent was demoted, not left beside it.
    assert [(v["version"], v["status"]) for v in
            client.get(f"/v1/assets/{asset_id}/classifications").json()["items"]] == [
        (2, "approved"),
        (1, "superseded"),
    ]


def test_every_status_change_is_audited(
    client: TestClient, conn: QueueConnection, proposal: dict[str, Any]
) -> None:
    """I7: the row and the audit entry are one write.

    The agent proposed and a human approved, and the trail says so — with the
    proposal attributed to the task that made it and the publication to the
    person who decided it.
    """
    client.post(f"/v1/reviews/{proposal['id']}:approve", json={"reason": "correct"})

    rows = conn.execute(SELECT_REVIEW_AUDIT).fetchall()
    conn.rollback()

    assert [row[0] for row in rows] == ["classification.proposed", "classification.approved"]
    assert rows[0][2]["status"] == "pending_review"
    assert rows[1][1]["status"] == "pending_review"
    assert rows[1][2]["status"] == "approved"


def _source_of(client: TestClient, asset_id: str) -> str:
    source: str = client.get(f"/v1/assets/{asset_id}").json()["asset"]["source_id"]
    return source
