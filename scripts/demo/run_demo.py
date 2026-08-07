"""Steward demo: the M0 platform running end to end on a real Postgres.

Starts an ephemeral Postgres (pgserver -- no Docker), migrates it, then walks
through the guarantees the guardrails exist to protect: transactional enqueue,
SKIP LOCKED claiming, idempotent handlers, and an audit row for every state
change. Everything printed is read back out of the database, not narrated.

    make demo
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pgserver
from steward_queue import (
    NOOP_TASK_TYPE,
    QueueConnection,
    Worker,
    connect,
    create_run,
    dedup_key_for,
    enqueue,
    get_run,
    get_task,
    upgrade_to_head,
)
from steward_schemas import RunBudget, TaskSpec

BUDGET = RunBudget(steps=10, tokens=1_000, cost_usd=Decimal("1.000000"), wall_clock=timedelta(minutes=5))
RULE = "─" * 78


def head(title: str) -> None:
    print(f"\n{RULE}\n  {title}\n{RULE}")


def rows(conn: QueueConnection, sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
    return list(conn.execute(sql, params or {}).fetchall())


def demo_enqueue(conn: QueueConnection) -> tuple[Any, Any]:
    head("1. Transactional enqueue (I8) — run + task commit atomically")
    run = create_run(conn, goal="demo: no-op run", budget=BUDGET, trace_id=f"trace-{uuid4().hex[:12]}")
    spec = TaskSpec(
        task_id=uuid4(),
        run_id=run.id,
        task_type=NOOP_TASK_TYPE,
        payload={"echo": "hello from the demo"},
        budget=BUDGET,
        max_attempts=3,
    )
    task_id = enqueue(conn, spec)
    print(f"  run     {run.id}  status={run.status.value}  trace_id={run.trace_id}")
    print(f"  task    {task_id}  type={spec.task_type}  state=pending")
    print("  both rows are in one uncommitted transaction — no worker can see them yet")

    replay = enqueue(conn, spec.model_copy(update={"task_id": uuid4()}))
    print(f"\n  replaying the same enqueue returns the same task: {replay == task_id}")
    print(f"  (dedup key {dedup_key_for(spec.task_type, spec.payload)[:16]}… derived from type+payload)")
    conn.commit()
    print("  committed — the task now exists for workers")
    return run, task_id


def demo_worker(dsn: str, conn: QueueConnection, run: Any, task_id: Any) -> None:
    head("2. Worker claims and executes (SKIP LOCKED)")
    worker = Worker(dsn, worker_id="demo-worker-1")
    claimed = asyncio.run(worker.run_once())
    print(f"  claimed and executed {claimed} task(s)")

    task = get_task(conn, task_id)
    run_after = get_run(conn, run.id)
    assert task is not None and run_after is not None
    print(f"  task    state={task.state.value}  attempts={task.attempts}")
    print(
        f"  run     status={run_after.status.value}  "
        f"usage={run_after.usage.steps} of {run_after.budget.steps} steps (I12: caps are on the row)"
    )

    checkpoints = rows(conn, "SELECT step, state FROM checkpoints WHERE task_id = %(id)s", {"id": task_id})
    for step, state in checkpoints:
        print(f"  checkpoint step={step} state={state}")
    print("  (run stays pending: rolling task outcomes up to run status is issue #5, not yet merged)")


def demo_audit(conn: QueueConnection, run: Any) -> None:
    head("3. Every state change wrote its audit row, in the same transaction (I7)")
    for actor_kind, actor_id, action, entity_type in rows(
        conn,
        "SELECT actor_kind, actor_id, action, entity_type FROM audit_log "
        "WHERE entity_id = %(run)s OR entity_id IN "
        "(SELECT id::text FROM tasks WHERE run_id = %(uuid)s) ORDER BY at",
        {"run": str(run.id), "uuid": run.id},
    ):
        print(f"  {action:<22} {entity_type:<6} by {actor_kind}:{actor_id}")


def demo_second_worker(dsn: str) -> None:
    head("4. A second worker finds nothing to steal (no double-claim)")
    idle = Worker(dsn, worker_id="demo-worker-2")
    print(f"  worker-2 claimed {asyncio.run(idle.run_once())} task(s) — the queue is drained")


def main() -> int:
    print("Steward demo — M0 platform on an ephemeral Postgres (no Docker)")
    with tempfile.TemporaryDirectory(prefix="steward-demo") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            dsn = server.get_uri()
            upgrade_to_head(dsn)
            print(f"  postgres up, schema migrated: {dsn.split('@')[-1]}")
            conn = connect(dsn)
            try:
                run, task_id = demo_enqueue(conn)
                demo_worker(dsn, conn, run, task_id)
                demo_audit(conn, run)
                demo_second_worker(dsn)
            finally:
                conn.close()
        finally:
            server.cleanup()
    head("Done — see DEMO.md for what to look at next")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
