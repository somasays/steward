"""The seam between the client and whatever actually reaches a model.

A transport receives a `GatewayCall` — the caller's request plus every binding
the alias resolves to — and yields the answer as increments. Two things follow
from that shape, and both are the point:

* **Spend is reported as it happens, not at the end.** Each chunk carries the
  usage that increment cost, so a stream that dies after two hundred tokens has
  already told the client about two hundred tokens. A transport that reported
  usage only in a final chunk would make every failure look free, which is the
  failure mode `errors.py` exists to prevent.
* **Routing is the transport's business, not the client's.** The client hands
  over *all* the bindings for an alias (SPEC.md §6 gives each alias two approved
  endpoints) and does not choose between them: retry, fallback and load
  balancing belong to the gateway, and a second retry layer in the client would
  be a duplicate of policy that already lives in the LiteLLM config.

There is one implementation in this package today: `StubGateway`, which is
deterministic and injectable. The transport that speaks to the deployment's
LiteLLM gateway lands with the deployment that has one to speak to — see the
`steward_llm.client` module docstring for why that is not this branch's call to
make.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

from steward_llm.completion import CompletionRequest, FinishReason, ModelUsage, ToolCall
from steward_llm.config import ModelBinding

__all__ = [
    "CompletionChunk",
    "GatewayCall",
    "GatewayTransport",
]


@dataclass(frozen=True, slots=True)
class GatewayCall:
    """A request with its alias already resolved to the destinations it may use."""

    request: CompletionRequest
    bindings: tuple[ModelBinding, ...]
    """Every binding the config holds for this alias — two in production, so a
    transport can route around one that is down without leaving the allowlist."""


@dataclass(frozen=True, slots=True)
class CompletionChunk:
    """One increment of an answer, and what that increment spent.

    `usage` is a delta, not a running total: the client adds chunks up. The
    prompt's tokens belong on the first chunk a transport yields, because they
    are spent as soon as the model accepts the prompt — a call that fails before
    a single completion token still cost the prompt.
    """

    text: str = ""
    tool_call: ToolCall | None = None
    usage: ModelUsage = field(default_factory=ModelUsage.nothing)
    finish_reason: FinishReason | None = None
    """Set on the last chunk of a completed answer. A stream that ends without
    one never finished, and the client treats that as a failure rather than
    inventing a reason the model did not give."""


class GatewayTransport(Protocol):
    """Whatever can carry a call to a model and stream the answer back.

    Implementations may raise anything; the client catches it and re-raises an
    owned error carrying the usage accumulated so far, so no provider exception
    type crosses this package's boundary (I2/I9).
    """

    def stream(self, call: GatewayCall) -> AsyncIterator[CompletionChunk]: ...
