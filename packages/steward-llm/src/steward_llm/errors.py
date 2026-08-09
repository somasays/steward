"""What this package raises when a call does not produce a result.

Every error here carries the usage the call spent before it failed, because that
is the number a bounded runtime needs and the one an exception normally throws
away. A call that streams two hundred tokens and then loses its connection has
spent those tokens and that money; a runtime that debits only successful calls
walks a run past its cap one failure at a time (I12), and no amount of retry
policy above this layer can recover a number this layer did not report.

Nothing from LiteLLM or a provider SDK is raised out of this package (I2/I9):
whatever a transport raises is caught and re-raised as one of these, chained to
the original so the cause is still readable in a traceback.

**One failure deliberately has no type here: a cancellation.** A cancelled call
has spent tokens too, so an owned error carrying them looks like the obvious
move — and it was rejected, twice over. `asyncio.timeout` converts a cancellation
into `TimeoutError` only when the exception reaching it *is* `CancelledError`
(CPython 3.12 compares the type by identity, not by `issubclass`), and the
worker's wall-clock enforcement is built on exactly that conversion: `_bounded`
recognises the cap's own expiry in-band and #57 established that this verdict
must rest on that evidence rather than on a clock comparison. A subclass of
`CancelledError` silently drops it back to the heuristic.

What that costs is worth stating exactly, because it is a real gap and not a
covered case: a cancelled call's spend is not reported by this package, so
nothing debits it. It is *bounded* rather than accounted — the task's whole cap
was reserved out of its run's budget before the task existed (SPEC.md §13 D9),
and the cancellation ends the task as `budget_exceeded`, whose usage is never
rolled into `runs.used_*` (that column sums succeeded tasks only). So the run
cannot be walked past its budget by cancellations; it just under-reports what a
cancelled one actually spent. Widening the accounting is a change to how a
failed attempt's usage is recorded, which is issue #69's other half, not a
reason to break the type the deadline machinery reads.
"""

from __future__ import annotations

from steward_llm.completion import ModelUsage

__all__ = [
    "CompletionFailed",
    "CompletionTimedOut",
    "LLMError",
    "UnboundAlias",
]


class LLMError(Exception):
    """A model call that did not produce a result, and what it spent trying."""

    def __init__(self, message: str, *, alias: str, usage: ModelUsage) -> None:
        self.alias = alias
        self.usage = usage
        spend = ""
        if usage.total_tokens or usage.cost_usd:
            spend = f" [spent {usage.total_tokens} tokens, ${usage.cost_usd}]"
        super().__init__(f"{message}{spend}")


class UnboundAlias(LLMError):
    """The request named an alias the gateway config binds no model for.

    Refused before any call, so its usage is nothing spent — the one error in
    this module for which that is a fact rather than a measurement.
    """


class CompletionFailed(LLMError):
    """The gateway or the model failed the call. Usage is what was spent first."""


class CompletionTimedOut(CompletionFailed):
    """The call ran out of time — the gateway's own timeout, not a cancellation.

    Distinguished from a plain failure because a caller with a wall-clock budget
    treats them differently: a timeout is a statement about this deployment's
    latency, not about the request.
    """
