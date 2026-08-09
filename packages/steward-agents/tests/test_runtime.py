"""What the bounded loop is asserted to do.

The properties here are the ones a budget can be faked past: that a step which
cannot fit is *never started* (not merely reported afterwards), that the tool
half of that check is as real as the model half, and that spend is announced as
it happens so a failure that never returns still charges the run.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel
from steward_agents import (
    SUBMIT_RESULT,
    AgentResult,
    AgentRuntime,
    AgentRuntimeError,
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


def budget(
    *, steps: int = 5, tokens: int = 2000, cost: str = "1", wall_clock: timedelta | None = None
) -> RunBudget:
    return RunBudget(
        steps=steps,
        tokens=tokens,
        cost_usd=Decimal(cost),
        wall_clock=wall_clock if wall_clock is not None else timedelta(minutes=1),
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


def reservation(tokens: int = 400) -> ModelReservation:
    """The per-call worst case. `tokens` is a *total* — prompt plus completion —
    so it has to be big enough to hold a realistic history, not just an answer."""
    return ModelReservation(
        tokens=tokens, cost_usd=Decimal("0.20"), wall_clock=timedelta(seconds=10)
    )


def registry(calls: list[str], *, wall_clock: timedelta = timedelta(seconds=5)) -> ToolRegistry:
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
        wall_clock=wall_clock,
    )
    return tools


def submits(answer: str, **usage: object) -> StubReply:
    """A reply that ends the run the only way it can end: through the tool."""
    return StubReply.completed(
        "",
        tool_calls=(
            ToolCall(id="submit-1", name=SUBMIT_RESULT, arguments=f'{{"answer":"{answer}"}}'),
        ),
        **usage,  # type: ignore[arg-type]
    )


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
        submits("profiled", prompt_tokens=4, completion_tokens=3, cost_usd=Decimal("0.07")),
    ])
    stores = InMemoryCheckpointStore()
    invoked: list[str] = []
    runtime = AgentRuntime(
        client=llm, tools=registry(invoked), checkpoints=stores, reservation=reservation()
    )

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
    # SerializeAsAny: the declared type is `BaseModel`, so without it every
    # field of the real result is silently dropped from the dump.
    assert result.model_dump()["output"] == {"answer": "profiled"}


@pytest.mark.asyncio
async def test_the_result_schema_is_shown_to_the_model_as_a_tool() -> None:
    """A result the model was never given a schema for is a result it can only
    produce by luck. The output model reaches it as `submit_result`'s parameters."""
    llm, gateway = client([submits("done")])
    runtime = AgentRuntime(
        client=llm,
        tools=registry([]),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(),
    )
    await runtime.run(
        key="task-schema",
        spec=spec(tools=()),
        prompt_version="proof.v1",
        messages=(Message(role=Role.USER, content="go"),),
        output_model=FinalOutput,
    )
    offered = {tool.name: tool for tool in gateway.calls[0].request.tools}
    assert SUBMIT_RESULT in offered
    assert offered[SUBMIT_RESULT].parameters == FinalOutput.model_json_schema()


@pytest.mark.asyncio
async def test_stopping_without_submitting_is_a_typed_failure() -> None:
    llm, _ = client([StubReply.completed("here is my answer in prose")])
    runtime = AgentRuntime(
        client=llm,
        tools=registry([]),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(),
    )
    with pytest.raises(AgentRuntimeError, match=SUBMIT_RESULT):
        await runtime.run(
            key="task-prose",
            spec=spec(tools=()),
            prompt_version="proof.v1",
            messages=(Message(role=Role.USER, content="go"),),
            output_model=FinalOutput,
        )


@pytest.mark.asyncio
async def test_disallowed_tool_is_rejected_without_invocation() -> None:
    llm, _ = client([
        StubReply.completed(
            "",
            tool_calls=(ToolCall(id="call-1", name="delete_everything", arguments="{}"),),
        )
    ])
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
    llm, gateway = client([submits("must not run")])
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
async def test_a_tool_that_cannot_fit_its_wall_clock_is_never_invoked() -> None:
    """The tool half of the preflight. Reserving zero wall-clock for tools and
    debiting the real elapsed time afterwards is an audit fence, not a budget:
    the overrun is discovered once it has already happened."""
    llm, _ = client([
        StubReply.completed(
            "",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=Decimal("0.01"),
            tool_calls=(ToolCall(id="call-1", name="echo", arguments='{"value":"slow"}'),),
        )
    ])
    invoked: list[str] = []
    runtime = AgentRuntime(
        client=llm,
        tools=registry(invoked, wall_clock=timedelta(seconds=30)),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(),
    )
    with pytest.raises(BudgetExceeded, match="tool step refused"):
        await runtime.run(
            key="task-slow-tool",
            spec=spec(limits=budget(wall_clock=timedelta(seconds=20))),
            prompt_version="proof.v1",
            messages=(Message(role=Role.USER, content="call the slow tool"),),
            output_model=FinalOutput,
        )
    assert invoked == []


@pytest.mark.asyncio
async def test_bad_tool_arguments_are_fed_back_once_then_fail() -> None:
    """SPEC §3.2: a validation error is the model's to fix, once."""
    bad = ToolCall(id="call-bad", name="echo", arguments='{"wrong":"shape"}')
    llm, _ = client([
        StubReply.completed("", tool_calls=(bad,), prompt_tokens=1, completion_tokens=1),
        submits("recovered", prompt_tokens=1, completion_tokens=1),
    ])
    invoked: list[str] = []
    stores = InMemoryCheckpointStore()
    runtime = AgentRuntime(
        client=llm, tools=registry(invoked), checkpoints=stores, reservation=reservation()
    )
    result = await runtime.run(
        key="task-feedback",
        spec=spec(),
        prompt_version="proof.v1",
        messages=(Message(role=Role.USER, content="go"),),
        output_model=FinalOutput,
    )
    assert result.output == FinalOutput(answer="recovered")
    assert invoked == []
    fed_back = [m for m in stores.values["task-feedback"].messages if m.role is Role.TOOL]
    assert "invalid input" in fed_back[0].content
    assert stores.values["task-feedback"].answered_with_feedback == ("call-bad",)


@pytest.mark.asyncio
async def test_the_same_bad_call_twice_is_not_fed_back_again() -> None:
    bad = ToolCall(id="call-bad", name="echo", arguments='{"wrong":"shape"}')
    llm, _ = client([
        StubReply.completed("", tool_calls=(bad, bad), prompt_tokens=1, completion_tokens=1),
    ])
    runtime = AgentRuntime(
        client=llm,
        tools=registry([]),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(),
    )
    with pytest.raises(Exception, match="invalid input"):
        await runtime.run(
            key="task-loop",
            spec=spec(),
            prompt_version="proof.v1",
            messages=(Message(role=Role.USER, content="go"),),
            output_model=FinalOutput,
        )


@pytest.mark.asyncio
async def test_a_model_that_never_submits_is_stopped_by_the_budget() -> None:
    """Not by LangGraph's recursion backstop, which would surface a framework
    type through a seam this package exists to keep closed (I2, I9)."""
    llm, _ = client([
        StubReply.completed(
            "",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=Decimal("0.01"),
            finish_reason=FinishReason.TOOL_CALLS,
            tool_calls=(ToolCall(id=f"call-{n}", name="echo", arguments='{"value":"again"}'),),
        )
        for n in range(20)
    ])
    runtime = AgentRuntime(
        client=llm,
        tools=registry([]),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(),
    )
    with pytest.raises(BudgetExceeded):
        await runtime.run(
            key="task-forever",
            spec=spec(limits=budget(steps=4)),
            prompt_version="proof.v1",
            messages=(Message(role=Role.USER, content="loop"),),
            output_model=FinalOutput,
        )


@pytest.mark.asyncio
async def test_every_increment_is_announced_as_it_is_spent() -> None:
    """The seam a task's usage ledger is wired to: a run that dies without
    returning is charged from these, not from a result it never built."""
    llm, _ = client([
        StubReply.completed(
            "",
            prompt_tokens=3,
            completion_tokens=2,
            cost_usd=Decimal("0.05"),
            tool_calls=(ToolCall(id="call-1", name="echo", arguments='{"value":"x"}'),),
        ),
        submits("done", prompt_tokens=1, completion_tokens=1, cost_usd=Decimal("0.02")),
    ])
    spends: list[RunBudget] = []
    runtime = AgentRuntime(
        client=llm,
        tools=registry([]),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(),
        on_spend=spends.append,
    )
    result = await runtime.run(
        key="task-spend",
        spec=spec(),
        prompt_version="proof.v1",
        messages=(Message(role=Role.USER, content="go"),),
        output_model=FinalOutput,
    )
    assert len(spends) == 3  # model, tool, model
    assert RunBudget.total(spends) == result.usage


@pytest.mark.asyncio
async def test_a_failed_call_announces_its_spend_before_the_failure_travels() -> None:
    llm, _ = client([
        StubReply.streaming(
            ("lost",),
            prompt_tokens=5,
            cost_per_token=Decimal("0.02"),
            fails_with=OSError("disconnect"),
        ),
    ])
    spends: list[RunBudget] = []
    runtime = AgentRuntime(
        client=llm,
        tools=registry([]),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(),
        on_spend=spends.append,
    )
    with pytest.raises(Exception, match="disconnect"):
        await runtime.run(
            key="task-lost",
            spec=spec(tools=()),
            prompt_version="proof.v1",
            messages=(Message(role=Role.USER, content="go"),),
            output_model=FinalOutput,
        )
    assert RunBudget.total(spends).tokens == 6
    assert RunBudget.total(spends).cost_usd == Decimal("0.02")


@pytest.mark.asyncio
async def test_failed_model_usage_is_checkpointed_and_debited_before_resume() -> None:
    llm, gateway = client([
        StubReply.streaming(
            ("lost",),
            prompt_tokens=5,
            cost_per_token=Decimal("0.02"),
            fails_with=OSError("disconnect"),
        ),
        submits("resumed", prompt_tokens=2, completion_tokens=2, cost_usd=Decimal("0.03")),
    ])
    stores = InMemoryCheckpointStore()
    runtime = AgentRuntime(
        client=llm, tools=registry([]), checkpoints=stores, reservation=reservation()
    )

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


def test_a_tool_must_declare_a_positive_wall_clock_reservation() -> None:
    tools = ToolRegistry()

    async def handler(request: BaseModel) -> BaseModel:
        return EchoOutput(value="x")

    with pytest.raises(ValueError, match="positive wall-clock"):
        tools.register(
            name="free",
            description="costs nothing, apparently",
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=handler,
            wall_clock=timedelta(0),
        )


@pytest.mark.asyncio
async def test_a_prompt_that_fills_the_reservation_is_refused_before_the_call() -> None:
    """`max_tokens` bounds the completion only. If the reservation were sent as
    `max_tokens`, a call would cost the prompt on top of everything checked for,
    and a long history would overrun the cap the preflight had just approved."""
    llm, gateway = client([submits("never reached")])
    runtime = AgentRuntime(
        client=llm,
        tools=registry([]),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(tokens=30),
    )
    with pytest.raises(BudgetExceeded, match="prompt alone"):
        await runtime.run(
            key="task-bloated",
            spec=spec(tools=()),
            prompt_version="proof.v1",
            messages=(Message(role=Role.USER, content="x" * 600),),
            output_model=FinalOutput,
        )
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_the_completion_allowance_is_the_reservation_minus_the_prompt() -> None:
    llm, gateway = client([submits("done")])
    runtime = AgentRuntime(
        client=llm,
        tools=registry([]),
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(tokens=100),
    )
    await runtime.run(
        key="task-allowance",
        spec=spec(tools=()),
        prompt_version="proof.v1",
        messages=(Message(role=Role.USER, content="x" * 90),),  # ~30 tokens
        output_model=FinalOutput,
    )
    assert gateway.calls[0].request.max_tokens == 70


@pytest.mark.asyncio
async def test_a_tool_that_outruns_its_declared_reservation_is_cut_off() -> None:
    """A reservation nothing enforces is a declaration. Without the timeout a
    tool reserved for a moment could run indefinitely, and the overrun would be
    discovered by debiting it afterwards -- the audit fence, one level down."""
    tools = ToolRegistry()

    async def crawls(request: BaseModel) -> BaseModel:
        await asyncio.sleep(5)
        return EchoOutput(value="too late")

    tools.register(
        name="echo",
        description="Takes far longer than it claims",
        input_model=EchoInput,
        output_model=EchoOutput,
        handler=crawls,
        wall_clock=timedelta(milliseconds=50),
    )
    llm, _ = client([
        StubReply.completed(
            "",
            prompt_tokens=1,
            completion_tokens=1,
            tool_calls=(ToolCall(id="call-1", name="echo", arguments='{"value":"go"}'),),
        )
    ])
    spends: list[RunBudget] = []
    runtime = AgentRuntime(
        client=llm,
        tools=tools,
        checkpoints=InMemoryCheckpointStore(),
        reservation=reservation(),
        on_spend=spends.append,
    )
    with pytest.raises(AgentRuntimeError, match="outran"):
        await runtime.run(
            key="task-slow",
            spec=spec(),
            prompt_version="proof.v1",
            messages=(Message(role=Role.USER, content="go"),),
            output_model=FinalOutput,
        )
    # The time it was allowed is charged, not the time it took.
    assert spends[-1].wall_clock == timedelta(milliseconds=50)


@pytest.mark.asyncio
async def test_an_invalid_submission_is_corrected_once_not_checkpointed_as_done() -> None:
    """Finding 4: checkpointing an unvalidated result made `finished` mean "will
    fail again on every resume, forever, without ever telling the model why"."""
    llm, _ = client([
        StubReply.completed(
            "",
            prompt_tokens=1,
            completion_tokens=1,
            tool_calls=(ToolCall(id="s1", name=SUBMIT_RESULT, arguments='{"wrong":"shape"}'),),
        ),
        submits("second time lucky", prompt_tokens=1, completion_tokens=1),
    ])
    stores = InMemoryCheckpointStore()
    runtime = AgentRuntime(
        client=llm, tools=registry([]), checkpoints=stores, reservation=reservation()
    )
    result = await runtime.run(
        key="task-bad-submit",
        spec=spec(tools=()),
        prompt_version="proof.v1",
        messages=(Message(role=Role.USER, content="go"),),
        output_model=FinalOutput,
    )
    assert result.output == FinalOutput(answer="second time lucky")
    corrections = [m for m in stores.values["task-bad-submit"].messages if m.role is Role.TOOL]
    assert "invalid result" in corrections[0].content


@pytest.mark.asyncio
async def test_submitting_alongside_other_calls_is_refused_not_guessed_at() -> None:
    llm, _ = client([
        StubReply.completed(
            "",
            prompt_tokens=1,
            completion_tokens=1,
            tool_calls=(
                ToolCall(id="e1", name="echo", arguments='{"value":"x"}'),
                ToolCall(id="s1", name=SUBMIT_RESULT, arguments='{"answer":"early"}'),
            ),
        ),
        submits("properly", prompt_tokens=1, completion_tokens=1),
    ])
    stores = InMemoryCheckpointStore()
    invoked: list[str] = []
    runtime = AgentRuntime(
        client=llm, tools=registry(invoked), checkpoints=stores, reservation=reservation()
    )
    result = await runtime.run(
        key="task-mixed",
        spec=spec(),
        prompt_version="proof.v1",
        messages=(Message(role=Role.USER, content="go"),),
        output_model=FinalOutput,
    )
    assert result.output == FinalOutput(answer="properly")
    assert invoked == []  # the discarded echo was never silently run
    corrections = [m for m in stores.values["task-mixed"].messages if m.role is Role.TOOL]
    assert "must be the only call" in corrections[0].content
