"""FastAPI application factory (SPEC.md §8; issues #4, #5 and #20)."""

from __future__ import annotations

from fastapi import FastAPI

from steward_api.catalog import CatalogStore, InMemoryCatalogStore
from steward_api.problem_details import install_problem_details
from steward_api.routes.assets import build_router as build_assets_router
from steward_api.routes.health import router as health_router
from steward_api.routes.runs import build_router as build_runs_router
from steward_api.routes.sources import build_router as build_sources_router
from steward_api.store import InMemoryRunStore, RunStore


def create_app(
    run_store: RunStore | None = None, catalog_store: CatalogStore | None = None
) -> FastAPI:
    """Build the API app around a `RunStore` and a `CatalogStore`.

    The defaults are the in-memory stores, which is what the OpenAPI export and
    the HTTP-layer tests need: both must build the app without a database.
    Every deployment passes the Postgres-backed pair instead -- see
    `steward_api.__main__`, which is the composition root and the only place
    that reads the environment.

    There is deliberately no module-level `app` to point an ASGI server at.
    One would default to the in-memory stores, and a deployment started the
    idiomatic way (`uvicorn steward_api.app:app`) would then accept runs,
    return 202 with a trace id and a budget, and execute none of them --
    forever, silently. `__main__` refuses to start without a DSN precisely to
    prevent that, and an importable app would have been the way around it.
    """

    app = FastAPI(title="Steward API", version="0.1.0")
    install_problem_details(app)
    catalog = catalog_store if catalog_store is not None else InMemoryCatalogStore()
    app.include_router(health_router)
    app.include_router(build_runs_router(run_store if run_store is not None else InMemoryRunStore()))
    app.include_router(build_sources_router(catalog))
    app.include_router(build_assets_router(catalog))
    return app
