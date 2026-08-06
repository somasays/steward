"""RunStore -- the storage seam `/v1/runs` handlers delegate to (I3, I4).

A `Protocol` typed entirely in `steward_schemas` models keeps route handlers
free of business logic (GUARDRAILS.md smell checklist: "Business logic in
services/api route handlers instead of packages") -- handlers call the
store and shape the HTTP response, they never decide anything themselves.
`InMemoryRunStore` is the M0 skeleton implementation; issue #5 swaps in a
Postgres/queue-backed store behind this same `Protocol`, with no
route-handler change required.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from steward_schemas import Run, RunCreate, RunStatus


class RunStore(Protocol):
    """Typed seam between the runs API and wherever runs actually live."""

    async def create_run(self, spec: RunCreate, idempotency_key: str | None) -> Run:
        """Create a run for `spec`. Replaying the same `idempotency_key`
        (when not None) returns the run created the first time, unchanged."""
        ...

    async def get_run(self, run_id: UUID) -> Run | None:
        """The run with `run_id`, or None if it does not exist."""
        ...


class InMemoryRunStore:
    """Process-local `RunStore` (M0 skeleton only -- not persistent, not
    shared across workers). Satisfies `RunStore` structurally.
    """

    def __init__(self) -> None:
        self._runs: dict[UUID, Run] = {}
        self._by_idempotency_key: dict[str, UUID] = {}
        self._lock = asyncio.Lock()

    async def create_run(self, spec: RunCreate, idempotency_key: str | None) -> Run:
        async with self._lock:
            if idempotency_key is not None:
                existing_id = self._by_idempotency_key.get(idempotency_key)
                if existing_id is not None:
                    return self._runs[existing_id]

            now = datetime.now(UTC)
            run = Run(
                id=uuid4(),
                goal=spec.goal,
                payload=spec.payload,
                status=RunStatus.PENDING,
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
