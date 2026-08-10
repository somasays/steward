"""The client's guarantees: alias resolution, typed results, and spend that
survives a failure.

The last one is the property everything above this package depends on. A call
that generates tokens and then fails has spent money, so each failure mode here
is asserted to carry a *non-zero* usage — a fixture that failed before spending
anything would agree with a client that reports nothing, forever.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from steward_llm.client import LLMClient
from steward_llm.completion import (
    CompletionRequest,
    FinishReason,
    Message,
    ModelUsage,
    Role,
    ToolCall,
    ToolSchema,
)
from steward_llm.config import (
    PRODUCTION_ALIASES,
    DeploymentMode,
    GatewayConfig,
    ModelBinding,
    TokenPricing,
    committed_production_config,
)
from steward_llm.endpoints import EndpointAllowlist
from steward_llm.errors import CompletionFailed, CompletionTimedOut, LLMError, UnboundAlias
from steward_llm.stub import StubGateway, StubReply
from steward_llm.transport import CompletionChunk

PRICING = TokenPricing(
    input_cost_per_token=Decimal("0.0000001"), output_cost_per_token=Decimal("0.0000003")
)

APPROVED = "http://vllm-reasoning-a.steward-inference.svc.cluster.local:8000/v1"
ALLOWLIST = EndpointAllowlist.from_urls([APPROVED])


def config(*extra: ModelBinding) -> GatewayConfig:
    """A validated production config: every required alias, all on the allowlist."""
    bindings = tuple(
        ModelBinding(
            alias=alias,
            model="hosted_vllm/qwen3-32b-instruct",
            api_base=APPROVED,
            pricing=PRICING,
        )
        for alias in sorted(PRODUCTION_ALIASES)
    )
    return GatewayConfig(
        mode=DeploymentMode.PRODUCTION,
        source="test",
        bindings=bindings + extra,
        allowlist=ALLOWLIST,
    )


def request(alias: str = "steward-fast", **overrides: object) -> CompletionRequest:
    fields: dict[str, object] = {
        "alias": alias,
        "messages": (Message(role=Role.USER, content="what is in this table?"),),
        "prompt_version": "catalog/describe@v3",
    }
    fields.update(overrides)
    return CompletionRequest(**fields)  # type: ignore[arg-type]


async def test_completes_a_request_and_reports_what_it_spent() -> None:
    gateway = StubGateway(
        {
            "steward-fast": [
                StubReply.completed(
                    "a customers table",
                    prompt_tokens=41,
                    completion_tokens=7,
                    cost_usd=Decimal("0.00031"),
                )
            ]
        }
    )
    result = await LLMClient(config(), gateway).complete(request())

    assert result.text == "a customers table"
    assert result.alias == "steward-fast"
    assert result.prompt_version == "catalog/describe@v3"
    assert result.finish_reason is FinishReason.STOP
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (41, 7)
    assert result.usage.total_tokens == 48
    assert result.usage.cost_usd == Decimal("0.00031")
    assert isinstance(result.usage.cost_usd, Decimal)
    assert result.usage.latency.total_seconds() > 0


async def test_streamed_chunks_are_joined_and_their_usage_summed() -> None:
    gateway = StubGateway(
        {
            "steward-fast": [
                StubReply.streaming(
                    ["a ", "customers ", "table"], prompt_tokens=41, cost_per_token=Decimal("0.0001")
                )
            ]
        }
    )
    result = await LLMClient(config(), gateway).complete(request())

    assert result.text == "a customers table"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (41, 3)
    assert result.usage.cost_usd == Decimal("0.0003")


async def test_tool_calls_come_back_typed() -> None:
    call = ToolCall(id="call-1", name="read_column_profile", arguments='{"column": "email"}')
    gateway = StubGateway({"steward-reasoning": [StubReply.completed("", tool_calls=[call])]})
    tools = (
        ToolSchema(
            name="read_column_profile",
            description="the latest profile of one column",
            parameters={"type": "object", "properties": {"column": {"type": "string"}}},
        ),
    )
    result = await LLMClient(config(), gateway).complete(request("steward-reasoning", tools=tools))

    assert result.tool_calls == (call,)
    assert result.finish_reason is FinishReason.TOOL_CALLS
    assert gateway.calls[0].request.tools == tools


async def test_a_failure_after_tokens_carries_the_tokens_it_spent() -> None:
    """The seam the agent runtime depends on: a stream that dies mid-answer has
    spent what it generated, and the error is where that number is reported."""
    gateway = StubGateway(
        {
            "steward-fast": [
                StubReply.streaming(
                    ["a ", "customers ", "table"],
                    prompt_tokens=41,
                    cost_per_token=Decimal("0.0001"),
                    fails_with=ConnectionResetError("gateway closed the stream"),
                )
            ]
        }
    )
    with pytest.raises(CompletionFailed) as raised:
        await LLMClient(config(), gateway).complete(request())

    assert raised.value.usage.completion_tokens == 3
    assert raised.value.usage.prompt_tokens == 41
    assert raised.value.usage.cost_usd == Decimal("0.0003")
    assert raised.value.usage.latency.total_seconds() > 0
    assert raised.value.alias == "steward-fast"
    assert isinstance(raised.value.__cause__, ConnectionResetError)
    assert "3 tokens" not in str(raised.value) and "44 tokens" in str(raised.value)


async def test_a_timeout_mid_stream_carries_its_spend_and_its_own_type() -> None:
    gateway = StubGateway(
        {
            "steward-fast": [
                StubReply.streaming(
                    ["half an "],
                    prompt_tokens=12,
                    cost_per_token=Decimal("0.0002"),
                    fails_with=TimeoutError("read timed out"),
                )
            ]
        }
    )
    with pytest.raises(CompletionTimedOut) as raised:
        await LLMClient(config(), gateway).complete(request())

    assert raised.value.usage.total_tokens == 13
    assert raised.value.usage.cost_usd == Decimal("0.0002")
    assert isinstance(raised.value, CompletionFailed)


async def test_a_cancelled_call_stays_exactly_a_cancellation() -> None:
    """The client owns failures, not cancellations. `asyncio.timeout` converts a
    cancellation to `TimeoutError` only when the exception reaching it *is*
    `CancelledError` — CPython compares the type by identity — and the worker's
    wall-clock verdict is built on that conversion (SPEC.md §13 D7, issue #57).
    An owned subclass would drop that proof back to a clock comparison, so the
    cancellation is left alone. The cost is stated in `errors.py`: a cancelled
    call's spend is bounded by the cap its task reserved, not reported."""
    gateway = StubGateway(
        {
            "steward-fast": [
                StubReply.streaming(
                    ["cut ", "off"],
                    prompt_tokens=9,
                    cost_per_token=Decimal("0.0005"),
                    fails_with=asyncio.CancelledError(),
                )
            ]
        }
    )
    with pytest.raises(asyncio.CancelledError) as raised:
        await LLMClient(config(), gateway).complete(request())

    assert type(raised.value) is asyncio.CancelledError
    assert not isinstance(raised.value, LLMError)


async def test_a_deadline_around_a_call_still_becomes_a_timeout() -> None:
    """The consequence of leaving cancellation alone, asserted rather than
    argued: a deadline wrapping the call converts to `TimeoutError`, which is the
    signal `steward_queue._bounded` reads to tell an overrun from a fault."""

    class Hanging:
        async def stream(self, call: object) -> AsyncIterator[CompletionChunk]:
            yield CompletionChunk(text="...", usage=ModelUsage(prompt_tokens=5, completion_tokens=1))
            await asyncio.sleep(30)

    client = LLMClient(config(), Hanging())  # type: ignore[arg-type]
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await client.complete(request())


async def test_a_stream_that_never_finishes_is_a_failure_not_a_result() -> None:
    gateway = StubGateway({"steward-fast": [StubReply(chunks=(CompletionChunk(text="half an answer"),))]})
    with pytest.raises(CompletionFailed, match="without a finish reason"):
        await LLMClient(config(), gateway).complete(request())


async def test_an_unbound_alias_is_refused_before_any_call_is_made() -> None:
    gateway = StubGateway({})
    client = LLMClient(config(), gateway)

    with pytest.raises(UnboundAlias) as raised:
        await client.complete(request("steward-rerank"))

    assert gateway.calls == [], "the alias must be refused before the gateway is touched"
    assert raised.value.usage == ModelUsage.nothing()
    assert "steward-fast" in str(raised.value)
    assert isinstance(raised.value, LLMError)


async def test_the_callable_aliases_are_the_config_s_not_a_constant() -> None:
    extra = ModelBinding(
        alias="steward-rerank",
        model="hosted_vllm/bge-reranker",
        api_base=APPROVED,
        pricing=PRICING,
    )
    client = LLMClient(config(extra), StubGateway({"steward-rerank": [StubReply.completed("ok")]}))

    assert "steward-rerank" in client.aliases
    assert (await client.complete(request("steward-rerank"))).alias == "steward-rerank"


async def test_every_binding_for_an_alias_reaches_the_transport() -> None:
    """SPEC.md §6 gives each alias two approved endpoints. Resolving to one of
    them would drop the redundancy that replaced provider diversity, so the
    client hands the transport all of them and routes between none."""
    gateway = StubGateway({"steward-fast": [StubReply.completed("ok")]})
    await LLMClient(committed_production_config(), gateway).complete(request())

    reached = gateway.calls[0].bindings
    assert len(reached) == 2
    assert {binding.api_base for binding in reached} == {
        "http://vllm-fast-a.steward-inference.svc.cluster.local:8000/v1",
        "http://vllm-fast-b.steward-inference.svc.cluster.local:8000/v1",
    }


async def test_a_pass_through_route_is_not_an_addressable_alias() -> None:
    """A pass-through endpoint is routing — the startup check validates it — but
    it is a proxy path, not a name a caller may complete against."""
    route = ModelBinding(alias="pass_through /v1/embeddings", model="openai/pass-through", api_base=APPROVED)
    client = LLMClient(config(route), StubGateway({}))

    assert "pass_through /v1/embeddings" not in client.aliases
    with pytest.raises(UnboundAlias):
        await client.complete(request("pass_through /v1/embeddings"))


async def test_the_stub_refuses_to_answer_more_calls_than_it_scripted() -> None:
    client = LLMClient(config(), StubGateway({"steward-fast": [StubReply.completed("once")]}))
    await client.complete(request())

    with pytest.raises(CompletionFailed) as raised:
        await client.complete(request())
    assert isinstance(raised.value.__cause__, LookupError)


def test_the_client_cannot_be_constructed_without_a_validated_config() -> None:
    """The I15 promotion, asserted on the surface itself: `GatewayConfig` is the
    only way to tell a client where calls may go, and that type cannot exist
    without having been validated (`GatewayConfig.__post_init__`). `mypy --strict`
    is what enforces the annotation; this pins the shape against a later edit that
    adds a path, an env read, or a `from_env` back door."""
    parameters = inspect.signature(LLMClient.__init__).parameters

    assert list(parameters) == ["self", "config", "transport"]
    assert parameters["config"].annotation == "GatewayConfig"
    assert parameters["config"].default is inspect.Parameter.empty
    assert [name for name in vars(LLMClient) if name.startswith("from_")] == []
