"""What a caller asks a model for, and what comes back.

These models are the package boundary. LiteLLM and provider SDKs stay behind
them (I2/I9): a caller names a gateway *alias*, hands over messages and tool
schemas it owns, and receives text, tool calls and the usage the call spent —
never a provider response object, never a `dict` shaped by whoever answered.

Two fields are worth their own sentence:

* **`cost_usd` is a `Decimal`**, matching `RunBudget.cost_usd`, because it is
  money that gets summed across a run and compared to a cap. A float that
  accumulates over a few hundred calls disagrees with the cap it is checked
  against, and the disagreement is silent.
* **`ToolCall.arguments` is the JSON text the model emitted**, not a parsed
  mapping. Validating it belongs to the caller's tool registry, which owns the
  input model for that tool; parsing it here would put a second, weaker
  validation in the layer that knows the least about what the arguments mean.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CompletionRequest",
    "CompletionResult",
    "FinishReason",
    "GatewayModel",
    "Message",
    "ModelUsage",
    "Role",
    "ToolCall",
    "ToolSchema",
]


class GatewayModel(BaseModel):
    """Frozen, closed base for this package's models.

    Same discipline as `steward_schemas._base.SchemaModel`, restated rather than
    imported for the reason `steward_catalog.models.CatalogModel` restates it:
    that base is another package's private module (I4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class Role(StrEnum):
    """Who a message is from. `TOOL` carries a tool's output back to the model."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    """Why the model stopped.

    `LENGTH` is not a synonym for `STOP`: it means the answer was truncated by a
    token limit, and a caller that treats it as a complete answer is reading half
    a result as a whole one.
    """

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"


class Message(GatewayModel):
    """One turn of a conversation."""

    role: Role
    content: str
    tool_call_id: str | None = None
    """The call this message answers. Required on a `TOOL` message, meaningless
    on any other — the model matches a result to the call it made by this id."""


class ToolSchema(GatewayModel):
    """A tool the model may call: its name, what it does, and its parameters as
    JSON Schema.

    `parameters` is a JSON Schema document — a `dict` because that is what it
    is, the same reason `TaskSpec.payload` is one. This package never reads it:
    it is produced by the caller's tool registry from that tool's Pydantic input
    model and forwarded verbatim, and the arguments that come back are validated
    against the same model by the registry, not here.
    """

    name: str = Field(min_length=1)
    description: str
    parameters: dict[str, Any]


class ToolCall(GatewayModel):
    """A model's request to call one tool."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: str
    """The arguments as JSON text, exactly as the model emitted them — including
    when they are not valid JSON, which is a thing models do and the caller's
    registry is the layer that decides what to do about it."""


class ModelUsage(GatewayModel):
    """What a call spent: tokens, money, and time.

    Carried by a `CompletionResult` and, just as importantly, by every error this
    package raises — a call that failed after generating tokens spent them, and a
    runtime that debits only successful calls under-reports what a run cost (I12).
    """

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    latency: timedelta = Field(default=timedelta(0), ge=timedelta(0))
    """Wall-clock time the call took, measured by the client around the whole
    call. A chunk of a streamed answer leaves this zero: an increment of a
    stream has a cost and a token count, but not a latency of its own."""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @classmethod
    def nothing(cls) -> ModelUsage:
        """The usage of a call that never reached a model — a refused alias."""
        return cls()

    def plus(self, other: ModelUsage) -> ModelUsage:
        """This usage plus `other`, dimension by dimension.

        How a stream's spend accumulates: the client adds each increment as it
        arrives, so the total it holds when a call fails mid-stream is what was
        spent up to that point rather than nothing.
        """
        return ModelUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            latency=self.latency + other.latency,
        )

    def with_latency(self, latency: timedelta) -> ModelUsage:
        """This usage, timed. The client measures the call; the transport counts."""
        return self.model_copy(update={"latency": latency})


class CompletionRequest(GatewayModel):
    """One completion, addressed to a gateway alias.

    `alias` is a gateway alias (`steward-reasoning`, `steward-fast`, ...), never a
    provider or model name: which model an alias resolves to is configuration
    (I14) and where it may resolve to is I15's subject. An alias with no binding
    in the client's config is refused before a call is made.
    """

    alias: str = Field(min_length=1)
    messages: tuple[Message, ...] = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    """The version of the prompt these messages were built from (I10). Required,
    and echoed on the result: a generation span without one cannot be traced back
    to what produced it (H6), and "unversioned" is not a state this seam allows."""

    tools: tuple[ToolSchema, ...] = ()
    max_tokens: int | None = Field(default=None, ge=1)


class CompletionResult(GatewayModel):
    """A completed call: what the model said, and what it cost."""

    alias: str
    prompt_version: str
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason
    usage: ModelUsage
