"""The production path to a model: an HTTP client for the LiteLLM proxy.

A **client for the proxy**, deliberately, not an in-process LiteLLM router. The
proxy is where SPEC §6 puts cost budgets, response caching, retry and fallback,
and key management; a worker that embedded the router would take a copy of all
of that and the two copies would drift. So this speaks the proxy's
OpenAI-compatible HTTP API and lets it route — which is also why the request
names a **Steward alias** and nothing else: the alias is the whole address, and
the routing table behind it is the deployment's (I14, I15).

What this module owns
---------------------
Everything between "a `GatewayCall`" and "increments of an answer": request
serialisation, the streaming protocol, timeouts, cancellation, usage
extraction, and translating whatever goes wrong into this package's own errors
so no HTTP type escapes (I2, I9).

Credentials
-----------
The key is held in a field that no representation reaches: `ProxyConfig` has a
`__repr__` that redacts it, it is never placed in an exception message, and it
is not part of the config's serialised form. What sends it is the request
builder, once, into a header. The reason for the care is that a gateway
credential in a traceback or a span payload is a credential in a log aggregator
(I6's reasoning applied to secrets rather than customer data).

What streaming can and cannot report
------------------------------------
The OpenAI streaming protocol reports usage **once, at the end**, so a call that
dies mid-stream cannot be told exactly what it spent. Rather than report zero --
which would make every failed call look free, the failure mode `errors.py`
exists to prevent -- each content delta carries one completion token as
provisional spend, and the final usage frame carries the *difference* between
what the proxy reports and what has already been counted. A completed call is
therefore exact; an interrupted one reports a lower bound. That is stated here
because it is a property of the protocol, not a choice this module could make
differently.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx

from steward_llm.completion import FinishReason, ModelUsage, ToolCall
from steward_llm.endpoints import GatewayConfigError
from steward_llm.errors import CompletionFailed, CompletionTimedOut
from steward_llm.transport import CompletionChunk, GatewayCall

__all__ = [
    "PROXY_KEY_ENV",
    "PROXY_URL_ENV",
    "InvalidProxyConfig",
    "LiteLLMProxyTransport",
    "ProxyConfig",
    "proxy_config_from_env",
]

PROXY_URL_ENV = "STEWARD_LLM_PROXY_URL"
PROXY_KEY_ENV = "STEWARD_LLM_PROXY_KEY"

INSECURE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
"""Hosts a plaintext proxy URL is tolerated on.

Not a general escape hatch: a loopback address is one a packet never leaves the
machine to reach, so there is no network to intercept it on. Anything else must
be HTTPS, because a gateway credential travels on every request and plaintext
would put it on the wire.
"""

REDACTED = "***"

DEFAULT_TIMEOUT = timedelta(seconds=60)


class InvalidProxyConfig(GatewayConfigError):
    """The proxy address or credential is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class ProxyConfig:
    """Where the gateway proxy is, and the credential for it.

    Separate from `GatewayConfig` on purpose: that one is the *routing table*
    the proxy itself runs, validated against the endpoint allowlist (I15), and
    this is the address of the proxy that runs it. Conflating them is what left
    the transport unimplementable -- a routing table has no address for the
    thing doing the routing.
    """

    base_url: str
    api_key: str = field(repr=False)
    """Never in a repr, an exception, or a serialised form. See the module
    docstring; `repr=False` is the mechanical half of that promise."""

    timeout: timedelta = DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise InvalidProxyConfig(f"{self.base_url!r} is not an http(s) URL")
        if parsed.scheme == "http" and parsed.hostname not in INSECURE_HOSTS:
            raise InvalidProxyConfig(
                f"{self.base_url} is plaintext and {parsed.hostname} is not a loopback "
                f"address; the gateway credential would travel unencrypted"
            )
        if not self.api_key:
            raise InvalidProxyConfig("no gateway credential configured")
        if self.timeout <= timedelta(0):
            raise InvalidProxyConfig("the proxy timeout must be positive")

    def __repr__(self) -> str:
        return f"ProxyConfig(base_url={self.base_url!r}, api_key={REDACTED!r})"


def proxy_config_from_env(env: Mapping[str, str]) -> ProxyConfig | None:
    """The proxy this process talks to, or None when it has none configured.

    `None` is not a degraded proxy: it means no model access at all, the state
    M0/M1 run in. A URL without a key, or a key without a URL, is a
    misconfiguration and refuses rather than falling back to either.
    """
    url = env.get(PROXY_URL_ENV, "").strip()
    key = env.get(PROXY_KEY_ENV, "").strip()
    if not url and not key:
        return None
    if not url or not key:
        raise InvalidProxyConfig(
            f"both {PROXY_URL_ENV} and {PROXY_KEY_ENV} are required to reach a gateway"
        )
    return ProxyConfig(base_url=url.rstrip("/"), api_key=key)


class LiteLLMProxyTransport:
    """`GatewayTransport` over the proxy's `/chat/completions` endpoint."""

    def __init__(self, config: ProxyConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout.total_seconds())

    async def aclose(self) -> None:
        await self._client.aclose()

    def _body(self, call: GatewayCall) -> dict[str, Any]:
        """The request, addressed by alias.

        `model` is the Steward alias and nothing else: the proxy holds the table
        that turns it into an endpoint, so a worker naming a provider model
        would be routing, which is the job this client exists not to do.
        `stream_options.include_usage` is what makes the proxy send the usage
        frame at all -- without it a completed call would report nothing spent.
        """
        request = call.request
        body: dict[str, Any] = {
            "model": request.alias,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [_message(message) for message in request.messages],
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        return body

    async def stream(self, call: GatewayCall) -> AsyncIterator[CompletionChunk]:
        """Send the call and yield the answer as it arrives.

        Cancellation is deliberately not caught: `asyncio.CancelledError` is how
        the worker's deadline reaches this coroutine, and swallowing or retyping
        it would break the enforcement built on it (SPEC §13 D11). The `async
        with` still closes the response, so a cancelled call does not leak a
        connection.
        """
        counted = 0
        try:
            async with self._client.stream(
                "POST",
                f"{self._config.base_url}/chat/completions",
                json=self._body(call),
                headers={"Authorization": f"Bearer {self._config.api_key}"},
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise CompletionFailed(
                        f"gateway returned {response.status_code} for "
                        f"{call.request.alias!r}: {_detail(response)}",
                        alias=call.request.alias,
                        usage=ModelUsage.nothing(),
                    )
                async for line in response.aiter_lines():
                    frame = _frame(line)
                    if frame is None:
                        continue
                    for chunk in _chunks(frame, counted):
                        counted += chunk.usage.completion_tokens
                        yield chunk
        except httpx.TimeoutException as exc:
            raise CompletionTimedOut(
                f"{call.request.alias!r} timed out after {self._config.timeout}",
                alias=call.request.alias,
                usage=ModelUsage.nothing(),
            ) from exc
        except httpx.HTTPError as exc:
            # Owned, and without the URL: the base URL is harmless but the header
            # that went with it is not, and an httpx error's string form has been
            # known to carry request detail. Name the alias instead.
            raise CompletionFailed(
                f"the gateway could not be reached for {call.request.alias!r}: "
                f"{type(exc).__name__}",
                alias=call.request.alias,
                usage=ModelUsage.nothing(),
            ) from exc


def _detail(response: httpx.Response) -> str:
    """A non-2xx body, trimmed, and never the request that caused it."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message", ""))[:200]
    return str(payload)[:200]


def _message(message: Any) -> dict[str, Any]:
    """One message in the wire shape, carrying whatever it holds."""
    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return payload


def _frame(line: str) -> dict[str, Any] | None:
    """One SSE data frame, or None for the lines that are not one.

    A malformed frame is a failure rather than something to skip past: a stream
    this client cannot read is a stream whose answer it does not know, and
    continuing would silently drop part of a completion.
    """
    stripped = line.strip()
    if not stripped or not stripped.startswith("data:"):
        return None
    payload = stripped.removeprefix("data:").strip()
    if payload == "[DONE]":
        return None
    try:
        frame = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CompletionFailed(
            f"the gateway sent a frame that is not JSON: {exc}",
            alias="unknown",
            usage=ModelUsage.nothing(),
        ) from exc
    if not isinstance(frame, dict):
        raise CompletionFailed(
            "the gateway sent a frame that is not an object",
            alias="unknown",
            usage=ModelUsage.nothing(),
        )
    return frame


def _chunks(frame: dict[str, Any], counted: int) -> list[CompletionChunk]:
    """The increments one frame carries.

    A frame is either a delta (content, or part of a tool call) or the usage
    frame that ends the stream. The usage frame reconciles: it reports the
    proxy's totals, and what is emitted here is the difference from what the
    deltas already accounted for, so the client's running sum lands exactly on
    the proxy's number rather than beside it.
    """
    chunks: list[CompletionChunk] = []
    usage = frame.get("usage")
    for choice in frame.get("choices") or ():
        delta = choice.get("delta") or {}
        finish = choice.get("finish_reason")
        content = delta.get("content")
        if content:
            chunks.append(
                CompletionChunk(
                    text=content,
                    usage=ModelUsage(
                        prompt_tokens=0,
                        completion_tokens=1,
                        cost_usd=Decimal(0),
                        latency=timedelta(0),
                    ),
                )
            )
        for call in delta.get("tool_calls") or ():
            function = call.get("function") or {}
            chunks.append(
                CompletionChunk(
                    tool_call=ToolCall(
                        id=str(call.get("id") or ""),
                        name=str(function.get("name") or ""),
                        arguments=str(function.get("arguments") or ""),
                    )
                )
            )
        if finish:
            chunks.append(CompletionChunk(finish_reason=_finish(finish)))
    if isinstance(usage, dict):
        chunks.append(
            CompletionChunk(
                usage=ModelUsage(
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=max(
                        0, int(usage.get("completion_tokens") or 0) - counted
                    ),
                    cost_usd=Decimal(str(usage.get("cost") or "0")),
                    latency=timedelta(0),
                )
            )
        )
    return chunks


def _finish(reason: str) -> FinishReason:
    """The model's stated reason, or a failure -- never a guess.

    An unrecognised reason is refused rather than mapped onto `STOP`: the loop
    above decides whether a run is finished by this value, and quietly calling
    an unknown state "stop" would end runs that did not end.
    """
    try:
        return FinishReason(reason)
    except ValueError as exc:
        raise CompletionFailed(
            f"the gateway reported an unknown finish reason: {reason!r}",
            alias="unknown",
            usage=ModelUsage.nothing(),
        ) from exc
