"""`profile_asset`'s failure paths, and what they are allowed to say.

The same discipline `test_scan_handler.py` holds `scan_source` to: every
failure is a typed `TaskResult(FAILED)` rather than a raised exception, and no
document may carry a credential -- they are persisted to `tasks.last_error` and
served over the API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from steward_catalog import (
    PROFILE_ASSET_SAMPLE_PAYLOAD,
    PROFILE_ASSET_TASK_TYPE,
    EnvSecretResolver,
    build_profile_asset,
    build_scan_source,
    postgres_inspector,
    postgres_profiler,
    register_source,
)
from steward_catalog.profile_handler import profile_state_probe
from steward_queue import REGISTRY, SYSTEM_ACTOR, QueueConnection, TaskContext, UsageLedger
from steward_schemas import SourceCreate, TaskResult, TaskSpec, TaskStatus


def _ctx(conn: QueueConnection, spec: TaskSpec, attempts: int = 1) -> TaskContext:
    """A handler context for a test: a trace to hang spans on, and a fresh
    per-attempt usage ledger (`steward_queue.usage`)."""
    return TaskContext(
        connection=conn,
        spec=spec,
        attempts=attempts,
        claimed_by="w-test",
        trace_id="trace-test",
        usage=UsageLedger(),
    )


MISSING_SECRET_REF = "env:STEWARD_NO_SUCH_SOURCE_DSN"

# A rotated-away credential: the source was registered and scanned, and the
# reference it now carries resolves to nothing.
ROTATE_SECRET = "UPDATE sources SET dsn_secret_ref = %(ref)s WHERE id = %(id)s"
SELECT_ASSET_ID = "SELECT id FROM assets WHERE schema_name = 'sales' AND name = 'orders'"


def profile_spec(spec_factory: Callable[[UUID], TaskSpec], payload: dict[str, Any]) -> TaskSpec:
    return spec_factory(uuid4()).model_copy(update={"task_type": PROFILE_ASSET_TASK_TYPE, "payload": payload})


def execute(conn: QueueConnection, spec: TaskSpec, resolver: Any) -> TaskResult:
    handler = build_profile_asset(resolver=resolver, profiler=postgres_profiler)
    return asyncio.run(handler(_ctx(conn, spec, 1)))


def test_the_handler_is_registered_under_its_task_type() -> None:
    # The string a planner names and the handler a worker dispatches to. The
    # two packages do not import each other, so this is where they are checked.
    assert PROFILE_ASSET_TASK_TYPE in REGISTRY
    assert REGISTRY[PROFILE_ASSET_TASK_TYPE].sample_payload == PROFILE_ASSET_SAMPLE_PAYLOAD


def test_a_payload_that_names_no_asset_fails_typed(
    conn: QueueConnection,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    result = execute(conn, profile_spec(spec_factory, {"table": "sales.orders"}), resolver)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:invalid-task-payload"


def test_an_unresolvable_credential_fails_without_naming_one(
    conn: QueueConnection,
    source_create: SourceCreate,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    """The credential was rotated away after the scan, so profiling stops before
    a connection exists. The reference is safe to report; a DSN would not be."""
    source, _ = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()
    scan = build_scan_source(resolver=resolver, inspect=postgres_inspector)
    ctx = _ctx(conn, spec_factory(source.id), 1)
    asyncio.run(scan(ctx))
    conn.execute(ROTATE_SECRET, {"ref": MISSING_SECRET_REF, "id": source.id})
    conn.commit()
    asset_id = conn.execute(SELECT_ASSET_ID).fetchone()
    conn.rollback()
    assert asset_id is not None

    result = execute(conn, profile_spec(spec_factory, {"asset_id": str(asset_id[0])}), resolver)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.type == "urn:steward:source-credential-unavailable"
    detail = result.error.detail or ""
    # The reference is safe to name; a DSN never is. Asserted as "no connection
    # string shape survives" rather than by hunting a password literal -- the
    # literal this copied from `test_scan_handler` cannot occur here, since this
    # fixture's role has no password, so it was an assertion nothing could fail.
    assert MISSING_SECRET_REF in detail
    assert "://" not in detail
    assert "@" not in detail


def test_the_state_probe_reports_an_invalid_payload_rather_than_raising(
    conn: QueueConnection, spec_factory: Callable[[UUID], TaskSpec]
) -> None:
    """H1 calls the probe with whatever the spec carries, including a payload
    the handler already rejected; it has to answer, not raise."""
    assert profile_state_probe(conn, profile_spec(spec_factory, {"nope": 1})) == {"payload": "invalid"}


def test_the_state_probe_returns_no_profiles_for_an_unprofiled_asset(
    conn: QueueConnection, spec_factory: Callable[[UUID], TaskSpec]
) -> None:
    spec = profile_spec(spec_factory, dict(PROFILE_ASSET_SAMPLE_PAYLOAD))

    assert profile_state_probe(conn, spec) == {"profiles": []}
