"""The tracing contract -- owned, framework-free (I7).

Everything the platform knows about tracing is here: a trace id it can always
produce, a `Span` it can always open, and an outcome it can always record.
Nothing in this module imports a tracing vendor, so the rest of the system
depends on Steward's contract rather than on Langfuse's -- swapping the
backend is one package's internals (N9), exactly the containment pattern
LangGraph and LiteLLM follow.

Two rules make tracing safe to depend on:

* **A trace id always exists.** `new_trace_id` is pure Python: no client, no
  credentials, no network. A run therefore carries a trace id whether or not
  anything is exporting spans, so `runs.trace_id` is never null and a
  credential-less deployment still correlates logs, audit rows and runs.
* **Tracing never decides anything.** A `Span` records; it does not gate,
  retry or fail. A tracer that cannot reach its backend degrades to dropped
  spans, never to a failed run.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

TRACE_ID_HEX_LENGTH = 32
"""Length of a W3C trace id in hex characters (128 bits).

Langfuse and every other OpenTelemetry-native backend expect this shape, so
producing it here -- rather than a vendor id -- is what lets the backend be
swapped without touching stored data.
"""


class SpanOutcome(StrEnum):
    """How a span ended. Deliberately two-valued: a span records whether the
    work it wrapped succeeded, and the typed failure itself lives in the task's
    `last_error` (SPEC.md §8) -- traces are not a second error store."""

    OK = "ok"
    ERROR = "error"


def new_trace_id(seed: str | None = None) -> str:
    """A W3C-shaped trace id: 32 lowercase hex characters.

    With a `seed` the id is a deterministic digest of it, so the same logical
    subject (a run id, say) always maps to the same trace -- retrying the
    transaction that creates a run cannot scatter its spans across two traces.
    Without one, the id is random. The digest is `sha256(seed)[:16]` because
    that is the mapping Langfuse's own seeded id generator uses; a test in this
    package pins the two together, so seeded ids stay resolvable in the UI.
    """
    if seed is None:
        return uuid4().hex
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:TRACE_ID_HEX_LENGTH]


class Span(Protocol):
    """A unit of traced work, open for the lifetime of its context manager."""

    def record(self, outcome: SpanOutcome, detail: str | None = None) -> None:
        """Record how this span's work ended. Callable at most once per span;
        a span left unrecorded that exits without an exception is `OK`, and one
        that exits with an exception is `ERROR` -- the explicit call exists for
        failures that are return values rather than raises (a handler returning
        `TaskStatus.FAILED`, say)."""
        ...


class Tracer(Protocol):
    """The seam every traced component depends on (I7).

    Two span kinds, matching the two things the platform executes: a run
    (created by the API) and a task (executed by a worker). Both take the
    run's `trace_id`, which is what puts them on one trace even though they
    are opened by different processes at different times.
    """

    def run_span(self, *, trace_id: str, run_id: UUID, goal: str) -> AbstractContextManager[Span]:
        """Open the span that records a run's creation on the run's trace.

        A sibling of the task spans, not their parent: the run it names exists
        before this opens, so a task claimed at once can be traced first."""
        ...

    def task_span(
        self, *, trace_id: str, run_id: UUID, task_id: UUID, task_type: str
    ) -> AbstractContextManager[Span]:
        """Open a span for one task execution, on the run's existing trace."""
        ...

    def generation_span(
        self, *, trace_id: str, task_id: UUID, model_alias: str, prompt_version: str
    ) -> AbstractContextManager[Span]:
        """Open a span for one model call inside a task's agent loop.

        `prompt_version` is required rather than optional: I7 asks every
        generation to carry the version of the prompt that produced it, and a
        field a caller may omit is one that will be omitted. The alias is the
        only name for the model here -- provider and endpoint are the gateway's
        business (I2), and a trace that named them would be describing a
        deployment decision rather than the work.
        """
        ...

    def tool_span(
        self, *, trace_id: str, task_id: UUID, tool_name: str
    ) -> AbstractContextManager[Span]:
        """Open a span for one tool invocation inside a task's agent loop."""
        ...


class NoopSpan:
    """A span that records nothing. Satisfies `Span` structurally."""

    def record(self, outcome: SpanOutcome, detail: str | None = None) -> None:
        """Discard the outcome. The trace id is still on the run row, so the
        run remains correlatable even with no span backend at all."""


class NoopTracer:
    """The tracer used when no Langfuse credentials are configured.

    This is the graceful-degradation half of I7: the system must run -- demo,
    tests, an air-gapped deployment -- with no observability credentials, and
    it must do so on the same code path, not a branch that only production
    takes. Every call site gets a real `Tracer`; only the export is missing.
    """

    @contextmanager
    def run_span(self, *, trace_id: str, run_id: UUID, goal: str) -> Iterator[Span]:
        yield NoopSpan()

    @contextmanager
    def task_span(self, *, trace_id: str, run_id: UUID, task_id: UUID, task_type: str) -> Iterator[Span]:
        yield NoopSpan()

    @contextmanager
    def generation_span(
        self, *, trace_id: str, task_id: UUID, model_alias: str, prompt_version: str
    ) -> Iterator[Span]:
        yield NoopSpan()

    @contextmanager
    def tool_span(self, *, trace_id: str, task_id: UUID, tool_name: str) -> Iterator[Span]:
        yield NoopSpan()
