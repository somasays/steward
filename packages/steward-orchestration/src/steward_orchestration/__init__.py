"""steward-orchestration: goals, deterministic planners, and the registry that
binds them (SPEC.md §3.1, ARCHITECTURE.md §4 "planner/worker over a
transactional queue").

A run starts as a goal. This package owns everything between that goal and the
tasks a worker will claim: the name a client may ask for, the schema its
payload must match, the planner that expands it, the task types that planner
may use, and the budget its runs are admitted under -- all declared at one
registration site per goal (`registry`).

It depends on `steward-schemas` and nothing else steward-owned. In particular
it does not depend on `steward-queue`: planning produces `TaskSpec`s, it never
opens a connection, so a plan cannot half-enqueue a DAG and the caller keeps
the transaction that makes a run and its tasks atomic (I8). The reverse edge is
forbidden too -- the queue dispatches task types and must not learn what goals
exist -- and both directions are import-linter contracts (S1).

Importing this package registers its goals, the same way importing
`steward_queue` registers its task handlers: `plan_run("noop", ...)` works off
the import alone, with no setup call a caller could forget.
"""

from steward_orchestration.goals import (
    CLASSIFY_ASSET_BUDGET,
    CLASSIFY_ASSET_GOAL,
    CLASSIFY_ASSET_TASK_BUDGET,
    CLASSIFY_ASSET_TASK_TYPE,
    NOOP_BUDGET,
    NOOP_GOAL,
    NOOP_TASK_BUDGET,
    NOOP_TASK_TYPE,
    PROFILE_ASSET_BUDGET,
    PROFILE_ASSET_GOAL,
    PROFILE_ASSET_TASK_BUDGET,
    PROFILE_ASSET_TASK_TYPE,
    SCAN_SOURCE_BUDGET,
    SCAN_SOURCE_GOAL,
    SCAN_SOURCE_TASK_BUDGET,
    SCAN_SOURCE_TASK_TYPE,
    ClassifyAssetParams,
    NoopParams,
    ProfileAssetParams,
    ScanSourceParams,
)
from steward_orchestration.registry import (
    DEFAULT_MAX_ATTEMPTS,
    DisallowedTaskType,
    EmptyRunPlan,
    GoalParams,
    GoalRegistration,
    InvalidGoalPayload,
    PlannedTask,
    Planner,
    RunBudgetExceeded,
    RunPlan,
    UnknownGoal,
    get_goal,
    goal,
    plan_run,
    registered_goals,
)

__all__ = [
    "CLASSIFY_ASSET_BUDGET",
    "CLASSIFY_ASSET_GOAL",
    "CLASSIFY_ASSET_TASK_BUDGET",
    "CLASSIFY_ASSET_TASK_TYPE",
    "DEFAULT_MAX_ATTEMPTS",
    "NOOP_BUDGET",
    "NOOP_GOAL",
    "NOOP_TASK_BUDGET",
    "NOOP_TASK_TYPE",
    "PROFILE_ASSET_BUDGET",
    "PROFILE_ASSET_GOAL",
    "PROFILE_ASSET_TASK_BUDGET",
    "PROFILE_ASSET_TASK_TYPE",
    "SCAN_SOURCE_BUDGET",
    "SCAN_SOURCE_GOAL",
    "SCAN_SOURCE_TASK_BUDGET",
    "SCAN_SOURCE_TASK_TYPE",
    "ClassifyAssetParams",
    "DisallowedTaskType",
    "EmptyRunPlan",
    "GoalParams",
    "GoalRegistration",
    "InvalidGoalPayload",
    "NoopParams",
    "Planner",
    "PlannedTask",
    "ProfileAssetParams",
    "RunBudgetExceeded",
    "RunPlan",
    "ScanSourceParams",
    "UnknownGoal",
    "get_goal",
    "goal",
    "plan_run",
    "registered_goals",
]
