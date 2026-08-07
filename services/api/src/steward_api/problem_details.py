"""RFC 9457 problem-details error handling (SPEC.md §8: "RFC 9457
problem-details errors"; issue #4).

Every error path -- application errors raised via `ProblemDetailsError`,
FastAPI's default request-validation (422) shape, and any other HTTP
exception -- is normalized to `steward_schemas.ProblemDetails`, served as
`application/problem+json`. Handlers raise `ProblemDetailsError`; they never
build a JSON error body by hand.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_core import ErrorDetails
from starlette.exceptions import HTTPException as StarletteHTTPException
from steward_schemas import ProblemDetails

PROBLEM_CONTENT_TYPE = "application/problem+json"


class ProblemDetailsError(Exception):
    """Raise from a route handler to produce a problem-details response."""

    def __init__(self, problem: ProblemDetails) -> None:
        super().__init__(problem.title)
        self.problem = problem


def not_found(detail: str, *, instance: str | None = None) -> ProblemDetailsError:
    """A `404 Not Found` problem, e.g. for a missing `GET /v1/runs/{id}`."""
    return ProblemDetailsError(
        ProblemDetails(
            type="urn:steward:not-found",
            title="Resource not found",
            status=status.HTTP_404_NOT_FOUND,
            detail=detail,
            instance=instance,
        )
    )


def conflict(detail: str, *, instance: str | None = None) -> ProblemDetailsError:
    """A `409 Conflict` problem, e.g. an idempotency key reused for a
    different request body."""
    return ProblemDetailsError(
        ProblemDetails(
            type="urn:steward:idempotency-key-reused",
            title="Idempotency key reused with a different request",
            status=status.HTTP_409_CONFLICT,
            detail=detail,
            instance=instance,
        )
    )


def unknown_goal(detail: str, *, instance: str | None = None) -> ProblemDetailsError:
    """A `422` problem for a `goal` no planner is registered for (issue #19).

    422 rather than 404: the request reached the right resource, its body just
    does not describe anything the system can run -- the same class of failure
    as a field of the wrong type, which FastAPI already answers 422.
    """
    return ProblemDetailsError(
        ProblemDetails(
            type="urn:steward:unknown-goal",
            title="Unknown goal",
            status=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=detail,
            instance=instance,
        )
    )


def invalid_goal_payload(
    detail: str, errors: Sequence[ErrorDetails], *, instance: str | None = None
) -> ProblemDetailsError:
    """A `422` problem for a payload that does not match its goal's schema.

    `errors` is the same RFC 9457 extension member the generic request
    validation handler below uses, carrying pydantic's per-field detail: a
    client gets told which parameter is wrong, not just that one is.
    """
    return ProblemDetailsError(
        ProblemDetails.model_validate(
            {
                "type": "urn:steward:invalid-goal-payload",
                "title": "Goal payload failed validation",
                "status": status.HTTP_422_UNPROCESSABLE_CONTENT,
                "detail": detail,
                "instance": instance,
                "errors": jsonable_encoder(errors),
            }
        )
    )


def _problem_response(problem: ProblemDetails) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=jsonable_encoder(problem.model_dump(mode="json")),
        media_type=PROBLEM_CONTENT_TYPE,
    )


def install_problem_details(app: FastAPI) -> None:
    """Register exception handlers so every error path -- including
    FastAPI's default 422 validation-error shape -- returns RFC 9457
    problem-details instead of FastAPI's stock JSON error bodies."""

    @app.exception_handler(ProblemDetailsError)
    async def _handle_problem_details_error(_: Request, exc: ProblemDetailsError) -> JSONResponse:
        return _problem_response(exc.problem)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # `errors` is an RFC 9457 extension member (ProblemDetails: extra="allow").
        # Pydantic's synthesized __init__ signature (PEP 681) only recognizes
        # declared fields, so extension members go through `model_validate`
        # with a plain dict, same as ProblemDetails' own extension-member test.
        problem = ProblemDetails.model_validate(
            {
                "type": "urn:steward:validation-error",
                "title": "Request validation failed",
                "status": status.HTTP_422_UNPROCESSABLE_CONTENT,
                "detail": "request body/parameters failed schema validation",
                "instance": request.url.path,
                "errors": jsonable_encoder(exc.errors()),
            }
        )
        return _problem_response(problem)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        problem = ProblemDetails(
            title=str(exc.detail) if exc.detail else "HTTP error",
            status=exc.status_code,
            instance=request.url.path,
        )
        return _problem_response(problem)
