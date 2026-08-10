"""The typed async client: aliases in, owned results out, spend always reported.

Three properties this client exists to hold:

* **It cannot be built without a validated config.** `GatewayConfig` validates in
  its `__post_init__`, so an instance of that type is evidence the I15 startup
  refusal ran over the routing table. Taking one as the constructor's first
  parameter — never a path, never the environment, never an internal
  `from_env()` — promotes I15 from "every composition root remembers to call the
  refusal" to "a process that skipped it cannot construct a client at all".
* **An alias with no binding is refused before anything is called.** The set of
  callable aliases is the config's, not a constant in this module: a deployment
  that binds a fifth alias gets a fifth alias, and a typo gets an `UnboundAlias`
  instead of a request to a model nobody configured.
* **Every failure reports what it spent.** The transport streams increments that
  each carry their own usage; the client accumulates them and, on any failure,
  attaches the running total to the error it raises (`errors.py`).

**Why there is no LiteLLM transport here yet.** SPEC.md §6 puts the gateway in a
LiteLLM *proxy deployment* that owns budgets, caching, keys and retry policy —
but `GatewayConfig` is that proxy's routing table, and carries no address for the
proxy itself. So the two ways to write a real transport today are both wrong:
calling the bindings' vLLM endpoints in-process bypasses the proxy that SPEC
says owns those policies, and calling the proxy needs a URL and key that no
config type in this repo holds. Rather than invent either, the transport is a
seam (`transport.GatewayTransport`) with one deterministic implementation
(`StubGateway`); the gateway transport lands with the change that decides how a
worker addresses the proxy, and nothing above this module changes when it does.
"""

from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal

from steward_llm.completion import (
    CompletionRequest,
    CompletionResult,
    FinishReason,
    ModelUsage,
    ToolCall,
)
from steward_llm.config import PASS_THROUGH_MODEL, GatewayConfig, ModelBinding
from steward_llm.errors import CompletionFailed, CompletionTimedOut, UnboundAlias
from steward_llm.transport import GatewayCall, GatewayTransport
from steward_llm.wire import request_body, serialised_size

__all__ = ["LLMClient"]


def _alias_index(bindings: tuple[ModelBinding, ...]) -> dict[str, tuple[ModelBinding, ...]]:
    """Callable aliases, each with every binding the config gives it.

    An alias appears more than once on purpose (SPEC.md §6: two approved
    endpoints per alias), so this maps to a tuple — keeping only one binding
    would quietly discard the redundancy that replaced provider diversity.

    Pass-through routes are excluded. They are routing, which is why the startup
    check validates them, but they are a proxy path rather than a name a caller
    may address, and letting one answer to `complete()` would make a config's
    HTTP surface look like a model alias.
    """
    index: dict[str, list[ModelBinding]] = {}
    for binding in bindings:
        if binding.model == PASS_THROUGH_MODEL:
            continue
        index.setdefault(binding.alias, []).append(binding)
    return {alias: tuple(group) for alias, group in index.items()}


class LLMClient:
    """Completions against the gateway a validated `GatewayConfig` describes."""

    def __init__(self, config: GatewayConfig, transport: GatewayTransport) -> None:
        """Build a client for `config`, calling models through `transport`.

        `config` is the *only* way to say where calls may go, and it is a type
        that cannot exist unvalidated. `transport` has no default because the
        only implementation today is `StubGateway` — a default would have to be a
        gateway transport this repo cannot yet write honestly (see the module
        docstring), and defaulting to the stub would let a production process
        believe it was calling a model while talking to a fixture.
        """
        self._config = config
        self._transport = transport
        self._bindings = _alias_index(config.bindings)

    def prompt_ceiling(self, request: CompletionRequest, *, max_tokens: int) -> int | None:
        """An upper bound on this request's prompt tokens, or None if unstated.

        Measured over the **whole serialised body** -- the document the
        transport actually sends, from the one function that builds it -- plus
        the per-message chat-template allowance the alias's configuration
        declares. Measuring message contents and tool schemas instead left the
        JSON framing uncounted, and a bound over a subset of the request is not
        a bound on the request.

        `None` when the alias declares no `model_info`: an unbounded call is
        refused by the caller rather than started on a number nobody set.
        """
        allowance = self._template_allowance(request.alias)
        if allowance is None:
            return None
        body = request_body(request, max_tokens=max_tokens)
        return serialised_size(body) + allowance * len(request.messages)

    def _template_allowance(self, alias: str) -> int | None:
        """The dearest declared template overhead among this alias's bindings."""
        declared = [
            binding.pricing.chat_template_tokens_per_message
            for binding in self._bindings.get(alias, ())
            if binding.pricing is not None
        ]
        return max(declared) if declared else None

    def cost_ceiling(
        self, alias: str, *, prompt_tokens: int, completion_tokens: int
    ) -> Decimal | None:
        """The most a call on `alias` can cost, or None if its prices are unstated.

        Evaluated **per binding against this call's own shape**, then maximised.
        Ranking bindings by `input + output` instead is wrong for any request
        that is not an even mixture: given `(in 10, out 1)` and `(in 1, out 9)`,
        the first has the larger sum and the second costs more for an
        output-heavy call, so the reservation would underestimate whichever
        endpoint LiteLLM happened to pick. The client does not choose the
        endpoint (SPEC §6 gives each alias two), so a bound that holds has to
        assume the dearest *for this call*.
        """
        ceilings = [
            binding.pricing.ceiling(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            )
            for binding in self._bindings.get(alias, ())
            if binding.pricing is not None
        ]
        return max(ceilings) if ceilings else None

    @property
    def aliases(self) -> tuple[str, ...]:
        """The aliases this client can call, sorted — the config's, not a constant."""
        return tuple(sorted(self._bindings))

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run one completion, or raise an `LLMError` carrying what it spent.

        Failure modes, all of them typed and all of them carrying usage: an alias
        the config does not bind (`UnboundAlias`, refused before the transport is
        touched), a call that runs out of time (`CompletionTimedOut`), and
        anything else the transport raises (`CompletionFailed`) — including a
        stream that simply stops, which is a truncated answer and must not be
        returned as a whole one.
        """
        bindings = self._bindings.get(request.alias)
        if not bindings:
            raise UnboundAlias(
                f"{request.alias!r} is not bound by {self._config.source}; "
                f"this gateway binds {', '.join(self.aliases) or 'no aliases'}",
                alias=request.alias,
                usage=ModelUsage.nothing(),
            )
        started = time.monotonic()
        spent = ModelUsage.nothing()
        text: list[str] = []
        tool_calls: list[ToolCall] = []
        finish_reason: FinishReason | None = None
        try:
            async for chunk in self._transport.stream(GatewayCall(request=request, bindings=bindings)):
                spent = spent.plus(chunk.usage)
                text.append(chunk.text)
                if chunk.tool_call is not None:
                    tool_calls.append(chunk.tool_call)
                if chunk.finish_reason is not None:
                    finish_reason = chunk.finish_reason
        except TimeoutError as exc:
            raise CompletionTimedOut(
                f"{request.alias!r} timed out: {exc}",
                alias=request.alias,
                usage=spent.with_latency(self._elapsed(started)),
            ) from exc
        # A cancellation is not caught here at all, and neither are `SystemExit` and
        # `KeyboardInterrupt`: `except Exception` reaches none of them. That is the
        # decision `errors.py` argues -- a cancelled call must arrive at the runtime
        # as the exact `CancelledError` its deadline machinery identifies by type
        # (SPEC.md §13 D7, issue #57), and an overrun is charged the whole cap anyway.
        except Exception as exc:
            raise CompletionFailed(
                f"{request.alias!r} failed: {exc}",
                alias=request.alias,
                usage=spent.with_latency(self._elapsed(started)),
            ) from exc
        usage = spent.with_latency(self._elapsed(started))
        if finish_reason is None:
            raise CompletionFailed(
                f"{request.alias!r} ended its answer without a finish reason",
                alias=request.alias,
                usage=usage,
            )
        return CompletionResult(
            alias=request.alias,
            prompt_version=request.prompt_version,
            text="".join(text),
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason,
            usage=usage,
        )

    @staticmethod
    def _elapsed(started: float) -> timedelta:
        return timedelta(seconds=time.monotonic() - started)
