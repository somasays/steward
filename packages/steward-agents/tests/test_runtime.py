from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel
from steward_agents import (
    AgentResult,
    AgentRuntime,
    BudgetExceeded,
    DisallowedTool,
    InMemoryCheckpointStore,
    ModelReservation,
    ToolRegistry,
)
from steward_llm import (
    DeploymentMode,
    EndpointAllowlist,
    FinishReason,
    GatewayConfig,
    LLMClient,
    Message,
    ModelBinding,
    Role,
    StubGateway,
    StubReply,
    ToolCall,
)
from steward_schemas import AgentSpec, RunBudget


class EchoInput(BaseModel):
    value: str


class EchoOutput(BaseModel):
    value: str


class FinalOutput(BaseModel):
    answer: str


def budget(*, steps: int = 5, tokens: int = 100, cost: str = "1") -> RunBudget:
    return RunBudget(
        steps=steps,
        tokens=tokens,
        cost_usd=Decimal(cost),
        wall_clock=timedelta(minutes=1),
    )


def client(replies: list[StubReply]) -> tuple[LLMClient, StubGateway]:
    endpoint = "http://127.0.0.1:8000/v1"
    config = GatewayConfig(
        mode=DeploymentMode.DEVELOPMENT,
        source="test",
        bindings=(ModelBinding(alias="steward-fast", model="openai/local", api_base=endpoint),),
        allowlist=EndpointAllowlist.from_urls((endpoint,)),
    )
    gateway = StubGateway({"steward-fast": replies})
    return LLMClient(config, gateway), gateway


def spec(*, tools: tuple[str, ...] = ("echo",), limits: RunBudget | None = None) -> AgentSpec:
    return AgentSpec(
        name="proof-agent",
        model_alias="steward-fast",
        tools=tools,
        limits=limits or budget(),
    )


def reservation() -> ModelReservation:
    return ModelReservation(tokens=20, cost_usd=Decimal("0.20"), wall_clock=timedelta(seconds=10))


def registry(calls: list[str]) -> ToolRegistry:
    tools = ToolRegistry()

    async def echo(request: BaseModel) -> BaseModel:
        parsed = EchoInput.model_validate(request)
        calls.append(parsed.value)
        return EchoOutput(value=parsed.value)

    tools.register(
        name="echo",
        description="Echo a value",
        input_model=EchoInput,
        output_model=EchoOutput,
        handler=echo,
    )
    return tools


@pytest.mark.asyncio
async def test_stub_backed_tool_flow_is_bounded_validated_and_checkpointed() -> None:
    llm, gateway = client([
        StubReply.completed(
            "",
            prompt_tokens=3,
            completion_tokens=2,
            cost_usd=Decimal("0.05"),
            tool_calls=(ToolCall(id="call-1", name="echo", arguments='{"value":"safe"}'),),
        ),
        StubReply.completed(
            '{"answer":"profiled"}',
            prompt_tokens=4,
            completion_tokens=3,
            cost_usd=Decimal("0.07"),
        ),
    ])
    stores = InMemoryCheckpointStore()
    invoked: list[str] = []
    runtime = AgentRuntime(client=llm, tools=registry(invoked), checkpoints=stores, reservation=reservation())

    result = await runtime.run(
        key="task-1",
        spec=spec(),
        prompt_version="proof.v1",
        messages=(Message(role=Role.USER, content="profile this"),),
        output_model=FinalOutput,
    )

    assert result.output == FinalOutput(answer="profiled")
    assert result.usage.steps == 3
    assert result.usage.tokens == 12
    assert result.usage.cost_usd == Decimal("0.12")
    assert invoked == ["safe"]
    assert len(gateway.calls) == 2
    assert stores.values["task-1"].finished


@pytest.mark.asyncio
async def test_disallowed_tool_is_rejected_without_invocation() -> None:
    llm, _ = client([StubReply.completed(
        "",
        tool_calls=(ToolCall(id="call-1", name="delete_everything", arguments="{}"),),
    )])
    invoked: list[str] = []
    runtime = AgentRuntime(
        client=llm,
        tools=registry(invoked),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(),
    )
    with pytest.raises(DisallowedTool, match="not allowed"):
        await runtime.run(
            key="task-2",
            spec=spec(),
            prompt_version="proof.v1",
            messages=(Message(role=Role.USER, content="try it"),),
            output_model=FinalOutput,
        )
    assert invoked == []


@pytest.mark.asyncio
async def test_unaffordable_model_step_never_starts() -> None:
    llm, gateway = client([StubReply.completed('{"answer":"must not run"}')])
    runtime = AgentRuntime(
        client=llm,
        tools=registry([]),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(),
    )
    with pytest.raises(BudgetExceeded, match="refused before execution"):
        await runtime.run(
            key="task-3",
            spec=spec(limits=budget(tokens=19)),
            prompt_version="proof.v1",
            messages=(Message(role=Role.USER, content="no budget"),),
            output_model=FinalOutput,
        )
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_failed_model_usage_is_checkpointed_and_debited_before_resume() -> None:
    llm, gateway = client([
        StubReply.streaming(
            ("lost",),
            prompt_tokens=5,
            cost_per_token=Decimal("0.02"),
            fails_with=OSError("disconnect"),
        ),
        StubReply.completed(
            '{"answer":"resumed"}',
            prompt_tokens=2,
            completion_tokens=2,
            cost_usd=Decimal("0.03"),
            finish_reason=FinishReason.STOP,
        ),
    ])
    stores = InMemoryCheckpointStore()
    runtime = AgentRuntime(client=llm, tools=registry([]), checkpoints=stores, reservation=reservation())
    async def execute() -> AgentResult:
        return await runtime.run(
            key="task-4",
            spec=spec(),
            prompt_version="proof.v1",
            messages=(Message(role=Role.USER, content="resume me"),),
            output_model=FinalOutput,
        )

    with pytest.raises(Exception, match="disconnect"):
        await execute()

    assert stores.values["task-4"].usage.tokens == 6
    assert stores.values["task-4"].usage.cost_usd == Decimal("0.02")
    result = await execute()
    assert result.output == FinalOutput(answer="resumed")
    assert result.usage.steps == 2
    assert result.usage.tokens == 10
    assert result.usage.cost_usd == Decimal("0.05")
    assert len(gateway.calls) == 2
