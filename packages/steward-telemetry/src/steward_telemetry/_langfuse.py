"""Langfuse behind the `Tracer` contract.

Private module by design (S5): every `langfuse` symbol this system touches is
imported here and nowhere else, so no Langfuse type can reach a public
signature or a re-export. `steward_telemetry.__init__` exports the class, not
the module -- callers get `Tracer`, and the vendor stays an implementation
detail (I9, ARCHITECTURE.md §4 containment pattern).

Delivery is deliberately not this module's problem. Langfuse's exporter batches
spans on a background thread and flushes at process exit, so a span never
blocks the work it wraps and an unreachable Langfuse costs dropped spans rather
than failed runs -- which is what makes tracing safe to leave switched on.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal, Protocol
from uuid import UUID

from langfuse import Langfuse

from steward_telemetry.tracer import Span, SpanOutcome

RUN_SPAN_NAME = "run.create"
"""Named for what the span actually covers.

The run-level span is opened by whoever creates the run, once the run it names
is known -- it records the run's identity on the trace, and it is milliseconds
long even for a run that goes on for ten minutes. Calling it `run` would put a
span in the trace that looks like it measures the run and does not. The task
spans a worker opens later are siblings of this one on the same trace id, and
nothing orders the two: a task claimed immediately can be exported first.
"""

TASK_SPAN_NAME = "task"

GENERATION_SPAN_NAME = "generation"
"""One model call. Unlike the run span, this one does measure its subject: it
opens before the request and closes when the completion (or the failure) is in
hand, so its duration is the latency an operator is looking for."""

TOOL_SPAN_NAME = "tool"

_LEVELS: Mapping[SpanOutcome, Literal["DEFAULT", "ERROR"]] = {
    SpanOutcome.OK: "DEFAULT",
    SpanOutcome.ERROR: "ERROR",
}


class _Observation(Protocol):
    """The slice of Langfuse's observation object this module uses.

    Narrowing it here keeps `_LangfuseSpan` typed against two method calls
    instead of against the nine-member union Langfuse's factory returns, and
    makes the coupling to the vendor explicit and small.
    """

    def update(
        self,
        *,
        output: Any = None,
        level: Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"] | None = None,
        status_message: str | None = None,
        **kwargs: Any,
    ) -> Any: ...


class _LangfuseSpan:
    """A `Span` writing through to one Langfuse observation.

    First outcome wins: the enclosing context manager records `OK` on a clean
    exit and `ERROR` on an exception, so a caller that already recorded a
    typed failure must not have it overwritten on the way out.
    """

    def __init__(self, observation: _Observation) -> None:
        self._observation = observation
        self._recorded = False

    def record(self, outcome: SpanOutcome, detail: str | None = None) -> None:
        if self._recorded:
            return
        self._recorded = True
        self._observation.update(output=outcome.value, level=_LEVELS[outcome], status_message=detail)

    def observe(self, measurements: Mapping[str, object]) -> None:
        """Write measurements onto the observation as metadata.

        Not `output`: that field carries the outcome, and a span whose output
        was overwritten by a token count would read as having succeeded with a
        number. Metadata is where Langfuse expects per-observation detail, and
        it survives a later `record`.
        """
        self._observation.update(metadata=dict(measurements))


class LangfuseTracer:
    """`Tracer` backed by a Langfuse client. Satisfies `Tracer` structurally.

    Takes an already-built client rather than credentials so the wiring
    decision (which credentials, which host, whether to trace at all) stays in
    `from_env`, and so this class is testable without reaching for the
    environment. `from_credentials` is the one-line convenience for callers
    that have the keys in hand.
    """

    def __init__(self, client: Langfuse) -> None:
        self._client = client

    @classmethod
    def from_credentials(cls, *, public_key: str, secret_key: str, host: str | None = None) -> LangfuseTracer:
        return cls(Langfuse(public_key=public_key, secret_key=secret_key, host=host))

    @contextmanager
    def run_span(self, *, trace_id: str, run_id: UUID, goal: str) -> Iterator[Span]:
        with self._span(RUN_SPAN_NAME, trace_id, {"run_id": str(run_id), "goal": goal}) as span:
            yield span

    @contextmanager
    def task_span(self, *, trace_id: str, run_id: UUID, task_id: UUID, task_type: str) -> Iterator[Span]:
        attributes = {
            "run_id": str(run_id),
            "task_id": str(task_id),
            "task_type": task_type,
        }
        with self._span(TASK_SPAN_NAME, trace_id, attributes) as span:
            yield span

    @contextmanager
    def generation_span(
        self, *, trace_id: str, task_id: UUID, model_alias: str, prompt_version: str
    ) -> Iterator[Span]:
        attributes = {
            "task_id": str(task_id),
            "model_alias": model_alias,
            "prompt_version": prompt_version,
        }
        with self._span(GENERATION_SPAN_NAME, trace_id, attributes) as span:
            yield span

    @contextmanager
    def tool_span(self, *, trace_id: str, task_id: UUID, tool_name: str) -> Iterator[Span]:
        with self._span(
            TOOL_SPAN_NAME, trace_id, {"task_id": str(task_id), "tool_name": tool_name}
        ) as span:
            yield span

    @contextmanager
    def _span(self, name: str, trace_id: str, attributes: Mapping[str, str]) -> Iterator[Span]:
        """One span on `trace_id`, ended with an outcome however the block exits.

        `trace_context` is what puts a worker's task span on the trace the API
        opened for the run: the two processes share nothing but the trace id
        stored on the `runs` row.
        """
        with self._client.start_as_current_observation(
            name=name,
            as_type="span",
            trace_context={"trace_id": trace_id},
            input=dict(attributes),
        ) as observation:
            span = _LangfuseSpan(observation)
            try:
                yield span
            except Exception as exc:
                span.record(SpanOutcome.ERROR, f"{type(exc).__name__}: {exc}")
                raise
            span.record(SpanOutcome.OK)
