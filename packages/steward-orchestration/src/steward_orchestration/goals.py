"""Registered goals.

`noop` was the whole list at M0, and `scan_source` (issue #20) arrived exactly
as issue #19 predicted it would: another block in this file, and nothing else
in the system changed to admit it -- the API validates it, plans it and
enqueues it through the same registry path.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import Field
from steward_schemas import RunBudget

from steward_orchestration.registry import GoalParams, PlannedTask, goal

NOOP_GOAL = "noop"

NOOP_TASK_TYPE = "noop"
"""The task type `noop` plans.

The string is the seam between a planner and a handler: `steward_queue`
registers the handler under the same name, and the two packages agree on the
name rather than on an import -- the queue must not depend on orchestration
(it dispatches task types, it does not know goals exist) and orchestration does
not take a runtime dependency on the queue to borrow a constant. The agreement
is asserted, not assumed: `tests/test_goals.py` checks every registered goal's
allowlist against the queue's handler registry.
"""

NOOP_BUDGET = RunBudget(
    steps=32,
    tokens=200_000,
    cost_usd=Decimal("2.000000"),
    wall_clock=timedelta(minutes=15),
)
"""What a `noop` run may spend (I12).

Conservative rather than tight: `noop` runs no model, so every dimension here
is slack. It is stated anyway because a goal cannot be registered without hard
caps -- the budget being per-goal is what replaced the API store's single
hardcoded default (issue #19).
"""

NOOP_TASK_BUDGET = RunBudget(
    steps=1,
    tokens=0,
    cost_usd=Decimal("0.000000"),
    wall_clock=timedelta(minutes=5),
)
"""What the one task a `noop` run plans may spend (issue #48).

Well inside `NOOP_BUDGET`, and deliberately so: the run budget is the pot, this
is the draw, and a goal that later plans a second task has room to declare one
without the run's advertised cap moving. The task budget, not the run's, is
what the queue enforces and what the worker's deadline is set from -- so the
handler that echoes a payload is bound by five minutes, not fifteen.
"""


class NoopParams(GoalParams):
    """`noop`'s parameters: a string to echo back, and nothing else.

    `echo` has a default, so `{}` is a valid payload; anything beyond it is
    not, because `GoalParams` forbids extras. That is the smallest schema that
    still demonstrates both rejections the boundary now makes.
    """

    echo: str = ""


@goal(
    NOOP_GOAL,
    params_model=NoopParams,
    allowed_task_types=[NOOP_TASK_TYPE],
    budget=NOOP_BUDGET,
    sample_payload={"echo": "steward"},
)
def plan_noop(params: NoopParams) -> tuple[PlannedTask, ...]:
    """Expand `noop` into the single task M0's exit criterion flows through."""
    return (PlannedTask(task_type=NOOP_TASK_TYPE, budget=NOOP_TASK_BUDGET, payload={"echo": params.echo}),)


SCAN_SOURCE_GOAL = "scan_source"

SCAN_SOURCE_TASK_TYPE = "scan_source"
"""The task type `scan_source` plans; `steward_catalog` registers its handler
under the same name. Same seam as `NOOP_TASK_TYPE`, checked the same way."""

# What a `scan_source` run may spend (I12). Tight, and it can be, because the
# plan below is exactly one task whose declared budget is this whole amount:
# the reservation (issue #48) is the run cap exactly, so what the API
# advertises for the run is what the run can spend, with nothing left over for
# a second task to be added quietly.
# `tokens` and `cost_usd` are zero because a metadata-only scan calls no model
# -- if a later slice makes it call one, the budget has to be raised
# deliberately rather than being found to be slack. `wall_clock` is also the
# source connection's connect and statement timeout
# (`steward_catalog.inspector`), which reads it off the *task* spec, so a
# source that accepts a connection and then stops answering cannot outlive the
# cap.
SCAN_SOURCE_BUDGET = RunBudget(
    steps=1,
    tokens=0,
    cost_usd=Decimal("0.000000"),
    wall_clock=timedelta(minutes=10),
)

SCAN_SOURCE_TASK_BUDGET = SCAN_SOURCE_BUDGET
"""What the one task a `scan_source` run plans may spend.

The run's whole budget, because the plan is one task -- the degenerate case of
the reservation, and the only shape in which a task budget and a run budget are
allowed to be equal. Fanning out later means splitting this, not repeating it:
`plan` refuses a second task that does not come out of the same pot (#48).
"""


class ScanSourceParams(GoalParams):
    """`scan_source`'s parameters: which registered source to scan.

    A `UUID`, not a string, so a malformed id is a 422 at the boundary rather
    than a task that fails on a worker twenty seconds later (I3).
    """

    source_id: UUID


@goal(
    SCAN_SOURCE_GOAL,
    params_model=ScanSourceParams,
    allowed_task_types=[SCAN_SOURCE_TASK_TYPE],
    budget=SCAN_SOURCE_BUDGET,
    sample_payload={"source_id": "00000000-0000-0000-0000-000000000000"},
)
def plan_scan_source(params: ScanSourceParams) -> tuple[PlannedTask, ...]:
    """Expand `scan_source` into **exactly one** bounded task -- by choice (#48).

    The obvious plan is a fan-out -- discover the schema, then one
    `profile_table` per table -- and SPEC.md §3.1 sketches it. Until #48 that
    plan was *unrepresentable* safely: every planned task got the run's whole
    budget, so an N-way fan-out let one run spend N times the cap the API
    published for it (I12), and #20 shipped one task because one task was the
    only honest shape. That constraint is gone: a fan-out now declares a budget
    per branch and is refused if the branches do not fit the run.

    What keeps this plan single-task is that a deterministic metadata scan has
    nothing to gain from splitting. One round trip enumerates every table in a
    schema, so N tasks would be N connections to the customer's database for
    work one connection already does, and the convergence diff (`plan_convergence`)
    is computed against the whole observed catalog at once -- a per-table task
    would have to either re-read the rest or give up detecting a dropped table.

    Profiling (#49) was expected to be the fan-out this unblocked, and turned
    out not to be one: see `plan_profile_asset` below. A planner cannot read the
    catalog, so there is nothing here to fan out *to*.
    """
    return (
        PlannedTask(
            task_type=SCAN_SOURCE_TASK_TYPE,
            budget=SCAN_SOURCE_TASK_BUDGET,
            payload={"source_id": str(params.source_id)},
        ),
    )


PROFILE_ASSET_GOAL = "profile_asset"

PROFILE_ASSET_TASK_TYPE = "profile_asset"
"""The task type `profile_asset` plans; `steward_catalog` registers its handler
under the same name. Same seam as `NOOP_TASK_TYPE`, checked the same way."""

# What a `profile_asset` run may spend (I12). `tokens` and `cost_usd` are zero
# because profiling is deterministic SQL and calls no model (#49) -- if a later
# slice makes it call one, the budget has to be raised deliberately rather than
# discovered to be slack. `wall_clock` is larger than a scan's because the work
# is: a statistics pass over every row of a relation plus one grouped query per
# column, where a scan reads catalog metadata only. It is also the source
# connection's connect and statement timeout (`steward_catalog.profiler`), which
# reads it off the *task* spec, so a table that stops answering mid-aggregate
# cannot outlive the cap.
PROFILE_ASSET_BUDGET = RunBudget(
    steps=1,
    tokens=0,
    cost_usd=Decimal("0.000000"),
    wall_clock=timedelta(minutes=30),
)

PROFILE_ASSET_TASK_BUDGET = PROFILE_ASSET_BUDGET
"""What the one task a `profile_asset` run plans may spend: the run's whole
budget, the degenerate reservation again (#48)."""


class ProfileAssetParams(GoalParams):
    """`profile_asset`'s parameters: which catalogued asset to profile.

    An asset id, and deliberately nothing else. A relation name would be a
    string a client chooses arriving at code that composes SQL identifiers; an
    id resolves through `assets`, whose names a scan read out of the source's
    own catalog (I5, `steward_catalog._profile_sql`).
    """

    asset_id: UUID


@goal(
    PROFILE_ASSET_GOAL,
    params_model=ProfileAssetParams,
    allowed_task_types=[PROFILE_ASSET_TASK_TYPE],
    budget=PROFILE_ASSET_BUDGET,
    sample_payload={"asset_id": "00000000-0000-0000-0000-000000000000"},
)
def plan_profile_asset(params: ProfileAssetParams) -> tuple[PlannedTask, ...]:
    """Expand `profile_asset` into exactly one bounded task -- one asset's worth.

    **Why this is not the fan-out SPEC.md §3.1 sketches.** The natural shape is
    `profile_source(source_id)` expanding to one task per table, and #48 made
    such a plan representable by having each `PlannedTask` declare its own
    budget. What #48 did not do -- and cannot, by design -- is let a planner
    *find out* what the tasks are: planners are pure functions of their
    validated params and touch no connection (ARCHITECTURE.md §4), so a
    per-asset expansion would have to read the catalog at plan time, which
    makes the planner impure and the determinism harness meaningless. The other
    route, a handler that enqueues its own children, skips the plan-time
    reservation altogether -- which is the hole #48 exists to close.

    So the asset is the unit that carries a budget, and a client asks for the
    assets it wants profiled.

    Budget is *not* the reason, and SPEC.md §3.1 is explicit about it: a
    deterministic fan-out spending no model budget is safe under reservation
    alone, which is precisely what profiling is. #48's scope explains only why
    the workaround is worse than the constraint -- a handler enqueuing its own
    children skips plan-time reservation, and since retried and failed spend is
    debited nowhere (SPEC.md §13 D9) those children would run under a cap
    nothing reconciles. A per-source expansion belongs with a planner that may
    consult the catalog.
    """
    return (
        PlannedTask(
            task_type=PROFILE_ASSET_TASK_TYPE,
            budget=PROFILE_ASSET_TASK_BUDGET,
            payload={"asset_id": str(params.asset_id)},
        ),
    )


CLASSIFY_ASSET_GOAL = "classify_asset"

CLASSIFY_ASSET_TASK_TYPE = "classify_asset"
"""The task type `classify_asset` plans; `steward_catalog` registers its handler
under the same name. Same seam as `NOOP_TASK_TYPE`, checked the same way.

This is the first goal in the shipped registry whose task calls a model, and it
took a design decision to make it one. The seam check below requires a
registered goal's task types to be executable by importing packages, and an
agent handler needs a gateway only a composition root may validate (I15) -- which
is why the proof agent's `agent_echo` goal is registered by its acceptance test
rather than here (SPEC.md §13 D1's consequence note). The Classifier resolves it
by splitting the two: `steward_catalog` owns and registers the workflow, and the
model call sits behind a protocol a worker binds an implementation of
(SPEC.md §13 D15).
"""

# What a `classify_asset` run may spend (I12). The first goal here with non-zero
# `tokens` and `cost_usd`, because it is the first whose task calls a model.
#
# The figures are the loop's shape multiplied out rather than round numbers: the
# expected run is one generation and one `submit_result`, SPEC.md §3.2 allows one
# validation correction, and `steps` is set at six so a correction and its retry
# fit with room to be refused rather than to be silently afforded. The
# worker-side `ModelReservation` is chosen to divide into these -- six steps of
# 18k tokens, $0.08 and 90 seconds each fit inside every dimension below with the
# margin an overestimate is allowed to cost (`steward_workers.classifier`).
CLASSIFY_ASSET_BUDGET = RunBudget(
    steps=6,
    tokens=120_000,
    cost_usd=Decimal("0.500000"),
    wall_clock=timedelta(minutes=10),
)

CLASSIFY_ASSET_TASK_BUDGET = CLASSIFY_ASSET_BUDGET
"""What the one task a `classify_asset` run plans may spend: the run's whole
budget, the degenerate reservation again (#48)."""


class ClassifyAssetParams(GoalParams):
    """`classify_asset`'s parameters: which asset, at which profile version.

    The version is required rather than defaulted to "the latest", because the
    latest at request time and the latest at claim time are not the same profile,
    and a classification is a statement about a *specific* one. Naming it makes
    the run reproducible and makes a re-profile between request and claim a
    refusal the handler can state (`steward_catalog.classify_handler`) instead of
    a substitution nobody is told about.
    """

    asset_id: UUID
    profile_version: int = Field(ge=1)


@goal(
    CLASSIFY_ASSET_GOAL,
    params_model=ClassifyAssetParams,
    allowed_task_types=[CLASSIFY_ASSET_TASK_TYPE],
    budget=CLASSIFY_ASSET_BUDGET,
    sample_payload={
        "asset_id": "00000000-0000-0000-0000-000000000000",
        "profile_version": 1,
    },
)
def plan_classify_asset(params: ClassifyAssetParams) -> tuple[PlannedTask, ...]:
    """Expand `classify_asset` into exactly one bounded task.

    One task for the reason `profile_asset` is one, and one more besides. The
    planner cannot read the catalog, so it cannot know how many columns the
    profile holds and has nothing to fan out to (ARCHITECTURE.md §4). And a
    per-column fan-out would be the wrong unit even if it could: the evidence a
    classifier reasons from is the *table's* profile -- neighbouring column names
    and the shape of the relation are what distinguish `ssn_hash` from `ssn` --
    and a proposal is stored per asset, not per column.
    """
    return (
        PlannedTask(
            task_type=CLASSIFY_ASSET_TASK_TYPE,
            budget=CLASSIFY_ASSET_TASK_BUDGET,
            payload={
                "asset_id": str(params.asset_id),
                "profile_version": params.profile_version,
            },
        ),
    )

