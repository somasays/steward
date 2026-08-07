"""FastAPI application factory (SPEC.md §8; issues #4 and #5)."""

from __future__ import annotations

from fastapi import FastAPI

from steward_api.problem_details import install_problem_details
from steward_api.routes.health import router as health_router
from steward_api.routes.runs import build_router as build_runs_router
from steward_api.store import InMemoryRunStore, RunStore


def create_app(run_store: RunStore | None = None) -> FastAPI:
    """Build the API app around a `RunStore`.

    The default is the in-memory store, which is what the OpenAPI export and
    the HTTP-layer tests need: both must build the app without a database.
    Every deployment passes a `PostgresRunStore` instead -- see
    `steward_api.__main__`, which is the composition root and the only place
    that reads the environment.
    """

    app = FastAPI(title="Steward API", version="0.1.0")
    install_problem_details(app)
    app.include_router(health_router)
    app.include_router(build_runs_router(run_store if run_store is not None else InMemoryRunStore()))
    return app


app = create_app()
