"""Steward demo: the M0 platform running end to end on a real Postgres.

Starts an ephemeral Postgres (pgserver -- no Docker) and a real HTTP server,
then drives the system the way a client does: `POST /v1/runs`, wait, `GET
/v1/runs/{id}`. Nothing here reaches past the API to make the run progress; a
worker does that, the way one would in a cluster. Everything printed is either
an HTTP response or a row read back out of the database.

    make demo
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import timedelta
from typing import Any
from uuid import UUID

import pgserver
import uvicorn
from steward_api.app import create_app
from steward_api.store import PostgresRunStore
from steward_queue import QueueConnection, Worker, connect, upgrade_to_head
from steward_telemetry import Tracer, tracer_from_env

RULE = "─" * 78
HOST = "127.0.0.1"
PORT = 8123
BASE_URL = f"http://{HOST}:{PORT}"
STARTUP_TIMEOUT = timedelta(seconds=30)
RUN_TIMEOUT = timedelta(seconds=30)
POLL_INTERVAL = timedelta(milliseconds=100)
TERMINAL = {"succeeded", "failed", "cancelled"}

SELECT_RUN_TASKS = """
SELECT id, task_type, state, attempts FROM tasks WHERE run_id = %(run_id)s ORDER BY created_at
"""
SELECT_CHECKPOINTS = """
SELECT step, state FROM checkpoints WHERE task_id = %(task_id)s ORDER BY step
"""
SELECT_AUDIT_TRAIL = """
SELECT actor_kind, actor_id, action, entity_type, after
FROM audit_log
WHERE entity_id = %(run)s OR entity_id IN (SELECT id::text FROM tasks WHERE run_id = %(run_id)s)
ORDER BY id
"""


def head(title: str) -> None:
    print(f"\n{RULE}\n  {title}\n{RULE}")


def rows(conn: QueueConnection, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    result = list(conn.execute(sql, params).fetchall())
    conn.rollback()  # end the read transaction so the next read sees fresh state
    return result


def request(method: str, path: str, body: dict[str, Any] | None = None, **headers: str) -> Any:
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE_URL}{path}", data=payload, method=method)
    req.add_header("Content-Type", "application/json")
    for name, value in headers.items():
        req.add_header(name.replace("_", "-"), value)
    with urllib.request.urlopen(req) as response:  # noqa: S310 -- fixed localhost URL
        return json.loads(response.read())


def serve(dsn: str, tracer: Tracer) -> uvicorn.Server:
    """Start the real API service on a real socket, in a background thread."""
    store = PostgresRunStore(dsn, tracer=tracer)
    config = uvicorn.Config(create_app(store), host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + STARTUP_TIMEOUT.total_seconds()
    while time.monotonic() < deadline:
        try:
            request("GET", "/healthz")
            return server
        except (urllib.error.URLError, ConnectionError):
            time.sleep(POLL_INTERVAL.total_seconds())
    raise RuntimeError("API did not come up")


def demo_create(conn: QueueConnection) -> dict[str, Any]:
    head("1. POST /v1/runs — the run and its first task commit together (I8)")
    created = request(
        "POST",
        "/v1/runs",
        {"goal": "noop", "payload": {"echo": "hello from the demo"}},
        Idempotency_Key="demo-1",
    )
    run_id = created["id"]
    print(f"  202 Accepted   run={run_id}  status={created['status']}")
    print(f"  trace_id       {created['trace_id']}  (on the row whichever tracer is wired — I7)")
    print(f"  budget         {created['budget']['steps']} steps, {created['budget']['cost_usd']} USD (I12)")

    for task_id, task_type, state, _ in rows(conn, SELECT_RUN_TASKS, {"run_id": UUID(run_id)}):
        print(f"  task in db     {task_id}  type={task_type}  state={state}")
    print("  one transaction wrote both: a 202 means there is work queued, never a run with none")

    replay = request(
        "POST",
        "/v1/runs",
        {"goal": "noop", "payload": {"echo": "hello from the demo"}},
        Idempotency_Key="demo-1",
    )
    tasks = rows(conn, SELECT_RUN_TASKS, {"run_id": UUID(run_id)})
    print(
        f"\n  replaying the POST with the same Idempotency-Key returns the same run: {replay['id'] == run_id}"
    )
    print(f"  tasks for this run after the replay: {len(tasks)}")
    return created


def demo_worker(dsn: str, conn: QueueConnection, created: dict[str, Any], tracer: Tracer) -> dict[str, Any]:
    head("2. A worker claims and executes it (SKIP LOCKED), and the run settles")
    worker = Worker(dsn, worker_id="demo-worker-1", tracer=tracer)
    deadline = time.monotonic() + RUN_TIMEOUT.total_seconds()
    finished = created
    while time.monotonic() < deadline:
        asyncio.run(worker.run_once())
        finished = request("GET", f"/v1/runs/{created['id']}")
        if finished["status"] in TERMINAL:
            break
        time.sleep(POLL_INTERVAL.total_seconds())

    print(f"  GET /v1/runs/{created['id']}")
    print(f"  status         {finished['status']}   (rolled up from its tasks, in their transaction)")
    print(f"  trace_id       {finished['trace_id']}   (same trace the POST returned)")
    print(f"  usage          {finished['usage']['steps']} of {finished['budget']['steps']} steps")

    for task_id, task_type, state, attempts in rows(conn, SELECT_RUN_TASKS, {"run_id": UUID(created["id"])}):
        print(f"  task           {task_id}  type={task_type}  state={state}  attempts={attempts}")
        for step, state_json in rows(conn, SELECT_CHECKPOINTS, {"task_id": task_id}):
            print(f"  checkpoint     step={step}  state={state_json}")
    return finished


def demo_audit(conn: QueueConnection, run_id: str) -> None:
    head("3. Every state change wrote its audit row, in the same transaction (I7)")
    for actor_kind, actor_id, action, entity_type, after in rows(
        conn, SELECT_AUDIT_TRAIL, {"run": run_id, "run_id": UUID(run_id)}
    ):
        landed = after.get("status") or after.get("state") if isinstance(after, dict) else None
        detail = f"-> {landed}" if landed else ""
        print(f"  {action:<20} {entity_type:<5} by {actor_kind}:{actor_id:<14} {detail}".rstrip())


def demo_second_worker(dsn: str) -> None:
    head("4. A second worker finds nothing to steal (no double-claim)")
    idle = Worker(dsn, worker_id="demo-worker-2")
    print(f"  worker-2 claimed {asyncio.run(idle.run_once())} task(s) — the queue is drained")


def main() -> int:
    print("Steward demo — M0 platform on an ephemeral Postgres (no Docker, no API keys)")
    with tempfile.TemporaryDirectory(prefix="steward-demo") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            dsn = server.get_uri()
            upgrade_to_head(dsn)
            print(f"  postgres up, schema migrated: {dsn.split('@')[-1]}")
            # One tracer for both the API and the worker, chosen from the
            # environment: LangfuseTracer with credentials, NoopTracer without.
            # Nothing else in the demo branches on which one it got.
            tracer = tracer_from_env()
            api = serve(dsn, tracer)
            print(f"  api up: {BASE_URL}")
            print(f"  tracing: {type(tracer).__name__} (LANGFUSE_PUBLIC_KEY + _SECRET_KEY to export)")
            try:
                with closing(connect(dsn)) as conn:
                    created = demo_create(conn)
                    finished = demo_worker(dsn, conn, created, tracer)
                    demo_audit(conn, finished["id"])
                    demo_second_worker(dsn)
            finally:
                api.should_exit = True
        finally:
            server.cleanup()
    head("Done — see DEMO.md for what to look at next")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
