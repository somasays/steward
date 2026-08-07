"""The goal registry — and the contract every goal registration signs.

A goal is the unit a client asks for (`POST /v1/runs`); a task is the unit a
worker executes. Everything that turns one into the other is declared here, at
**one registration site per goal**:

1. **Name.** What a client puts in `RunCreate.goal`. Unregistered names are not
   goals, and a request naming one is rejected before anything is persisted.
2. **Input schema.** A `GoalParams` subclass — frozen and `extra="forbid"` by
   construction, so an unknown or misspelled parameter is a rejection rather
   than a value silently dropped on the floor (I3).
3. **Planner.** `params -> the tasks to enqueue`. Deterministic, pure, and
   given the *validated* params, never the raw payload (SPEC.md §3.1). It
   returns `PlannedTask`s, not queue rows: planning does not touch a
   connection, so it cannot half-enqueue a DAG, and the caller keeps ownership
   of the transaction that makes the run and its tasks atomic (I8).
4. **Allowed task types.** The planner's least-privilege list. Planning a type
   outside it raises `DisallowedTaskType` -- checked on every expansion, not
   reviewed by convention (SPEC.md §3.2's least-privilege rule, applied one
   level up).
5. **Budget.** The caps a run of this goal is admitted under. Required, with no
   default: a goal whose runs have no hard limits is unrepresentable (I12).

Where this lives is a decision, not an accident. Planners do not belong in
`steward-agents` (that package is the LangGraph-contained execution runtime,
I9), nor in the API (routes own HTTP, not planning), nor in `steward-queue`:
the queue dispatches *task types* and must never learn which goals exist. The
import-linter contracts in the root `pyproject.toml` state that as an edge, so
S1 fails if it is ever crossed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import ErrorDetails
from steward_schemas import RunBudget, TaskSpec

DEFAULT_MAX_ATTEMPTS = 3
"""Retries a planned task gets before it is dead-lettered, unless it says otherwise."""


class GoalParams(BaseModel):
    """Base class for every goal's input schema.

    Frozen and closed to unknown fields for the same reason
    `steward_schemas._base.SchemaModel` is: a payload that does not match the
    goal's shape must fail loudly at the boundary (I3). It is a separate base
    rather than a reuse of the published-contract base because goal params are
    not a published contract -- they are validated request parameters, and
    binding them to the S6 snapshot set would freeze a goal's parameters the
    moment it is first registered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True, slots=True)
class PlannedTask:
    """One node of a planned expansion: what to run, and on what.

    Deliberately not a `TaskSpec`: a planner names work, it does not mint run
    identity. `RunPlan.task_specs` supplies the run id and the run's budget,
    which is what keeps the planner a pure function of its params and makes an
    expansion assertable in a test without a database.
    """

    task_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


type Planner[P: GoalParams] = Callable[[P], Sequence[PlannedTask]]


class UnknownGoal(LookupError):
    """No goal is registered under this name."""

    def __init__(self, goal: str) -> None:
        super().__init__(f"unknown goal: {goal!r}")
        self.goal = goal


class InvalidGoalPayload(ValueError):
    """A payload did not validate against its goal's input schema.

    Carries the underlying pydantic errors so the boundary can report *which*
    field failed instead of "invalid": the caller's request is wrong, and a
    rejection that does not say how is a support ticket.
    """

    def __init__(self, goal: str, error: ValidationError) -> None:
        super().__init__(f"payload does not match the schema of goal {goal!r}")
        self.goal = goal
        self.validation_error = error

    def errors(self) -> list[ErrorDetails]:
        """Pydantic's per-field errors, without the documentation URLs."""
        return self.validation_error.errors(include_url=False)


class DisallowedTaskType(RuntimeError):
    """A planner returned a task type outside its goal's allowlist.

    A programming error, not a client error: the request was valid and the
    goal exists: the planner asked for privilege it was not registered with.
    It is raised (rather than filtered) so the run is never created holding a
    partial DAG.
    """

    def __init__(self, goal: str, task_type: str, allowed: frozenset[str]) -> None:
        super().__init__(
            f"goal {goal!r} planned task type {task_type!r}, which is not in its allowlist {sorted(allowed)}"
        )
        self.goal = goal
        self.task_type = task_type
        self.allowed = allowed


@dataclass(frozen=True, slots=True)
class RunPlan:
    """A validated, expanded, allowlist-checked plan for one run.

    Produced before the run exists (`plan_run`), so every rejection a goal can
    raise happens with nothing persisted. `task_specs` is the only way from a
    plan to the queue's contract.
    """

    goal: str
    params: GoalParams
    budget: RunBudget
    tasks: tuple[PlannedTask, ...]

    def task_specs(self, run_id: UUID) -> tuple[TaskSpec, ...]:
        """The plan as queue-ready specs for `run_id`, under the goal's budget.

        Every task carries the whole run budget, which is the placeholder the
        queue's per-task caps already imply and *not* the end state: with a
        fan-out plan it means N tasks may each spend the run's cap, so a run
        could exceed the budget the API reports for it. Nothing in M1 fans out
        yet (`noop` plans one task), and the fix is run-level enforcement --
        the accumulated `runs.used_*` totals compared against `runs.budget_*`
        by the runtime -- which lands with the agent loop that H4's
        step/token/cost half measures (I12, N6). The first goal that fans out
        (#20) must not ship before it.
        """
        return tuple(
            TaskSpec(
                task_id=uuid4(),
                run_id=run_id,
                task_type=task.task_type,
                payload=dict(task.payload),
                budget=self.budget,
                max_attempts=task.max_attempts,
            )
            for task in self.tasks
        )


@dataclass(frozen=True, slots=True)
class GoalRegistration[P: GoalParams]:
    """One registry entry: everything that is true of a goal, in one place."""

    goal: str
    params_model: type[P]
    planner: Planner[P]
    allowed_task_types: frozenset[str]
    budget: RunBudget

    def validate(self, payload: Mapping[str, Any]) -> P:
        """`payload` as this goal's typed params, or `InvalidGoalPayload`."""
        try:
            return self.params_model.model_validate(dict(payload))
        except ValidationError as exc:
            raise InvalidGoalPayload(self.goal, exc) from exc

    def plan(self, payload: Mapping[str, Any]) -> RunPlan:
        """Validate `payload`, expand it, and check the expansion's privilege.

        The allowlist is enforced here rather than at the enqueue call because
        this is the only path from a planner's output to a `TaskSpec` -- a
        planner has no other way to reach the queue, so "cannot enqueue outside
        its allowlist" is a property of the code, not of reviewer attention.
        """
        params = self.validate(payload)
        tasks = tuple(self.planner(params))
        for task in tasks:
            if task.task_type not in self.allowed_task_types:
                raise DisallowedTaskType(self.goal, task.task_type, self.allowed_task_types)
        return RunPlan(goal=self.goal, params=params, budget=self.budget, tasks=tasks)


REGISTRY: dict[str, GoalRegistration[Any]] = {}
"""Registered goals by name; read it through `registered_goals()`/`get_goal()`.

Module-private by intent -- not re-exported from the package -- so `goal()` is
the only way in and the one-registration-site property cannot be sidestepped
with a dict assignment.

The value type is erased because the registry is heterogeneous: each entry
pairs its own params model with a planner taking exactly that model, and the
decorator below type-checks that pairing at the registration site. The erasure
is the container's, not a field's -- no `Any` reaches a payload or a task.
"""


def goal[P: GoalParams](
    name: str,
    *,
    params_model: type[P],
    allowed_task_types: Sequence[str],
    budget: RunBudget,
) -> Callable[[Planner[P]], Planner[P]]:
    """Register `name`'s planner. See this module's registration contract.

    Decorating the planner is what makes the registration site singular: the
    function and everything true about it are one block of code, so adding a
    goal is adding a file, never an edit to a route, a switch or a dict
    somewhere else. `params_model` binds the planner's parameter type, so a
    planner that does not take exactly this goal's params fails `mypy --strict`
    at the registration site rather than at runtime on a client's request.
    """

    def register(planner: Planner[P]) -> Planner[P]:
        if name in REGISTRY:
            raise ValueError(f"goal already registered: {name}")
        if not allowed_task_types:
            raise ValueError(f"goal {name!r} registered with an empty task-type allowlist")
        REGISTRY[name] = GoalRegistration(
            goal=name,
            params_model=params_model,
            planner=planner,
            allowed_task_types=frozenset(allowed_task_types),
            budget=budget,
        )
        return planner

    return register


def get_goal(name: str) -> GoalRegistration[Any]:
    """The registration for `name`, or `UnknownGoal`."""
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise UnknownGoal(name) from exc


def registered_goals() -> tuple[str, ...]:
    """Goal names a client may ask for."""
    return tuple(sorted(REGISTRY))


def plan_run(name: str, payload: Mapping[str, Any]) -> RunPlan:
    """The plan for `name` and `payload` -- the admission decision, whole.

    Raises `UnknownGoal` or `InvalidGoalPayload` for a request that must not
    become a run, and `DisallowedTaskType` for a planner that exceeded its
    privilege. Callers create the run only after this returns, which is what
    makes "no run row for a rejected request" structural rather than an
    ordering a route handler has to remember (issue #19).
    """
    return get_goal(name).plan(payload)
