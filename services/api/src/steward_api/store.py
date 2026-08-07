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

Both admit a run the same way, through `steward_orchestration.plan_run`: the
goal registry decides whether a request is a run at all, what it expands to,
and what it may spend. Neither store knows what a goal means, and neither one
holds a default budget any more (issue #19).

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

from steward_orchestration import RunPlan, plan_run
from steward_queue import (
    RunRecord,
    connect,
    create_run,
    enqueue,
    get_run,
)
from steward_schemas import Run, RunBudget, RunCreate, RunStatus
from steward_telemetry import NoopTracer, SpanOutcome, Tracer, new_trace_id

REPLAYED_DETAIL = "idempotency key replayed an existing run"

RUN_LOCATION_PREFIX = "/v1/runs/"


class IdempotencyKeyReused(Exception):
    """An idempotency key was replayed with a body that is not the same request.

    A domain error, not an HTTP one: the store decides that the two requests
    differ, the route decides that is a 409 (I4 -- storage does not know about
    status codes). Returning the original run instead would be the dangerous
    answer, because a client that retried with an edited goal would read 202
    and believe the edited goal was queued when nothing will ever run it.
    """

    def __init__(self, idempotency_key: str, existing: Run) -> None:
        super().__init__(f"idempotency key {idempotency_key!r} was used for a different request")
        self.idempotency_key = idempotency_key
        self.existing = existing


class RunStore(Protocol):
    """Typed seam between the runs API and wherever runs actually live."""

    async def create_run(self, spec: RunCreate, idempotency_key: str | None) -> Run:
        """Create a run for `spec`. Replaying the same `idempotency_key`
        (when not None) with the same body returns the run created the first
        time, unchanged; replaying it with a different body raises
        `IdempotencyKeyReused`.

        Raises `steward_orchestration.UnknownGoal` or `InvalidGoalPayload`
        without creating anything when `spec` does not name a registered goal
        or does not match that goal's schema (issue #19)."""
        ...

    async def get_run(self, run_id: UUID) -> Run | None:
        """The run with `run_id`, or None if it does not exist."""
        ...


def _same_request(run: Run, spec: RunCreate) -> bool:
    """Whether `spec` asks for what `run` was created to do."""
    return run.goal == spec.goal and run.payload == spec.payload


def to_response(record: RunRecord) -> Run:
    """Project a `runs` row onto the published contract.

    An explicit projection, not a shared model: `RunRecord` is persistence
    state that follows the schema, `Run` is a versioned promise to clients
    (I3). Keeping them apart is what lets a column be added, renamed or
    denormalised without that being an API change.
    """
    return Run(
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

    def __init__(self, dsn: str, *, tracer: Tracer | None = None) -> None:
        self._dsn = dsn
        self._tracer: Tracer = tracer if tracer is not None else NoopTracer()

    async def create_run(self, spec: RunCreate, idempotency_key: str | None) -> Run:
        return await asyncio.to_thread(self._create_run, spec, idempotency_key)

    async def get_run(self, run_id: UUID) -> Run | None:
        return await asyncio.to_thread(self._get_run, run_id)

    # --- synchronous halves, always called through asyncio.to_thread ---

    def _create_run(self, spec: RunCreate, idempotency_key: str | None) -> Run:
        """The run row and its planned tasks, committed together (I8).

        Admission comes first and outside everything else: `plan_run` rejects
        an unknown goal or a payload that does not match its goal's schema
        before a run id, a trace id or a row exists, so a rejected request
        leaves nothing behind -- not a run, not a task, not a trace (issue
        #19). A trace for work the system refused to accept would be a trace
        with no run to attach to.

        Two properties pull in opposite directions here. The span has to exist
        even when creation fails -- a failure is a trace with an error, not a
        missing trace -- and it has to name the run it describes, which on an
        idempotency replay is the *original* run, not the identity this call
        generated and then discarded (#27). Opening the span first satisfied
        only the first, and put replay spans on a trace no run points at.

        So the transaction is resolved first and the span opened over its
        result: a persisted record names the span (its own run id, trace id and
        goal), and a failure -- which persisted no run at all -- is re-raised
        inside a span on the identity this call generated, so the tracer marks
        it `ERROR` on the way out exactly as it did before. The cost is that
        the span no longer brackets the transaction and so measures nothing;
        identity is the guarantee (I7), duration was incidental.

        `record.id != run_id` is how a replay is detected: the id this call
        generated is on the row only if this call created it. A replay's
        transaction is a no-op -- `create_run` returns the original row and
        nothing is enqueued onto it -- and the span says so instead of
        pretending work happened. A replay with a different body raises inside
        that same span, so the conflict is an error on the original run's trace
        rather than on an orphan.
        """
        plan = plan_run(spec.goal, spec.payload)
        run_id = uuid4()
        trace_id = new_trace_id(seed=str(run_id))
        try:
            record = self._persist(
                spec, plan, run_id=run_id, trace_id=trace_id, idempotency_key=idempotency_key
            )
        except Exception:
            # Nothing was persisted, so the identity this call generated is the
            # only one that ever named the work. Re-raising inside the span is
            # what makes the tracer record the failure, exactly as before.
            with self._tracer.run_span(trace_id=trace_id, run_id=run_id, goal=spec.goal):
                raise
        with self._tracer.run_span(trace_id=record.trace_id, run_id=record.id, goal=record.goal) as span:
            run = to_response(record)
            if record.id != run_id:
                if idempotency_key is not None and not _same_request(run, spec):
                    raise IdempotencyKeyReused(idempotency_key, run)
                span.record(SpanOutcome.OK, REPLAYED_DETAIL)
        return run

    def _persist(
        self,
        spec: RunCreate,
        plan: RunPlan,
        *,
        run_id: UUID,
        trace_id: str,
        idempotency_key: str | None,
    ) -> RunRecord:
        """The run row and the tasks its goal expands to, committed together (I8).

        Separate from `_create_run` only so the run this call is about is known
        -- returned or raised -- before anything opens a span about it.

        The run is admitted under the *goal's* budget (I12): the caps a run
        gets are a property of what it was asked to do, not of this service.

        Enqueueing is skipped when the row already existed, because `plan`
        expands the body of *this* call: a replay carrying a different body
        would otherwise attach its tasks to the original run before the
        conflict that rejects it is even detected. The original's tasks were
        committed with it, so there is nothing to re-create.
        """
        with connect(self._dsn) as conn:
            record = create_run(
                conn,
                goal=spec.goal,
                payload=spec.payload,
                budget=plan.budget,
                run_id=run_id,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
            )
            if record.id == run_id:
                for task in plan.task_specs(record.id):
                    enqueue(conn, task)
            conn.commit()
        return record

    def _get_run(self, run_id: UUID) -> Run | None:
        with connect(self._dsn) as conn:
            record = get_run(conn, run_id)
            conn.rollback()  # a read-only transaction still has to be ended
        return to_response(record) if record is not None else None


class InMemoryRunStore:
    """Process-local `RunStore` for testing the HTTP layer in isolation.

    Not persistent, not shared across replicas, and no queue behind it: a run
    created here stays `pending` forever because nothing executes it. Satisfies
    `RunStore` structurally.

    It admits runs through the same goal registry the real store does. That is
    not a convenience: admission is the behavior the HTTP-layer tests are
    about, and a double that accepted goals the system rejects would make those
    tests prove the opposite of the truth.
    """

    def __init__(self) -> None:
        self._runs: dict[UUID, Run] = {}
        self._by_idempotency_key: dict[str, UUID] = {}
        self._lock = asyncio.Lock()

    async def create_run(self, spec: RunCreate, idempotency_key: str | None) -> Run:
        plan = plan_run(spec.goal, spec.payload)
        async with self._lock:
            if idempotency_key is not None:
                existing_id = self._by_idempotency_key.get(idempotency_key)
                if existing_id is not None:
                    existing = self._runs[existing_id]
                    if not _same_request(existing, spec):
                        raise IdempotencyKeyReused(idempotency_key, existing)
                    return existing

            now = datetime.now(UTC)
            run_id = uuid4()
            run = Run(
                id=run_id,
                goal=spec.goal,
                payload=spec.payload,
                status=RunStatus.PENDING,
                trace_id=new_trace_id(seed=str(run_id)),
                budget=plan.budget,
                usage=RunBudget(steps=0, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(0)),
                created_at=now,
                updated_at=now,
            )
            self._runs[run.id] = run
            if idempotency_key is not None:
                self._by_idempotency_key[idempotency_key] = run.id
            return run

    async def get_run(self, run_id: UUID) -> Run | None:
        async with self._lock:
            return self._runs.get(run_id)
