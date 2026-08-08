"""RFC 9457 problem-details error handling (SPEC.md §8: "RFC 9457
problem-details errors"; issue #4).

Every error path -- application errors raised via `ProblemDetailsError`,
FastAPI's default request-validation (422) shape, and any other HTTP
exception -- is normalized to `steward_schemas.ProblemDetails`, served as
`application/problem+json`. Handlers raise `ProblemDetailsError`; they never
build a JSON error body by hand.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_core import ErrorDetails
from starlette.exceptions import HTTPException as StarletteHTTPException
from steward_schemas import ProblemDetails

PROBLEM_CONTENT_TYPE = "application/problem+json"

_logger = logging.getLogger(__name__)

INTERNAL_ERROR_TYPE = "urn:steward:internal-error"
"""Every unexpected server error's `type`, whatever raised it -- deliberately
one type for all of them, since the body carries no exception message and no
goal or planner detail (SPEC.md §8); the real detail goes to the log only."""


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


def bad_request(detail: str, *, instance: str | None = None) -> ProblemDetailsError:
    """A `400 Bad Request` problem, e.g. a pagination cursor this API never
    issued. 400 rather than 422: the cursor is not a field whose *value* is out
    of range, it is a token the client should only ever be echoing back."""
    return ProblemDetailsError(
        ProblemDetails(
            type="urn:steward:bad-request",
            title="Malformed request",
            status=status.HTTP_400_BAD_REQUEST,
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


def idempotency_key_unbindable(detail: str, *, instance: str | None = None) -> ProblemDetailsError:
    """A `409 Conflict` for an idempotency key single-flight answered with a
    run that already carries a different key of its own (issue #47).

    A distinct `type` from `conflict()`'s: the request named the same work as
    the run it got back -- this is not a key reused for a different request,
    it is two legitimate requests for the same work whose keys cannot both be
    remembered by a run that stores one.
    """
    return ProblemDetailsError(
        ProblemDetails(
            type="urn:steward:idempotency-key-unbindable",
            title="Idempotency key could not be bound to this run",
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


def sanitized_errors(errors: Sequence[ErrorDetails]) -> list[dict[str, object]]:
    """Per-field validation detail with the submitted values stripped out.

    Pydantic's `ErrorDetails` carries `input` (and sometimes `ctx`), which echo
    the offending value back. That turns any rejected field into a mirror -- and
    the first field this API rejects for *being* a credential is
    `SourceCreate.dsn_secret_ref`, where a client that posts a DSN instead of a
    secret reference would otherwise get the password reflected into the
    response body, its own logs, and any proxy that records bodies (N7).

    Dropping them costs a client nothing: it knows what it sent. `loc` says
    which field, `type` and `msg` say what was wrong with it.
    """
    return [{"type": error["type"], "loc": list(error["loc"]), "msg": error["msg"]} for error in errors]


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
                "errors": jsonable_encoder(sanitized_errors(errors)),
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
                "errors": jsonable_encoder(sanitized_errors(exc.errors())),
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

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Registered against the base `Exception`, this becomes Starlette's
        # `ServerErrorMiddleware` handler (see starlette.applications), so it
        # is the one path left uncovered by the handlers above: a programming
        # error that escapes a route -- e.g. `EmptyRunPlan` or
        # `DisallowedTaskType` reaching this deep is defense in depth,
        # registration (issue #39) rejects the common case before a goal is
        # even reachable, but any other bug below the route lands here too.
        # The client gets a generic, sanitized document; the exception itself
        # -- type, message, traceback -- goes to the log only.
        _logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        problem = ProblemDetails(
            type=INTERNAL_ERROR_TYPE,
            title="Internal server error",
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            instance=request.url.path,
        )
        return _problem_response(problem)
