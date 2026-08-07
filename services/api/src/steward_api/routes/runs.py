"""`POST /v1/runs`, `GET /v1/runs/{id}` (SPEC.md §8, issues #4 and #5).

No business logic here (GUARDRAILS.md smell checklist): handlers parse the
request, delegate to a `RunStore`, and shape the HTTP response. The idempotency
key (SPEC.md §8: "idempotency keys on all POSTs that create runs") is a
header the store, not the handler, resolves into "same run back" -- or into a
409, when the same key arrives with a different request.

`POST` answers 202 because the run is accepted, not finished: the store has
committed the run row and the tasks its goal planned, and a worker executes
them. `GET` is how a client learns it finished.

Acceptance is the goal registry's decision, not this module's: an unknown goal
or a payload its schema rejects comes back as `UnknownGoal` /
`InvalidGoalPayload` from below the seam, and the handler's only job is to say
that is a 422 (issue #19).
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Response, status
from steward_orchestration import InvalidGoalPayload, UnknownGoal
from steward_schemas import ProblemDetails, Run, RunCreate

from steward_api.problem_details import conflict, invalid_goal_payload, not_found, unknown_goal
from steward_api.store import RUN_LOCATION_PREFIX, IdempotencyKeyReused, RunStore

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]

RUNS_PATH = "/v1/runs"

# Every error response actually served is `ProblemDetails` (problem_details.py
# overrides FastAPI's default shapes) -- documented here too, so the
# published OpenAPI spec (contracts/openapi.json) matches runtime behavior.
_VALIDATION_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {"model": ProblemDetails, "description": "Validation error"}
}
_GOAL_REJECTED_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {
        "model": ProblemDetails,
        "description": "Malformed body, unknown goal, or a payload the goal's schema rejects",
    }
}
_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetails, "description": "Run not found"}
}
_CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {"model": ProblemDetails, "description": "Idempotency key reused with a different request"}
}


def build_router(store: RunStore) -> APIRouter:
    """Bind the `/v1/runs` router to `store`. A closure rather than FastAPI's
    app-state lookup keeps every handler's dependency explicit and typed --
    no `Any` escape hatch to read a store back out of request state."""

    router = APIRouter(prefix=RUNS_PATH, tags=["runs"])

    @router.post(
        "",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=Run,
        responses={**_CONFLICT_RESPONSE, **_GOAL_REJECTED_RESPONSE},
    )
    async def create_run(
        body: RunCreate,
        response: Response,
        idempotency_key: IdempotencyKey = None,
    ) -> Run:
        try:
            run = await store.create_run(body, idempotency_key)
        except UnknownGoal as exc:
            # The goal registry decided this is not a runnable request; the
            # handler only chooses the status code. Nothing was created --
            # `plan_run` runs before the store persists anything (issue #19).
            raise unknown_goal(str(exc), instance=RUNS_PATH) from exc
        except InvalidGoalPayload as exc:
            raise invalid_goal_payload(str(exc), exc.errors(), instance=RUNS_PATH) from exc
        except IdempotencyKeyReused as exc:
            # Translating the store's domain error into a status code is the
            # handler's whole job here -- the decision that the two requests
            # differ was made below the seam.
            raise conflict(str(exc), instance=f"{RUN_LOCATION_PREFIX}{exc.existing.id}") from exc
        response.headers["Location"] = f"{RUN_LOCATION_PREFIX}{run.id}"
        return run

    @router.get(
        "/{run_id}",
        response_model=Run,
        responses={**_NOT_FOUND_RESPONSE, **_VALIDATION_ERROR_RESPONSE},
    )
    async def get_run(run_id: UUID) -> Run:
        run = await store.get_run(run_id)
        if run is None:
            raise not_found(f"run {run_id} not found", instance=f"{RUN_LOCATION_PREFIX}{run_id}")
        return run

    return router
