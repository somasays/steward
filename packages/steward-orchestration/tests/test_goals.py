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
    NOOP_GOAL,
    NOOP_TASK_TYPE,
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


def test_the_task_type_names_on_both_sides_of_the_seam_agree() -> None:
    """The two packages agree on a *string*, never an import: the queue must
    not learn what a goal is, and orchestration takes no runtime dependency on
    the catalog. This is where that agreement stops being an assumption --
    importing `steward_catalog` here is also what registers the handler the
    check below looks for."""
    assert SCAN_SOURCE_TASK_TYPE == steward_catalog.SCAN_SOURCE_TASK_TYPE


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
    """I12, and the constraint issue #37 put on this slice: with one task, the
    per-task cap the queue enforces is the run cap the API advertises. A plan
    that grew a second task would silently double what a run may spend."""
    plan = plan_run(SCAN_SOURCE_GOAL, {"source_id": SOURCE_ID})

    assert [(task.task_type, dict(task.payload)) for task in plan.tasks] == [
        (SCAN_SOURCE_TASK_TYPE, {"source_id": SOURCE_ID})
    ]
    assert plan.budget == get_goal(SCAN_SOURCE_GOAL).budget
    assert [spec.budget for spec in plan.task_specs(uuid4())] == [plan.budget]


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
    `payload` and `max_attempts`, all supplied by the planner -- so a plain
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
        return (PlannedTask(task_type="noop", payload={"id": str(uuid4())}),)

    params = Params()
    first = tuple(reads_uuid4(params))
    second = tuple(reads_uuid4(params))

    assert first != second
