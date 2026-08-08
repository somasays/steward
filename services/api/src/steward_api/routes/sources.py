"""`POST /v1/sources`, `POST /v1/sources/{id}/scan` (SPEC.md §8, issue #20).

No business logic here (GUARDRAILS.md §4): handlers parse the request, delegate
to a `CatalogStore`, and shape the response. Both endpoints are idempotent and
neither decides what that means -- registration converges on a source's natural
key in the database, and a scan converges on the run already in flight, both
below the seam.

The status codes carry the difference a client needs. Registration answers 201
the first time and 200 on a repeat, so "did I just create this" is answerable
without a second request. Scanning always answers 202 -- the run is accepted,
not finished -- and the body says which run, whether this call started it or
found it.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Response, status
from steward_schemas import ProblemDetails, Run, Source, SourceCreate

from steward_api.catalog import (
    SOURCE_LOCATION_PREFIX,
    CatalogStore,
    IdempotencyKeyUnbindable,
    SourceNotFound,
)
from steward_api.problem_details import conflict, idempotency_key_unbindable, not_found
from steward_api.store import RUN_LOCATION_PREFIX, IdempotencyKeyReused

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]

SOURCES_PATH = "/v1/sources"

_EXISTING_SOURCE_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"model": Source, "description": "The source was already registered under this key"}
}
_VALIDATION_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {"model": ProblemDetails, "description": "Validation error"}
}
_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetails, "description": "Source not found"}
}
_CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ProblemDetails,
        "description": (
            "Idempotency key reused with a different request, or answered by a run "
            "already carrying a different key of its own"
        ),
    }
}
_INTERNAL_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    500: {"model": ProblemDetails, "description": "Unexpected server error"}
}


def build_router(store: CatalogStore) -> APIRouter:
    """Bind the `/v1/sources` router to `store` -- a closure, not app state, so
    every handler's dependency is explicit and typed."""

    router = APIRouter(prefix=SOURCES_PATH, tags=["sources"])

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=Source,
        responses={**_EXISTING_SOURCE_RESPONSE, **_VALIDATION_ERROR_RESPONSE, **_INTERNAL_ERROR_RESPONSE},
    )
    async def register_source(body: SourceCreate, response: Response) -> Source:
        source, created = await store.register_source(body)
        if not created:
            response.status_code = status.HTTP_200_OK
        response.headers["Location"] = f"{SOURCE_LOCATION_PREFIX}{source.id}"
        return source

    @router.post(
        "/{source_id}/scan",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=Run,
        responses={
            **_NOT_FOUND_RESPONSE,
            **_CONFLICT_RESPONSE,
            **_VALIDATION_ERROR_RESPONSE,
            **_INTERNAL_ERROR_RESPONSE,
        },
    )
    async def scan_source(
        source_id: UUID,
        response: Response,
        idempotency_key: IdempotencyKey = None,
    ) -> Run:
        try:
            run, _started = await store.start_scan(source_id, idempotency_key)
        except SourceNotFound as exc:
            raise not_found(str(exc), instance=f"{SOURCE_LOCATION_PREFIX}{source_id}") from exc
        except IdempotencyKeyReused as exc:
            raise conflict(str(exc), instance=f"{RUN_LOCATION_PREFIX}{exc.existing.id}") from exc
        except IdempotencyKeyUnbindable as exc:
            raise idempotency_key_unbindable(
                str(exc), instance=f"{RUN_LOCATION_PREFIX}{exc.existing.id}"
            ) from exc
        response.headers["Location"] = f"{RUN_LOCATION_PREFIX}{run.id}"
        return run

    return router
