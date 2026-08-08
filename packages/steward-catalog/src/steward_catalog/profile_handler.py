"""`profile_asset` — one bounded task, one asset, every column on it (#49).

**One task per asset, and no fan-out.** SPEC.md §3.1 sketches `profile_table
(×N)` hanging off a scan, and #48 made a fan-out *representable* by having a
plan divide its run's budget. It is still not the shape this slice ships, and
the reason is singular: **a planner cannot enumerate a source's assets.**
Planners are pure functions of their validated params (ARCHITECTURE.md §4,
enforced by `test_every_registered_planner_is_deterministic`); they touch no
connection. A `profile_source(source_id)` goal would have to read the catalog at
plan time, which is what makes a planner impure. So the asset is the unit that
carries a budget, and one asset is one run.

Budget is explicitly *not* the reason, and SPEC.md §3.1 says so: a deterministic
fan-out that spends no model budget is safe under reservation alone, and
profiling is exactly that. What #48's scope does explain is why the available
workaround is the wrong move -- a handler that enqueues its own children skips
plan-time reservation entirely, and since reservation counts each planned task
once and spend on failed or retried attempts is debited nowhere (SPEC.md §13
D9), those children would put an unaccounted tail under one advertised cap.

What the task itself is bounded by: one statistics pass over the relation plus
one top-values query per column, all on the read-only role, all under the
task's wall-clock budget through the connection's `connect_timeout` and
`statement_timeout` (`inspector`, SPEC.md §13 D7). No model is called.

Idempotence (registry contract clause 2) is `profiles.record_profile` refusing
to write a version whose digest already stands, exactly as `plan_convergence`
is `scan_source`'s: profiling unchanged data twice leaves byte-identical state.
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
from steward_schemas import (
    AssetLifecycle,
    ProblemDetails,
    RunBudget,
    TaskResult,
    TaskSpec,
    TaskStatus,
)

from steward_catalog import _sql, profiles, repository
from steward_catalog.models import (
    AssetRecord,
    CatalogModel,
    ColumnRecord,
    DiscoveredColumn,
    ProfileTarget,
)
from steward_catalog.profiler import SourceProfilerFactory, postgres_profiler
from steward_catalog.secrets import (
    EnvSecretResolver,
    MalformedSecretRef,
    SecretNotFound,
    SecretResolver,
)

__all__ = [
    "PROFILE_ASSET_SAMPLE_PAYLOAD",
    "PROFILE_ASSET_TASK_TYPE",
    "ProfileAssetPayload",
    "build_profile_asset",
    "profile_state_probe",
]

_logger = logging.getLogger(__name__)

PROFILE_ASSET_TASK_TYPE = "profile_asset"

NO_USAGE_TOKENS = 0
PROFILE_STEPS = 1
"""Profiling is one step: deterministic SQL, no loop and no model (I12)."""

UNCATALOGUED_ASSET = UUID(int=0)
"""The asset id the registry sample names. `assets.id` is a fresh `uuid4`, so
no asset is ever registered under it and the sample exercises the
missing-asset path deterministically -- the same device `scan_source` uses for
an unregistered source."""

# The payload H1 executes this handler twice with (GUARDRAILS.md Tier H). Like
# `scan_source`'s it names something that does not exist, because a generic
# harness cannot conjure a catalogued asset backed by a reachable customer
# database. The success path is leashed in `tests/test_profile_convergence.py`,
# which runs the real handler twice against the fixture source under the same
# `invariants` marker.
PROFILE_ASSET_SAMPLE_PAYLOAD: dict[str, Any] = {"asset_id": str(UNCATALOGUED_ASSET)}


class ProfileAssetPayload(CatalogModel):
    """`profile_asset`'s task payload: which catalogued asset to profile.

    An asset *id*, never a relation name: the identifiers that reach the
    customer's database are read back out of the `assets`/`columns` rows a scan
    wrote, so nothing a client sends is ever composed into SQL (`_profile_sql`).
    """

    asset_id: UUID


def _actor(spec: TaskSpec) -> Actor:
    return Actor(kind=ActorKind.AGENT, id=f"{spec.task_type}:{spec.task_id}")


def _problem(problem_type: str, title: str, detail: str, status: int) -> ProblemDetails:
    return ProblemDetails(type=problem_type, title=title, status=status, detail=detail)


def _profile_usage() -> RunBudget:
    """What profiling reports having spent: one step, no tokens, no money.

    `wall_clock` is zero for the reason `scan_source`'s is (SPEC.md §13 D9): H1
    compares a handler's result byte for byte across two executions, and a real
    duration differs between them by construction. The cap that binds this task
    is the worker's deadline, not this number.
    """
    return RunBudget(
        steps=PROFILE_STEPS, tokens=NO_USAGE_TOKENS, cost_usd=Decimal("0"), wall_clock=timedelta(0)
    )


def _failed(spec: TaskSpec, error: ProblemDetails) -> TaskResult:
    return TaskResult(task_id=spec.task_id, status=TaskStatus.FAILED, usage=_profile_usage(), error=error)


def profile_state_probe(conn: QueueConnection, spec: TaskSpec) -> object:
    """Every profile version stored for this task's asset, id- and clock-free.

    The default probe reads the task's result and checkpoints, neither of which
    this handler writes, so H1 would compare nothing. Version, digest and the
    profile itself are exactly what must not change when the same profiling
    runs twice; the row id and `created_at` differ by construction and are
    excluded for the reason the scan probe excludes them.
    """
    try:
        payload = ProfileAssetPayload.model_validate(dict(spec.payload))
    except ValidationError:
        return {"payload": "invalid"}
    rows = conn.execute(_sql.SELECT_ASSET_PROFILES, {"asset_id": payload.asset_id}).fetchall()
    return {"profiles": [[row[0], row[1], row[2]] for row in rows]}


def _target(asset: AssetRecord, columns: list[ColumnRecord]) -> ProfileTarget:
    """The relation and the columns to profile: the active ones, in ordinal order.

    A `missing` column is one the source no longer has, so asking for it would
    fail the whole statement -- and a `missing` asset is caught earlier, before
    a connection is opened at all.
    """
    active = sorted(
        (column for column in columns if column.lifecycle is AssetLifecycle.ACTIVE),
        key=lambda column: (column.ordinal, column.name),
    )
    return ProfileTarget(
        schema_name=asset.schema_name,
        name=asset.name,
        columns=tuple(
            DiscoveredColumn(
                name=column.name,
                data_type=column.data_type,
                ordinal=column.ordinal,
                nullable=column.nullable,
            )
            for column in active
        ),
    )


def _profile(
    conn: QueueConnection,
    spec: TaskSpec,
    *,
    resolver: SecretResolver,
    profiler: SourceProfilerFactory,
) -> TaskResult:
    """The profiling task itself, synchronous: every step is a driver call."""
    try:
        payload = ProfileAssetPayload.model_validate(dict(spec.payload))
    except ValidationError as exc:
        return _failed(
            spec,
            _problem(
                "urn:steward:invalid-task-payload",
                "Invalid profile payload",
                f"{spec.task_type} payload does not name an asset: {exc.error_count()} error(s)",
                400,
            ),
        )

    asset = repository.get_asset(conn, payload.asset_id)
    if asset is None:
        return _failed(
            spec,
            _problem(
                "urn:steward:unknown-asset",
                "Unknown asset",
                f"no asset catalogued with id {payload.asset_id}",
                404,
            ),
        )
    if asset.lifecycle is not AssetLifecycle.ACTIVE:
        # Profiling a relation the last scan could not find would fail in the
        # driver with a message about a missing table; failing here says the
        # true thing instead -- the catalog already knows it is gone.
        return _failed(
            spec,
            _problem(
                "urn:steward:asset-not-active",
                "Asset is not active",
                f"asset {payload.asset_id} is {asset.lifecycle.value} and cannot be profiled",
                409,
            ),
        )

    source = repository.get_source(conn, asset.source_id)
    if source is None:  # pragma: no cover -- an FK guarantees the source exists
        raise RuntimeError(f"asset {asset.id} references a source that does not exist")

    try:
        secret = resolver.resolve(source.dsn_secret_ref)
    except (SecretNotFound, MalformedSecretRef) as exc:
        return _failed(
            spec,
            _problem(
                "urn:steward:source-credential-unavailable",
                "Source credential unavailable",
                str(exc),
                503,
            ),
        )

    target = _target(asset, repository.list_asset_columns(conn, asset.id))
    try:
        with profiler(secret, spec.budget.wall_clock) as reader:
            profile = reader.profile(target)
    except psycopg.Error as exc:
        # Same rule as the scan handler's: the type and the SQLSTATE are logged,
        # the message is not. A driver error can quote the conninfo (a
        # credential) and, here, the row or value it failed on (N7).
        _logger.warning(
            "asset %s could not be profiled: %s (sqlstate %s)",
            payload.asset_id,
            type(exc).__name__,
            getattr(exc, "sqlstate", None),
        )
        return _failed(
            spec,
            _problem(
                "urn:steward:asset-unprofilable",
                "Asset could not be profiled",
                f"asset {payload.asset_id} did not answer a profiling query",
                502,
            ),
        )

    recorded = profiles.record_profile(conn, asset.id, profile, actor=_actor(spec))
    return TaskResult(
        task_id=spec.task_id,
        status=TaskStatus.SUCCEEDED,
        usage=_profile_usage(),
        output={
            "asset_id": str(asset.id),
            "columns": len(profile.columns),
            "row_count": profile.row_count,
            "version": recorded.version,
            "changed": recorded.changed,
        },
    )


def build_profile_asset(*, resolver: SecretResolver, profiler: SourceProfilerFactory) -> TaskHandler:
    """A `profile_asset` handler bound to a secret resolver and a profiler.

    A factory for the reason `build_scan_source` is one: the collaborators are
    visible where they are chosen, and a test binds its own without mutating
    process state another test inherits.
    """

    async def profile_asset(ctx: TaskContext) -> TaskResult:
        return _profile(ctx.connection, ctx.spec, resolver=resolver, profiler=profiler)

    return profile_asset


profile_asset: TaskHandler = task_handler(
    PROFILE_ASSET_TASK_TYPE,
    sample_payload=PROFILE_ASSET_SAMPLE_PAYLOAD,
    state_probe=profile_state_probe,
)(build_profile_asset(resolver=EnvSecretResolver(), profiler=postgres_profiler))
"""The registered handler: environment-backed secrets, real Postgres profiling.

Registered at import, like `scan_source`, so the worker finds it by task type
and H1 leashes it the moment it exists.
"""
