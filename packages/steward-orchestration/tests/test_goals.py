"""The registered goals themselves, checked against the rest of the system.

`test_every_goal_plans_only_executable_task_types` binds to both registries
rather than to a list in this file (GUARDRAILS.md §3): a goal added in a later
milestone is checked the moment it is registered, with no test to remember to
edit. It is the reason orchestration does not import the queue at runtime --
the two packages agree on task-type *names*, and this is where that agreement
is verified instead of assumed.

`test_every_registered_planner_is_deterministic` binds the same way: it is
ARCHITECTURE.md §4's "planners are deterministic and pure" turned into a harness
over each goal's representative payload -- falsifiable evidence, not a proof for
every input: a planner deterministic here but time-dependent on another branch
would still pass
(issue #37), run against every registration's required `sample_payload`
(GUARDRAILS.md §3, the same registry-bound shape H1 uses).
"""

from uuid import uuid4

import steward_catalog
from steward_orchestration import (
    CLASSIFY_ASSET_GOAL,
    CLASSIFY_ASSET_TASK_TYPE,
    NOOP_GOAL,
    NOOP_TASK_BUDGET,
    NOOP_TASK_TYPE,
    PROFILE_ASSET_GOAL,
    PROFILE_ASSET_TASK_TYPE,
    SCAN_SOURCE_GOAL,
    SCAN_SOURCE_TASK_TYPE,
    GoalParams,
    InvalidGoalPayload,
    NoopParams,
    PlannedTask,
    get_goal,
    plan_run,
    registered_goals,
)
from steward_queue import registered_types

SOURCE_ID = "22222222-2222-2222-2222-222222222222"
ASSET_ID = "33333333-3333-3333-3333-333333333333"


def test_the_task_type_names_on_both_sides_of_the_seam_agree() -> None:
    """The two packages agree on a *string*, never an import: the queue must
    not learn what a goal is, and orchestration takes no runtime dependency on
    the catalog. This is where that agreement stops being an assumption --
    importing `steward_catalog` here is also what registers the handler the
    check below looks for."""
    assert SCAN_SOURCE_TASK_TYPE == steward_catalog.SCAN_SOURCE_TASK_TYPE
    assert PROFILE_ASSET_TASK_TYPE == steward_catalog.PROFILE_ASSET_TASK_TYPE
    assert CLASSIFY_ASSET_TASK_TYPE == steward_catalog.CLASSIFY_ASSET_TASK_TYPE


def test_every_goal_plans_only_executable_task_types() -> None:
    executable = set(registered_types())

    for name in registered_goals():
        registration = get_goal(name)
        assert registration.allowed_task_types <= executable, (
            f"goal {name!r} may plan task types no handler can execute: "
            f"{sorted(registration.allowed_task_types - executable)}"
        )


def test_every_goal_is_registered_with_hard_caps() -> None:
    # I12: a goal without a budget cannot be registered, so this asserts the
    # caps are meaningful rather than zeroed placeholders.
    for name in registered_goals():
        budget = get_goal(name).budget
        assert budget.steps > 0, name
        assert budget.wall_clock.total_seconds() > 0, name


def test_noop_plans_exactly_one_noop_task() -> None:
    plan = plan_run(NOOP_GOAL, {"echo": "hello"})

    assert [(task.task_type, dict(task.payload)) for task in plan.tasks] == [
        (NOOP_TASK_TYPE, {"echo": "hello"})
    ]


def test_noop_accepts_an_empty_payload() -> None:
    # M0's exit criterion posts `{"goal": "noop"}` with no payload at all.
    plan = plan_run(NOOP_GOAL, {})

    assert plan.tasks[0].payload == {"echo": ""}
    assert NoopParams().echo == ""


def test_scan_source_plans_exactly_one_task() -> None:
    """One task, whose declared budget is the run's whole budget -- the
    degenerate reservation, and the only shape in which the two may be equal
    (issue #48). A second task would now have to be funded out of the same
    pot, or `plan` refuses the expansion; before #48 it would silently have
    doubled what a run may spend."""
    plan = plan_run(SCAN_SOURCE_GOAL, {"source_id": SOURCE_ID})

    assert [(task.task_type, dict(task.payload)) for task in plan.tasks] == [
        (SCAN_SOURCE_TASK_TYPE, {"source_id": SOURCE_ID})
    ]
    assert plan.budget == get_goal(SCAN_SOURCE_GOAL).budget
    assert [spec.budget for spec in plan.task_specs(uuid4())] == [plan.budget]
    assert plan.reserved() == plan.budget


def test_profile_asset_plans_exactly_one_task_per_asset() -> None:
    """The fan-out decision of issue #49, stated as an assertion.

    SPEC.md §3.1 sketches `profile_table (×N)` and #48 made such a plan
    representable -- but a planner is a pure function of its params and cannot
    read the catalog to find out what N is, so the expansion that would need
    one is not available here. The asset is the unit that carries a budget:
    one asset, one task, one run's cap.
    """
    plan = plan_run(PROFILE_ASSET_GOAL, {"asset_id": ASSET_ID})

    assert [(task.task_type, dict(task.payload)) for task in plan.tasks] == [
        (PROFILE_ASSET_TASK_TYPE, {"asset_id": ASSET_ID})
    ]
    assert plan.reserved() == plan.budget
    assert plan.budget.tokens == 0  # no model is called in this slice (#49)


def test_classify_asset_plans_one_task_naming_a_profile_version() -> None:
    """The first shipped goal whose task calls a model (#50).

    Two things are asserted that no other goal here can be: the payload carries
    the *version* rather than leaving the handler to resolve "the latest" -- which
    is what makes a re-profile between request and claim a refusal rather than a
    silent substitution -- and the budget is non-zero in the dimensions a model
    spends. A classifier goal with `tokens == 0` would be a cap that refuses the
    first call.
    """
    plan = plan_run(CLASSIFY_ASSET_GOAL, {"asset_id": ASSET_ID, "profile_version": 3})

    assert [(task.task_type, dict(task.payload)) for task in plan.tasks] == [
        (CLASSIFY_ASSET_TASK_TYPE, {"asset_id": ASSET_ID, "profile_version": 3})
    ]
    assert plan.reserved() == plan.budget
    assert plan.budget.tokens > 0
    assert plan.budget.cost_usd > 0


def test_classify_asset_rejects_a_payload_that_does_not_name_one_profile_version() -> None:
    for payload in (
        {},
        {"asset_id": ASSET_ID},
        {"profile_version": 1},
        {"asset_id": ASSET_ID, "profile_version": 0},
        {"asset_id": "not-a-uuid", "profile_version": 1},
        {"asset_id": ASSET_ID, "profile_version": 1, "columns": ["email"]},
    ):
        try:
            plan_run(CLASSIFY_ASSET_GOAL, payload)
        except InvalidGoalPayload:
            continue
        raise AssertionError(f"payload should not have been accepted: {payload}")


def test_profile_asset_rejects_a_payload_that_is_not_an_asset_id() -> None:
    for payload in ({}, {"asset_id": "not-a-uuid"}, {"asset_id": ASSET_ID, "columns": ["email"]}):
        try:
            plan_run(PROFILE_ASSET_GOAL, payload)
        except InvalidGoalPayload:
            continue
        raise AssertionError(f"payload should not have been accepted: {payload}")


def test_every_registered_goals_plan_fits_the_budget_it_advertises() -> None:
    """I12 over the whole registry, not the two goals this file names.

    `plan` refuses an unaffordable expansion, so this cannot fail for a
    registered goal on its own sample -- which is the point: it states the
    property a reader would otherwise have to infer, and it will fail the
    moment someone weakens the check rather than only when a planner
    misbehaves.
    """
    for name in registered_goals():
        registration = get_goal(name)
        plan = registration.plan(registration.sample_payload)
        assert plan.reserved().over(plan.budget) == (), name
        assert all(task.budget.over(plan.budget) == () for task in plan.tasks), name


def test_scan_source_rejects_a_payload_that_is_not_a_source_id() -> None:
    for payload in ({}, {"source_id": "not-a-uuid"}, {"source_id": SOURCE_ID, "depth": 2}):
        try:
            plan_run(SCAN_SOURCE_GOAL, payload)
        except InvalidGoalPayload:
            continue
        raise AssertionError(f"payload should not have been accepted: {payload}")


def test_the_registry_has_subjects() -> None:
    # A harness bound to a registry must fail loudly on zero subjects rather
    # than pass on none, the same guard `steward_queue`'s H1 harness carries.
    assert registered_goals(), "no registered goals to check determinism on"


def test_every_registered_planner_is_deterministic() -> None:
    """ARCHITECTURE.md §4: planners are deterministic and pure code, but
    nothing checked it -- a planner reading `uuid4()` or the clock registered
    happily. Each registration's own `sample_payload` (required at
    registration, GUARDRAILS.md §3) is run through the planner twice; the
    resulting `PlannedTask`s are compared in full.

    `PlannedTask` carries no generated identity of its own -- `task_type`,
    `budget`, `payload` and `max_attempts`, all supplied by the planner -- so a plain
    tuple comparison covers everything the planner is answerable for. The ids
    `RunPlan.task_specs` mints (`task_id`, `run_id`) come from `uuid4()` calls
    made *after* planning, on the plan's output, never from the planner
    itself, so they are out of scope here by construction, not by omission.
    """
    for name in registered_goals():
        registration = get_goal(name)

        first = registration.plan(registration.sample_payload).tasks
        second = registration.plan(registration.sample_payload).tasks

        assert first == second, f"goal {name!r} planned different tasks across two runs"


def test_the_comparison_would_catch_a_nondeterministic_planner() -> None:
    """Determinism checks must be falsifiable: a planner reaching for `uuid4()`
    has to make the comparison above fail, or a green result proves nothing.

    Exercised directly against a throwaway planner rather than a real
    registration, so this test does not depend on any goal staying
    non-deterministic-free to prove the check works.
    """

    class Params(GoalParams):
        pass

    def reads_uuid4(params: Params) -> tuple[PlannedTask, ...]:
        return (PlannedTask(task_type="noop", budget=NOOP_TASK_BUDGET, payload={"id": str(uuid4())}),)

    params = Params()
    first = tuple(reads_uuid4(params))
    second = tuple(reads_uuid4(params))

    assert first != second
