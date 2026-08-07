"""The registry's promises: validation, expansion, and least privilege.

Every test here builds its own `GoalRegistration` rather than registering one
globally. Registration is an import-time side effect shared by the whole
process (that is what makes goals reachable without a setup call), so a test
that registered its fixtures would leak them into every other test's view of
the system.
"""

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from steward_orchestration import (
    DisallowedTaskType,
    GoalParams,
    GoalRegistration,
    InvalidGoalPayload,
    PlannedTask,
    Planner,
    UnknownGoal,
    get_goal,
    goal,
    plan_run,
    registered_goals,
)
from steward_orchestration.registry import REGISTRY
from steward_schemas import RunBudget

BUDGET = RunBudget(steps=4, tokens=100, cost_usd=Decimal("0.5"), wall_clock=timedelta(minutes=1))


class Params(GoalParams):
    table: str
    limit: int = 10


def _default_planner(params: Params) -> tuple[PlannedTask, ...]:
    return tuple(
        PlannedTask(task_type="profile_table", payload={"table": params.table, "n": n})
        for n in range(params.limit)
    )


def _registration(
    *,
    planner: Planner[Params] = _default_planner,
    allowed: frozenset[str] = frozenset({"profile_table"}),
) -> GoalRegistration[Params]:
    return GoalRegistration(
        goal="fixture_goal",
        params_model=Params,
        planner=planner,
        allowed_task_types=allowed,
        budget=BUDGET,
    )


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """Undo anything a test registers globally, so goal names cannot leak.

    Reaches for the module attribute rather than a package export: the registry
    dict is deliberately not part of the public surface.
    """
    snapshot = dict(REGISTRY)
    try:
        yield
    finally:
        REGISTRY.clear()
        REGISTRY.update(snapshot)


def test_a_valid_payload_becomes_typed_params() -> None:
    plan = _registration().plan({"table": "public.users", "limit": 2})

    assert isinstance(plan.params, Params)
    assert plan.params.table == "public.users"
    assert [task.payload["n"] for task in plan.tasks] == [0, 1]


def test_a_payload_missing_a_required_field_is_rejected() -> None:
    with pytest.raises(InvalidGoalPayload) as excinfo:
        _registration().plan({"limit": 2})

    assert excinfo.value.goal == "fixture_goal"
    assert [error["loc"] for error in excinfo.value.errors()] == [("table",)]


def test_a_payload_of_the_wrong_type_is_rejected() -> None:
    with pytest.raises(InvalidGoalPayload) as excinfo:
        _registration().plan({"table": "t", "limit": "many"})

    assert [error["loc"] for error in excinfo.value.errors()] == [("limit",)]


def test_an_unknown_parameter_is_rejected_rather_than_dropped() -> None:
    # The dangerous failure is silence: a client that misspells a parameter and
    # gets a 202 believes the value took effect.
    with pytest.raises(InvalidGoalPayload) as excinfo:
        _registration().plan({"table": "t", "limitt": 3})

    assert [error["type"] for error in excinfo.value.errors()] == ["extra_forbidden"]


def test_goal_params_are_frozen() -> None:
    params = Params(table="t")

    with pytest.raises(ValidationError):
        params.table = "other"  # type: ignore[misc]


def test_a_planner_cannot_plan_outside_its_allowlist() -> None:
    """The least-privilege claim, asserted rather than reviewed.

    `plan` is the only path from a planner's output to a `TaskSpec`, so a
    planner that names a type it was not registered with cannot reach the
    queue by any route.
    """

    def overreaching(params: Params) -> tuple[PlannedTask, ...]:
        return (
            PlannedTask(task_type="profile_table", payload={}),
            PlannedTask(task_type="drop_table", payload={}),
        )

    registration = _registration(planner=overreaching)

    with pytest.raises(DisallowedTaskType) as excinfo:
        registration.plan({"table": "t"})

    assert excinfo.value.task_type == "drop_table"
    assert "profile_table" in str(excinfo.value)


def test_an_allowed_expansion_becomes_queue_specs_under_the_goals_budget() -> None:
    # Per-task caps are the goal's caps today; see `task_specs` on why that is a
    # placeholder until run-level budget enforcement lands with the agent loop.
    run_id = uuid4()

    specs = _registration().plan({"table": "t", "limit": 3}).task_specs(run_id)

    assert [spec.run_id for spec in specs] == [run_id] * 3
    assert {spec.task_id for spec in specs} == set(spec.task_id for spec in specs)
    assert all(spec.budget == BUDGET for spec in specs)
    assert all(spec.task_type == "profile_table" for spec in specs)
    assert all(spec.max_attempts == 3 for spec in specs)


def test_a_planner_can_ask_for_fewer_attempts() -> None:
    def once(params: Params) -> tuple[PlannedTask, ...]:
        return (PlannedTask(task_type="profile_table", payload={}, max_attempts=1),)

    [spec] = _registration(planner=once).plan({"table": "t"}).task_specs(uuid4())

    assert spec.max_attempts == 1


def test_an_unregistered_goal_is_unknown() -> None:
    with pytest.raises(UnknownGoal) as excinfo:
        plan_run("not_a_goal", {})

    assert excinfo.value.goal == "not_a_goal"
    assert "not_a_goal" in str(excinfo.value)


def test_get_goal_raises_for_an_unregistered_name() -> None:
    with pytest.raises(UnknownGoal):
        get_goal("not_a_goal")


def test_registered_goals_are_sorted_names() -> None:
    names = registered_goals()

    assert names == tuple(sorted(names))
    assert "noop" in names


def test_registering_a_goal_makes_it_plannable(isolated_registry: None) -> None:
    @goal("fixture_goal", params_model=Params, allowed_task_types=["profile_table"], budget=BUDGET)
    def plan(params: Params) -> tuple[PlannedTask, ...]:
        return (PlannedTask(task_type="profile_table", payload={"table": params.table}),)

    plan_result = plan_run("fixture_goal", {"table": "t"})

    assert "fixture_goal" in registered_goals()
    assert plan_result.budget == BUDGET
    assert plan_result.tasks == (PlannedTask(task_type="profile_table", payload={"table": "t"}),)


def test_a_goal_name_cannot_be_registered_twice(isolated_registry: None) -> None:
    # Two planners under one name is the bug the single registration site
    # exists to prevent: whichever imported last would silently win.
    def plan(params: Params) -> tuple[PlannedTask, ...]:
        return ()

    goal("fixture_goal", params_model=Params, allowed_task_types=["profile_table"], budget=BUDGET)(plan)

    with pytest.raises(ValueError, match="already registered"):
        goal("fixture_goal", params_model=Params, allowed_task_types=["profile_table"], budget=BUDGET)(plan)


def test_a_goal_cannot_be_registered_with_an_empty_allowlist(isolated_registry: None) -> None:
    # An empty allowlist reads as "no privilege" but would mean "plans nothing
    # that can ever be enqueued" -- a goal that always fails at expansion.
    def plan(params: Params) -> tuple[PlannedTask, ...]:
        return ()

    with pytest.raises(ValueError, match="empty task-type allowlist"):
        goal("fixture_goal", params_model=Params, allowed_task_types=[], budget=BUDGET)(plan)
