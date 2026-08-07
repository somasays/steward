"""CatalogStore -- the storage seam the `/v1/sources` and `/v1/assets` handlers
delegate to (I3, I4).

Same shape and the same reasoning as `store.RunStore`: a `Protocol` typed
entirely in `steward_schemas` models, so route handlers shape HTTP responses
and decide nothing (GUARDRAILS.md §4: "business logic in services/api route
handlers"). Every decision below this seam belongs to a package --
`steward_catalog` for registration, convergence and paging, `steward_orchestration`
for what a scan run is allowed to do.

Two implementations, and the difference is scope rather than fidelity:

* `PostgresCatalogStore` is the system. Registration is idempotent on the
  source's natural key; starting a scan is idempotent on "a scan of this source
  is already in flight", decided under the queue's admission lock so two
  simultaneous requests cannot both start one (I8).
* `InMemoryCatalogStore` exists so the routing and problem-details layers -- and
  the OpenAPI export -- can be built without a database. It never scans
  anything, so its catalog is always empty; it is not a deployment option.

The queue's functions are synchronous and caller-transactional on purpose (that
is what makes I8 structural), so the async boundary is bridged with
`asyncio.to_thread`, exactly as `store.PostgresRunStore` does it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from steward_catalog import (
    AssetRecord,
    ColumnRecord,
    InvalidCursor,
    SourceKey,
    SourceRecord,
    decode_cursor,
    encode_cursor,
    get_asset,
    get_source,
    list_asset_columns,
    list_assets,
    register_source,
)
from steward_orchestration import SCAN_SOURCE_GOAL, plan_run
from steward_queue import (
    Actor,
    ActorKind,
    RunRecord,
    claim_single_flight,
    connect,
    create_run,
    enqueue,
)
from steward_schemas import (
    Asset,
    AssetDetail,
    AssetPage,
    Column,
    Run,
    RunBudget,
    RunStatus,
    Source,
    SourceCreate,
)
from steward_telemetry import NoopTracer, Tracer, new_trace_id

from steward_api.store import IdempotencyKeyReused, to_response

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

SOURCE_LOCATION_PREFIX = "/v1/sources/"

API_ACTOR = Actor(kind=ActorKind.HUMAN, id="api")
"""Who a registration is attributed to on its audit row.

`human` rather than `system`: `POST /v1/sources` is only ever reached because a
person or their client asked for it. Real identity lands with authentication;
until then, saying "the API" is honest and saying "the system" would not be.
"""


class SourceNotFound(LookupError):
    """A scan or listing named a source that was never registered.

    A domain error, not an HTTP one: the store decides the source does not
    exist, the route decides that is a 404 (I4).
    """

    def __init__(self, source_id: UUID) -> None:
        super().__init__(f"no source registered with id {source_id}")
        self.source_id = source_id


class CatalogStore(Protocol):
    """Typed seam between the catalog API and wherever the catalog lives."""

    async def register_source(self, create: SourceCreate) -> tuple[Source, bool]:
        """Register `create`, or return the source its natural key already
        names. The flag says whether this call created it."""
        ...

    async def start_scan(self, source_id: UUID, idempotency_key: str | None) -> tuple[Run, bool]:
        """Start a scan of `source_id`, or return the one already in flight.
        The flag says whether this call started it.

        Raises `SourceNotFound` when nothing is registered under `source_id`,
        and `IdempotencyKeyReused` when `idempotency_key` was used for a
        different request."""
        ...

    async def list_assets(
        self, *, source_id: UUID | None, cursor: str | None, limit: int
    ) -> AssetPage:
        """One page of assets, in a total, stable order. Raises `InvalidCursor`
        for a cursor this API did not issue."""
        ...

    async def get_asset(self, asset_id: UUID) -> AssetDetail | None:
        """An asset and its columns, or None."""
        ...


def source_response(record: SourceRecord) -> Source:
    """Project a `sources` row onto the published contract.

    Carries `dsn_secret_ref` -- a reference into the secret store, which is the
    whole point of storing one -- and no credential, because the row holds
    none (N7).
    """
    return Source(
        id=record.id,
        workspace_id=record.workspace_id,
        name=record.name,
        engine=record.key.engine,
        dsn_secret_ref=record.dsn_secret_ref,
        scan_schedule=record.scan_schedule,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def asset_response(record: AssetRecord) -> Asset:
    return Asset(
        id=record.id,
        workspace_id=record.workspace_id,
        source_id=record.source_id,
        fqn=record.fqn,
        asset_type=record.asset_type,
        lifecycle=record.lifecycle,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def column_response(record: ColumnRecord) -> Column:
    return Column(
        id=record.id,
        workspace_id=record.workspace_id,
        asset_id=record.asset_id,
        name=record.name,
        data_type=record.data_type,
        ordinal=record.ordinal,
        nullable=record.nullable,
        lifecycle=record.lifecycle,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def page_of(records: list[AssetRecord], limit: int) -> AssetPage:
    """A page, plus the cursor that resumes after its last row.

    A full page always carries a cursor, even when it happens to be the last
    one: knowing that requires reading ahead, and a client that follows the
    cursor simply gets an empty final page. The alternative -- fetching
    `limit + 1` -- buys one saved round trip for a row read on every page.
    """
    items = tuple(asset_response(record) for record in records)
    if len(records) < limit:
        return AssetPage(items=items)
    last = records[-1]
    return AssetPage(items=items, next_cursor=encode_cursor(last.schema_name, last.name, last.id))


def scan_payload(source_id: UUID) -> dict[str, str]:
    """The goal payload a scan of `source_id` is admitted under.

    One function, used both to plan the run and to ask whether one is already
    in flight, because those two have to agree on what "the same scan" means.
    """
    return {"source_id": str(source_id)}


class PostgresCatalogStore:
    """The queue-backed `CatalogStore`. Satisfies `CatalogStore` structurally.

    One connection per request, deliberately: neither registration nor scan
    admission is a hot path, and a pool is a change to this class alone once
    one of them is.
    """

    def __init__(self, dsn: str, *, tracer: Tracer | None = None) -> None:
        self._dsn = dsn
        self._tracer: Tracer = tracer if tracer is not None else NoopTracer()

    async def register_source(self, create: SourceCreate) -> tuple[Source, bool]:
        return await asyncio.to_thread(self._register_source, create)

    async def start_scan(self, source_id: UUID, idempotency_key: str | None) -> tuple[Run, bool]:
        return await asyncio.to_thread(self._start_scan, source_id, idempotency_key)

    async def list_assets(
        self, *, source_id: UUID | None, cursor: str | None, limit: int
    ) -> AssetPage:
        return await asyncio.to_thread(self._list_assets, source_id, cursor, limit)

    async def get_asset(self, asset_id: UUID) -> AssetDetail | None:
        return await asyncio.to_thread(self._get_asset, asset_id)

    # --- synchronous halves, always called through asyncio.to_thread ---

    def _register_source(self, create: SourceCreate) -> tuple[Source, bool]:
        with connect(self._dsn) as conn:
            record, created = register_source(conn, create, actor=API_ACTOR)
            conn.commit()
        return source_response(record), created

    def _start_scan(self, source_id: UUID, idempotency_key: str | None) -> tuple[Run, bool]:
        """The run row, its one task, and the decision not to start a second --
        all in one transaction (I8).

        The order is the whole design. `claim_single_flight` takes the queue's
        admission lock *before* reading, so two simultaneous requests for the
        same source serialise: the second finds the run the first committed
        instead of both finding nothing and both starting a scan. The lock is
        transaction-scoped, so committing releases it.
        """
        payload = scan_payload(source_id)
        plan = plan_run(SCAN_SOURCE_GOAL, payload)
        run_id = uuid4()
        with connect(self._dsn) as conn:
            if get_source(conn, source_id) is None:
                conn.rollback()
                raise SourceNotFound(source_id)
            in_flight = claim_single_flight(conn, goal=SCAN_SOURCE_GOAL, payload=payload)
            if in_flight is not None:
                conn.rollback()
                return self._traced(in_flight), False
            record = create_run(
                conn,
                goal=SCAN_SOURCE_GOAL,
                payload=payload,
                budget=plan.budget,
                run_id=run_id,
                trace_id=new_trace_id(seed=str(run_id)),
                idempotency_key=idempotency_key,
                actor=API_ACTOR,
            )
            if record.id == run_id:
                for task in plan.task_specs(record.id):
                    enqueue(conn, task, actor=API_ACTOR)
            conn.commit()
        if record.id != run_id and record.payload != payload and idempotency_key is not None:
            # The key was replayed for a *different* source. Returning the
            # original run would tell the client its request was queued when
            # nothing will ever scan that source.
            raise IdempotencyKeyReused(idempotency_key, to_response(record))
        return self._traced(record), record.id == run_id

    def _traced(self, record: RunRecord) -> Run:
        """Open the run's span on the identity the row carries (I7)."""
        with self._tracer.run_span(trace_id=record.trace_id, run_id=record.id, goal=record.goal):
            return to_response(record)

    def _list_assets(self, source_id: UUID | None, cursor: str | None, limit: int) -> AssetPage:
        with connect(self._dsn) as conn:
            records = list_assets(
                conn,
                source_id=source_id,
                after=decode_cursor(cursor) if cursor is not None else None,
                limit=limit,
            )
            conn.rollback()  # a read-only transaction still has to be ended
        return page_of(records, limit)

    def _get_asset(self, asset_id: UUID) -> AssetDetail | None:
        with connect(self._dsn) as conn:
            record = get_asset(conn, asset_id)
            columns = list_asset_columns(conn, asset_id) if record is not None else []
            conn.rollback()
        if record is None:
            return None
        return AssetDetail(
            asset=asset_response(record),
            columns=tuple(column_response(column) for column in columns),
        )


class InMemoryCatalogStore:
    """Process-local `CatalogStore` for testing the HTTP layer in isolation.

    Registration is idempotent on the same natural key the database enforces,
    because that is behaviour the HTTP tests are about. Scanning creates a run
    nothing will ever execute, and the catalog is therefore always empty --
    which is why this is not a deployment option.
    """

    def __init__(self) -> None:
        self._sources: dict[SourceKey, Source] = {}
        self._by_id: dict[UUID, Source] = {}
        self._scans: dict[UUID, Run] = {}
        self._lock = asyncio.Lock()

    async def register_source(self, create: SourceCreate) -> tuple[Source, bool]:
        key = SourceKey.of(create)
        async with self._lock:
            existing = self._sources.get(key)
            if existing is not None:
                return existing, False
            now = datetime.now(UTC)
            source = Source(
                id=uuid4(),
                workspace_id=UUID(int=0),
                name=create.name,
                engine=create.engine,
                dsn_secret_ref=create.dsn_secret_ref,
                scan_schedule=create.scan_schedule,
                created_at=now,
                updated_at=now,
            )
            self._sources[key] = source
            self._by_id[source.id] = source
            return source, True

    async def start_scan(self, source_id: UUID, idempotency_key: str | None) -> tuple[Run, bool]:
        payload = scan_payload(source_id)
        plan = plan_run(SCAN_SOURCE_GOAL, payload)
        async with self._lock:
            if source_id not in self._by_id:
                raise SourceNotFound(source_id)
            existing = self._scans.get(source_id)
            if existing is not None:
                return existing, False
            now = datetime.now(UTC)
            run_id = uuid4()
            run = Run(
                id=run_id,
                goal=SCAN_SOURCE_GOAL,
                payload=dict(payload),
                status=RunStatus.PENDING,
                trace_id=new_trace_id(seed=str(run_id)),
                budget=plan.budget,
                usage=RunBudget(steps=0, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(0)),
                created_at=now,
                updated_at=now,
            )
            self._scans[source_id] = run
            return run, True

    async def list_assets(
        self, *, source_id: UUID | None, cursor: str | None, limit: int
    ) -> AssetPage:
        if cursor is not None:
            decode_cursor(cursor)  # a bad cursor is a rejection here too
        return AssetPage(items=())

    async def get_asset(self, asset_id: UUID) -> AssetDetail | None:
        return None


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "SOURCE_LOCATION_PREFIX",
    "CatalogStore",
    "InMemoryCatalogStore",
    "InvalidCursor",
    "PostgresCatalogStore",
    "SourceNotFound",
]
