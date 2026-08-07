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
from pydantic import BaseModel
from steward_schemas import (
    CONTRACTS,
    AgentSpec,
    Asset,
    AssetLifecycle,
    AssetType,
    Column,
    ProblemDetails,
    Run,
    RunBudget,
    RunCreate,
    RunStatus,
    Source,
    SourceEngine,
    TaskResult,
    TaskSpec,
    TaskStatus,
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


# One sample per `CONTRACTS` entry -- test_samples_cover_every_registered_contract
# below asserts nothing is missing, and each one is exercised by
# test_contract_sample_roundtrips.
SAMPLES: dict[str, BaseModel] = {
    "source": build_source(),
    "asset": build_asset(),
    "column": build_column(),
    "task_spec": build_task_spec(),
    "task_result": build_task_result_succeeded(),
    "run_budget": build_run_budget(),
    "agent_spec": build_agent_spec(),
    "problem_details": build_problem_details(),
    "run_create": build_run_create(),
    "run": build_run(),
}

# Additional shapes worth round-tripping that aren't 1:1 with a CONTRACTS
# entry (a second, distinct instance of an already-covered contract).
EXTRA_SAMPLES: dict[str, BaseModel] = {
    "source_without_schedule": build_source_no_schedule(),
    "task_result_failed": build_task_result_failed(),
    "problem_details_minimal": ProblemDetails(title="Internal error", status=500),
}


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
