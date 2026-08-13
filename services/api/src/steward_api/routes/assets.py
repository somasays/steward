"""`GET /v1/assets`, `GET /v1/assets/{id}` (SPEC.md §8, issue #20).

Cursor pagination, not offsets: a scan committing between two pages would shift
offsets underneath a client and silently skip assets. The cursor is opaque and
the store issues it; a cursor this API did not produce is a 400, never a
different page.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from steward_catalog import InvalidCursor
from steward_schemas import (
    AssetDetail,
    AssetPage,
    Classification,
    ClassificationHistory,
    ProblemDetails,
)

from steward_api.catalog import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, AssetNotFound, CatalogStore
from steward_api.problem_details import bad_request, not_found

ASSETS_PATH = "/v1/assets"

_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetails, "description": "Asset not found"}
}
_BAD_REQUEST_RESPONSE: dict[int | str, dict[str, Any]] = {
    400: {"model": ProblemDetails, "description": "Malformed pagination cursor"}
}
_VALIDATION_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {"model": ProblemDetails, "description": "Validation error"}
}
_INTERNAL_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    500: {"model": ProblemDetails, "description": "Unexpected server error"}
}

PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


def build_router(store: CatalogStore) -> APIRouter:
    """Bind the `/v1/assets` router to `store`."""

    router = APIRouter(prefix=ASSETS_PATH, tags=["assets"])

    @router.get(
        "",
        response_model=AssetPage,
        responses={**_BAD_REQUEST_RESPONSE, **_VALIDATION_ERROR_RESPONSE, **_INTERNAL_ERROR_RESPONSE},
    )
    async def list_assets(
        source: UUID | None = None,
        cursor: str | None = None,
        limit: PageLimit = DEFAULT_PAGE_SIZE,
    ) -> AssetPage:
        try:
            return await store.list_assets(source_id=source, cursor=cursor, limit=limit)
        except InvalidCursor as exc:
            raise bad_request(str(exc), instance=ASSETS_PATH) from exc

    @router.get(
        "/{asset_id}",
        response_model=AssetDetail,
        responses={**_NOT_FOUND_RESPONSE, **_VALIDATION_ERROR_RESPONSE, **_INTERNAL_ERROR_RESPONSE},
    )
    async def get_asset(asset_id: UUID) -> AssetDetail:
        detail = await store.get_asset(asset_id)
        if detail is None:
            raise not_found(f"asset {asset_id} not found", instance=f"{ASSETS_PATH}/{asset_id}")
        return detail

    @router.get(
        "/{asset_id}/classification",
        response_model=Classification,
        responses={**_NOT_FOUND_RESPONSE, **_VALIDATION_ERROR_RESPONSE, **_INTERNAL_ERROR_RESPONSE},
    )
    async def get_current_classification(asset_id: UUID) -> Classification:
        """The asset's *published* classification -- singular, and approved.

        Only ever an approved version: a pending or rejected proposal is
        readable through `/v1/reviews/{id}` and through the history below, and
        neither is this asset's answer. That is the human-in-the-loop gate
        expressed as a resource -- there is no request that returns an
        unreviewed classification as the current one (SPEC.md §3.3).

        The two 404s are deliberately different sentences: an unknown asset is
        not the same fact as an asset nobody has classified yet, and a client
        that cannot tell them apart retries a typo forever.
        """
        instance = f"{ASSETS_PATH}/{asset_id}/classification"
        try:
            current = await store.current_classification(asset_id)
        except AssetNotFound as exc:
            raise not_found(str(exc), instance=instance) from exc
        if current is None:
            raise not_found(
                f"asset {asset_id} has no approved classification", instance=instance
            )
        return current

    @router.get(
        "/{asset_id}/classifications",
        response_model=ClassificationHistory,
        responses={**_NOT_FOUND_RESPONSE, **_VALIDATION_ERROR_RESPONSE, **_INTERNAL_ERROR_RESPONSE},
    )
    async def get_classification_history(asset_id: UUID) -> ClassificationHistory:
        """Every version this asset has ever had, newest first.

        Including the rejected and the superseded, because that is what an
        append-only table is for: "why is this column labelled PII" is answered
        by the version that was approved, and "why is it not labelled PHI" often
        only by the one that was rejected.

        An asset with no proposals answers 200 with an empty list -- it exists
        and has no classifications, which is a fact. An asset that does not
        exist answers 404. An empty list would otherwise be the same response
        for both, and a client could not tell a scanned-but-unclassified asset
        from an id it made up.
        """
        try:
            return await store.classification_history(asset_id)
        except AssetNotFound as exc:
            raise not_found(
                str(exc), instance=f"{ASSETS_PATH}/{asset_id}/classifications"
            ) from exc

    return router
