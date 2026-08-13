"""`classify_asset` against a real Postgres and a deterministic classifier (#50).

The handler is the whole subject; the model is not. So the `ColumnClassifier`
bound here is a stub that returns what the test wants and records what it was
given — which is what makes two otherwise-untestable properties testable:

* **What the model may see is asserted on the real thing.** The asset is scanned
  and profiled out of the fixture estate, which contains planted canaries, so the
  request handed to the classifier is a *real* profile of real data rather than a
  hand-built model of one. A test that fed it a fixture profile would assert that
  the fixture had no canaries in it.
* **Refusals are asserted to happen before the model runs.** A stale profile
  version that failed *after* a model call would still be a failure, and still
  cost the run its budget. The stub counts its calls, so "refused before" is a
  measurement rather than a reading of the code.

Every rejection here has a positive case beside it: a guard that refuses invalid
input and a guard that refuses *everything* pass the same negative test, and this
repo has shipped the second one before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from steward_catalog import (
    CLASSIFIER,
    CLASSIFY_ASSET_SAMPLE_PAYLOAD,
    CLASSIFY_ASSET_TASK_TYPE,
    ClassificationRequest,
    ClassificationRun,
    ClassifierAlreadyBound,
    ClassifierBudgetExceeded,
    ClassifierFailed,
    ClassifierProvider,
    ClassifierUnbound,
    EnvSecretResolver,
    ProposedClassification,
    build_classify_asset,
    build_profile_asset,
    build_scan_source,
    classifier_bound,
    postgres_inspector,
    postgres_profiler,
    provide_classifier,
    register_source,
)
from steward_catalog.classification import proposal_history
from steward_queue import (
    SYSTEM_ACTOR,
    QueueConnection,
    TaskContext,
    UsageLedger,
    create_run,
)
from steward_schemas import (
    ColumnClassification,
    EvidenceKind,
    EvidenceRef,
    ProposalStatus,
    RunBudget,
    SensitivityLabel,
    SourceCreate,
    TaskResult,
    TaskSpec,
    TaskStatus,
)

pytestmark = pytest.mark.invariants

CLASSIFY_BUDGET = RunBudget(
    steps=6, tokens=120_000, cost_usd=Decimal("0.5"), wall_clock=timedelta(minutes=10)
)

SELECT_ASSET_ID = "SELECT id FROM assets WHERE schema_name = %(schema)s AND name = %(name)s"
RETIRE_ASSET = "UPDATE assets SET lifecycle = 'missing' WHERE id = %(id)s"
SELECT_PROPOSAL_AUDIT = (
    "SELECT action, after FROM audit_log WHERE entity_type = 'classification_proposal' ORDER BY id"
)

PROMPT_VERSION = "classify_asset@v1"
MODEL_ALIAS = "steward-classify"


@dataclass
class StubClassifier:
    """A `ColumnClassifier` that answers from a script and remembers the question.

    `seen` is what makes the data-boundary assertions possible: it is exactly
    what the handler decided a classifier may have, captured before anything
    renders it.
    """

    answer: Callable[[ClassificationRequest], ProposedClassification] | None = None
    fails_with: Exception | None = None
    seen: list[ClassificationRequest] = field(default_factory=list)
    runs: list[ClassificationRun] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return len(self.seen)

    async def classify(
        self, run: ClassificationRun, request: ClassificationRequest
    ) -> ProposedClassification:
        self.seen.append(request)
        self.runs.append(run)
        if self.fails_with is not None:
            raise self.fails_with
        assert self.answer is not None, "the stub was scripted with neither an answer nor a failure"
        return self.answer(request)


def cites_its_own_column(request: ClassificationRequest) -> ProposedClassification:
    """Label the `email` column `pii`, citing a fact the stored profile holds.

    The locator is read *out of the request* rather than written down here, so
    this answer stays resolvable when the fixture estate changes -- and so the
    test cannot accidentally assert against a citation the profile never had.
    """
    return ProposedClassification(
        columns=(
            ColumnClassification(
                column_name="email",
                labels=(SensitivityLabel.PII,),
                confidence=Decimal("0.95"),
                evidence=(
                    EvidenceRef(
                        profile_version=request.profile_version,
                        column_name="email",
                        kind=EvidenceKind.COLUMN_NAME,
                        locator="email",
                        detail="the column is named 'email'",
                    ),
                ),
            ),
        ),
        prompt_version=PROMPT_VERSION,
        model_alias=MODEL_ALIAS,
    )


def _ctx(conn: QueueConnection, spec: TaskSpec) -> TaskContext:
    return TaskContext(
        connection=conn,
        spec=spec,
        attempts=1,
        claimed_by="w-test",
        trace_id="trace-test",
        usage=UsageLedger(),
    )


@pytest.fixture
def scan_spec(conn: QueueConnection) -> Callable[[UUID], TaskSpec]:
    def factory(source_id: UUID) -> TaskSpec:
        run = create_run(conn, goal="scan_source", budget=CLASSIFY_BUDGET)
        conn.commit()
        return TaskSpec(
            task_id=uuid4(),
            run_id=run.id,
            task_type="scan_source",
            payload={"source_id": str(source_id)},
            budget=CLASSIFY_BUDGET,
            max_attempts=3,
        )

    return factory


@pytest.fixture
def asset_id(
    conn: QueueConnection,
    source_create: SourceCreate,
    resolver: EnvSecretResolver,
    scan_spec: Callable[[UUID], TaskSpec],
) -> UUID:
    """`sales.customers`, scanned and profiled for real.

    Profiled by the real profiler against the fixture estate, which is the point:
    the canaries planted in that table pass through masking on their way into
    this profile, so a test asserting they do not reach a classifier is asserting
    it about values that were really there.
    """
    source, _ = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()
    scan = asyncio.run(
        build_scan_source(resolver=resolver, inspect=postgres_inspector)(
            _ctx(conn, scan_spec(source.id))
        )
    )
    conn.commit()
    assert scan.status is TaskStatus.SUCCEEDED, scan.error

    row = conn.execute(SELECT_ASSET_ID, {"schema": "sales", "name": "customers"}).fetchone()
    assert row is not None, "the fixture estate has no sales.customers to classify"
    identifier: UUID = row[0]

    profiled = asyncio.run(
        build_profile_asset(resolver=resolver, profiler=postgres_profiler)(
            _ctx(conn, _spec(conn, "profile_asset", {"asset_id": str(identifier)}))
        )
    )
    conn.commit()
    assert profiled.status is TaskStatus.SUCCEEDED, profiled.error
    return identifier


def _spec(conn: QueueConnection, task_type: str, payload: dict[str, Any]) -> TaskSpec:
    run = create_run(conn, goal=task_type, budget=CLASSIFY_BUDGET)
    conn.commit()
    return TaskSpec(
        task_id=uuid4(),
        run_id=run.id,
        task_type=task_type,
        payload=payload,
        budget=CLASSIFY_BUDGET,
        max_attempts=3,
    )


def classify(
    conn: QueueConnection, asset: UUID, classifier: StubClassifier, *, profile_version: int = 1
) -> TaskResult:
    """Run the registered handler the way a worker would, in one transaction."""
    spec = _spec(
        conn,
        CLASSIFY_ASSET_TASK_TYPE,
        {"asset_id": str(asset), "profile_version": profile_version},
    )
    provider = ClassifierProvider()
    provider.provide(classifier)
    result = asyncio.run(build_classify_asset(provider)(_ctx(conn, spec)))
    conn.commit()
    return result


def test_a_classification_lands_as_pending_review(conn: QueueConnection, asset_id: UUID) -> None:
    stub = StubClassifier(answer=cites_its_own_column)

    result = classify(conn, asset_id, stub)

    assert result.status is TaskStatus.SUCCEEDED, result.error
    stored = proposal_history(conn, asset_id)
    assert len(stored) == 1
    record = stored[0]
    assert record.status is ProposalStatus.PENDING_REVIEW
    assert record.profile_version == 1
    assert record.prompt_version == PROMPT_VERSION
    assert record.model_alias == MODEL_ALIAS
    assert record.trace_id == "trace-test"
    assert [column.column_name for column in record.proposal.columns] == ["email"]
    assert result.output == {
        "proposal_id": str(record.id),
        "asset_id": str(asset_id),
        "version": 1,
        "profile_version": 1,
        "prompt_version": PROMPT_VERSION,
        "model_alias": MODEL_ALIAS,
        "status": "pending_review",
        "sensitive_columns": 1,
    }


def test_the_proposal_is_audited_as_the_agent_that_made_it(
    conn: QueueConnection, asset_id: UUID
) -> None:
    """I7: the row and its audit entry are one write, and the actor is the task."""
    classify(conn, asset_id, StubClassifier(answer=cites_its_own_column))

    rows = conn.execute(SELECT_PROPOSAL_AUDIT).fetchall()

    assert [row[0] for row in rows] == ["classification.proposed"]
    assert rows[0][1]["status"] == "pending_review"
    assert rows[0][1]["sensitive_columns"] == 1


def test_only_the_masked_profile_reaches_the_classifier(
    conn: QueueConnection,
    asset_id: UUID,
    canaries: tuple[str, ...],
    canary_tail: str,
    canary_email: str,
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
) -> None:
    """The data boundary, asserted on what the classifier was actually handed.

    Three halves, and the first two are what make the third mean anything:

    * the profiled table really contains a canary, read back from the source --
      without this, "no canary reached the classifier" is equally true of a
      table that never held one, which is the 0-rows-agreeing-with-itself shape
      this repo has shipped green before;
    * the request *does* carry the column, its type, its statistics and its
      masked samples, so this is a test of masking rather than of an empty
      object; and
    * no planted canary appears anywhere in the request, in whole or in the
      fragment a partial mask would leak.
    """
    planted = [row[0] for row in source_admin.execute("SELECT email FROM sales.customers")]
    assert canary_email in planted, "the profiled table holds no canary; the sweep proves nothing"

    stub = StubClassifier(answer=cites_its_own_column)

    classify(conn, asset_id, stub)

    assert stub.calls == 1
    request = stub.seen[0]
    serialised = request.model_dump_json()
    for canary in (*canaries, canary_tail):
        assert canary not in serialised, f"{canary!r} reached the classifier"

    columns = {column.name: column for column in request.profile.columns}
    assert set(columns) == {"id", "email", "card"}, "the classifier saw a different table"
    email = columns["email"]
    assert email.data_type == "text"
    assert email.distinct_count > 0
    assert email.top_values, "no masked samples reached the classifier at all"
    assert all("*" in frequency.value.masked for frequency in email.top_values)
    assert request.profile.row_count > 0


def test_the_request_carries_no_way_to_reach_the_source(
    conn: QueueConnection, asset_id: UUID
) -> None:
    """A classifier holding this cannot open a connection, because nothing in it
    names one.

    Asserted on the model's fields rather than on a rendering: a substring sweep
    would pass on a request that carried a secret reference under a different
    name.
    """
    stub = StubClassifier(answer=cites_its_own_column)

    classify(conn, asset_id, stub)

    fields = set(ClassificationRequest.model_fields)
    assert fields == {"asset_id", "profile_version", "profile"}
    serialised = stub.seen[0].model_dump()
    assert "source_id" not in serialised
    assert "dsn_secret_ref" not in serialised


def test_a_stale_profile_version_is_refused_before_the_model_runs(
    conn: QueueConnection,
    asset_id: UUID,
    resolver: EnvSecretResolver,
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
) -> None:
    """A request naming a superseded version costs no model call.

    The positive half is the first assertion: version 1 is classifiable while it
    is current. Then the table changes, a second profile version lands, and the
    *same* request becomes a refusal -- so the guard is discriminating between
    versions rather than refusing every request it sees.
    """
    first = StubClassifier(answer=cites_its_own_column)
    assert classify(conn, asset_id, first, profile_version=1).status is TaskStatus.SUCCEEDED
    assert first.calls == 1

    source_admin.execute("INSERT INTO sales.customers (id, email, card) VALUES (9, 'x@y.zz', NULL)")
    reprofiled = asyncio.run(
        build_profile_asset(resolver=resolver, profiler=postgres_profiler)(
            _ctx(conn, _spec(conn, "profile_asset", {"asset_id": str(asset_id)}))
        )
    )
    conn.commit()
    assert reprofiled.status is TaskStatus.SUCCEEDED, reprofiled.error
    assert reprofiled.output is not None and reprofiled.output["version"] == 2

    stale = StubClassifier(answer=cites_its_own_column)
    result = classify(conn, asset_id, stale, profile_version=1)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.type == "urn:steward:stale-profile-version"
    assert stale.calls == 0, "a stale request reached the model before it was refused"


def test_an_unknown_asset_is_refused_before_the_model_runs(conn: QueueConnection) -> None:
    stub = StubClassifier(answer=cites_its_own_column)

    result = classify(conn, uuid4(), stub)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:asset-not-found"
    assert stub.calls == 0


def test_a_retired_asset_is_refused_before_the_model_runs(
    conn: QueueConnection, asset_id: UUID
) -> None:
    """A relation the source no longer has cannot receive a new classification.

    Paired with the happy path above, which classifies the same asset while it
    is active: the refusal is about the lifecycle, not about the asset.
    """
    conn.execute(RETIRE_ASSET, {"id": asset_id})
    conn.commit()
    stub = StubClassifier(answer=cites_its_own_column)

    result = classify(conn, asset_id, stub)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:asset-not-classifiable"
    assert stub.calls == 0


def test_an_asset_that_was_never_profiled_is_refused(
    conn: QueueConnection,
    source_create: SourceCreate,
    resolver: EnvSecretResolver,
    scan_spec: Callable[[UUID], TaskSpec],
) -> None:
    source, _ = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()
    asyncio.run(
        build_scan_source(resolver=resolver, inspect=postgres_inspector)(
            _ctx(conn, scan_spec(source.id))
        )
    )
    conn.commit()
    row = conn.execute(SELECT_ASSET_ID, {"schema": "sales", "name": "orders"}).fetchone()
    assert row is not None
    stub = StubClassifier(answer=cites_its_own_column)

    result = classify(conn, row[0], stub)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:profile-not-found"
    assert stub.calls == 0


def test_an_invented_citation_cannot_be_persisted(conn: QueueConnection, asset_id: UUID) -> None:
    """A locator the profile does not hold fails, and writes nothing.

    The positive case is `test_a_classification_lands_as_pending_review`, which
    cites a locator the profile *does* hold through the same path. Without it,
    a resolver that rejected every citation would pass this test -- which is
    exactly the defect that hid here through two review rounds (#50 review).
    """

    def invents_a_sample(request: ClassificationRequest) -> ProposedClassification:
        return ProposedClassification(
            columns=(
                ColumnClassification(
                    column_name="email",
                    labels=(SensitivityLabel.PII,),
                    confidence=Decimal("0.9"),
                    evidence=(
                        EvidenceRef(
                            profile_version=request.profile_version,
                            column_name="email",
                            kind=EvidenceKind.MASKED_SAMPLE,
                            locator="n***@n***.***",
                            detail="a sample nothing in this profile contains",
                        ),
                    ),
                ),
            ),
            prompt_version=PROMPT_VERSION,
            model_alias=MODEL_ALIAS,
        )

    result = classify(conn, asset_id, StubClassifier(answer=invents_a_sample))

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:unresolvable-evidence"
    assert proposal_history(conn, asset_id) == ()


def test_a_cross_profile_citation_cannot_be_persisted(
    conn: QueueConnection, asset_id: UUID
) -> None:
    """Evidence from a profile version this proposal does not classify is refused."""

    def cites_another_version(request: ClassificationRequest) -> ProposedClassification:
        return ProposedClassification(
            columns=(
                ColumnClassification(
                    column_name="email",
                    labels=(SensitivityLabel.PII,),
                    confidence=Decimal("0.9"),
                    evidence=(
                        EvidenceRef(
                            profile_version=request.profile_version + 1,
                            column_name="email",
                            kind=EvidenceKind.COLUMN_NAME,
                            locator="email",
                            detail="read from a version this proposal does not classify",
                        ),
                    ),
                ),
            ),
            prompt_version=PROMPT_VERSION,
            model_alias=MODEL_ALIAS,
        )

    result = classify(conn, asset_id, StubClassifier(answer=cites_another_version))

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:invalid-classification"
    assert proposal_history(conn, asset_id) == ()


def test_a_column_the_profile_does_not_have_cannot_be_classified(
    conn: QueueConnection, asset_id: UUID
) -> None:
    def invents_a_column(request: ClassificationRequest) -> ProposedClassification:
        return ProposedClassification(
            columns=(
                ColumnClassification(
                    column_name="ssn",
                    labels=(SensitivityLabel.PII,),
                    confidence=Decimal("0.9"),
                    evidence=(
                        EvidenceRef(
                            profile_version=request.profile_version,
                            column_name="ssn",
                            kind=EvidenceKind.COLUMN_NAME,
                            locator="ssn",
                            detail="a column this table does not have",
                        ),
                    ),
                ),
            ),
            prompt_version=PROMPT_VERSION,
            model_alias=MODEL_ALIAS,
        )

    result = classify(conn, asset_id, StubClassifier(answer=invents_a_column))

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:unresolvable-evidence"
    assert proposal_history(conn, asset_id) == ()


def test_repeating_the_same_request_converges_on_one_proposal(
    conn: QueueConnection, asset_id: UUID
) -> None:
    """I8 on the real path: the same effective request cannot produce two
    publishable proposals, however many times the task is retried."""
    first = classify(conn, asset_id, StubClassifier(answer=cites_its_own_column))
    second = classify(conn, asset_id, StubClassifier(answer=cites_its_own_column))

    assert first.status is TaskStatus.SUCCEEDED
    assert second.status is TaskStatus.SUCCEEDED
    assert first.output is not None and second.output is not None
    assert first.output["proposal_id"] == second.output["proposal_id"]
    assert len(proposal_history(conn, asset_id)) == 1


def test_a_new_prompt_version_makes_a_new_proposal(conn: QueueConnection, asset_id: UUID) -> None:
    """Convergence is on the *request*, not on the asset: re-running under a new
    prompt is a new finding to review, not a duplicate to swallow."""

    def under_v2(request: ClassificationRequest) -> ProposedClassification:
        return cites_its_own_column(request).model_copy(
            update={"prompt_version": "classify_asset@v2"}
        )

    classify(conn, asset_id, StubClassifier(answer=cites_its_own_column))
    classify(conn, asset_id, StubClassifier(answer=under_v2))

    versions = {record.prompt_version for record in proposal_history(conn, asset_id)}
    assert versions == {PROMPT_VERSION, "classify_asset@v2"}


def test_a_refused_step_is_reported_as_a_budget_failure(
    conn: QueueConnection, asset_id: UUID
) -> None:
    """A cap doing its job must not read as a bug (I12)."""
    stub = StubClassifier(fails_with=ClassifierBudgetExceeded("the next step does not fit"))

    result = classify(conn, asset_id, stub)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:budget-exceeded"
    assert proposal_history(conn, asset_id) == ()


def test_a_classifier_failure_is_a_typed_task_failure(
    conn: QueueConnection, asset_id: UUID
) -> None:
    stub = StubClassifier(fails_with=ClassifierFailed("the gateway would not answer"))

    result = classify(conn, asset_id, stub)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:classifier-failed"
    assert proposal_history(conn, asset_id) == ()


def test_an_unbound_classifier_fails_the_task_rather_than_raising(
    conn: QueueConnection, asset_id: UUID
) -> None:
    """A process with no capability still answers in the queue's vocabulary."""
    spec = _spec(
        conn, CLASSIFY_ASSET_TASK_TYPE, {"asset_id": str(asset_id), "profile_version": 1}
    )

    result = asyncio.run(build_classify_asset(ClassifierProvider())(_ctx(conn, spec)))

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:classifier-unbound"


def test_a_malformed_payload_is_refused(conn: QueueConnection, asset_id: UUID) -> None:
    spec = _spec(conn, CLASSIFY_ASSET_TASK_TYPE, {"asset_id": str(asset_id)})
    provider = ClassifierProvider()
    stub = StubClassifier(answer=cites_its_own_column)
    provider.provide(stub)

    result = asyncio.run(build_classify_asset(provider)(_ctx(conn, spec)))

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:invalid-task-payload"
    assert stub.calls == 0


def test_the_sample_payload_reaches_no_model(conn: QueueConnection) -> None:
    """H1 executes this handler twice with the registry's sample.

    It must therefore be a payload that resolves without a gateway, a budget or
    a model's cooperation -- so the idempotency harness measures the handler and
    not a stub's willingness to answer identically.
    """
    stub = StubClassifier(answer=cites_its_own_column)
    provider = ClassifierProvider()
    provider.provide(stub)
    spec = _spec(conn, CLASSIFY_ASSET_TASK_TYPE, dict(CLASSIFY_ASSET_SAMPLE_PAYLOAD))

    result = asyncio.run(build_classify_asset(provider)(_ctx(conn, spec)))

    assert result.status is TaskStatus.FAILED
    assert stub.calls == 0


def test_the_run_handed_to_a_classifier_carries_no_connection(
    conn: QueueConnection, asset_id: UUID
) -> None:
    """The classifier is given execution identity, never the catalog's transaction."""
    stub = StubClassifier(answer=cites_its_own_column)

    classify(conn, asset_id, stub)

    assert set(ClassificationRun.model_fields) == {
        "run_id",
        "task_id",
        "trace_id",
        "claimed_by",
        "attempts",
        "budget",
    }
    run = stub.runs[0]
    assert run.claimed_by == "w-test"
    assert run.trace_id == "trace-test"
    assert run.attempts == 1
    assert run.budget == CLASSIFY_BUDGET


class TestTheProvider:
    """Binding is once, and an override restores what it replaced."""

    def test_a_second_binding_is_refused(self) -> None:
        provider = ClassifierProvider()
        provider.provide(StubClassifier(answer=cites_its_own_column))

        with pytest.raises(ClassifierAlreadyBound):
            provider.provide(StubClassifier(answer=cites_its_own_column))

    def test_an_unbound_provider_raises_rather_than_returning_none(self) -> None:
        with pytest.raises(ClassifierUnbound):
            ClassifierProvider().get()

    def test_an_override_is_put_back(self) -> None:
        provider = ClassifierProvider()
        first = StubClassifier(answer=cites_its_own_column)
        provider.provide(first)
        second = StubClassifier(answer=cites_its_own_column)

        with provider.overridden(second):
            assert provider.get() is second

        assert provider.get() is first

    def test_the_process_provider_starts_unbound_and_restores(self) -> None:
        """The module-level provider is not bound by importing the package.

        Registration is systemwide; capability is per process. If importing
        `steward_catalog` bound a classifier, every process would claim
        `classify_asset` whether or not it could run one.
        """
        assert classifier_bound() is False

        with CLASSIFIER.overridden(StubClassifier(answer=cites_its_own_column)):
            assert classifier_bound() is True

        assert classifier_bound() is False


@pytest.fixture(autouse=True)
def _provider_is_clean() -> Iterator[None]:
    """No test may leave a classifier bound to the process provider."""
    yield
    assert not classifier_bound(), "a test left a classifier bound to the process"


def test_provide_classifier_binds_the_provider_the_handler_reads() -> None:
    """The composition root's entry point and the handler's provider are one object.

    Worth asserting rather than reading off the source: if `provide_classifier`
    ever bound something else, a worker would report itself configured, claim
    `classify_asset`, and fail every one of them with `classifier-unbound`.
    """
    with CLASSIFIER.overridden(None):
        provide_classifier(StubClassifier(answer=cites_its_own_column))
        assert classifier_bound() is True
