"""`scan_source` — the one task a scan run plans, and the whole scan.

**Exactly one bounded task, deliberately** (issue #20, #37). A per-table fan-out
is the obvious shape and it is wrong here: `RunPlan.task_specs` gives every
planned task the *run's* budget, so an N-way fan-out lets one run spend N times
the cap the API published for it (I12). A deterministic metadata scan does not
need parallelism to be correct, and one task whose budget is the run's budget
is honest about what it may cost. Fan-out waits for run-level budget
reservation.

The handler reads through two different connections and that separation is the
point:

* `ctx.connection` -- Steward's system of record, the worker's transaction. The
  catalog rows, the audit rows and the task's terminal state commit together
  (I1, I7, I8).
* a `SourceInspector` -- the customer's database, opened from a resolved secret
  on a read-only role, and capable of nothing but reading metadata (I5, N7).

Both collaborators are injected. `build_scan_source` takes the secret resolver
and the inspector factory, and the module registers one built from the
environment-backed defaults. A test substitutes fakes without touching a real
source, and a deployment swaps the resolver for Vault without touching this
file (N9).

Idempotence (registry contract clause 2) is not a property of this function
retrying carefully -- it is `plan_convergence` being a pure function of
(stored, observed) and an empty plan writing nothing. Executing the same scan
twice against an unchanged source leaves byte-identical state.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from pydantic import ValidationError
from steward_queue import (
    Actor,
    ActorKind,
    QueueConnection,
    TaskContext,
    TaskHandler,
    task_handler,
)
from steward_schemas import ProblemDetails, RunBudget, TaskResult, TaskSpec, TaskStatus

from steward_catalog import repository
from steward_catalog.diff import plan_convergence
from steward_catalog.inspector import SourceInspectorFactory, postgres_inspector
from steward_catalog.models import CatalogModel
from steward_catalog.secrets import (
    EnvSecretResolver,
    MalformedSecretRef,
    SecretNotFound,
    SecretResolver,
)

__all__ = [
    "SCAN_SOURCE_SAMPLE_PAYLOAD",
    "SCAN_SOURCE_TASK_TYPE",
    "ScanSourcePayload",
    "build_scan_source",
    "scan_state_probe",
]

_logger = logging.getLogger(__name__)

SCAN_SOURCE_TASK_TYPE = "scan_source"

NO_USAGE_TOKENS = 0
SCAN_STEPS = 1
"""A scan is one step: no model is called and no loop is run (I12)."""

UNREGISTERED_SOURCE = UUID(int=0)
"""The source id the registry sample names. No source is ever registered under
it -- `sources.id` is a fresh `uuid4` -- so the sample exercises the
missing-source path deterministically."""

# The payload H1 executes this handler twice with (GUARDRAILS.md Tier H).
#
# It has to be self-contained -- "no dependency on rows another task created"
# (`steward_queue.registry`) -- and a real scan depends on a registered source
# and a reachable customer database, neither of which a generic harness can
# conjure. So the sample takes the honest option: it names a source that does
# not exist, and H1 asserts that failing to find one is deterministic and
# writes nothing.
#
# The success path is leashed too, just not here: `tests/test_convergence.py`
# runs the real handler twice against a real fixture source and asserts
# byte-identical catalog state, marked `invariants` so it runs in the same
# Tier H sweep.
SCAN_SOURCE_SAMPLE_PAYLOAD: dict[str, Any] = {"source_id": str(UNREGISTERED_SOURCE)}

SELECT_SOURCE_PROBE = """
SELECT name, engine, host, database_name, include_schemas, exclude_schemas, dsn_secret_ref
FROM sources WHERE id = %(source_id)s
"""

SELECT_ASSET_PROBE = """
SELECT a.schema_name, a.name, a.asset_type, a.lifecycle, c.name, c.data_type, c.ordinal,
       c.nullable, c.lifecycle
FROM assets AS a
LEFT JOIN columns AS c ON c.asset_id = a.id
WHERE a.source_id = %(source_id)s
ORDER BY a.schema_name, a.name, c.name
"""


class ScanSourcePayload(CatalogModel):
    """`scan_source`'s task payload: which registered source to scan."""

    source_id: UUID


def _actor(spec: TaskSpec) -> Actor:
    """The audit actor for this scan: an agent, identified by the task that ran
    it, so every catalog row traces back to the execution that wrote it (I7)."""
    return Actor(kind=ActorKind.AGENT, id=f"{spec.task_type}:{spec.task_id}")


def _problem(problem_type: str, title: str, detail: str, status: int) -> ProblemDetails:
    return ProblemDetails(type=problem_type, title=title, status=status, detail=detail)


def _failed(spec: TaskSpec, error: ProblemDetails) -> TaskResult:
    """A typed failure. The task's own transaction has written nothing."""
    return TaskResult(task_id=spec.task_id, status=TaskStatus.FAILED, usage=_scan_usage(), error=error)


def _scan_usage() -> RunBudget:
    """What a scan reports having spent: one step, no tokens, no money.

    `wall_clock` is reported as zero rather than measured, and that is a trade
    with a visible cost. H1 compares this handler's returned result byte for
    byte across two executions; a real duration differs between them by
    construction, so reporting one would make the harness fail always -- and a
    harness that always fails is one that gets ignored. The consequence is that
    `runs.used_wall_clock` under-reports a scan, so wall-clock is enforced by
    the runtime -- the worker's deadline over the thread this handler runs on,
    plus both connections' budget-derived timeouts (SPEC.md §13, D7) -- and
    never by this number. Measuring it for N6 needs a usage field the
    idempotency comparison excludes, which belongs with the agent loop.
    """
    return RunBudget(steps=SCAN_STEPS, tokens=NO_USAGE_TOKENS, cost_usd=Decimal("0"), wall_clock=timedelta(0))


def scan_state_probe(conn: QueueConnection, spec: TaskSpec) -> object:
    """The catalog this handler owns for the scanned source, timestamp-free.

    The default probe (`steward_queue.default_state_probe`) reads the task's
    result and checkpoints, which this handler does not write -- it would
    therefore compare nothing and H1 would pass vacuously. This reads the rows
    the scan actually produces. Generated ids and timestamps are excluded on
    purpose: they differ between two executions by construction, and a probe
    that included them would make the harness always fail and so eventually be
    ignored.
    """
    try:
        payload = ScanSourcePayload.model_validate(dict(spec.payload))
    except ValidationError:
        return {"payload": "invalid"}
    params = {"source_id": payload.source_id}
    source = conn.execute(SELECT_SOURCE_PROBE, params).fetchone()
    assets = conn.execute(SELECT_ASSET_PROBE, params).fetchall()
    return {"source": list(source) if source is not None else None, "catalog": [list(r) for r in assets]}


def _scan(
    conn: QueueConnection,
    spec: TaskSpec,
    *,
    resolver: SecretResolver,
    inspect: SourceInspectorFactory,
) -> TaskResult:
    """The scan itself, synchronous: every step is a blocking driver call."""
    try:
        payload = ScanSourcePayload.model_validate(dict(spec.payload))
    except ValidationError as exc:
        return _failed(
            spec,
            _problem(
                "urn:steward:invalid-task-payload",
                "Invalid scan payload",
                f"{spec.task_type} payload does not name a source: {exc.error_count()} error(s)",
                400,
            ),
        )

    source = repository.get_source(conn, payload.source_id)
    if source is None:
        return _failed(
            spec,
            _problem(
                "urn:steward:unknown-source",
                "Unknown source",
                f"no source registered with id {payload.source_id}",
                404,
            ),
        )

    try:
        secret = resolver.resolve(source.dsn_secret_ref)
    except (SecretNotFound, MalformedSecretRef) as exc:
        # The reference is safe to name -- that is the point of storing one --
        # and there is no secret to leak because none was found.
        return _failed(
            spec,
            _problem(
                "urn:steward:source-credential-unavailable",
                "Source credential unavailable",
                str(exc),
                503,
            ),
        )

    try:
        with inspect(secret, spec.budget.wall_clock) as inspector:
            observed = inspector.inspect(source.key.schemas)
    except psycopg.Error as exc:
        # Neither the response nor the log gets the exception itself. psycopg
        # renders the conninfo it failed on, which means its message can carry
        # the credential -- and `tasks.last_error` is served over the API while
        # the log is shipped off the box. Every other credential guarantee in
        # this package is structural (`Secret` redacts, the column has a CHECK,
        # the connector takes `Secret` not `str`); trusting a driver's error
        # formatting would be the one that is not. So the type is logged and
        # the text is dropped: an operator gets `OperationalError on source X`,
        # which is what they act on, and the SQLSTATE, which is what they
        # diagnose with (N7).
        _logger.warning(
            "source %s could not be inspected: %s (sqlstate %s)",
            payload.source_id,
            type(exc).__name__,
            getattr(exc, "sqlstate", None),
        )
        return _failed(
            spec,
            _problem(
                "urn:steward:source-unreachable",
                "Source could not be inspected",
                f"source {payload.source_id} did not answer a metadata query",
                502,
            ),
        )

    plan = plan_convergence(repository.load_state(conn, payload.source_id), observed)
    repository.apply_plan(conn, payload.source_id, plan, actor=_actor(spec))
    return TaskResult(
        task_id=spec.task_id,
        status=TaskStatus.SUCCEEDED,
        usage=_scan_usage(),
        output={
            "source_id": str(payload.source_id),
            "assets": len(observed),
            "columns": sum(len(asset.columns) for asset in observed),
            "changed": not plan.is_empty(),
        },
    )


def build_scan_source(*, resolver: SecretResolver, inspect: SourceInspectorFactory) -> TaskHandler:
    """A `scan_source` handler bound to a secret resolver and an inspector.

    A factory rather than module-level globals: the collaborators are visible
    at the one place they are chosen, and a test binds its own without mutating
    process state that another test would inherit.
    """

    async def scan_source(ctx: TaskContext) -> TaskResult:
        return _scan(ctx.connection, ctx.spec, resolver=resolver, inspect=inspect)

    return scan_source


scan_source: TaskHandler = task_handler(
    SCAN_SOURCE_TASK_TYPE,
    sample_payload=SCAN_SOURCE_SAMPLE_PAYLOAD,
    state_probe=scan_state_probe,
)(build_scan_source(resolver=EnvSecretResolver(), inspect=postgres_inspector))
"""The registered handler: environment-backed secrets, real Postgres inspection.

Registering at import is how `steward_queue` finds it (the worker looks handlers
up by task type, it does not import them) and how the H1 harness picks it up --
a handler is leashed the moment it is registered, with no test file to edit.
"""
