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
    EmptyRunPlan,
    GoalParams,
    GoalRegistration,
    InvalidGoalPayload,
    PlannedTask,
    Planner,
    RunBudgetExceeded,
    UnknownGoal,
    get_goal,
    goal,
    plan_run,
    registered_goals,
)
from steward_orchestration.registry import REGISTRY
from steward_schemas import RunBudget

TASK_BUDGET = RunBudget(steps=4, tokens=100, cost_usd=Decimal("0.5"), wall_clock=timedelta(minutes=1))
"""What each fixture task declares it may spend."""

BUDGET = RunBudget(steps=40, tokens=1000, cost_usd=Decimal("5.0"), wall_clock=timedelta(minutes=10))
"""What a fixture run may spend: exactly ten task budgets.

Deliberately a whole multiple of `TASK_BUDGET`, so `Params.limit` reads
directly as "how much of the run this expansion reserves": the default 10
reserves all of it, 11 reserves more than exists, and the boundary between them
is the reservation check (issue #48).
"""


class Params(GoalParams):
    table: str
    limit: int = 10


def _default_planner(params: Params) -> tuple[PlannedTask, ...]:
    return tuple(
        PlannedTask(budget=TASK_BUDGET, task_type="profile_table", payload={"table": params.table, "n": n})
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
        sample_payload={"table": "public.users"},
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
            PlannedTask(budget=TASK_BUDGET, task_type="profile_table", payload={}),
            PlannedTask(budget=TASK_BUDGET, task_type="drop_table", payload={}),
        )

    registration = _registration(planner=overreaching)

    with pytest.raises(DisallowedTaskType) as excinfo:
        registration.plan({"table": "t"})

    assert excinfo.value.task_type == "drop_table"
    assert "profile_table" in str(excinfo.value)


def test_a_planner_that_plans_nothing_is_rejected() -> None:
    # `_default_planner` fans out one task per unit of `limit`; zero is a
    # planner returning no tasks, the exact shape a conditional planner with a
    # missed branch would take at runtime.
    with pytest.raises(EmptyRunPlan) as excinfo:
        _registration().plan({"table": "t", "limit": 0})

    assert excinfo.value.goal == "fixture_goal"
    assert "fixture_goal" in str(excinfo.value)


def test_an_allowed_expansion_becomes_queue_specs_under_their_declared_budgets() -> None:
    # The defect issue #48 removed: every spec used to carry the *run's*
    # budget, so these three tasks could each spend what the API published for
    # the whole run.
    run_id = uuid4()

    specs = _registration().plan({"table": "t", "limit": 3}).task_specs(run_id)

    assert [spec.run_id for spec in specs] == [run_id] * 3
    assert {spec.task_id for spec in specs} == set(spec.task_id for spec in specs)
    assert all(spec.budget == TASK_BUDGET for spec in specs)
    assert all(spec.budget != BUDGET for spec in specs)
    assert all(spec.task_type == "profile_table" for spec in specs)
    assert all(spec.max_attempts == 3 for spec in specs)


def test_n_tasks_cannot_each_spend_the_run_budget() -> None:
    """The property the fan-out prerequisite is for (issue #48, I12).

    Three tasks, and what they may spend between them is one run budget --
    not three, which is what "every task carries the run's budget" meant.
    """
    plan = _registration().plan({"table": "t", "limit": 3})

    specs = plan.task_specs(uuid4())
    assert RunBudget.total(spec.budget for spec in specs) == plan.reserved()
    assert plan.reserved().over(plan.budget) == ()
    assert plan.reserved().steps == 3 * TASK_BUDGET.steps < BUDGET.steps


def test_an_expansion_reserving_more_than_the_run_budget_is_refused() -> None:
    # Eleven tasks against a ten-task budget: every dimension overruns, and
    # the refusal names all four rather than the first one it noticed.
    with pytest.raises(RunBudgetExceeded) as excinfo:
        _registration().plan({"table": "t", "limit": 11})

    assert excinfo.value.goal == "fixture_goal"
    assert excinfo.value.dimensions == ("steps", "tokens", "cost_usd", "wall_clock")
    assert excinfo.value.budget == BUDGET
    assert excinfo.value.reserved.steps == 11 * TASK_BUDGET.steps


def test_an_expansion_reserving_the_whole_run_budget_is_allowed() -> None:
    # The boundary is `>`, not `>=`: a plan may spend exactly what its run was
    # admitted for. `scan_source` is this case -- one task, the run's budget.
    plan = _registration().plan({"table": "t", "limit": 10})

    assert plan.reserved() == BUDGET


def test_only_one_dimension_need_overrun_for_the_plan_to_be_refused() -> None:
    # A fan-out that fits on every axis but one is still unaffordable, and the
    # error says which axis -- an operator should not have to diff two budgets.
    def one_expensive_task(params: Params) -> tuple[PlannedTask, ...]:
        return (
            PlannedTask(
                task_type="profile_table",
                budget=TASK_BUDGET.model_copy(update={"tokens": BUDGET.tokens + 1}),
            ),
        )

    with pytest.raises(RunBudgetExceeded) as excinfo:
        _registration(planner=one_expensive_task).plan({"table": "t"})

    assert excinfo.value.dimensions == ("tokens",)


def test_a_goal_whose_sample_overruns_its_budget_cannot_register(isolated_registry: None) -> None:
    # The same guard #39 put on the other three planner bugs: a goal whose own
    # sample payload cannot be afforded fails at import, before the goal is
    # reachable, rather than on whichever request hits it first.
    with pytest.raises(RunBudgetExceeded):
        goal(
            "fixture_goal",
            params_model=Params,
            allowed_task_types=["profile_table"],
            budget=TASK_BUDGET,  # one task budget for a plan of ten tasks
            sample_payload={"table": "public.users"},
        )(_default_planner)

    assert "fixture_goal" not in registered_goals()


def test_a_planner_can_ask_for_fewer_attempts() -> None:
    def once(params: Params) -> tuple[PlannedTask, ...]:
        return (PlannedTask(budget=TASK_BUDGET, task_type="profile_table", payload={}, max_attempts=1),)

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
    @goal(
        "fixture_goal",
        params_model=Params,
        allowed_task_types=["profile_table"],
        budget=BUDGET,
        sample_payload={"table": "public.users"},
    )
    def plan(params: Params) -> tuple[PlannedTask, ...]:
        return (PlannedTask(budget=TASK_BUDGET, task_type="profile_table", payload={"table": params.table}),)

    plan_result = plan_run("fixture_goal", {"table": "t"})

    assert "fixture_goal" in registered_goals()
    assert plan_result.budget == BUDGET
    assert plan_result.tasks == (
        PlannedTask(budget=TASK_BUDGET, task_type="profile_table", payload={"table": "t"}),
    )


def test_a_goal_name_cannot_be_registered_twice(isolated_registry: None) -> None:
    # Two planners under one name is the bug the single registration site
    # exists to prevent: whichever imported last would silently win.
    def plan(params: Params) -> tuple[PlannedTask, ...]:
        return (PlannedTask(budget=TASK_BUDGET, task_type="profile_table", payload={}),)

    goal(
        "fixture_goal",
        params_model=Params,
        allowed_task_types=["profile_table"],
        budget=BUDGET,
        sample_payload={"table": "public.users"},
    )(plan)

    with pytest.raises(ValueError, match="already registered"):
        goal(
            "fixture_goal",
            params_model=Params,
            allowed_task_types=["profile_table"],
            budget=BUDGET,
            sample_payload={"table": "public.users"},
        )(plan)


def test_a_goal_with_a_sample_payload_that_fails_its_own_schema_cannot_register(
    isolated_registry: None,
) -> None:
    # Issue #39: `sample_payload` used to be stored, never exercised, so a
    # sample that does not even match the goal's own params model booted fine
    # and only surfaced on a customer's request.
    def plan(params: Params) -> tuple[PlannedTask, ...]:
        return (PlannedTask(budget=TASK_BUDGET, task_type="profile_table", payload={}),)

    with pytest.raises(InvalidGoalPayload):
        goal(
            "fixture_goal",
            params_model=Params,
            allowed_task_types=["profile_table"],
            budget=BUDGET,
            sample_payload={"limit": "not-an-int"},  # missing "table", wrong type for "limit"
        )(plan)

    assert "fixture_goal" not in registered_goals()


def test_a_goal_whose_sample_plans_nothing_cannot_register(isolated_registry: None) -> None:
    # Issue #39: a planner that names zero tasks on its own sample used to
    # register happily and only raise `EmptyRunPlan` on a real request.
    def plans_nothing(params: Params) -> tuple[PlannedTask, ...]:
        return ()

    with pytest.raises(EmptyRunPlan):
        goal(
            "fixture_goal",
            params_model=Params,
            allowed_task_types=["profile_table"],
            budget=BUDGET,
            sample_payload={"table": "public.users"},
        )(plans_nothing)

    assert "fixture_goal" not in registered_goals()


def test_a_goal_whose_sample_plans_outside_its_allowlist_cannot_register(
    isolated_registry: None,
) -> None:
    # Issue #39: a planner reaching outside its own least-privilege list on
    # its own sample used to register happily and only raise
    # `DisallowedTaskType` on a real request.
    def overreaching(params: Params) -> tuple[PlannedTask, ...]:
        return (PlannedTask(budget=TASK_BUDGET, task_type="drop_table", payload={}),)

    with pytest.raises(DisallowedTaskType):
        goal(
            "fixture_goal",
            params_model=Params,
            allowed_task_types=["profile_table"],
            budget=BUDGET,
            sample_payload={"table": "public.users"},
        )(overreaching)

    assert "fixture_goal" not in registered_goals()


def test_a_goal_cannot_be_registered_with_an_empty_allowlist(isolated_registry: None) -> None:
    # An empty allowlist reads as "no privilege" but would mean "plans nothing
    # that can ever be enqueued" -- a goal that always fails at expansion.
    def plan(params: Params) -> tuple[PlannedTask, ...]:
        return ()

    with pytest.raises(ValueError, match="empty task-type allowlist"):
        goal(
            "fixture_goal",
            params_model=Params,
            allowed_task_types=[],
            budget=BUDGET,
            sample_payload={"table": "public.users"},
        )(plan)
