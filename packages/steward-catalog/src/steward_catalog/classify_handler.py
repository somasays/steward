"""`classify_asset` — the classification workflow, minus the model call (#50).

The workflow is a catalog use case and lives here: it reads an immutable
profile version, decides what a classifier is allowed to see, turns what comes
back into a `ClassificationProposal`, and persists it as `pending_review`
through `classification.propose`. Every one of those steps is about catalog
state, and none of them needs a model.

**The model call is the one thing this package cannot do**, and it is injected
rather than imported: I4 forbids `steward-catalog` from importing
`steward-agents` or `steward-llm`, and it should — a package that owns the
privacy boundary must not also own a gateway client. So the handler depends on
`ColumnClassifier`, a two-method-free protocol whose whole vocabulary is
evidence in and proposed columns out. The bounded agent runtime, the prompt
artifact, the `steward-classify` alias, the tool allowlist and the gateway all
live in the worker that binds an implementation of it (SPEC.md §13 D15).

That split is what lets `classify_asset` be a *shipped* goal. The registry seam
check (`test_every_goal_plans_only_executable_task_types`) requires a goal's
task types to have handlers registered by importing packages — `agent.echo`
could not satisfy it, because its handler only exists once a worker's
composition root opts into a transport, and SPEC.md §13 records that as the
reason the proof agent's goal stayed out of the shipped registry. Registering
here satisfies it honestly: the handler exists, and what a process supplies is
the *capability*, not the registration.

Two consequences that are deliberate rather than incidental:

* **A process that has not bound a classifier must not claim these tasks.** The
  registration is systemwide; the capability is per process. A worker without
  one narrows its claim list (`Worker(task_types=...)`) instead of claiming
  work it would only fail — see `steward_workers.__main__`.
* **The classifier never receives the handler's transaction.** It is given
  `ClassificationRun`, which carries the identity and cap it needs to checkpoint
  and be bounded, and not `TaskContext`, which would hand a model-facing
  adapter the catalog's open connection.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from pydantic import Field, ValidationError
from steward_queue import (
    Actor,
    ActorKind,
    QueueConnection,
    TaskContext,
    TaskHandler,
    task_handler,
)
from steward_schemas import (
    AssetLifecycle,
    ClassificationProposal,
    ColumnClassification,
    ProblemDetails,
    RunBudget,
    TableProfile,
    TaskResult,
    TaskSpec,
    TaskStatus,
)

from steward_catalog import classification, profiles, repository
from steward_catalog.models import CatalogModel

__all__ = [
    "CLASSIFY_ASSET_SAMPLE_PAYLOAD",
    "CLASSIFY_ASSET_TASK_TYPE",
    "CLASSIFIER",
    "ClassificationRequest",
    "ClassificationRun",
    "ClassifierAlreadyBound",
    "ClassifierBudgetExceeded",
    "ClassifierFailed",
    "ClassifierProvider",
    "ClassifierUnbound",
    "ClassifyAssetPayload",
    "ColumnClassifier",
    "ProposedClassification",
    "build_classify_asset",
    "classifier_bound",
    "classify_state_probe",
    "provide_classifier",
]

_logger = logging.getLogger(__name__)

CLASSIFY_ASSET_TASK_TYPE = "classify_asset"
"""The task type `classify_asset` plans; `steward_orchestration` registers the
goal under the same name. The same string seam `scan_source` and
`profile_asset` use, checked the same way."""

UNCLASSIFIABLE_ASSET = UUID(int=0)
"""The asset id the registry sample names.

`assets.id` is a fresh `uuid4`, so no asset is ever registered under it and the
sample exercises the missing-asset path deterministically -- the device
`scan_source` and `profile_asset` both use, and here it earns a second keep: it
means H1 executes this handler twice **without reaching a model**. A sample that
did would make the idempotency harness depend on a gateway, a budget and a
model's willingness to answer the same way twice.
"""

CLASSIFY_ASSET_SAMPLE_PAYLOAD: dict[str, Any] = {
    "asset_id": str(UNCLASSIFIABLE_ASSET),
    "profile_version": 1,
}

NOTHING_SPENT = RunBudget(steps=0, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(0))
"""What this handler reports having spent: nothing, because its spend is not
its own to report.

The model call belongs to the bound classifier, which charges each increment
where it becomes durable -- inside its own checkpoint transaction, on its own
connection (`steward_workers.agent_tasks.DurableCheckpointStore`, SPEC.md §13
D12/D13). Reporting the same tokens here would bill the run for them twice.
"""


class ClassifyAssetPayload(CatalogModel):
    """`classify_asset`'s task payload: which asset, at which profile version.

    The version is part of the request rather than resolved by the handler,
    because "classify whatever the latest profile happens to be" is a request
    whose answer changes between the client asking and the worker claiming. A
    named version makes the task reproducible and makes staleness a *refusal*
    rather than a silent substitution.
    """

    asset_id: UUID
    profile_version: int = Field(ge=1)


class ClassificationRequest(CatalogModel):
    """Everything a classifier is allowed to see, and the only thing it is given.

    The profile is the whole payload because a `TableProfile` is *already* the
    evidence-only view: every value-carrying field on it is a `MaskedSample`,
    which is the only thing `masking.mask` returns and the only type a profile
    can hold (I6, D10). There is deliberately no second projection re-deriving
    that guarantee by hand -- a hand-written allowlist of "safe" fields is a
    thing to keep in sync with the profile, and the day it falls behind it is
    still green.

    What is absent is the point: no source id, no secret reference, no
    connection, no `RawCell`. A classifier holding this cannot reach a customer's
    database, because nothing here names one.
    """

    asset_id: UUID
    profile_version: int = Field(ge=1)
    profile: TableProfile


class ClassificationRun(CatalogModel):
    """The execution identity a classifier needs, and nothing more.

    Every field is one the classifier genuinely needs: `task_id`/`run_id` to key
    a durable checkpoint, `claimed_by`/`attempts` to fence writes made outside
    the handler's transaction (D7), `trace_id` so its generations land on the
    run's trace (I7), and `budget` because the cap it runs under is the task's,
    already reserved out of the run's (D9).

    `TaskContext` carries all of these and would have been the obvious thing to
    pass. It also carries the handler's open `QueueConnection`, and handing a
    model-facing adapter the catalog's transaction is precisely the privilege
    this seam exists to withhold.
    """

    run_id: UUID
    task_id: UUID
    trace_id: str
    claimed_by: str
    attempts: int = Field(ge=1)
    budget: RunBudget


class ProposedClassification(CatalogModel):
    """What a classifier proposes, plus the provenance only it knows.

    `prompt_version` and `model_alias` come from the classifier rather than from
    the model: they are facts about *which classifier ran*, and a model asked to
    state them could say anything. `asset_id` and `profile_version` are absent
    for the stronger version of the same reason -- they are the handler's, read
    from the request it built, so a classifier cannot propose a classification
    of some other asset.
    """

    columns: tuple[ColumnClassification, ...] = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    model_alias: str = Field(min_length=1)


class ColumnClassifier(Protocol):
    """Evidence in, proposed columns out. The seam the model call hides behind.

    Structural, so nothing that implements it imports this module, and narrow,
    so nothing that implements it can do anything else through it: there is no
    connection on either side of the signature, no gateway, no runtime, and no
    way to ask for a value the profile does not already publish.
    """

    async def classify(
        self, run: ClassificationRun, request: ClassificationRequest
    ) -> ProposedClassification: ...


class ClassifierFailed(RuntimeError):
    """The bound classifier could not produce a proposal.

    The seam's failure vocabulary is declared here, in catalog terms, for the
    same reason its success vocabulary is: `steward-catalog` cannot catch a
    `steward-agents` exception without importing the package I4 forbids it. An
    adapter converts its own failures -- a refused model call, a gateway that
    would not answer, output that never validated -- into this, and the handler
    turns it into a typed `TaskResult` rather than a `handler raised`.
    """


class ClassifierBudgetExceeded(ClassifierFailed):
    """A step was refused because the task's cap could not afford it.

    Separate from its parent because it is the budget *working*, and an operator
    reading the trail should see a cap rather than go looking for a bug (I12).
    """


class ClassifierUnbound(RuntimeError):
    """This process has no classifier, so it cannot execute `classify_asset`.

    Reaching this is a wiring bug rather than an operational condition: a worker
    without the capability is supposed to leave these tasks unclaimed, not claim
    them and discover it here.
    """


class ClassifierAlreadyBound(RuntimeError):
    """A second classifier was supplied to a process that already had one.

    Refused rather than accepted, because the two plausible readings of a silent
    rebind are both bad: tasks claimed before it ran under a different prompt
    and model than tasks claimed after, and nothing in the proposal rows says
    which. Composition happens once, at the root.
    """


class ClassifierProvider:
    """Single-assignment holder for this process's `ColumnClassifier`.

    Not a mutable module global with a setter: assignment is once and only once,
    a second one raises, and the only way to put a different classifier in place
    is `overridden`, which restores the previous one on exit. That is what keeps
    a test's stub from leaking into the next test, and what makes "the worker
    binds at boot" a checkable statement rather than a convention.
    """

    def __init__(self) -> None:
        self._classifier: ColumnClassifier | None = None

    @property
    def bound(self) -> bool:
        """Whether this process can execute `classify_asset` at all.

        Read by the worker's composition root to decide its claim list -- a
        process answering False must not claim these tasks.
        """
        return self._classifier is not None

    def provide(self, classifier: ColumnClassifier) -> None:
        """Bind the process's classifier. Once."""
        if self._classifier is not None:
            raise ClassifierAlreadyBound(
                "a classifier is already bound to this process; composition happens "
                "once, at the root, or proposals stop saying which one produced them"
            )
        self._classifier = classifier

    def get(self) -> ColumnClassifier:
        if self._classifier is None:
            raise ClassifierUnbound(
                "no classifier is bound to this process, so `classify_asset` cannot "
                "run here; a worker without the capability should not claim it"
            )
        return self._classifier

    @contextmanager
    def overridden(self, classifier: ColumnClassifier | None) -> Iterator[None]:
        """Bind `classifier` for the duration of the block, then put back what
        was there. `None` unbinds for the duration.

        For tests, and shaped so that it cannot be the way production binds:
        production calls `provide`, which refuses a second assignment, while
        this restores rather than accumulates. A deterministic classifier
        installed here needs no gateway, no credentials and no model.

        `None` is admitted because the two things worth testing about a
        single-assignment provider both need it: that a composition root's
        `provide` really reaches *this* object, and that a process starts
        without a capability. Both would otherwise need a reset, and a public
        reset is the assignment guarantee with a hole in it.
        """
        previous = self._classifier
        self._classifier = classifier
        try:
            yield
        finally:
            self._classifier = previous


CLASSIFIER = ClassifierProvider()
"""The process's classifier. Bound by a composition root; read by the handler."""


def provide_classifier(classifier: ColumnClassifier) -> None:
    """Bind this process's classifier. Called once, by a composition root."""
    CLASSIFIER.provide(classifier)


def classifier_bound() -> bool:
    """Whether this process can execute `classify_asset`."""
    return CLASSIFIER.bound


def _actor(spec: TaskSpec) -> Actor:
    return Actor(kind=ActorKind.AGENT, id=f"{spec.task_type}:{spec.task_id}")


def _problem(problem_type: str, title: str, detail: str, status: int) -> ProblemDetails:
    return ProblemDetails(type=problem_type, title=title, status=status, detail=detail)


def _failed(spec: TaskSpec, error: ProblemDetails) -> TaskResult:
    return TaskResult(
        task_id=spec.task_id, status=TaskStatus.FAILED, usage=NOTHING_SPENT, error=error
    )


def classify_state_probe(conn: QueueConnection, spec: TaskSpec) -> object:
    """Every proposal stored for this task's asset, id- and clock-free.

    The default probe reads the task's result and checkpoints. This handler
    writes neither -- its output is a `classification_proposals` row -- so
    without this, H1 would compare two empty readings and vouch for nothing.
    What must not change when the same classification runs twice is the set of
    proposals and their content; the row id, the run and task ids and
    `created_at` differ between two executions by construction.
    """
    try:
        payload = ClassifyAssetPayload.model_validate(dict(spec.payload))
    except ValidationError:
        return {"payload": "invalid"}
    proposals = classification.proposal_history(conn, payload.asset_id)
    return {
        "proposals": [
            {
                "version": record.version,
                "profile_version": record.profile_version,
                "prompt_version": record.prompt_version,
                "model_alias": record.model_alias,
                "status": record.status.value,
                "proposal": record.proposal.model_dump(mode="json"),
            }
            for record in proposals
        ]
    }


def _prepare(
    conn: QueueConnection, payload: ClassifyAssetPayload
) -> ClassificationRequest | ProblemDetails:
    """Load the one immutable profile version this request names, or refuse.

    Fail closed, in the order the checks become answerable: an asset that is
    gone or retired cannot receive a classification at all; an asset never
    profiled has no evidence to classify from; and a version that is not the
    asset's current one describes data that has since moved, so classifying it
    would publish a finding about a table that no longer looks like that.

    The last check is the reason the payload names a version rather than the
    handler resolving one. `classification.approve` makes the same refusal at
    publication time (`_require_classifiable`); making it here as well means a
    stale request costs nothing rather than costing a model call whose result
    is refused later.
    """
    asset = repository.get_asset(conn, payload.asset_id)
    if asset is None:
        return _problem(
            "urn:steward:asset-not-found",
            "Asset not found",
            f"no catalogued asset {payload.asset_id}",
            404,
        )
    if asset.lifecycle is not AssetLifecycle.ACTIVE:
        return _problem(
            "urn:steward:asset-not-classifiable",
            "Asset not classifiable",
            f"asset {payload.asset_id} is {asset.lifecycle.value}; "
            "a classification of a relation the source no longer has is not a finding",
            409,
        )
    latest = profiles.latest_profile(conn, payload.asset_id)
    if latest is None:
        return _problem(
            "urn:steward:profile-not-found",
            "Asset has no profile",
            f"asset {payload.asset_id} has never been profiled; "
            "there is no evidence to classify from",
            409,
        )
    if latest.version != payload.profile_version:
        return _problem(
            "urn:steward:stale-profile-version",
            "Stale profile version",
            f"profile version {payload.profile_version} was requested, but the asset's "
            f"current version is {latest.version}; the data it describes has changed",
            409,
        )
    if not latest.profile.columns:
        # Postgres permits a relation with no columns and this catalog profiles
        # one -- `TableProfile(row_count=n, columns=())` (`test_profiler.py`) --
        # so this is a state the system really reaches, not a defensive branch.
        #
        # Refused here rather than left to fail later, because there is no output
        # that could satisfy the contract: a proposal must classify exactly the
        # profiled columns *and* carry at least one, and no set is both empty and
        # non-empty. Without this the task would spend a model call, spend its one
        # correction on a validation error the model cannot fix, and end as
        # `classifier-failed` -- an error naming the classifier for a property of
        # the asset.
        #
        # Admitting an empty proposal instead would be the larger change, and the
        # wrong one at this size: `ClassificationProposal` and the persistence
        # schema both require a non-empty proposal, and "reviewed and approved
        # that nothing is sensitive" would become a publishable claim about a
        # relation with nothing in it to be sensitive.
        return _problem(
            "urn:steward:no-classifiable-columns",
            "Asset has no columns to classify",
            f"profile version {latest.version} of asset {payload.asset_id} records no "
            "columns; a classification of nothing is not a finding",
            409,
        )
    return ClassificationRequest(
        asset_id=payload.asset_id,
        profile_version=latest.version,
        profile=latest.profile,
    )


def _require_full_coverage(
    request: ClassificationRequest, proposed: ProposedClassification
) -> ProblemDetails | None:
    """The proposal must classify **exactly** the profile's columns.

    Set equality, and each half closes a hole nothing else does.

    *Missing columns.* Every other check here is about whether a column's labels
    are supportable; none of them notices a column that was never labelled at
    all. Without this, a classifier returning one column of a three-column table
    produced a `pending_review` proposal, a `SUCCEEDED` task and an asset that
    reads as classified, while two columns had not been assessed. A reviewer
    approving it publishes that silence as a finding.

    *Unknown columns.* An invented column labelled `none` carries no evidence --
    the type only requires it for sensitive labels -- so the resolver, which
    walks citations, never sees it. Only a column labelled *sensitive* was
    caught, which means the check that existed covered the case a careless model
    would fail and missed the one it would pass.

    Names, not counts. A count comparison agrees with itself whenever a model
    drops one column and invents another, which is the shape most likely to
    reach here.
    """
    profiled = {column.name for column in request.profile.columns}
    classified = {column.column_name for column in proposed.columns}
    if classified == profiled:
        return None
    missing = sorted(profiled - classified)
    unknown = sorted(classified - profiled)
    said = [
        part
        for part in (
            f"never classified {', '.join(missing)}" if missing else "",
            f"classified {', '.join(unknown)}, which the profile does not contain"
            if unknown
            else "",
        )
        if part
    ]
    return _problem(
        "urn:steward:classification-column-mismatch",
        "Classification does not cover the profile",
        f"a classification of profile version {request.profile_version} must cover "
        f"exactly its {len(profiled)} column(s); this one {' and '.join(said)}",
        422,
    )


def _run_of(ctx: TaskContext) -> ClassificationRun:
    return ClassificationRun(
        run_id=ctx.spec.run_id,
        task_id=ctx.spec.task_id,
        trace_id=ctx.trace_id,
        claimed_by=ctx.claimed_by,
        attempts=ctx.attempts,
        budget=ctx.spec.budget,
    )


async def _classify(ctx: TaskContext, provider: ClassifierProvider) -> TaskResult:
    """Prepare, classify, validate, persist -- refusing at the first failure.

    Every exit is a typed `TaskResult`. A raise here would reach the queue as
    `handler raised` and an operator reading the trail would look for a bug
    where there is a stale profile, an unresolvable citation, or a model that
    answered with something no reviewer could check.
    """
    spec = ctx.spec
    try:
        payload = ClassifyAssetPayload.model_validate(dict(spec.payload))
    except ValidationError as exc:
        return _failed(
            spec,
            _problem(
                "urn:steward:invalid-task-payload",
                "Invalid classification payload",
                f"{spec.task_type} payload does not name an asset and profile version: "
                f"{exc.error_count()} error(s)",
                422,
            ),
        )

    prepared = _prepare(ctx.connection, payload)
    if isinstance(prepared, ProblemDetails):
        return _failed(spec, prepared)

    try:
        classifier = provider.get()
    except ClassifierUnbound as exc:
        return _failed(
            spec,
            _problem(
                "urn:steward:classifier-unbound",
                "No classifier bound",
                str(exc),
                503,
            ),
        )

    try:
        proposed = await classifier.classify(_run_of(ctx), prepared)
    except ClassifierBudgetExceeded as exc:
        return _failed(
            spec,
            _problem("urn:steward:budget-exceeded", "budget_exceeded", str(exc), 422),
        )
    except ClassifierFailed as exc:
        return _failed(
            spec,
            _problem("urn:steward:classifier-failed", "Classifier failed", str(exc), 500),
        )

    mismatch = _require_full_coverage(prepared, proposed)
    if mismatch is not None:
        return _failed(spec, mismatch)

    try:
        proposal = ClassificationProposal(
            asset_id=prepared.asset_id,
            profile_version=prepared.profile_version,
            prompt_version=proposed.prompt_version,
            model_alias=proposed.model_alias,
            columns=proposed.columns,
        )
    except ValidationError as exc:
        # The classifier's own validation should have caught this; that it did
        # not is why the boundary is checked twice. `none`-exclusivity, evidence
        # per sensitive label and same-column citations are properties of the
        # type (I3), so an output that violates one cannot become a row.
        return _failed(
            spec,
            _problem(
                "urn:steward:invalid-classification",
                "Invalid classification",
                f"the classifier's output is not a publishable proposal: "
                f"{exc.error_count()} error(s)",
                422,
            ),
        )

    try:
        record = classification.propose(
            ctx.connection,
            proposal,
            run_id=spec.run_id,
            task_id=spec.task_id,
            trace_id=ctx.trace_id,
            actor=_actor(spec),
        )
    except classification.EvidenceNotResolvable as exc:
        return _failed(
            spec,
            _problem(
                "urn:steward:unresolvable-evidence",
                "Unresolvable evidence",
                str(exc),
                422,
            ),
        )
    except classification.ClassificationConflict as exc:
        return _failed(
            spec,
            _problem(
                "urn:steward:classification-conflict",
                "Classification conflict",
                str(exc),
                409,
            ),
        )

    return TaskResult(
        task_id=spec.task_id,
        status=TaskStatus.SUCCEEDED,
        usage=NOTHING_SPENT,
        output={
            "proposal_id": str(record.id),
            "asset_id": str(record.asset_id),
            "version": record.version,
            "profile_version": record.profile_version,
            "prompt_version": record.prompt_version,
            "model_alias": record.model_alias,
            "status": record.status.value,
            "sensitive_columns": len(record.proposal.sensitive_columns),
        },
    )


def build_classify_asset(provider: ClassifierProvider) -> TaskHandler:
    """A `classify_asset` handler reading its classifier from `provider`.

    Read per execution rather than captured at build time, because the
    registration happens at import and the binding happens at boot -- capturing
    here would freeze the unbound state the module was imported in.
    """

    async def classify_asset(ctx: TaskContext) -> TaskResult:
        return await _classify(ctx, provider)

    return classify_asset


classify_asset: TaskHandler = task_handler(
    CLASSIFY_ASSET_TASK_TYPE,
    sample_payload=CLASSIFY_ASSET_SAMPLE_PAYLOAD,
    state_probe=classify_state_probe,
)(build_classify_asset(CLASSIFIER))
"""The registered handler.

Registered at import like `scan_source` and `profile_asset`, which is what makes
`classify_asset` a goal the shipped registry can carry. What differs is that the
*capability* it needs is not in this package: a process binds one through
`provide_classifier`, and a process that does not must narrow its claim list
rather than claim these tasks and fail them.
"""
