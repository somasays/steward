"""FastAPI application factory (SPEC.md §8; issue #4)."""

from __future__ import annotations

from fastapi import FastAPI

from steward_api.problem_details import install_problem_details
from steward_api.routes.health import router as health_router
from steward_api.routes.runs import build_router as build_runs_router
from steward_api.store import InMemoryRunStore, RunStore


def create_app(run_store: RunStore | None = None) -> FastAPI:
    """Build the API app. `run_store` defaults to the M0 in-memory
    implementation; tests and issue #5's queue-backed store both pass their
    own `RunStore` here instead of reaching into app internals."""

    app = FastAPI(title="Steward API", version="0.1.0")
    install_problem_details(app)
    app.include_router(health_router)
    app.include_router(build_runs_router(run_store if run_store is not None else InMemoryRunStore()))
    return app


app = create_app()
