"""H7 — the masking canary (GUARDRAILS.md §1 Tier H, I6, N7, issue #49).

Secrets are planted in the fixture source's data (`conftest.FIXTURE_DATA`): an
email, a payment card, an opaque token, a value whose payload sits *after the
last dot*, and one whose payload sits *before a `://`* -- each a string that
occurs nowhere else in this repository. The
real `profile_asset` handler is then executed the way production executes it --
claimed off the queue by a real `Worker`, through the registered handler with
the environment-backed secret resolver and the real Postgres profiler -- and the
harness asserts that none of those strings appears in:

* any row of **any table** in Steward's database (not just `profiles` and
  `audit_log`: the sweep is over `pg_tables`, so a table added in a later
  milestone is covered the day it exists, with no edit here);
* any **log record** emitted while the task ran, at any level, from any logger;
* anything written to **stdout or stderr**;
* any **span** the worker opened -- the trace payload, which is why this runs
  through a `Worker` with a recording tracer rather than calling the handler
  directly: handlers do not open spans, the worker does.

Three things make the result mean something rather than merely be green:

* **The canaries were really read.** The harness asserts the profile of the
  canary-bearing column exists and carries the *masked* form, so a run that
  silently profiled nothing fails here.
* **The sweep can find a leak.** `test_the_sweep_would_catch_a_planted_leak`
  writes a canary into an audit row and asserts the sweep reports it, and the
  log half plants one through a lazy `%s` call -- an assertion that cannot fail
  is not evidence.
* **A canary is evidence only for the shapes it takes, and only where it is
  swept.** That is not a caveat, it is the lesson this file learned twice. The
  first three canaries all ended in `.test` or had no dot, so when `_mask_email`
  published everything after the final dot this harness watched the payload land
  in `profiles` and reported green. `CANARY_AFTER_LAST_DOT` was added for that
  region -- and `CANARY_BEFORE_SCHEME` for the URL scheme, the next leak, which
  no canary was shaped for either.

  Shape alone is not enough. A partial leak publishes the *payload*, not the
  whole canary, so a sweep for the full string finds nothing: a regressed
  `_mask_url` writes `X-CANARY-CASE-7d21e9f0://h***/****`, which does not
  contain `CANARY_BEFORE_SCHEME`. So each of those two is swept for by its
  payload as well -- `CANARY_TAIL` and `CANARY_HEAD` -- and those are the
  assertions that would actually fail.

What this cannot prove is stated in the PR and in GUARDRAILS' H7 row: it
observes one masker over one fixture estate, and there are no prompts yet to
observe at all (#50). What it does prove is that today, on this path, nothing
that entered the masker came out the other side.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from psycopg import sql
from steward_catalog import (
    PROFILE_ASSET_TASK_TYPE,
    EnvSecretResolver,
    build_scan_source,
    postgres_inspector,
    register_source,
)
from steward_queue import (
    SYSTEM_ACTOR,
    QueueConnection,
    TaskContext,
    TaskState,
    Worker,
    create_run,
    enqueue,
    write_audit,
)
from steward_schemas import RunBudget, SourceCreate, TaskSpec, TaskStatus
from steward_telemetry import Span, SpanOutcome

pytestmark = pytest.mark.invariants

SOURCE_SECRET_ENV = "STEWARD_TEST_SOURCE_DSN"

SELECT_TABLES = "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
SELECT_ASSET_IDS = "SELECT name, id FROM assets WHERE lifecycle = 'active'"
SELECT_PROFILE = "SELECT profile FROM profiles WHERE asset_id = %(asset_id)s ORDER BY version DESC LIMIT 1"
SELECT_TASK_STATES = "SELECT state, count(*) FROM tasks GROUP BY state"

# Built here rather than imported from `steward_orchestration`. steward-catalog
# does not declare that package as a dependency and must not: the boundary
# contract in the root `pyproject.toml` says the catalog "must not learn about
# goals", and a dev dependency would be a cycle, since orchestration already
# declares a test-only dependency on this package. import-linter cannot see it
# either way -- its `root_packages` are the `src/` trees, so a test-tree import
# crosses the boundary invisibly. The task type itself comes from
# `steward_catalog`, which exports it, and `test_goals.py` is where the two
# packages' names are asserted equal.
PROFILE_BUDGET = RunBudget(steps=1, tokens=0, cost_usd=Decimal("0.000000"), wall_clock=timedelta(minutes=30))

# `t::text` renders a whole row as one record literal, so this asks "does this
# table contain the needle anywhere at all" without naming a column -- which is
# what keeps the sweep generic over tables it has never heard of.
ROW_MATCHES = sql.SQL("SELECT count(*) FROM {relation} AS t WHERE t::text LIKE %(needle)s")


@dataclass
class RecordedSpan:
    """One span the worker opened, with everything it was told."""

    trace_id: str
    run_id: UUID
    task_id: UUID
    task_type: str
    outcome: SpanOutcome | None = None
    detail: str | None = None

    def record(self, outcome: SpanOutcome, detail: str | None = None) -> None:
        self.outcome, self.detail = outcome, detail

    def payload(self) -> str:
        return " ".join(
            str(part)
            for part in (self.trace_id, self.run_id, self.task_id, self.task_type, self.outcome, self.detail)
        )


class RecordingTracer:
    """A `Tracer` that keeps its spans instead of exporting them (I7).

    Spans are the observable output of tracing, so the assertions below are
    about emitted events rather than about how the worker called a collaborator.
    """

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def run_span(self, *, trace_id: str, run_id: UUID, goal: str) -> Iterator[Span]:
        raise NotImplementedError("the worker executes tasks; runs are opened by their creator")
        yield  # pragma: no cover -- unreachable, kept so the signature is a generator

    @contextmanager
    def task_span(self, *, trace_id: str, run_id: UUID, task_id: UUID, task_type: str) -> Iterator[Span]:
        span = RecordedSpan(trace_id=trace_id, run_id=run_id, task_id=task_id, task_type=task_type)
        self.spans.append(span)
        yield span


@dataclass
class ProfileRun:
    """What one canary-profiling run produced, in the three places it could leak.

    Log records are rendered rather than kept raw: a handler that passed a
    value as a lazy `%s` argument leaks it through `getMessage()` while its
    format string looks perfectly clean, and that is the shape this hunts.
    """

    targets: dict[str, UUID]
    tracer: RecordingTracer
    logs: list[str]

    def logged(self) -> str:
        return "\n".join(self.logs)


def steward_tables(conn: QueueConnection) -> list[str]:
    return [row[0] for row in conn.execute(SELECT_TABLES).fetchall()]


def rows_containing(conn: QueueConnection, needle: str) -> dict[str, int]:
    """Every table in Steward's database that holds `needle`, and how often."""
    found: dict[str, int] = {}
    for table in steward_tables(conn):
        row = conn.execute(
            ROW_MATCHES.format(relation=sql.Identifier(table)), {"needle": f"%{needle}%"}
        ).fetchone()
        if row is not None and row[0]:
            found[table] = int(row[0])
    return found


@pytest.fixture
def scanned_source(
    conn: QueueConnection,
    source_create: SourceCreate,
    resolver: EnvSecretResolver,
    spec_factory: Callable[[UUID], TaskSpec],
) -> dict[str, UUID]:
    """The fixture estate, catalogued: asset name -> asset id."""
    source, _ = register_source(conn, source_create, actor=SYSTEM_ACTOR)
    conn.commit()
    scan = build_scan_source(resolver=resolver, inspect=postgres_inspector)
    result = asyncio.run(scan(TaskContext(connection=conn, spec=spec_factory(source.id), attempts=1)))
    conn.commit()
    assert result.status is TaskStatus.SUCCEEDED, result.error
    assets = {row[0]: row[1] for row in conn.execute(SELECT_ASSET_IDS).fetchall()}
    conn.rollback()
    return assets


@pytest.fixture
def profiled(
    conn: QueueConnection,
    steward_dsn: str,
    source_dsn: str,
    scanned_source: dict[str, UUID],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> ProfileRun:
    """Profile the canary-bearing tables the way production does.

    Through the *registered* handler -- environment-backed secrets, the real
    profiler -- claimed off the queue by a real `Worker`, so the spans, the log
    records and the rows this harness inspects are the ones a deployment would
    produce.

    The captured log records are carried on the result rather than read back
    off `caplog` in the test: profiling happens during *setup*, and
    `caplog.records` in a test body holds the call phase's records only -- so a
    test that read it directly would assert over an empty list and pass on work
    it never saw.
    """
    monkeypatch.setenv(SOURCE_SECRET_ENV, source_dsn)
    targets = {name: scanned_source[name] for name in ("customers", "raw_events")}
    run = create_run(conn, goal="profile_asset", budget=PROFILE_BUDGET)
    for asset_id in targets.values():
        enqueue(
            conn,
            TaskSpec(
                task_id=uuid4(),
                run_id=run.id,
                task_type=PROFILE_ASSET_TASK_TYPE,
                payload={"asset_id": str(asset_id)},
                budget=PROFILE_BUDGET,
                max_attempts=1,
            ),
        )
    conn.commit()

    tracer = RecordingTracer()
    worker = Worker(
        steward_dsn,
        "h7-canary",
        task_types=[PROFILE_ASSET_TASK_TYPE],
        batch_size=len(targets),
        tracer=tracer,
    )
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(worker.run_once()) == len(targets)
        logs = [record.getMessage() for record in caplog.records]
    # A failed task would leave nothing profiled and make every assertion below
    # true for the wrong reason -- the shape GUARDRAILS.md §3 warns about.
    states = conn.execute(SELECT_TASK_STATES).fetchall()
    conn.rollback()
    assert states == [(TaskState.SUCCEEDED.value, len(targets))], states
    return ProfileRun(targets=targets, tracer=tracer, logs=logs)


def test_the_canaries_were_actually_profiled(
    conn: QueueConnection,
    profiled: ProfileRun,
    canary_email: str,
) -> None:
    """The guard against a vacuous pass: a profile that never read the canary
    column would satisfy every assertion below for the wrong reason."""
    row = conn.execute(SELECT_PROFILE, {"asset_id": profiled.targets["customers"]}).fetchone()
    conn.rollback()

    assert row is not None, "the canary table was never profiled"
    columns = {column["name"]: column for column in row[0]["columns"]}
    masked = [frequency["value"]["masked"] for frequency in columns["email"]["top_values"]]
    assert "c***@s***.****" in masked  # the canary row, and only in masked form
    assert canary_email not in masked
    assert [span.task_type for span in profiled.tracer.spans] == [PROFILE_ASSET_TASK_TYPE] * len(
        profiled.targets
    )


def test_nothing_before_a_canarys_scheme_separator_reaches_the_database(
    conn: QueueConnection,
    profiled: ProfileRun,
    canary_head: str,
) -> None:
    """Escape #3 in canary form, swept the way it can actually be caught.

    `_mask_url` published the scheme verbatim, and a regression doing so again
    writes `X-CANARY-CASE-7d21e9f0://h***/****` -- which does not contain the
    full canary, so the whole-string sweep returns nothing and the harness goes
    green. The payload has to be swept on its own, exactly as the email tail is.
    """
    assert rows_containing(conn, canary_head) == {}
    conn.rollback()


def test_nothing_behind_a_canarys_last_dot_reaches_the_database(
    conn: QueueConnection,
    profiled: ProfileRun,
    canary_tail: str,
) -> None:
    """The class the other canaries could not see (#49 review).

    `_mask_email` used to copy everything after the final dot verbatim, and
    every canary here ended in `.test`, so the harness watched a payload land in
    `profiles` and reported green. Swept for as the tail alone: a mask that
    published only the tail would leave the full canary absent and satisfy every
    other assertion in this file.
    """
    assert rows_containing(conn, canary_tail) == {}
    conn.rollback()


def test_no_canary_reaches_any_row_of_stewards_database(
    conn: QueueConnection,
    profiled: ProfileRun,
    canaries: tuple[str, ...],
) -> None:
    """I6/N7 over the system of record, table by table -- including the ones
    this harness has never heard of."""
    assert "profiles" in steward_tables(conn)  # the sweep covers the new table
    conn.rollback()
    for canary in canaries:
        assert rows_containing(conn, canary) == {}, f"{canary!r} leaked into Steward's database"
        conn.rollback()


def test_no_canary_reaches_a_log_line_or_the_console(
    profiled: ProfileRun,
    canaries: tuple[str, ...],
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The paths types cannot cover, which is the reason H7 exists at all.

    The non-emptiness assertion is the point of the first line: `canary not in
    ""` passes on a capture of nothing, so without it this test would be green
    on work it never inspected (GUARDRAILS.md §3).
    """
    assert profiled.logs, "no log records were captured; these assertions would be vacuous"
    logged = profiled.logged()
    captured = capfd.readouterr()

    for canary in canaries:
        assert canary not in logged
        assert canary not in captured.out
        assert canary not in captured.err


def test_no_canary_reaches_a_trace_payload(
    profiled: ProfileRun,
    canaries: tuple[str, ...],
) -> None:
    assert profiled.tracer.spans, "no spans were opened; this assertion would be vacuous"

    payloads = " ".join(span.payload() for span in profiled.tracer.spans)
    for canary in canaries:
        assert canary not in payloads


def test_the_sweep_would_catch_a_planted_leak(conn: QueueConnection, canary_secret: str) -> None:
    """H7 must be falsifiable: a leak has to make the sweep fail.

    A canary is written into an audit row -- the shape a careless `after`
    payload would take -- and the sweep is asserted to find it. Rolled back, so
    the leak exists only inside this test's transaction.
    """
    assert rows_containing(conn, canary_secret) == {}

    write_audit(
        conn,
        actor=SYSTEM_ACTOR,
        action="canary.planted",
        entity_type="profile",
        entity_id="planted",
        after={"leaked": canary_secret},
    )

    assert rows_containing(conn, canary_secret) == {"audit_log": 1}
    conn.rollback()


def test_the_log_assertion_would_catch_a_planted_leak(
    canary_secret: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The log half needs its own planted leak, for the same reason the database
    half does: `assert canary not in ""` passes on a capture of nothing.

    The leak is planted the way a real one would arrive -- a value passed as a
    lazy `%s` argument, where the format string looks perfectly clean.
    """
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("steward_catalog.canary_probe").warning("profiled %s", canary_secret)
        logs = [record.getMessage() for record in caplog.records]

    assert any(canary_secret in line for line in logs)
