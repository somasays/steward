"""`GET /v1/reviews/{id}`, `POST /v1/reviews/{id}:approve|:reject`
(SPEC.md §8's review queue, issue #50 step 7).

The human-in-the-loop gate, as HTTP. Nothing an agent produces is published by
the agent: a classification reaches an operator as `pending_review`, and this is
the only door from there to `approved` (SPEC.md §3.3).

No business logic here (GUARDRAILS.md §4). The handler parses the request,
delegates, and turns a domain error into a status code. Every decision the
endpoint appears to make -- whether the proposal may still be decided, whether
approving it would publish a classification of data that has changed, which
version it supersedes, whether a replayed key is the same request -- is made in
`steward_catalog.classification`, under an advisory lock, in one transaction
(SPEC.md §13 D14).

Why the outcome is in the path and not the body
-----------------------------------------------
`:approve` and `:reject` are two endpoints over one resource rather than a
`PATCH` carrying `{"outcome": ...}`, for the reason `ReviewCommand` carries no
outcome: they are opposite governance actions, one of which supersedes an
asset's published classification and one of which must leave it untouched. A
field would make "approve, outcome=rejected" representable at the seam, and the
version of this that existed recorded a rejection while publishing the proposal.

The colon form is SPEC.md §8's own convention for an action on a resource
(`POST /v1/runs/{id}:cancel`, `POST /v1/incidents/{id}:resolve`), kept here
rather than invented differently.

Idempotency is the same convention as every other POST: an `Idempotency-Key`
header, resolved below the seam. Replaying a key returns the decision it
settled; sending it with a *different* decision -- another proposal, the
opposite outcome, another reviewer, another reason -- is a 409, because
returning the settled record would tell a reviewer their rejection had been
recorded when an approval is what actually happened.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header
from steward_catalog.classification import (
    AssetNotClassifiable,
    ClassificationConflict,
    IdempotencyKeyReused,
    ProposalNotPending,
    StaleProposal,
)
from steward_schemas import (
    Classification,
    ClassificationDetail,
    ProblemDetails,
    ReviewRequest,
)

from steward_api.catalog import CatalogStore
from steward_api.problem_details import (
    ProblemDetailsError,
    asset_not_classifiable,
    classification_conflict,
    conflict,
    not_found,
    proposal_not_pending,
    stale_proposal,
)

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]

REVIEWS_PATH = "/v1/reviews"

Decide = Callable[[UUID, ReviewRequest, str | None], Awaitable[Classification]]

_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetails, "description": "No such classification proposal"}
}
_VALIDATION_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {"model": ProblemDetails, "description": "Validation error"}
}
_CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ProblemDetails,
        "description": (
            "The proposal was already decided, describes a profile the asset has moved "
            "past, belongs to an inactive asset, or the idempotency key already settled "
            "a different decision"
        ),
    }
}
_INTERNAL_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    500: {"model": ProblemDetails, "description": "Unexpected server error"}
}

_CONFLICT_PROBLEMS: dict[
    type[ClassificationConflict], Callable[..., ProblemDetailsError]
] = {
    ProposalNotPending: proposal_not_pending,
    StaleProposal: stale_proposal,
    AssetNotClassifiable: asset_not_classifiable,
    IdempotencyKeyReused: conflict,
}
"""Which 409 each refusal is, keyed by the exact exception type.

Exact type, with `classification_conflict` as the fallback, so a refusal this
module has not been taught about still reaches a client as a typed 409 rather
than a 500 -- and so adding a conflict to the repository is a decision about
what to *call* it, not a prerequisite for the endpoint continuing to work.
"""


def build_router(store: CatalogStore) -> APIRouter:
    """Bind the `/v1/reviews` router to `store` -- a closure, not app state, so
    every handler's dependency is explicit and typed."""

    router = APIRouter(prefix=REVIEWS_PATH, tags=["reviews"])

    async def decided(
        decide: Decide,
        proposal_id: UUID,
        body: ReviewRequest,
        idempotency_key: str | None,
    ) -> Classification:
        """Run one decision and translate its refusals. Shared by both verbs
        because the *translation* is identical; the decision is not, which is
        why `decide` is the store method itself rather than an outcome flag."""
        instance = f"{REVIEWS_PATH}/{proposal_id}"
        try:
            return await decide(proposal_id, body, idempotency_key)
        except LookupError as exc:
            raise not_found(f"no classification proposal {proposal_id}", instance=instance) from exc
        except ClassificationConflict as exc:
            problem = _CONFLICT_PROBLEMS.get(type(exc), classification_conflict)
            raise problem(str(exc), instance=instance) from exc

    @router.get(
        "/{proposal_id}",
        response_model=ClassificationDetail,
        responses={**_NOT_FOUND_RESPONSE, **_VALIDATION_ERROR_RESPONSE, **_INTERNAL_ERROR_RESPONSE},
    )
    async def get_review(proposal_id: UUID) -> ClassificationDetail:
        """One proposal with its labels, evidence, provenance and every decision
        recorded against it -- what a reviewer needs in order to decide."""
        detail = await store.get_classification(proposal_id)
        if detail is None:
            raise not_found(
                f"no classification proposal {proposal_id}",
                instance=f"{REVIEWS_PATH}/{proposal_id}",
            )
        return detail

    @router.post(
        "/{proposal_id}:approve",
        response_model=Classification,
        responses={
            **_NOT_FOUND_RESPONSE,
            **_CONFLICT_RESPONSE,
            **_VALIDATION_ERROR_RESPONSE,
            **_INTERNAL_ERROR_RESPONSE,
        },
    )
    async def approve_review(
        proposal_id: UUID,
        body: ReviewRequest,
        idempotency_key: IdempotencyKey = None,
    ) -> Classification:
        """Publish this classification, superseding whatever it replaces."""
        return await decided(store.approve_classification, proposal_id, body, idempotency_key)

    @router.post(
        "/{proposal_id}:reject",
        response_model=Classification,
        responses={
            **_NOT_FOUND_RESPONSE,
            **_CONFLICT_RESPONSE,
            **_VALIDATION_ERROR_RESPONSE,
            **_INTERNAL_ERROR_RESPONSE,
        },
    )
    async def reject_review(
        proposal_id: UUID,
        body: ReviewRequest,
        idempotency_key: IdempotencyKey = None,
    ) -> Classification:
        """Refuse this classification. What is published stays published.

        A reason is required by the contract (`ReviewRequest.reason`, min length
        1) and that is not a formality: SPEC.md §8 exports every rejection with
        its reason to the eval dataset, so a rejection with nothing to say is a
        training signal thrown away.
        """
        return await decided(store.reject_classification, proposal_id, body, idempotency_key)

    return router
