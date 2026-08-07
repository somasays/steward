"""Round-trip serialization: model -> json -> model == model, for every
published contract (issue #2 acceptance criteria).

No third-party test framework here on purpose: this package is pydantic +
stdlib only (I4), enforced by S1 against `packages/steward-schemas` as a
whole, tests included — see GUARDRAILS.md S1. Plain `test_*` functions and
stdlib-only assertions.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from steward_schemas import (
    CONTRACTS,
    AgentSpec,
    Asset,
    AssetLifecycle,
    AssetType,
    Column,
    ProblemDetails,
    RunBudget,
    RunCreate,
    RunResponse,
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


def build_run_response() -> RunResponse:
    return RunResponse(
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


def test_source_roundtrips() -> None:
    _roundtrips(build_source())


def test_source_without_schedule_roundtrips() -> None:
    _roundtrips(build_source_no_schedule())


def test_asset_roundtrips() -> None:
    _roundtrips(build_asset())


def test_column_roundtrips() -> None:
    _roundtrips(build_column())


def test_run_budget_roundtrips() -> None:
    _roundtrips(build_run_budget())


def test_task_spec_roundtrips() -> None:
    _roundtrips(build_task_spec())


def test_task_result_succeeded_roundtrips() -> None:
    _roundtrips(build_task_result_succeeded())


def test_task_result_failed_roundtrips() -> None:
    _roundtrips(build_task_result_failed())


def test_agent_spec_roundtrips() -> None:
    _roundtrips(build_agent_spec())


def test_problem_details_roundtrips() -> None:
    _roundtrips(build_problem_details())


def test_problem_details_minimal_roundtrips() -> None:
    _roundtrips(ProblemDetails(title="Internal error", status=500))


def test_run_create_roundtrips() -> None:
    _roundtrips(build_run_create())


def test_run_response_roundtrips() -> None:
    _roundtrips(build_run_response())


def test_problem_details_extension_member_survives_roundtrip() -> None:
    """RFC 9457 extension members (extra="allow") must not be dropped."""
    original = ProblemDetails.model_validate(
        {"title": "Run budget exceeded", "status": 422, "budget": {"steps": 20}}
    )
    restored = ProblemDetails.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.__pydantic_extra__ == {"budget": {"steps": 20}}


def test_all_contract_samples_roundtrip() -> None:
    """Every `CONTRACTS` entry has a sample here and it round-trips."""
    samples: dict[str, BaseModel] = {
        "source": build_source(),
        "asset": build_asset(),
        "column": build_column(),
        "task_spec": build_task_spec(),
        "task_result": build_task_result_succeeded(),
        "run_budget": build_run_budget(),
        "agent_spec": build_agent_spec(),
        "problem_details": build_problem_details(),
        "run_create": build_run_create(),
        "run_response": build_run_response(),
    }
    assert set(samples) == set(CONTRACTS)
    for name, model in samples.items():
        assert isinstance(model, CONTRACTS[name])
        _roundtrips(model)
