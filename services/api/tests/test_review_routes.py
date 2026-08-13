"""The review endpoints' own job: translation, and nothing else (#50 step 7).

What this file tests is the layer, not the lifecycle. Whether a proposal may be
approved, what a decision supersedes and how a replayed key resolves are
`steward_catalog.classification`'s decisions, tested against a real Postgres in
`test_classification_lifecycle.py`, and proven end to end through the API in
`test_acceptance_m1_classification.py`. What is left here is the part only the
HTTP layer can get wrong:

* every refusal reaches the client as *its own* problem type, so a client can
  tell "already decided" from "the data changed underneath this" from "your key
  means something else" -- rather than as one 409 or, worse, a 500;
* `:approve` calls approve and `:reject` calls reject, which is the one thing a
  shared translation helper could silently get wrong;
* the `Idempotency-Key` header reaches the store, because a key that is quietly
  dropped makes every retry a fresh decision;
* the two enums either side of the `steward-schemas`/`steward-queue` boundary
  agree, since neither package may import the other.

The store here is a stub that raises on command. That is the point -- a real
one cannot be made to produce every conflict on demand -- but it is also the
risk, so every negative below has a positive beside it: a stub that raised on
*every* call would satisfy each rejection test on its own.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from steward_api.app import create_app
from steward_api.catalog import (
    AssetNotFound,
    InMemoryCatalogStore,
    classification_response,
    review_response,
)
from steward_catalog.classification import (
    AssetNotClassifiable,
    ClassificationConflict,
    IdempotencyKeyReused,
    ProposalNotPending,
    ProposalRecord,
    ReviewRecord,
    StaleProposal,
)
from steward_queue import Actor, ActorKind
from steward_schemas import (
    Classification,
    ClassificationHistory,
    ClassificationProposal,
    ColumnClassification,
    EvidenceKind,
    EvidenceRef,
    ProposalStatus,
    ReviewerKind,
    ReviewOutcome,
    ReviewRequest,
    SensitivityLabel,
)

PROPOSAL_ID = UUID("99999999-9999-9999-9999-999999999999")
ASSET_ID = UUID("88888888-8888-8888-8888-888888888888")
RUN_ID = UUID("77777777-7777-7777-7777-777777777777")
TASK_ID = UUID("66666666-6666-6666-6666-666666666666")
REVIEW_ID = UUID("55555555-5555-5555-5555-555555555555")
NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)

A_REASON = {"reason": "the evidence resolves"}


def a_proposal(asset_id: UUID = ASSET_ID) -> ClassificationProposal:
    return ClassificationProposal(
        asset_id=asset_id,
        profile_version=3,
        prompt_version="classify_asset@v1",
        model_alias="steward-classify",
        columns=(
            ColumnClassification(
                column_name="email",
                labels=(SensitivityLabel.PII,),
                confidence=Decimal("0.95"),
                evidence=(
                    EvidenceRef(
                        profile_version=3,
                        column_name="email",
                        kind=EvidenceKind.COLUMN_NAME,
                        locator="email",
                        detail="the column is named 'email'",
                    ),
                ),
            ),
        ),
    )


def a_record(status: ProposalStatus = ProposalStatus.PENDING_REVIEW) -> ProposalRecord:
    return ProposalRecord(
        id=PROPOSAL_ID,
        asset_id=ASSET_ID,
        version=2,
        profile_version=3,
        prompt_version="classify_asset@v1",
        model_alias="steward-classify",
        status=status,
        proposal=a_proposal(),
        run_id=RUN_ID,
        task_id=TASK_ID,
        trace_id="0123456789abcdef0123456789abcdef",
        created_at=NOW,
    )


def a_review(kind: ActorKind = ActorKind.HUMAN, policy_id: str | None = None) -> ReviewRecord:
    return ReviewRecord(
        id=REVIEW_ID,
        proposal_id=PROPOSAL_ID,
        outcome=ReviewOutcome.APPROVED,
        actor=Actor(kind=kind, id=policy_id or "api"),
        reason="the evidence resolves",
        policy_id=policy_id,
        decided_at=NOW,
    )


class StubStore(InMemoryCatalogStore):
    """A `CatalogStore` that answers reviews from a script and records the ask.

    Inherits the in-memory store so the source and asset halves of the protocol
    stay real; only the classification methods are scripted.
    """

    def __init__(self, *, raises: Exception | None = None) -> None:
        super().__init__()
        self._raises = raises
        self.calls: list[tuple[str, UUID, str, str | None]] = []
        self.current: Classification | None = None
        self.history: ClassificationHistory | None = None

    async def approve_classification(
        self, proposal_id: UUID, request: ReviewRequest, idempotency_key: str | None
    ) -> Classification:
        return self._decide("approve", proposal_id, request, idempotency_key)

    async def reject_classification(
        self, proposal_id: UUID, request: ReviewRequest, idempotency_key: str | None
    ) -> Classification:
        return self._decide("reject", proposal_id, request, idempotency_key)

    async def current_classification(self, asset_id: UUID) -> Classification | None:
        if self._raises is not None:
            raise self._raises
        return self.current

    async def classification_history(self, asset_id: UUID) -> ClassificationHistory:
        if self._raises is not None:
            raise self._raises
        assert self.history is not None
        return self.history

    def _decide(
        self, verb: str, proposal_id: UUID, request: ReviewRequest, idempotency_key: str | None
    ) -> Classification:
        reason = request.reason
        self.calls.append((verb, proposal_id, reason, idempotency_key))
        if self._raises is not None:
            raise self._raises
        status = ProposalStatus.APPROVED if verb == "approve" else ProposalStatus.REJECTED
        return classification_response(a_record(status))


def client_for(store: StubStore) -> Iterator[TestClient]:
    with TestClient(create_app(catalog_store=store)) as test_client:
        yield test_client


@pytest.fixture
def store() -> StubStore:
    return StubStore()


@pytest.fixture
def review_client(store: StubStore) -> Iterator[TestClient]:
    yield from client_for(store)


class TestDecisions:
    """The happy paths. Every refusal test below is meaningless without these:
    a store that refused everything would pass all of them."""

    def test_approving_publishes_and_answers_with_the_published_version(
        self, review_client: TestClient, store: StubStore
    ) -> None:
        response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:approve", json=A_REASON)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert body["id"] == str(PROPOSAL_ID)
        assert body["version"] == 2
        assert store.calls == [("approve", PROPOSAL_ID, A_REASON["reason"], None)]

    def test_rejecting_calls_reject_and_answers_rejected(
        self, review_client: TestClient, store: StubStore
    ) -> None:
        """The one mistake a shared translation helper invites.

        Both verbs go through one error-translating wrapper, so nothing but this
        distinguishes them: if `:reject` were wired to `approve_classification`
        the endpoint would still answer 200 with a plausible body, and it would
        publish the classification it was asked to refuse.
        """
        response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:reject", json=A_REASON)

        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        assert store.calls == [("reject", PROPOSAL_ID, A_REASON["reason"], None)]

    def test_the_idempotency_key_reaches_the_store(
        self, review_client: TestClient, store: StubStore
    ) -> None:
        """A dropped key is invisible until the day a retry decides twice."""
        review_client.post(
            f"/v1/reviews/{PROPOSAL_ID}:approve",
            json=A_REASON,
            headers={"Idempotency-Key": "review-42"},
        )

        assert store.calls == [("approve", PROPOSAL_ID, A_REASON["reason"], "review-42")]

    def test_the_response_carries_the_evidence_and_the_provenance(
        self, review_client: TestClient
    ) -> None:
        """What a reviewer reads must include what they are meant to check."""
        body = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:approve", json=A_REASON).json()

        assert body["trace_id"] == "0123456789abcdef0123456789abcdef"
        assert body["run_id"] == str(RUN_ID)
        assert body["proposal"]["prompt_version"] == "classify_asset@v1"
        assert body["proposal"]["model_alias"] == "steward-classify"
        [column] = body["proposal"]["columns"]
        assert column["labels"] == ["pii"]
        assert column["evidence"][0]["locator"] == "email"

    def test_a_decision_without_a_reason_is_refused(self, review_client: TestClient) -> None:
        """SPEC §8 exports every rejection with its reason as eval data; a
        decision with nothing to say is that signal thrown away."""
        response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:reject", json={"reason": ""})

        assert response.status_code == 422
        assert response.json()["type"] == "urn:steward:validation-error"


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [
        (ProposalNotPending("already decided"), "urn:steward:proposal-not-pending"),
        (StaleProposal("the profile moved on"), "urn:steward:proposal-stale"),
        (AssetNotClassifiable("the asset is gone"), "urn:steward:asset-not-classifiable"),
        (IdempotencyKeyReused("that key settled something else"), "urn:steward:idempotency-key-reused"),
        (ClassificationConflict("another decision won"), "urn:steward:classification-conflict"),
    ],
    ids=["not-pending", "stale", "inactive-asset", "key-reused", "unnamed-conflict"],
)
@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_each_refusal_reaches_the_client_as_its_own_409(
    error: Exception, expected_type: str, verb: str
) -> None:
    """One status, five meanings, and a client can act on the difference.

    The last row is the fallback and the reason it is a `dict.get` with a
    default: a conflict the route has never heard of is still a 409 naming the
    proposal, not a 500 naming nothing.
    """
    store = StubStore(raises=error)
    for review_client in client_for(store):
        response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:{verb}", json=A_REASON)

    assert response.status_code == 409
    body = response.json()
    assert body["type"] == expected_type
    assert body["instance"] == f"/v1/reviews/{PROPOSAL_ID}"
    assert body["detail"] == str(error)


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_deciding_a_proposal_that_does_not_exist_is_a_404(verb: str) -> None:
    store = StubStore(raises=LookupError(f"no such proposal: {PROPOSAL_ID}"))
    for review_client in client_for(store):
        response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:{verb}", json=A_REASON)

    assert response.status_code == 404
    assert response.json()["type"] == "urn:steward:not-found"


class TestReads:
    def test_an_unknown_proposal_is_a_404_not_an_empty_document(
        self, review_client: TestClient
    ) -> None:
        response = review_client.get(f"/v1/reviews/{uuid4()}")

        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"

    def test_an_unknown_asset_and_an_unclassified_one_are_different_answers(self) -> None:
        """Both are 404s and they must not be the same 404.

        A client that cannot tell a mistyped id from an asset nobody has
        classified yet retries the typo forever.
        """
        unknown = StubStore(raises=AssetNotFound(ASSET_ID))
        for review_client in client_for(unknown):
            missing_asset = review_client.get(f"/v1/assets/{ASSET_ID}/classification")

        unclassified = StubStore()  # the asset exists; `current` stays None
        for review_client in client_for(unclassified):
            no_classification = review_client.get(f"/v1/assets/{ASSET_ID}/classification")

        assert missing_asset.status_code == no_classification.status_code == 404
        assert "no asset with id" in missing_asset.json()["detail"]
        assert "has no approved classification" in no_classification.json()["detail"]

    def test_a_scanned_asset_with_no_proposals_is_an_empty_history_not_a_404(self) -> None:
        """200 and an empty list: the asset exists and has no classifications.

        The negative case above is what gives this one meaning -- if both
        answered 404 the endpoint would be indistinguishable from one that
        cannot find anything at all.
        """
        store = StubStore()
        store.history = ClassificationHistory(items=())
        for review_client in client_for(store):
            response = review_client.get(f"/v1/assets/{ASSET_ID}/classifications")

        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_the_history_carries_every_version_newest_first(self) -> None:
        """Named versions and statuses, not a count.

        A length assertion agrees with itself whenever the store drops one
        version and repeats another -- which is exactly the shape a paging or
        ordering bug produces.
        """
        store = StubStore()
        newest = classification_response(a_record(ProposalStatus.PENDING_REVIEW))
        published = classification_response(a_record(ProposalStatus.APPROVED)).model_copy(
            update={"id": uuid4(), "version": 1}
        )
        store.history = ClassificationHistory(items=(newest, published))
        for review_client in client_for(store):
            body = review_client.get(f"/v1/assets/{ASSET_ID}/classifications").json()

        assert [(item["version"], item["status"]) for item in body["items"]] == [
            (2, "pending_review"),
            (1, "approved"),
        ]


class TestProjection:
    """The row-to-contract projections, which nothing else asserts directly."""

    def test_a_review_keeps_the_actor_the_repository_recorded(self) -> None:
        published = review_response(a_review())

        assert published.actor_kind is ReviewerKind.HUMAN
        assert published.actor_id == "api"
        assert published.policy_id is None

    def test_a_policy_decision_keeps_its_policy(self) -> None:
        published = review_response(a_review(ActorKind.POLICY, policy_id="auto-approve-none"))

        assert published.actor_kind is ReviewerKind.POLICY
        assert published.policy_id == "auto-approve-none"

    def test_the_two_actor_enums_have_not_drifted_apart(self) -> None:
        """`ReviewerKind` duplicates `ActorKind` because `steward-schemas` may
        not import `steward-queue` (I4), and a duplicate nobody checks is a
        duplicate that drifts.

        The projection converts by *value*, so a kind added to the queue and not
        to the contract would reach a client as a `ValueError` at serialisation
        time -- or, if the conversion were ever relaxed, as a string no
        generated client knows. This fails at the moment of divergence instead.
        """
        assert {kind.value for kind in ReviewerKind} == {kind.value for kind in ActorKind}

    def test_every_actor_kind_projects(self) -> None:
        """The parity assertion above compares sets; this proves the conversion
        actually runs for each one."""
        for kind in ActorKind:
            policy = "some-policy" if kind is ActorKind.POLICY else None
            assert review_response(a_review(kind, policy_id=policy)).actor_kind.value == kind.value
