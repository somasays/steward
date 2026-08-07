"""Run the API service: `python -m steward_api` (dev) or the `steward-api`
console script (`[project.scripts]` in pyproject.toml).

This is the composition root: the only place in the service that reads the
environment. Everything below it is handed what it needs.
"""

from __future__ import annotations

import os

import uvicorn
from steward_queue import DSN_ENV
from steward_telemetry import tracer_from_env

from steward_api.app import create_app
from steward_api.catalog import PostgresCatalogStore
from steward_api.store import PostgresRunStore

DEFAULT_HOST = "0.0.0.0"  # noqa: S104 -- containers bind all interfaces; exposure is the NetworkPolicy's job
DEFAULT_PORT = 8000


def main() -> None:
    """Serve the app against the configured Postgres.

    The DSN is required, not defaulted: an API that silently came up on an
    in-memory store would accept runs, return 202, and execute nothing --
    exactly the "fail loud, skip honest" rule GUARDRAILS.md §3 applies to
    checks, applied to wiring.
    """
    dsn = os.environ.get(DSN_ENV, "").strip()
    if not dsn:
        raise SystemExit(f"{DSN_ENV} is not set")
    tracer = tracer_from_env()
    app = create_app(PostgresRunStore(dsn, tracer=tracer), PostgresCatalogStore(dsn, tracer=tracer))
    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
