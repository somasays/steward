"""The registered goals themselves, checked against the rest of the system.

`test_every_goal_plans_only_executable_task_types` binds to both registries
rather than to a list in this file (GUARDRAILS.md §3): a goal added in a later
milestone is checked the moment it is registered, with no test to remember to
edit. It is the reason orchestration does not import the queue at runtime --
the two packages agree on task-type *names*, and this is where that agreement
is verified instead of assumed.
"""

from steward_orchestration import NOOP_GOAL, NOOP_TASK_TYPE, REGISTRY, NoopParams, plan_run
from steward_queue import registered_types


def test_every_goal_plans_only_executable_task_types() -> None:
    executable = set(registered_types())

    for name, registration in REGISTRY.items():
        assert registration.allowed_task_types <= executable, (
            f"goal {name!r} may plan task types no handler can execute: "
            f"{sorted(registration.allowed_task_types - executable)}"
        )


def test_every_goal_is_registered_with_hard_caps() -> None:
    # I12: a goal without a budget cannot be registered, so this asserts the
    # caps are meaningful rather than zeroed placeholders.
    for name, registration in REGISTRY.items():
        budget = registration.budget
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
