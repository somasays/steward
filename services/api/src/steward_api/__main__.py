"""Run the API service: `python -m steward_api` (dev) or the `steward-api`
console script (`[project.scripts]` in pyproject.toml).

This is the composition root: the only place in the service that reads the
environment. Everything below it is handed what it needs.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from steward_llm import gateway_config_from_env
from steward_queue import DSN_ENV
from steward_telemetry import tracer_from_env

from steward_api.app import create_app
from steward_api.auth import API_KEYS_ENV, ApiKeyRegistry
from steward_api.catalog import PostgresCatalogStore
from steward_api.store import PostgresRunStore

_logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"  # noqa: S104 -- containers bind all interfaces; exposure is the NetworkPolicy's job
DEFAULT_PORT = 8000


def main() -> None:
    """Serve the app against the configured Postgres.

    The DSN is required, not defaulted: an API that silently came up on an
    in-memory store would accept runs, return 202, and execute nothing --
    exactly the "fail loud, skip honest" rule GUARDRAILS.md §3 applies to
    checks, applied to wiring.

    The gateway config is validated first for a reason that is not about this
    process calling models -- it does not. I15's refusal has to be true of every
    process a deployment starts, or it is true of whichever ones someone
    remembered: a deployment whose config routes off the allowlist must not come
    up serving an API that admits runs against it. The check is the same code the
    worker runs, and H12 boots every entry point to prove none of them skipped it.
    """
    gateway_config_from_env()
    dsn = os.environ.get(DSN_ENV, "").strip()
    if not dsn:
        raise SystemExit(f"{DSN_ENV} is not set")
    api_keys = _api_keys()
    tracer = tracer_from_env()
    app = create_app(
        PostgresRunStore(dsn, tracer=tracer),
        PostgresCatalogStore(dsn, tracer=tracer),
        api_keys=api_keys,
    )
    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT)


def _api_keys() -> ApiKeyRegistry:
    """The credentials this deployment accepts, said out loud either way.

    Unlike the DSN, an absent value is not fatal. The DSN is required because an
    API without one accepts runs and executes none of them -- it lies about its
    whole job. An API with no credentials refuses every review decision with a
    401, which is fail-closed and *honest*: nothing is recorded on anyone's
    behalf. Refusing to boot would take the reads and the scan endpoints down
    with it for a deployment that may not review anything.

    What it must not do is be silent. A governance gate that can never succeed,
    discovered later by a reviewer getting a 401, is the shape this project keeps
    finding: a thing that looks configured and measures nothing. So the startup
    log names the state, and a malformed value is fatal -- an operator who tried
    to configure credentials and got the syntax wrong must not be left with a
    deployment that authenticates nobody and says nothing.
    """
    registry = ApiKeyRegistry.from_env(os.environ.get(API_KEYS_ENV))
    if not registry.configured:
        _logger.warning(
            "%s is not set: review decisions (POST /v1/reviews/{id}:approve|:reject) "
            "will refuse every request with 401 until it is",
            API_KEYS_ENV,
        )
    return registry


if __name__ == "__main__":
    main()
