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

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from steward_api.app import create_app
from steward_api.auth import (
    API_KEY_SCHEME_NAME,
    ApiKeyRegistry,
    MalformedApiKeys,
    Principal,
)
from steward_api.catalog import (
    AssetNotFound,
    InMemoryCatalogStore,
    classification_response,
    review_response,
)
from steward_api.problem_details import API_KEY_HEADER, ProblemDetailsError
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

REVIEWER, REVIEWER_KEY = "alice", "alice-secret-value"
SECOND_REVIEWER, SECOND_KEY = "bob", "bob-secret-value"
KEYS = ApiKeyRegistry({REVIEWER: REVIEWER_KEY, SECOND_REVIEWER: SECOND_KEY})
AUTH = {API_KEY_HEADER: REVIEWER_KEY}

OPENAPI_SNAPSHOT = Path(__file__).resolve().parents[3] / "contracts" / "openapi.json"
"""The committed contract, so a stale snapshot is a failure here too."""


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
        self.calls: list[tuple[str, UUID, str, str | None, str]] = []
        self.current: Classification | None = None
        self.history: ClassificationHistory | None = None

    async def approve_classification(
        self,
        proposal_id: UUID,
        request: ReviewRequest,
        idempotency_key: str | None,
        *,
        reviewer: Principal,
    ) -> Classification:
        return self._decide("approve", proposal_id, request, idempotency_key, reviewer)

    async def reject_classification(
        self,
        proposal_id: UUID,
        request: ReviewRequest,
        idempotency_key: str | None,
        *,
        reviewer: Principal,
    ) -> Classification:
        return self._decide("reject", proposal_id, request, idempotency_key, reviewer)

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
        self,
        verb: str,
        proposal_id: UUID,
        request: ReviewRequest,
        idempotency_key: str | None,
        reviewer: Principal,
    ) -> Classification:
        self.calls.append((verb, proposal_id, request.reason, idempotency_key, reviewer.id))
        if self._raises is not None:
            raise self._raises
        status = ProposalStatus.APPROVED if verb == "approve" else ProposalStatus.REJECTED
        return classification_response(a_record(status))


def client_for(store: StubStore, api_keys: ApiKeyRegistry = KEYS) -> Iterator[TestClient]:
    with TestClient(create_app(catalog_store=store, api_keys=api_keys)) as test_client:
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
        response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:approve", json=A_REASON, headers=AUTH)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert body["id"] == str(PROPOSAL_ID)
        assert body["version"] == 2
        assert store.calls == [("approve", PROPOSAL_ID, A_REASON["reason"], None, REVIEWER)]

    def test_rejecting_calls_reject_and_answers_rejected(
        self, review_client: TestClient, store: StubStore
    ) -> None:
        """The one mistake a shared translation helper invites.

        Both verbs go through one error-translating wrapper, so nothing but this
        distinguishes them: if `:reject` were wired to `approve_classification`
        the endpoint would still answer 200 with a plausible body, and it would
        publish the classification it was asked to refuse.
        """
        response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:reject", json=A_REASON, headers=AUTH)

        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        assert store.calls == [("reject", PROPOSAL_ID, A_REASON["reason"], None, REVIEWER)]

    def test_the_idempotency_key_reaches_the_store(
        self, review_client: TestClient, store: StubStore
    ) -> None:
        """A dropped key is invisible until the day a retry decides twice."""
        review_client.post(
            f"/v1/reviews/{PROPOSAL_ID}:approve",
            json=A_REASON,
            headers={**AUTH, "Idempotency-Key": "review-42"},
        )

        assert store.calls == [("approve", PROPOSAL_ID, A_REASON["reason"], "review-42", REVIEWER)]

    def test_the_response_carries_the_evidence_and_the_provenance(
        self, review_client: TestClient
    ) -> None:
        """What a reviewer reads must include what they are meant to check."""
        body = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:approve", json=A_REASON, headers=AUTH).json()

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
        response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:reject", json={"reason": ""}, headers=AUTH)

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
        response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:{verb}", json=A_REASON, headers=AUTH)

    assert response.status_code == 409
    body = response.json()
    assert body["type"] == expected_type
    assert body["instance"] == f"/v1/reviews/{PROPOSAL_ID}"
    assert body["detail"] == str(error)


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_deciding_a_proposal_that_does_not_exist_is_a_404(verb: str) -> None:
    store = StubStore(raises=LookupError(f"no such proposal: {PROPOSAL_ID}"))
    for review_client in client_for(store):
        response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:{verb}", json=A_REASON, headers=AUTH)

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


class TestAuthentication:
    """A decision is recorded against whoever proved they may make it (SPEC §2).

    This is not a generic auth suite: it is the set of ways an unauthenticated
    or misattributed decision could still be recorded. The repository refuses a
    caller-supplied actor so that the credential is the *only* thing that can
    say who approved a classification — which makes these tests the other half
    of that guarantee.
    """

    @pytest.mark.parametrize("verb", ["approve", "reject"])
    def test_a_decision_without_a_credential_is_refused(
        self, store: StubStore, verb: str
    ) -> None:
        for review_client in client_for(store):
            response = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:{verb}", json=A_REASON)

        assert response.status_code == 401
        assert response.json()["type"] == "urn:steward:unauthenticated"
        assert "ApiKey" in response.headers["WWW-Authenticate"]
        # Nothing reached the store: an unauthenticated request must not decide.
        assert store.calls == []

    @pytest.mark.parametrize(
        "presented",
        ["wrong-secret-value", REVIEWER_KEY[:-1], REVIEWER_KEY + "x", ""],
        ids=["unrelated", "prefix-of-a-valid-key", "valid-key-plus-a-character", "empty"],
    )
    def test_a_credential_this_deployment_does_not_accept_is_refused(
        self, store: StubStore, presented: str
    ) -> None:
        """Including the near-misses. A comparison that stopped at the first
        differing character would still reject these, but a check that
        `startswith` or truncated would not."""
        for review_client in client_for(store):
            response = review_client.post(
                f"/v1/reviews/{PROPOSAL_ID}:approve",
                json=A_REASON,
                headers={API_KEY_HEADER: presented},
            )

        assert response.status_code == 401
        assert store.calls == []

    def test_a_rejected_credential_is_never_echoed_or_described(
        self, store: StubStore
    ) -> None:
        """Two rules in one response.

        The secret must not come back — a body that echoed it would put it in
        the client's logs and any proxy in between (N7, the rule
        `sanitized_errors` applies to a rejected field). And "no key" and "wrong
        key" must read identically, because telling them apart tells an
        unauthenticated caller whether a guessed secret exists.
        """
        secret = "a-guessed-secret-value"
        for review_client in client_for(store):
            rejected = review_client.post(
                f"/v1/reviews/{PROPOSAL_ID}:approve",
                json=A_REASON,
                headers={API_KEY_HEADER: secret},
            )
            absent = review_client.post(f"/v1/reviews/{PROPOSAL_ID}:approve", json=A_REASON)

        assert secret not in rejected.text
        assert rejected.json() == absent.json()

    def test_an_unconfigured_deployment_accepts_nobody(self, store: StubStore) -> None:
        """Fail closed. An API with no credentials configured cannot record a
        decision on anyone's behalf, rather than recording every decision on
        behalf of nobody in particular."""
        for review_client in client_for(store, ApiKeyRegistry({})):
            response = review_client.post(
                f"/v1/reviews/{PROPOSAL_ID}:approve",
                json=A_REASON,
                headers={API_KEY_HEADER: REVIEWER_KEY},
            )

        assert response.status_code == 401
        assert store.calls == []

    def test_two_reviewers_are_two_actors(self, store: StubStore) -> None:
        """The point of the whole exercise: distinct keys attribute distinctly.

        Without this, every assertion above is satisfied by an implementation
        that authenticates correctly and then records `human:api` regardless.
        """
        for review_client in client_for(store):
            review_client.post(
                f"/v1/reviews/{PROPOSAL_ID}:approve",
                json=A_REASON,
                headers={API_KEY_HEADER: REVIEWER_KEY},
            )
            review_client.post(
                f"/v1/reviews/{PROPOSAL_ID}:reject",
                json=A_REASON,
                headers={API_KEY_HEADER: SECOND_KEY},
            )

        assert [(call[0], call[4]) for call in store.calls] == [
            ("approve", REVIEWER),
            ("reject", SECOND_REVIEWER),
        ]

    def test_a_credential_never_produces_a_policy_actor(self) -> None:
        """SPEC §3.3: an automatic approval must resolve to a configured policy.

        No configuration of this registry yields a `policy` principal, so an API
        key cannot be used to record "a policy approved this" — which the
        repository would then have to either trust or refuse on the strength of
        two free-form strings agreeing.
        """
        for identifier in (REVIEWER, SECOND_REVIEWER, "auto-approve-none"):
            assert Principal(id=identifier).actor.kind is ActorKind.HUMAN


class TestKeyConfiguration:
    """`STEWARD_API_KEYS` is parsed at the composition root, where a mistake is
    an operator's to see — and every mistake is refused rather than dropped."""

    def test_credentials_are_read_from_the_configured_value(self) -> None:
        registry = ApiKeyRegistry.from_env("alice:one-secret,bob:another-secret")

        assert registry.configured
        assert registry.principal("one-secret") == Principal(id="alice")
        assert registry.principal("another-secret") == Principal(id="bob")

    @pytest.mark.parametrize(
        "raw", [None, "", "   "], ids=["unset", "empty", "whitespace"]
    )
    def test_an_absent_configuration_authenticates_nobody(self, raw: str | None) -> None:
        registry = ApiKeyRegistry.from_env(raw)

        assert not registry.configured
        with pytest.raises(ProblemDetailsError):
            registry.principal("anything")

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("alice", "no-separator"),
            ("alice:", "no-secret"),
            (":secret", "no-id"),
            ("alice:one,alice:two", "duplicate-id"),
            ("alice:same,bob:same", "shared-secret"),
        ],
        ids=["no-separator", "no-secret", "no-id", "duplicate-id", "shared-secret"],
    )
    def test_a_malformed_configuration_is_refused_at_startup(self, raw: str, reason: str) -> None:
        """Refused, not skipped. A dropped entry leaves a reviewer quietly
        without a credential; a duplicate id or a shared secret makes an audit
        row unable to say which of two people acted."""
        with pytest.raises(MalformedApiKeys):
            ApiKeyRegistry.from_env(raw)

    def test_a_configuration_error_does_not_quote_the_secret(self) -> None:
        with pytest.raises(MalformedApiKeys) as raised:
            ApiKeyRegistry.from_env("this-entry-has-no-separator-and-is-secret")

        assert "this-entry-has-no-separator-and-is-secret" not in str(raised.value)


class TestPublishedSecurity:
    """The credential has to be *published* as a credential, not just enforced.

    SPEC §8 generates the SDK's types from `contracts/openapi.json`, so what the
    document says about authentication is what every generated client believes.
    A key described as an ordinary optional header describes an unsecured
    operation with a spare parameter: the client offers nowhere to configure a
    key, sends none, and every caller finds out by receiving a 401. Runtime was
    already correct when this was wrong, which is exactly why it needs its own
    test — no behavioural assertion could have caught it.
    """

    @pytest.fixture
    def schema(self) -> dict[str, Any]:
        return dict(create_app(api_keys=KEYS).openapi())

    def test_the_credential_is_published_as_a_security_scheme(
        self, schema: dict[str, Any]
    ) -> None:
        schemes = schema["components"]["securitySchemes"]

        scheme = schemes[API_KEY_SCHEME_NAME]
        assert (scheme["type"], scheme["in"], scheme["name"]) == ("apiKey", "header", API_KEY_HEADER)
        assert scheme["description"], "a generated client shows this to whoever configures the key"

    @pytest.mark.parametrize("verb", ["approve", "reject"])
    def test_both_decision_operations_require_it(self, schema: dict[str, Any], verb: str) -> None:
        operation = schema["paths"][f"/v1/reviews/{{proposal_id}}:{verb}"]["post"]

        assert operation["security"] == [{API_KEY_SCHEME_NAME: []}]

    @pytest.mark.parametrize("verb", ["approve", "reject"])
    def test_the_credential_is_not_also_an_ordinary_parameter(
        self, schema: dict[str, Any], verb: str
    ) -> None:
        """Published twice would be worse than published once wrongly: a
        generated client would send the key as a security credential *and*
        expose a nullable header field for it."""
        operation = schema["paths"][f"/v1/reviews/{{proposal_id}}:{verb}"]["post"]

        assert API_KEY_HEADER not in [p["name"] for p in operation.get("parameters", [])]

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/reviews/{proposal_id}",
            "/v1/assets/{asset_id}/classification",
            "/v1/assets/{asset_id}/classifications",
        ],
        ids=["review-detail", "current-classification", "history"],
    )
    def test_the_reads_do_not_claim_to_require_one(
        self, schema: dict[str, Any], path: str
    ) -> None:
        """The positive case's opposite, and the one that stops this test class
        from passing against an app that secured everything indiscriminately."""
        assert "security" not in schema["paths"][path]["get"]

    def test_the_committed_contract_says_the_same(self, schema: dict[str, Any]) -> None:
        """S6 diffs the snapshot for compatibility; this asserts the snapshot is
        not stale in the one respect a client's authentication depends on."""
        committed = json.loads(OPENAPI_SNAPSHOT.read_text())

        assert committed["components"]["securitySchemes"] == schema["components"]["securitySchemes"]
        for verb in ("approve", "reject"):
            path = f"/v1/reviews/{{proposal_id}}:{verb}"
            assert committed["paths"][path]["post"]["security"] == [{API_KEY_SCHEME_NAME: []}]
