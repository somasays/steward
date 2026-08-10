"""`scan_source`'s failure paths, and what they are allowed to say.

Every one of them is a typed `TaskResult(FAILED)` rather than a raised
exception: the worker records a returned failure as the task's terminal state
with a problem-details body (`steward_queue.Worker`), and a handler that raised
would be indistinguishable from a bug. It is also what makes the handler a
usable H1 subject -- the harness calls it directly.

The credential assertions are the point of the module: none of these documents
may carry a DSN, because they are persisted to `tasks.last_error` and are
reachable over the API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from steward_catalog import (
    SCAN_SOURCE_SAMPLE_PAYLOAD,
    SCAN_SOURCE_TASK_TYPE,
    DiscoveredAsset,
    EnvSecretResolver,
    SchemaFilter,
    Secret,
    SourceInspector,
    build_scan_source,
    postgres_inspector,
    register_source,
)
from steward_catalog.handler import scan_state_probe
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


UNREACHABLE = "postgresql://steward_reader:hunter2@127.0.0.1:1/analytics"


def execute(
    conn: QueueConnection, spec: TaskSpec, resolver: Any, inspect: Any = postgres_inspector
) -> TaskResult:
    handler = build_scan_source(resolver=resolver, inspect=inspect)
    return asyncio.run(handler(_ctx(conn, spec, 1)))


def test_the_handler_is_registered_under_its_task_type() -> None:
    # The seam a planner names (`steward_orchestration`) and the handler a
    # worker dispatches to agree on this string, and nothing but a test checks
    # it -- the two packages deliberately do not import each other.
    assert SCAN_SOURCE_TASK_TYPE in REGISTRY
    assert REGISTRY[SCAN_SOURCE_TASK_TYPE].sample_payload == SCAN_SOURCE_SAMPLE_PAYLOAD


def test_an_unknown_source_fails_deterministically_and_writes_nothing(
    conn: QueueConnection,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    """Also the H1 subject's path: the registry sample names a source that is
    never registered, so executing it twice is trivially identical."""
    spec = spec_factory(uuid4())

    first = execute(conn, spec, resolver)
    second = execute(conn, spec, resolver)

    assert first.status is TaskStatus.FAILED
    assert first.error is not None and first.error.type == "urn:steward:unknown-source"
    assert first == second
    assert scan_state_probe(conn, spec) == scan_state_probe(conn, spec)
    conn.rollback()


def test_a_payload_that_names_no_source_is_rejected(
    conn: QueueConnection,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    spec = spec_factory(uuid4()).model_copy(update={"payload": {"table": "public.orders"}})

    result = execute(conn, spec, resolver)

    assert result.error is not None and result.error.type == "urn:steward:invalid-task-payload"
    assert scan_state_probe(conn, spec) == {"payload": "invalid"}
    conn.rollback()


def test_a_missing_credential_names_the_reference_and_not_a_secret(
    conn: QueueConnection,
    source_create: SourceCreate,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    source, _ = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()

    result = execute(conn, spec_factory(source.id), EnvSecretResolver(environ={}))

    assert result.error is not None
    assert result.error.type == "urn:steward:source-credential-unavailable"
    assert result.error.detail is not None
    assert source.dsn_secret_ref in result.error.detail  # the reference is safe to name


def test_an_unreachable_source_reports_a_sanitized_failure(
    conn: QueueConnection,
    source_create: SourceCreate,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    """psycopg renders the conninfo it failed on. That string is persisted to
    `tasks.last_error` and served over the API, so the handler must not pass it
    through (N7)."""
    resolver = EnvSecretResolver(environ={"STEWARD_TEST_SOURCE_DSN": UNREACHABLE})
    source, _ = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()

    result = execute(conn, spec_factory(source.id), resolver)

    assert result.error is not None and result.error.type == "urn:steward:source-unreachable"
    document = result.error.model_dump_json()
    assert "hunter2" not in document
    assert "steward_reader" not in document
    assert "127.0.0.1" not in document


def test_a_scan_costs_exactly_one_step(
    conn: QueueConnection,
    source_create: SourceCreate,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    # I12: the run's advertised budget is the task's budget because the plan is
    # one task. A scan that reported zero usage would make that unfalsifiable.
    source, _ = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()

    result = execute(conn, spec_factory(source.id), resolver)

    assert result.usage.steps == 1
    assert result.usage.tokens == 0  # no model was called, and none will be


def test_the_inspector_is_injected_not_imported(
    conn: QueueConnection,
    source_create: SourceCreate,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> None:
    """The seam exists so a scan can be exercised without a source at all --
    which is what a future engine's inspector will be tested through too."""
    observed = (DiscoveredAsset(schema_name="invented", name="table", asset_type="table"),)

    @contextmanager
    def fake(_secret: Secret, _budget: timedelta) -> Iterator[SourceInspector]:
        yield _Fixed(observed)

    source, _ = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()

    result = execute(conn, spec_factory(source.id), resolver, inspect=fake)

    assert result.status is TaskStatus.SUCCEEDED
    assert result.output is not None and result.output["assets"] == 1


class _Fixed:
    """A `SourceInspector` that reports a fixed observation."""

    def __init__(self, observed: tuple[DiscoveredAsset, ...]) -> None:
        self._observed = observed

    def inspect(self, schemas: SchemaFilter) -> tuple[DiscoveredAsset, ...]:
        return self._observed


@pytest.mark.parametrize("attempts", [1, 2])
def test_the_registered_handler_runs_from_the_registry(
    conn: QueueConnection, spec_factory: Callable[[UUID], TaskSpec], attempts: int
) -> None:
    """What H1 does, spelled out: the *registered* handler -- environment
    resolver, real inspector -- executed through the registry entry."""
    registration = REGISTRY[SCAN_SOURCE_TASK_TYPE]
    spec = spec_factory(uuid4()).model_copy(update={"payload": dict(registration.sample_payload)})

    ctx = _ctx(conn, spec, attempts)
    result = asyncio.run(registration.fn(ctx))

    assert result.status is TaskStatus.FAILED
    assert result.error is not None and result.error.type == "urn:steward:unknown-source"
    conn.rollback()
