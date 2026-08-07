"""RunStore -- the storage seam `/v1/runs` handlers delegate to (I3, I4).

A `Protocol` typed entirely in `steward_schemas` models keeps route handlers
free of business logic (GUARDRAILS.md smell checklist: "Business logic in
services/api route handlers instead of packages") -- handlers call the store
and shape the HTTP response, they never decide anything themselves.

Two implementations, and the difference is not "fake vs real" but *scope*:

* `PostgresRunStore` is the system. Creating a run writes the run row and
  enqueues its first task in one transaction (I8), so a client that got a 202
  is guaranteed a task exists for it; a client that got an error is guaranteed
  neither does.
* `InMemoryRunStore` exists so the routing, validation and problem-details
  layers can be tested without a database. It is process-local and forgets
  everything on restart -- it is not a deployment option.

The queue's functions are synchronous and caller-transactional on purpose:
that is what makes I8 structural rather than a convention. Rather than make
the queue async and lose it, the async boundary is bridged here with
`asyncio.to_thread`, the same way `steward_queue.Worker` does it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from steward_queue import (
    NOOP_TASK_TYPE,
    RunRecord,
    connect,
    create_run,
    enqueue,
    get_run,
)
from steward_schemas import RunBudget, RunCreate, RunResponse, RunStatus, TaskSpec
from steward_telemetry import NoopTracer, SpanOutcome, Tracer, new_trace_id

DEFAULT_RUN_BUDGET = RunBudget(
    steps=32,
    tokens=200_000,
    cost_usd=Decimal("2.000000"),
    wall_clock=timedelta(minutes=15),
)
"""The caps a run created over the generic `POST /v1/runs` is admitted under.

I12 says autonomy is bounded, which means *something* has to name the bound at
the point a run is created. A conservative default here is that something: it
is injectable per store, and per-goal budgets arrive with the goal-specific
endpoints in M1 (SPEC.md §8), at which point this stops being the only answer.
"""

DEFAULT_MAX_ATTEMPTS = 3

REPLAYED_DETAIL = "idempotency key replayed an existing run"

RUN_LOCATION_PREFIX = "/v1/runs/"


class RunStore(Protocol):
    """Typed seam between the runs API and wherever runs actually live."""

    async def create_run(self, spec: RunCreate, idempotency_key: str | None) -> RunResponse:
        """Create a run for `spec`. Replaying the same `idempotency_key`
        (when not None) returns the run created the first time, unchanged."""
        ...

    async def get_run(self, run_id: UUID) -> RunResponse | None:
        """The run with `run_id`, or None if it does not exist."""
        ...


def to_response(record: RunRecord) -> RunResponse:
    """Project a `runs` row onto the published contract.

    An explicit projection, not a shared model: `RunRecord` is persistence
    state that follows the schema, `RunResponse` is a versioned promise to
    clients (I3). Keeping them apart is what lets a column be added, renamed or
    denormalised without that being an API change.
    """
    return RunResponse(
        id=record.id,
        goal=record.goal,
        payload=record.payload,
        status=record.status,
        trace_id=record.trace_id,
        budget=record.budget,
        usage=record.usage,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class PostgresRunStore:
    """The queue-backed `RunStore`: a run is a row plus its first task.

    Satisfies `RunStore` structurally. One connection per request is
    deliberate at M0 -- run creation is not a hot path, and a pool is a change
    to this class alone once it is.
    """

    def __init__(
        self,
        dsn: str,
        *,
        tracer: Tracer | None = None,
        task_type: str = NOOP_TASK_TYPE,
        budget: RunBudget = DEFAULT_RUN_BUDGET,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._dsn = dsn
        self._tracer: Tracer = tracer if tracer is not None else NoopTracer()
        self._task_type = task_type
        self._budget = budget
        self._max_attempts = max_attempts

    async def create_run(self, spec: RunCreate, idempotency_key: str | None) -> RunResponse:
        return await asyncio.to_thread(self._create_run, spec, idempotency_key)

    async def get_run(self, run_id: UUID) -> RunResponse | None:
        return await asyncio.to_thread(self._get_run, run_id)

    # --- synchronous halves, always called through asyncio.to_thread ---

    def _create_run(self, spec: RunCreate, idempotency_key: str | None) -> RunResponse:
        """The run row and its task, committed together (I8).

        The span wraps the whole attempt, including the transaction, so a
        creation that fails is a trace with an error rather than no trace at
        all. On an idempotency replay the transaction is a no-op -- `create_run`
        returns the original row and `enqueue` deduplicates on the payload the
        original was enqueued with -- and the span says so instead of pretending
        work happened.
        """
        run_id = uuid4()
        trace_id = new_trace_id(seed=str(run_id))
        with self._tracer.run_span(trace_id=trace_id, run_id=run_id, goal=spec.goal) as span:
            with connect(self._dsn) as conn:
                record = create_run(
                    conn,
                    goal=spec.goal,
                    payload=spec.payload,
                    budget=self._budget,
                    run_id=run_id,
                    trace_id=trace_id,
                    idempotency_key=idempotency_key,
                )
                enqueue(conn, self._first_task(record))
                conn.commit()
            if record.id != run_id:
                span.record(SpanOutcome.OK, REPLAYED_DETAIL)
        return to_response(record)

    def _first_task(self, record: RunRecord) -> TaskSpec:
        """The task the run's goal expands to.

        M0 has one task type and no planner, so the expansion is a constant.
        The deterministic orchestrator that turns a goal into a DAG (SPEC.md
        §3.1) replaces this function, not its callers.
        """
        return TaskSpec(
            task_id=uuid4(),
            run_id=record.id,
            task_type=self._task_type,
            payload=record.payload,
            budget=record.budget,
            max_attempts=self._max_attempts,
        )

    def _get_run(self, run_id: UUID) -> RunResponse | None:
        with connect(self._dsn) as conn:
            record = get_run(conn, run_id)
            conn.rollback()  # a read-only transaction still has to be ended
        return to_response(record) if record is not None else None


class InMemoryRunStore:
    """Process-local `RunStore` for testing the HTTP layer in isolation.

    Not persistent, not shared across replicas, and no queue behind it: a run
    created here stays `pending` forever because nothing executes it. Satisfies
    `RunStore` structurally.
    """

    def __init__(self, *, budget: RunBudget = DEFAULT_RUN_BUDGET) -> None:
        self._runs: dict[UUID, RunResponse] = {}
        self._by_idempotency_key: dict[str, UUID] = {}
        self._budget = budget
        self._lock = asyncio.Lock()

    async def create_run(self, spec: RunCreate, idempotency_key: str | None) -> RunResponse:
        async with self._lock:
            if idempotency_key is not None:
                existing_id = self._by_idempotency_key.get(idempotency_key)
                if existing_id is not None:
                    return self._runs[existing_id]

            now = datetime.now(UTC)
            run_id = uuid4()
            run = RunResponse(
                id=run_id,
                goal=spec.goal,
                payload=spec.payload,
                status=RunStatus.PENDING,
                trace_id=new_trace_id(seed=str(run_id)),
                budget=self._budget,
                usage=RunBudget(steps=0, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(0)),
                created_at=now,
                updated_at=now,
            )
            self._runs[run.id] = run
            if idempotency_key is not None:
                self._by_idempotency_key[idempotency_key] = run.id
            return run

    async def get_run(self, run_id: UUID) -> RunResponse | None:
        async with self._lock:
            return self._runs.get(run_id)
