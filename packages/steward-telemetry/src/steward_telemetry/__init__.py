"""steward-telemetry: the tracing seam (I7, ARCHITECTURE.md §3 Langfuse row).

I7 has two halves. The audit half is a Postgres write in the same transaction
as the mutation it records and lives in `steward-queue`. The tracing half is
this package: every run and every task execution opens a span, and the run's
trace id is stored on its `runs` row so a trace, an audit trail and a run
record all name the same thing.

Tracing is a dependency the platform cannot be allowed to fail on, so the
design is two-layer:

* `Tracer` / `Span` / `new_trace_id` -- owned contract, no vendor import. This
  is what every caller depends on.
* `LangfuseTracer` -- the vendor, contained in a private module. `langfuse`
  imports are legal only in this package (S1) and no Langfuse type reaches a
  public signature (S5), so replacing the backend is this package's internals.

`tracer_from_env` picks between the two: with credentials, spans export; with
none, `NoopTracer`. The trace id is generated and stored either way, which is
what lets the system be operated -- demoed, tested, deployed air-gapped --
with no observability credentials at all. It is also the *only* way to obtain
the Langfuse-backed tracer: `LangfuseTracer` is deliberately not exported,
because its constructor takes a `Langfuse` client and exporting it would put a
vendor type back in this package's reachable surface (I9) -- the thing the
private module exists to prevent.
"""

from steward_telemetry.env import HOST_ENV, PUBLIC_KEY_ENV, SECRET_KEY_ENV, tracer_from_env
from steward_telemetry.tracer import (
    TRACE_ID_HEX_LENGTH,
    NoopSpan,
    NoopTracer,
    Span,
    SpanOutcome,
    Tracer,
    new_trace_id,
)

__all__ = [
    "HOST_ENV",
    "PUBLIC_KEY_ENV",
    "SECRET_KEY_ENV",
    "TRACE_ID_HEX_LENGTH",
    "NoopSpan",
    "NoopTracer",
    "Span",
    "SpanOutcome",
    "Tracer",
    "new_trace_id",
    "tracer_from_env",
]
