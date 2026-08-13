"""Round-trip serialization: model -> json -> model == model, for every
published contract (issue #2 acceptance criteria).

Uses pytest.mark.parametrize to run the same assertion once per sample
instead of one hand-written test per contract: S1 (GUARDRAILS.md) scopes
the schemas independence contract to the installed package (`src/`), not
`tests/` (issue #12), so tests are free to import pytest (issue #13).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError
from steward_schemas import (
    CONTRACTS,
    AgentSpec,
    Asset,
    AssetDetail,
    AssetLifecycle,
    AssetPage,
    AssetType,
    Classification,
    ClassificationDetail,
    ClassificationHistory,
    ClassificationProposal,
    ClassificationReview,
    Column,
    ColumnClassification,
    ColumnProfile,
    EvidenceKind,
    EvidenceRef,
    MaskedSample,
    ProblemDetails,
    ProposalStatus,
    ReviewerKind,
    ReviewOutcome,
    ReviewRequest,
    Run,
    RunBudget,
    RunCreate,
    RunStatus,
    SemanticType,
    SensitivityLabel,
    Source,
    SourceCreate,
    SourceEngine,
    TableProfile,
    TaskResult,
    TaskSpec,
    TaskStatus,
    ValueFrequency,
)

WORKSPACE_ID = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)


def _roundtrips(model: BaseModel) -> None:
    cls = type(model)
    restored = cls.model_validate_json(model.model_dump_json())
    assert restored == model


def build_run_budget() -> RunBudget:
    return RunBudget(steps=20, tokens=100_000, cost_usd=Decimal("1.50"), wall_clock=timedelta(minutes=5))


def build_source() -> Source:
    return Source(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        workspace_id=WORKSPACE_ID,
        name="warehouse-prod",
        engine=SourceEngine.POSTGRES,
        dsn_secret_ref="secret://sources/warehouse-prod/dsn",
        scan_schedule="0 * * * *",
        created_at=NOW,
        updated_at=NOW,
    )


def build_source_no_schedule() -> Source:
    return Source(
        id=UUID("22222222-2222-2222-2222-222222222223"),
        workspace_id=WORKSPACE_ID,
        name="warehouse-manual",
        engine=SourceEngine.SNOWFLAKE,
        dsn_secret_ref="secret://sources/warehouse-manual/dsn",
        created_at=NOW,
        updated_at=NOW,
    )


def build_asset() -> Asset:
    return Asset(
        id=UUID("33333333-3333-3333-3333-333333333333"),
        workspace_id=WORKSPACE_ID,
        source_id=UUID("22222222-2222-2222-2222-222222222222"),
        fqn="analytics.public.orders",
        asset_type=AssetType.TABLE,
        lifecycle=AssetLifecycle.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )


def build_column() -> Column:
    return Column(
        id=UUID("44444444-4444-4444-4444-444444444444"),
        workspace_id=WORKSPACE_ID,
        asset_id=UUID("33333333-3333-3333-3333-333333333333"),
        name="customer_id",
        data_type="bigint",
        ordinal=1,
        nullable=False,
        created_at=NOW,
        updated_at=NOW,
    )


def build_masked_sample() -> MaskedSample:
    return MaskedSample(masked="j***@g***.***", semantic_type=SemanticType.EMAIL, length=17)


def build_value_frequency() -> ValueFrequency:
    return ValueFrequency(value=build_masked_sample(), count=12)


def build_column_profile() -> ColumnProfile:
    return ColumnProfile(
        name="email",
        data_type="text",
        null_count=3,
        null_ratio=Decimal("0.030000"),
        distinct_count=97,
        distinct_ratio=Decimal("0.970000"),
        min_value=build_masked_sample(),
        max_value=build_masked_sample(),
        top_values=(build_value_frequency(),),
        semantic_type=SemanticType.EMAIL,
    )


def build_table_profile() -> TableProfile:
    return TableProfile(row_count=100, columns=(build_column_profile(),))


def build_source_create() -> SourceCreate:
    return SourceCreate(
        name="warehouse-prod",
        engine=SourceEngine.POSTGRES,
        host="warehouse.internal",
        database="analytics",
        dsn_secret_ref="env:STEWARD_SOURCE_DSN_WAREHOUSE",
        include_schemas=("public", "sales"),
        scan_schedule="0 * * * *",
    )


def build_asset_page() -> AssetPage:
    return AssetPage(items=(build_asset(),), next_cursor="cHVibGljLm9yZGVycw")


def build_asset_detail() -> AssetDetail:
    return AssetDetail(asset=build_asset(), columns=(build_column(),))


def build_task_spec() -> TaskSpec:
    return TaskSpec(
        task_id=UUID("55555555-5555-5555-5555-555555555555"),
        run_id=UUID("66666666-6666-6666-6666-666666666666"),
        task_type="profile_table",
        payload={"asset_id": "33333333-3333-3333-3333-333333333333"},
        budget=build_run_budget(),
        max_attempts=3,
    )


def build_problem_details() -> ProblemDetails:
    return ProblemDetails(
        type="urn:steward:budget_exceeded",
        title="Run budget exceeded",
        status=422,
        detail="step budget of 20 exceeded at step 21",
        instance="/v1/runs/66666666-6666-6666-6666-666666666666",
    )


def build_task_result_succeeded() -> TaskResult:
    return TaskResult(
        task_id=UUID("55555555-5555-5555-5555-555555555555"),
        status=TaskStatus.SUCCEEDED,
        usage=RunBudget(steps=4, tokens=1_200, cost_usd=Decimal("0.02"), wall_clock=timedelta(seconds=8)),
        output={"row_count": 42},
    )


def build_task_result_failed() -> TaskResult:
    return TaskResult(
        task_id=UUID("55555555-5555-5555-5555-555555555555"),
        status=TaskStatus.FAILED,
        usage=build_run_budget(),
        error=build_problem_details(),
    )


def build_agent_spec() -> AgentSpec:
    return AgentSpec(
        name="profiler",
        model_alias="steward-fast",
        tools=("run_profile_sql", "sample_rows"),
        limits=build_run_budget(),
    )


def build_run_create() -> RunCreate:
    return RunCreate(goal="scan_source", payload={"source_id": "22222222-2222-2222-2222-222222222222"})


def build_run() -> Run:
    return Run(
        id=UUID("77777777-7777-7777-7777-777777777777"),
        goal="scan_source",
        payload={"source_id": "22222222-2222-2222-2222-222222222222"},
        status=RunStatus.PENDING,
        trace_id="0123456789abcdef0123456789abcdef",
        budget=build_run_budget(),
        usage=RunBudget(steps=0, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(0)),
        created_at=NOW,
        updated_at=NOW,
    )


ASSET_ID = UUID("88888888-8888-8888-8888-888888888888")
PROPOSAL_ID = UUID("99999999-9999-9999-9999-999999999999")


def build_evidence_ref() -> EvidenceRef:
    return EvidenceRef(
        profile_version=3,
        column_name="email",
        kind=EvidenceKind.COLUMN_NAME,
        locator="email",
        detail="the column is named 'email'",
    )


def build_column_classification() -> ColumnClassification:
    return ColumnClassification(
        column_name="email",
        labels=(SensitivityLabel.PII,),
        confidence=Decimal("0.95"),
        evidence=(build_evidence_ref(),),
    )


def build_classification_proposal() -> ClassificationProposal:
    return ClassificationProposal(
        asset_id=ASSET_ID,
        profile_version=3,
        prompt_version="classify_asset@v1",
        model_alias="steward-classify",
        columns=(
            build_column_classification(),
            ColumnClassification(
                column_name="id",
                labels=(SensitivityLabel.NONE,),
                confidence=Decimal("0.99"),
            ),
        ),
    )


def build_classification() -> Classification:
    return Classification(
        id=PROPOSAL_ID,
        asset_id=ASSET_ID,
        version=2,
        status=ProposalStatus.PENDING_REVIEW,
        proposal=build_classification_proposal(),
        run_id=UUID("77777777-7777-7777-7777-777777777777"),
        task_id=UUID("66666666-6666-6666-6666-666666666666"),
        trace_id="0123456789abcdef0123456789abcdef",
        created_at=NOW,
    )


def build_classification_review() -> ClassificationReview:
    return ClassificationReview(
        id=UUID("55555555-5555-5555-5555-555555555555"),
        proposal_id=PROPOSAL_ID,
        outcome=ReviewOutcome.APPROVED,
        actor_kind=ReviewerKind.HUMAN,
        actor_id="api",
        reason="evidence checks out",
        decided_at=NOW,
    )


# One sample per `CONTRACTS` entry -- test_samples_cover_every_registered_contract
# below asserts nothing is missing, and each one is exercised by
# test_contract_sample_roundtrips.
SAMPLES: dict[str, BaseModel] = {
    "source": build_source(),
    "source_create": build_source_create(),
    "asset": build_asset(),
    "asset_detail": build_asset_detail(),
    "asset_page": build_asset_page(),
    "column": build_column(),
    "masked_sample": build_masked_sample(),
    "value_frequency": build_value_frequency(),
    "column_profile": build_column_profile(),
    "table_profile": build_table_profile(),
    "task_spec": build_task_spec(),
    "task_result": build_task_result_succeeded(),
    "run_budget": build_run_budget(),
    "agent_spec": build_agent_spec(),
    "problem_details": build_problem_details(),
    "run_create": build_run_create(),
    "run": build_run(),
    "evidence_ref": build_evidence_ref(),
    "column_classification": build_column_classification(),
    "classification_proposal": build_classification_proposal(),
    "classification": build_classification(),
    "classification_review": build_classification_review(),
    "classification_detail": ClassificationDetail(
        classification=build_classification(), reviews=(build_classification_review(),)
    ),
    "classification_history": ClassificationHistory(items=(build_classification(),)),
    "review_request": ReviewRequest(reason="evidence checks out"),
}

# Additional shapes worth round-tripping that aren't 1:1 with a CONTRACTS
# entry (a second, distinct instance of an already-covered contract).
EXTRA_SAMPLES: dict[str, BaseModel] = {
    "source_without_schedule": build_source_no_schedule(),
    "asset_page_last": AssetPage(items=()),
    "task_result_failed": build_task_result_failed(),
    "problem_details_minimal": ProblemDetails(title="Internal error", status=500),
    "column_profile_unsampled": ColumnProfile(
        name="notes",
        data_type="text",
        null_count=0,
        null_ratio=Decimal("0.000000"),
        distinct_count=0,
        distinct_ratio=Decimal("0.000000"),
    ),
    "table_profile_empty": TableProfile(row_count=0),
    "classification_review_by_policy": ClassificationReview(
        id=UUID("44444444-4444-4444-4444-444444444444"),
        proposal_id=PROPOSAL_ID,
        outcome=ReviewOutcome.APPROVED,
        actor_kind=ReviewerKind.POLICY,
        actor_id="auto-approve-none",
        reason="no sensitive labels proposed",
        policy_id="auto-approve-none",
        decided_at=NOW,
    ),
    "classification_history_empty": ClassificationHistory(items=()),
}


def test_a_profile_cannot_carry_an_unmasked_value() -> None:
    """I6 at the contract: the sample fields take `MaskedSample`, not `str`.

    `mypy --strict` is the real enforcement (G2) -- this asserts the runtime
    agrees, so the guarantee does not depend on the type checker having been
    run over the code that built the profile.
    """
    raw = "ada@example.com"
    for field, value in (("min_value", raw), ("max_value", raw), ("top_values", (raw,))):
        try:
            ColumnProfile(
                name="email",
                data_type="text",
                null_count=0,
                null_ratio=Decimal("0"),
                distinct_count=1,
                distinct_ratio=Decimal("1"),
                **{field: value},
            )
        except ValidationError:
            continue
        raise AssertionError(f"{field} accepted a raw string")


def test_samples_cover_every_registered_contract() -> None:
    """Every `CONTRACTS` entry has a sample here (issue #2 acceptance criteria)."""
    assert set(SAMPLES) == set(CONTRACTS)
    for name, model in SAMPLES.items():
        assert isinstance(model, CONTRACTS[name])


@pytest.mark.parametrize("model", list(SAMPLES.values()), ids=list(SAMPLES))
def test_contract_sample_roundtrips(model: BaseModel) -> None:
    _roundtrips(model)


@pytest.mark.parametrize("model", list(EXTRA_SAMPLES.values()), ids=list(EXTRA_SAMPLES))
def test_extra_sample_roundtrips(model: BaseModel) -> None:
    _roundtrips(model)


def test_problem_details_extension_member_survives_roundtrip() -> None:
    """RFC 9457 extension members (extra="allow") must not be dropped."""
    original = ProblemDetails.model_validate(
        {"title": "Run budget exceeded", "status": 422, "budget": {"steps": 20}}
    )
    restored = ProblemDetails.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.__pydantic_extra__ == {"budget": {"steps": 20}}
