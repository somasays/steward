"""The HTTP boundary, against a fake proxy.

Every assertion here is about bytes on the wire or the types that come back out,
because that is the whole of what this module owns. A stub transport would test
the client above it; `httpx.MockTransport` tests the request this actually
sends, the stream it actually parses, and what it does when either is wrong.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from steward_llm import (
    CompletionRequest,
    CompletionTimedOut,
    DeploymentMode,
    EndpointAllowlist,
    FinishReason,
    GatewayConfig,
    InvalidProxyConfig,
    LiteLLMProxyTransport,
    LLMClient,
    Message,
    ModelBinding,
    ProxyConfig,
    Role,
    TokenPricing,
    ToolSchema,
    proxy_config_from_env,
)
from steward_llm.endpoints import GatewayConfigError
from steward_llm.errors import CompletionFailed
from steward_llm.transport import GatewayCall

KEY = "sk-do-not-log-me"

PRICING = TokenPricing(
    input_cost_per_token=Decimal("0.00001"),
    output_cost_per_token=Decimal("0.00002"),
    chat_template_tokens_per_message=8,
)


def config(**overrides: object) -> ProxyConfig:
    defaults: dict[str, object] = {"base_url": "https://gateway.internal/v1", "api_key": KEY}
    return ProxyConfig(**{**defaults, **overrides})  # type: ignore[arg-type]


def call(*, tools: tuple[ToolSchema, ...] = (), max_tokens: int | None = None) -> GatewayCall:
    return GatewayCall(
        request=CompletionRequest(
            alias="steward-fast",
            messages=(Message(role=Role.USER, content="describe this table"),),
            prompt_version="catalog/describe@v3",
            tools=tools,
            max_tokens=max_tokens,
        ),
        bindings=(
            ModelBinding(
                alias="steward-fast",
                model="hosted_vllm/qwen",
                api_base="https://a/v1",
                pricing=PRICING,
            ),
        ),
    )


def sse(*frames: object) -> str:
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    return body + "data: [DONE]\n\n"


def delta(text: str) -> dict[str, object]:
    return {"choices": [{"delta": {"content": text}, "finish_reason": None}]}


def finished(reason: str = "stop") -> dict[str, object]:
    return {"choices": [{"delta": {}, "finish_reason": reason}]}


def usage_frame(prompt: int, completion: int, cost: str) -> dict[str, object]:
    return {
        "choices": [],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "cost": cost},
    }


def transport_for(handler: object) -> LiteLLMProxyTransport:
    mock = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return LiteLLMProxyTransport(config(), httpx.AsyncClient(transport=mock))


def responding(body: str, status: int = 200) -> object:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text=body)

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


async def drain(transport: LiteLLMProxyTransport, gateway_call: GatewayCall) -> list[object]:
    return [chunk async for chunk in transport.stream(gateway_call)]


class TestConfiguration:
    def test_plaintext_is_refused_off_loopback(self) -> None:
        with pytest.raises(InvalidProxyConfig, match="unencrypted"):
            config(base_url="http://gateway.internal/v1")

    def test_plaintext_is_allowed_on_loopback(self) -> None:
        assert config(base_url="http://127.0.0.1:4000/v1").base_url.startswith("http://")

    def test_a_url_without_a_key_refuses(self) -> None:
        with pytest.raises(InvalidProxyConfig, match="both"):
            proxy_config_from_env({"STEWARD_LLM_PROXY_URL": "https://gateway/v1"})

    def test_neither_configured_is_no_gateway_rather_than_an_error(self) -> None:
        assert proxy_config_from_env({}) is None

    def test_the_credential_is_not_in_any_representation(self) -> None:
        settings = config()
        assert KEY not in repr(settings)
        assert KEY not in str(settings)
        assert KEY not in json.dumps(
            {"base_url": settings.base_url, "repr": repr(settings)}
        )


class TestRequestSerialisation:
    async def test_the_model_is_the_alias_and_usage_is_requested(self) -> None:
        handler = responding(sse(delta("ok"), finished(), usage_frame(11, 1, "0.001")))
        await drain(transport_for(handler), call())

        sent = json.loads(handler.seen[0].content)  # type: ignore[attr-defined]
        # The alias is the whole address: routing is the proxy's table, not ours.
        assert sent["model"] == "steward-fast"
        assert sent["stream"] is True
        assert sent["stream_options"] == {"include_usage": True}
        assert sent["messages"] == [{"role": "user", "content": "describe this table"}]

    async def test_the_credential_travels_in_the_header_only(self) -> None:
        handler = responding(sse(finished(), usage_frame(1, 0, "0")))
        await drain(transport_for(handler), call())

        request = handler.seen[0]  # type: ignore[attr-defined]
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert KEY not in request.content.decode()

    async def test_tools_and_max_tokens_are_serialised_when_present(self) -> None:
        handler = responding(sse(finished(), usage_frame(1, 0, "0")))
        tools = (ToolSchema(name="echo", description="echo it", parameters={"type": "object"}),)
        await drain(transport_for(handler), call(tools=tools, max_tokens=64))

        sent = json.loads(handler.seen[0].content)  # type: ignore[attr-defined]
        assert sent["max_tokens"] == 64
        assert sent["tools"][0]["function"]["name"] == "echo"


class TestStreamAssembly:
    async def test_deltas_assemble_and_usage_reconciles_to_the_proxys_total(self) -> None:
        handler = responding(
            sse(delta("a "), delta("customers "), delta("table"), finished(), usage_frame(41, 7, "0.0003"))
        )
        chunks = await drain(transport_for(handler), call())

        assert "".join(chunk.text for chunk in chunks) == "a customers table"  # type: ignore[attr-defined]
        # Three deltas counted one token each provisionally; the usage frame
        # carries the remaining four so the sum is the proxy's number exactly.
        assert sum(chunk.usage.completion_tokens for chunk in chunks) == 7  # type: ignore[attr-defined]
        assert sum(chunk.usage.prompt_tokens for chunk in chunks) == 41  # type: ignore[attr-defined]
        # Computed from the tokens the gateway reported and the prices the
        # routing table declares -- not read from `usage.cost`, which is not part
        # of the OpenAI streaming object and left every real call recording $0.
        assert sum(chunk.usage.cost_usd for chunk in chunks) == (  # type: ignore[attr-defined]
            41 * Decimal("0.00001") + 7 * Decimal("0.00002")
        )
        assert chunks[-2].finish_reason is FinishReason.STOP  # type: ignore[attr-defined]

    async def test_a_stream_that_dies_mid_answer_reports_a_lower_bound(self) -> None:
        """Not zero. The protocol reports usage at the end, so an interrupted
        call cannot be exact -- but reporting nothing would make every failure
        look free, which is the thing the owned errors exist to prevent."""
        handler = responding(sse(delta("one"), delta("two")))  # no usage frame
        chunks = await drain(transport_for(handler), call())

        assert sum(chunk.usage.completion_tokens for chunk in chunks) == 2  # type: ignore[attr-defined]

    async def test_a_fragmented_tool_call_assembles_into_one(self) -> None:
        """How a gateway actually streams a tool call: identity on the first
        delta, arguments in slices after it, correlated by `index` and not by
        id -- most of the fragments do not carry one. Emitting a call per delta
        produced three malformed calls with empty names and partial JSON, and
        the client above appends rather than merges, so the run saw all three."""
        fragments = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "echo", "arguments": "{"},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '"value":'}}]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '"steward"}'}}]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        handler = responding(sse(*fragments, usage_frame(9, 6, "0.0002")))
        chunks = await drain(transport_for(handler), call())

        calls = [chunk.tool_call for chunk in chunks if chunk.tool_call is not None]  # type: ignore[attr-defined]
        assert len(calls) == 1, "each fragment became its own call"
        assert calls[0].id == "call_1"
        assert calls[0].name == "echo"
        assert json.loads(calls[0].arguments) == {"value": "steward"}

    async def test_two_parallel_tool_calls_stay_separate(self) -> None:
        """Correlated by index, so concurrent calls do not merge into one."""
        frame = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"}},
                            {"index": 1, "id": "b", "function": {"name": "two", "arguments": "{}"}},
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        handler = responding(sse(frame, usage_frame(3, 2, "0")))
        chunks = await drain(transport_for(handler), call())

        calls = [chunk.tool_call for chunk in chunks if chunk.tool_call is not None]  # type: ignore[attr-defined]
        assert [(c.id, c.name) for c in calls] == [("a", "one"), ("b", "two")]

    async def test_tool_calls_come_back_typed(self) -> None:
        frame = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "echo", "arguments": '{"v":1}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        handler = responding(sse(frame, usage_frame(3, 2, "0")))
        chunks = await drain(transport_for(handler), call())

        calls = [chunk.tool_call for chunk in chunks if chunk.tool_call is not None]  # type: ignore[attr-defined]
        assert (calls[0].id, calls[0].name, calls[0].arguments) == ("c1", "echo", '{"v":1}')
        # The finish reason arrives after the call it belongs to, so a client
        # reading in order never ends the answer without it.
        assert chunks.index(next(c for c in chunks if c.tool_call is not None)) < chunks.index(  # type: ignore[attr-defined]
            next(c for c in chunks if c.finish_reason is not None)
        )


class TestFailures:
    async def test_a_non_2xx_becomes_an_owned_error_naming_the_alias(self) -> None:
        handler = responding(json.dumps({"error": {"message": "no capacity"}}), status=503)
        with pytest.raises(CompletionFailed, match="503") as raised:
            await drain(transport_for(handler), call())
        assert "no capacity" in str(raised.value)
        assert raised.value.alias == "steward-fast"

    async def test_a_malformed_frame_is_a_failure_not_a_skipped_line(self) -> None:
        handler = responding("data: {not json}\n\n")
        with pytest.raises(CompletionFailed, match="not JSON"):
            await drain(transport_for(handler), call())

    async def test_an_unknown_finish_reason_is_refused_rather_than_mapped(self) -> None:
        handler = responding(sse(finished("moon_phase")))
        with pytest.raises(CompletionFailed, match="unknown finish reason"):
            await drain(transport_for(handler), call())

    async def test_a_timeout_becomes_the_owned_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with pytest.raises(CompletionTimedOut, match="timed out"):
            await drain(transport_for(handler), call())

    async def test_a_transport_error_does_not_carry_the_credential(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"refused by {request.headers['authorization']}", request=request)

        with pytest.raises(CompletionFailed) as raised:
            await drain(transport_for(handler), call())
        assert KEY not in str(raised.value)
        assert KEY not in repr(raised.value)

    async def test_cancellation_is_not_retyped(self) -> None:
        """The worker's deadline arrives as a cancellation, and D11's overrun
        proof depends on it reaching the loop as itself."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise TimeoutError("cancelled")  # not an httpx error

        with pytest.raises(TimeoutError):
            await drain(transport_for(handler), call())


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Nothing here may open a socket -- and this makes that true rather than
    stating it.

    It was a bare `yield` under exactly this docstring: a guarantee asserted in
    prose and enforced by nothing, which is the shape this repo hunts. Now the
    real connection pool raises, so a test that reached the network would fail
    instead of quietly reaching it.
    """

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this suite drives MockTransport; nothing here opens a socket")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    yield


def test_the_timeout_must_be_positive() -> None:
    with pytest.raises(InvalidProxyConfig, match="positive"):
        config(timeout=timedelta(0))


class TestUrlSafety:
    def test_userinfo_is_refused_rather_than_stripped(self) -> None:
        with pytest.raises(InvalidProxyConfig, match="userinfo") as raised:
            config(base_url=f"https://user:{KEY}@gateway.internal/v1")
        assert KEY not in str(raised.value)

    def test_a_query_or_fragment_is_refused(self) -> None:
        with pytest.raises(InvalidProxyConfig, match="query or fragment"):
            config(base_url=f"https://gateway.internal/v1?token={KEY}")

    async def test_a_reflected_credential_is_redacted_from_the_error(self) -> None:
        """The body is the remote end's text. A proxy that echoes the
        Authorization header into its error message would otherwise put our key
        inside an owned exception."""
        handler = responding(
            json.dumps({"error": {"message": f"bad key: Bearer {KEY}"}}), status=401
        )
        with pytest.raises(CompletionFailed) as raised:
            await drain(transport_for(handler), call())
        assert KEY not in str(raised.value)
        assert "***" in str(raised.value)


class TestPricingBounds:
    def test_the_dearest_binding_for_this_call_wins_not_the_largest_sum(self) -> None:
        """Ranking by `input + output` is wrong for any request that is not an
        even mixture: the binding with the larger sum can be the cheaper one for
        an output-heavy call, and the reservation would then underestimate
        whichever endpoint answered."""
        cheap_out = TokenPricing(
            input_cost_per_token=Decimal("10"),
            output_cost_per_token=Decimal("1"),
            chat_template_tokens_per_message=8,
        )
        dear_out = TokenPricing(
            input_cost_per_token=Decimal("1"),
            output_cost_per_token=Decimal("9"),
            chat_template_tokens_per_message=8,
        )
        assert (
            cheap_out.input_cost_per_token + cheap_out.output_cost_per_token
            > dear_out.input_cost_per_token + dear_out.output_cost_per_token
        )
        # ... and yet, for one prompt token and ten completion tokens:
        assert dear_out.ceiling(prompt_tokens=1, completion_tokens=10) > cheap_out.ceiling(
            prompt_tokens=1, completion_tokens=10
        )

    @pytest.mark.parametrize("bad", ["-0.1", "NaN", "Infinity"])
    def test_a_price_that_is_not_a_price_is_refused(self, bad: str) -> None:
        """A NaN price makes every budget comparison false, an infinite one
        refuses every call, and a negative one funds a run by being used."""
        with pytest.raises(GatewayConfigError, match="finite, non-negative"):
            TokenPricing(
                input_cost_per_token=Decimal(bad),
                output_cost_per_token=Decimal("1"),
                chat_template_tokens_per_message=8,
            )

    async def test_a_malformed_tool_call_fails_the_stream_rather_than_vanishing(self) -> None:
        """Dropping it would produce a `tool_calls` finish with the call absent:
        the model would appear to have asked for nothing."""
        frame = {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        }
        handler = responding(sse(frame, usage_frame(1, 1, "0")))
        with pytest.raises(CompletionFailed, match="without an id or a name"):
            await drain(transport_for(handler), call())


class TestInterruptedCallAccounting:
    """An interrupted production call is charged, not forgiven (I12).

    The gateway reports tokens once, in a terminal frame. A stream cut short
    before it leaves the prompt uncounted and the cost at zero -- so a failed
    call that really spent money looked free, which is the direction that lets a
    run overspend quietly. It is charged its proven upper bound instead: the
    ceiling the preflight already approved.
    """

    def client_for(self, handler: object) -> LLMClient:
        endpoint = "https://gateway.internal/v1"
        config = GatewayConfig(
            mode=DeploymentMode.DEVELOPMENT,
            source="test",
            bindings=(
                ModelBinding(
                    alias="steward-fast",
                    model="hosted_vllm/qwen",
                    api_base=endpoint,
                    pricing=PRICING,
                ),
            ),
            allowlist=EndpointAllowlist.from_urls((endpoint,)),
        )
        return LLMClient(config, transport_for(handler))

    async def test_a_stream_cut_before_the_usage_frame_is_charged_its_bound(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # Content, and then the connection dies -- no usage frame, which is
            # the only place a gateway states what the call cost.
            def body() -> Iterator[bytes]:
                yield f"data: {json.dumps(delta('partial answer'))}\n\n".encode()
                raise httpx.ReadError("connection reset", request=request)

            return httpx.Response(200, content=body())

        request = CompletionRequest(
            alias="steward-fast",
            messages=(Message(role=Role.USER, content="describe this table"),),
            prompt_version="catalog/describe@v3",
            max_tokens=500,
        )
        with pytest.raises(CompletionFailed) as raised:
            await self.client_for(handler).complete(request)

        spent = raised.value.usage
        assert spent.prompt_tokens > 0, "a failed call reported no prompt tokens"
        assert spent.cost_usd > 0, "a failed call that spent money was charged nothing"
        # And it is the bound the preflight approved, not a number invented here.
        bound = self.client_for(handler).prompt_ceiling(request, max_tokens=500)
        assert bound is not None and spent.prompt_tokens == bound
