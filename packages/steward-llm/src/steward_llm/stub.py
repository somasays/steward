"""A deterministic gateway for tests, injected rather than patched.

It ships in `src/`, not in a test tree, because it is the fixture every caller of
this package tests against — the agent runtime's whole proof runs on it. Making
it a real, typed implementation of `GatewayTransport` means those tests exercise
the client's actual code path: alias resolution, usage accumulation and error
translation all happen for real, and only the thing across the wire is scripted.

The alternative — monkeypatching LiteLLM internals or pointing a call at a local
HTTP server — was rejected on both counts: it would test the patch rather than
the seam, and it would let a test's model access escape the one type that says
where calls may go.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from steward_llm.completion import FinishReason, ModelUsage, ToolCall
from steward_llm.transport import CompletionChunk, GatewayCall

__all__ = ["StubGateway", "StubReply"]


@dataclass(frozen=True, slots=True)
class StubReply:
    """One scripted answer: the chunks to yield, then optionally a failure.

    A reply with both is the case that matters most — a stream that produces
    tokens and *then* fails, which is what a mid-stream disconnect or a 500 after
    generation looks like, and which must leave the caller holding the spend.
    """

    chunks: tuple[CompletionChunk, ...] = ()
    fails_with: BaseException | None = None

    @classmethod
    def completed(
        cls,
        text: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: Decimal = Decimal(0),
        tool_calls: Iterable[ToolCall] = (),
        finish_reason: FinishReason = FinishReason.STOP,
    ) -> StubReply:
        """A whole answer in one chunk. The common case, spelled once."""
        calls = tuple(tool_calls)
        return cls(
            chunks=(
                CompletionChunk(
                    text=text,
                    usage=ModelUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cost_usd=cost_usd,
                    ),
                    finish_reason=None if calls else finish_reason,
                ),
                *(
                    CompletionChunk(
                        tool_call=call,
                        finish_reason=FinishReason.TOOL_CALLS if index == len(calls) - 1 else None,
                    )
                    for index, call in enumerate(calls)
                ),
            )
        )

    @classmethod
    def streaming(
        cls,
        tokens: Sequence[str],
        *,
        prompt_tokens: int = 0,
        cost_per_token: Decimal = Decimal(0),
        fails_with: BaseException | None = None,
    ) -> StubReply:
        """One chunk per token, each carrying its own spend.

        The prompt's tokens ride on the first chunk, because that is when they
        are spent: a call that fails before any completion token still cost them.
        With `fails_with`, the stream ends in that exception after every token has
        been yielded and counted — the fixture behind the property that a failed
        call still reports its usage.
        """
        chunks = tuple(
            CompletionChunk(
                text=token,
                usage=ModelUsage(
                    prompt_tokens=prompt_tokens if index == 0 else 0,
                    completion_tokens=1,
                    cost_usd=cost_per_token,
                ),
                finish_reason=(
                    None if fails_with is not None or index < len(tokens) - 1 else FinishReason.STOP
                ),
            )
            for index, token in enumerate(tokens)
        )
        return cls(chunks=chunks, fails_with=fails_with)


@dataclass(slots=True)
class StubGateway:
    """A `GatewayTransport` that answers from a script, keyed by alias.

    Replies are consumed in order per alias. Running out is an error rather than
    a repeat: a test that calls more often than it scripted is a test whose
    expectations have drifted, and silently replaying the last answer is how that
    goes unnoticed.
    """

    replies: Mapping[str, Sequence[StubReply]]
    calls: list[GatewayCall] = field(default_factory=list)
    """Every call this gateway received, in order — including the bindings the
    client resolved for it, so a test can assert what was reachable."""

    _served: dict[str, int] = field(default_factory=dict)

    async def stream(self, call: GatewayCall) -> AsyncIterator[CompletionChunk]:
        self.calls.append(call)
        alias = call.request.alias
        scripted = self.replies.get(alias, ())
        index = self._served.get(alias, 0)
        if index >= len(scripted):
            raise LookupError(f"StubGateway has no reply {index + 1} scripted for {alias!r}")
        self._served[alias] = index + 1
        reply = scripted[index]
        for chunk in reply.chunks:
            yield chunk
        if reply.fails_with is not None:
            raise reply.fails_with
