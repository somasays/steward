"""What a fan-out run may spend, end to end (issue #48, I12, N6).

The property the profiling fan-out (#49) was blocked on, asserted over the
real components: the goal registry plans it, the API admits it under the run
budget it advertises, the queue enqueues one row per branch, and a real worker
executes them and rolls their usage up onto the run.

Before this, `RunPlan.task_specs` handed every planned task the *run's* budget,
so the three tasks below could each have spent what the API published for the
whole run -- three times the advertised cap, from one accepted request. The
assertions are therefore about arithmetic on real rows, not about a planner's
return value: what the tasks were enqueued with, and what the run ended up
having spent.

    uv run pytest -q services/api/tests/test_run_budgets.py
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pgserver
import pytest
from fastapi.testclient import TestClient
from steward_api.app import create_app
from steward_api.store import PostgresRunStore
from steward_orchestration import GoalParams, PlannedTask
from steward_orchestration.registry import goal, unregister
from steward_queue import RunStatus, Worker, connect, get_run, upgrade_to_head
from steward_queue.db import QueueConnection
from steward_schemas import RunBudget

FAN_OUT_GOAL = "fans_out_across_three_tasks"

BRANCHES = 3

TASK_BUDGET = RunBudget(steps=1, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(minutes=1))
"""What one branch of the fan-out declares it may spend."""

RUN_BUDGET = RunBudget(
    steps=BRANCHES * TASK_BUDGET.steps,
    tokens=BRANCHES * TASK_BUDGET.tokens,
    cost_usd=BRANCHES * TASK_BUDGET.cost_usd,
    wall_clock=BRANCHES * TASK_BUDGET.wall_clock,
)
"""What the run is admitted for: the three branches together, exactly.

Deliberately equal to the reservation rather than comfortably above it, so the
test would fail if a fourth branch appeared -- and so "the run's cap is spent
once between the branches, not once per branch" is the literal arithmetic.
"""

SELECT_TASK_BUDGETS = """
SELECT budget_steps, budget_wall_clock FROM tasks WHERE run_id = %(run_id)s ORDER BY payload->>'echo'
"""


class FanOutParams(GoalParams):
    echo: str = "fan-out"


@pytest.fixture(scope="session")
def dsn() -> Iterator[str]:
    with tempfile.TemporaryDirectory(prefix="steward-budgets") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            uri: str = server.get_uri()
            upgrade_to_head(uri)
            yield uri
        finally:
            server.cleanup()


@pytest.fixture
def conn(dsn: str) -> Iterator[QueueConnection]:
    connection = connect(dsn)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def fan_out_goal() -> Iterator[str]:
    """A goal that plans three `noop` tasks, each funded out of the run budget.

    Payloads differ per branch on purpose: enqueue dedupes on
    (task_type, payload) within a run, so three identical branches would
    collapse to one row and the test would silently be about a single task.
    """

    @goal(
        FAN_OUT_GOAL,
        params_model=FanOutParams,
        allowed_task_types=["noop"],
        budget=RUN_BUDGET,
        sample_payload={"echo": "sample"},
    )
    def plan(params: FanOutParams) -> tuple[PlannedTask, ...]:
        return tuple(
            PlannedTask(
                task_type="noop",
                budget=TASK_BUDGET,
                payload={"echo": f"{params.echo}-{branch}"},
            )
            for branch in range(BRANCHES)
        )

    try:
        yield FAN_OUT_GOAL
    finally:
        unregister(FAN_OUT_GOAL)


@pytest.fixture
def client(dsn: str) -> Iterator[TestClient]:
    with TestClient(create_app(PostgresRunStore(dsn))) as test_client:
        yield test_client


async def _drain(worker: Worker) -> None:
    while await worker.run_once():
        pass


def test_n_tasks_of_one_run_cannot_each_spend_the_full_cap(
    client: TestClient, conn: QueueConnection, dsn: str, fan_out_goal: str
) -> None:
    accepted = client.post("/v1/runs", json={"goal": fan_out_goal, "payload": {"echo": "e"}})
    assert accepted.status_code == 202
    run_id = UUID(accepted.json()["id"])

    budgets = conn.execute(SELECT_TASK_BUDGETS, {"run_id": run_id}).fetchall()
    conn.rollback()
    assert len(budgets) == BRANCHES
    # The defect, gone: every task used to carry the run's own budget.
    assert [row[0] for row in budgets] == [TASK_BUDGET.steps] * BRANCHES
    assert all(row[0] < RUN_BUDGET.steps for row in budgets)
    assert all(row[1] < RUN_BUDGET.wall_clock for row in budgets)
    # And what they may spend between them is one run budget, not three.
    assert sum(row[0] for row in budgets) == RUN_BUDGET.steps


def test_a_finished_fan_out_run_reports_the_sum_of_its_tasks_and_no_more(
    client: TestClient, conn: QueueConnection, dsn: str, fan_out_goal: str
) -> None:
    accepted = client.post("/v1/runs", json={"goal": fan_out_goal, "payload": {"echo": "f"}})
    run_id = UUID(accepted.json()["id"])

    asyncio.run(_drain(Worker(dsn, "budget-worker", task_types=["noop"])))

    run = get_run(conn, run_id)
    conn.rollback()
    assert run is not None
    assert run.status is RunStatus.SUCCEEDED
    # Every `noop` reports one step, so the run's total is the fan-out's width
    # -- the sum of what its tasks consumed, which is the whole point of
    # aggregating them on the run row.
    assert run.usage.steps == BRANCHES
    assert run.usage.over(run.budget) == ()
    assert run.usage.steps == run.budget.steps  # spent to the cap, never past it
