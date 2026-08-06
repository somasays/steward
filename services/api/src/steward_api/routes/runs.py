"""`POST /v1/runs`, `GET /v1/runs/{id}` (SPEC.md §8, issue #4).

No business logic here (GUARDRAILS.md smell checklist): handlers parse the
request, delegate to a `RunStore`, and shape the HTTP response. The idempotency
key (SPEC.md §8: "idempotency keys on all POSTs that create runs") is a
header the store, not the handler, resolves into "same run back".
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Response, status
from steward_schemas import ProblemDetails, Run, RunCreate

from steward_api.problem_details import not_found
from steward_api.store import RunStore

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]

# Every error response actually served is `ProblemDetails` (problem_details.py
# overrides FastAPI's default shapes) -- documented here too, so the
# published OpenAPI spec (contracts/openapi.json) matches runtime behavior.
_VALIDATION_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {"model": ProblemDetails, "description": "Validation error"}
}
_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetails, "description": "Run not found"}
}


def build_router(store: RunStore) -> APIRouter:
    """Bind the `/v1/runs` router to `store`. A closure rather than FastAPI's
    app-state lookup keeps every handler's dependency explicit and typed --
    no `Any` escape hatch to read a store back out of request state."""

    router = APIRouter(prefix="/v1/runs", tags=["runs"])

    @router.post(
        "",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=Run,
        responses=_VALIDATION_ERROR_RESPONSE,
    )
    async def create_run(
        body: RunCreate,
        response: Response,
        idempotency_key: IdempotencyKey = None,
    ) -> Run:
        run = await store.create_run(body, idempotency_key)
        response.headers["Location"] = f"/v1/runs/{run.id}"
        return run

    @router.get(
        "/{run_id}",
        response_model=Run,
        responses={**_NOT_FOUND_RESPONSE, **_VALIDATION_ERROR_RESPONSE},
    )
    async def get_run(run_id: UUID) -> Run:
        run = await store.get_run(run_id)
        if run is None:
            raise not_found(f"run {run_id} not found", instance=f"/v1/runs/{run_id}")
        return run

    return router
