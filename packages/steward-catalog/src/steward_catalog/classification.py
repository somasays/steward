"""Classification proposals, and the review that publishes one.

**The caller owns the transaction.** Nothing here commits, so a decision, the
status changes it causes and the audit rows that record them belong to one
transaction and settle together or not at all (I7, I8).

Approval is one transaction, not two
------------------------------------
Approving version 5 while version 4 is approved has to demote 4 and promote 5
together. Splitting it -- "reject the old one, then approve the new one" --
would make the operator perform a second action to leave a state nobody asked
for: an asset with *no* current classification, visible to every reader in
between, and permanent if the second action never happens. So the operator
approves a replacement and the supersession is part of what that means.

The order inside the transaction is forced by the database: the partial unique
index allows one `approved` row per asset, so the incumbent is demoted *before*
the target is promoted. Doing it the other way round fails at the index, which
is the right failure but a needless one.

Why an advisory lock rather than a row lock
-------------------------------------------
Decisions serialise per **asset**, on the asset id itself. Locking the currently
approved proposal is the obvious alternative and is wrong in the case that
matters most: on a first approval there is no approved row to lock, so two
concurrent first approvals would each find nothing to contend for, and both
would promote. One of them would then meet the unique index and surface a raw
`UniqueViolation` -- a database error where the product has a decision to
report. The advisory lock exists so the loser gets a typed conflict instead.

The index stays as the final fence. It is what makes "one approved version" true
even for a caller that never came through here.

SQL lives in `_classification_sql` as static constants (I5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from steward_queue import Actor, QueueConnection, write_audit
from steward_schemas import (
    ClassificationProposal,
    ProposalStatus,
    ReviewDecision,
    ReviewOutcome,
)

from steward_catalog import _classification_sql as _sql
from steward_catalog.models import WORKSPACE_ID

__all__ = [
    "PROPOSAL_ENTITY",
    "AssetNotClassifiable",
    "EvidenceNotResolvable",
    "IdempotencyKeyReused",
    "ClassificationConflict",
    "ProposalNotPending",
    "ProposalRecord",
    "StaleProposal",
    "approve",
    "current_classification",
    "propose",
    "proposal_history",
    "record_proposal_reviews",
    "reject",
]

PROPOSAL_ENTITY = "classification_proposal"
FIRST_VERSION = 1


class ClassificationConflict(RuntimeError):
    """Another decision for this asset won.

    Typed, and that is the whole point of the advisory lock: without it the
    loser of two concurrent approvals meets the partial unique index and gets a
    `UniqueViolation`, which tells an API nothing it can turn into a response.
    """


class ProposalNotPending(ClassificationConflict):
    """This proposal has already been decided."""


class StaleProposal(ClassificationConflict):
    """The proposal describes a profile that is no longer the asset's latest.

    Approving it would publish a classification of data that has since changed --
    silently, because nothing about the proposal says so on its face. Refused
    rather than published with a caveat nobody reads.
    """


class AssetNotClassifiable(ClassificationConflict):
    """The asset is gone or inactive, so nothing about it can be published."""


class IdempotencyKeyReused(ClassificationConflict):
    """This key already settled a *different* decision.

    A key identifies one request. Reusing it for another proposal, or for the
    opposite outcome, is a caller bug -- and the dangerous kind: without this
    check a `reject` replayed under an `approve`'s key returned the approved
    record and silently did nothing, so the rejection looked like it had
    happened. Approve and reject are opposite governance actions sharing one key
    index; conflating them is worse than conflating two creates.
    """


class EvidenceNotResolvable(ClassificationConflict):
    """A citation points at a column the cited profile does not contain.

    The type checks that evidence cites *its own* column; only the database
    knows whether that column was in the profile the proposal claims to have
    read. An unresolvable citation is indistinguishable, to a reviewer, from a
    fabricated one (#50).
    """


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    """A `classification_proposals` row, projected."""

    id: UUID
    asset_id: UUID
    version: int
    profile_version: int
    prompt_version: str
    model_alias: str
    status: ProposalStatus
    proposal: ClassificationProposal
    run_id: UUID
    task_id: UUID
    trace_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """A `classification_reviews` row: an event, never edited."""

    id: UUID
    proposal_id: UUID
    outcome: ReviewOutcome
    actor: str
    reason: str
    policy_id: str | None
    decided_at: datetime


def _proposal(row: Sequence[Any]) -> ProposalRecord:
    return ProposalRecord(
        id=row[0],
        asset_id=row[1],
        version=row[2],
        profile_version=row[3],
        prompt_version=row[4],
        model_alias=row[5],
        status=ProposalStatus(row[6]),
        proposal=ClassificationProposal.model_validate(row[7]),
        run_id=row[8],
        task_id=row[9],
        trace_id=row[10],
        created_at=row[11],
    )


def _review(row: Sequence[Any]) -> ReviewRecord:
    return ReviewRecord(
        id=row[0],
        proposal_id=row[1],
        outcome=ReviewOutcome(row[2]),
        actor=row[3],
        reason=row[4],
        policy_id=row[5],
        decided_at=row[6],
    )


def propose(
    conn: QueueConnection,
    proposal: ClassificationProposal,
    *,
    run_id: UUID,
    task_id: UUID,
    trace_id: str,
    actor: Actor,
) -> ProposalRecord:
    """Record `proposal` as `pending_review`, or return the one already there.

    Convergent on `(asset, profile version, prompt version, model alias)`: the
    same effective request cannot produce two publishable proposals, so a
    retried task lands on the row its first attempt wrote instead of a second
    one competing for review (I8).
    """
    _lock(conn, proposal.asset_id)
    _require_resolvable_evidence(conn, proposal)
    existing = conn.execute(
        _sql.SELECT_PROPOSAL_BY_REQUEST,
        {
            "asset_id": proposal.asset_id,
            "profile_version": proposal.profile_version,
            "prompt_version": proposal.prompt_version,
            "model_alias": proposal.model_alias,
        },
    ).fetchone()
    if existing is not None:
        return _proposal(existing)

    latest = conn.execute(
        _sql.SELECT_LATEST_PROPOSAL_VERSION, {"asset_id": proposal.asset_id}
    ).fetchone()
    version = (latest[0] if latest else 0) + FIRST_VERSION
    proposal_id = uuid4()
    row = conn.execute(
        _sql.INSERT_PROPOSAL,
        {
            "id": proposal_id,
            "workspace_id": WORKSPACE_ID,
            "asset_id": proposal.asset_id,
            "version": version,
            "profile_version": proposal.profile_version,
            "prompt_version": proposal.prompt_version,
            "model_alias": proposal.model_alias,
            "proposal": Jsonb(proposal.model_dump(mode="json")),
            "run_id": run_id,
            "task_id": task_id,
            "trace_id": trace_id,
        },
    ).fetchone()
    if row is None:  # pragma: no cover -- the lock above serialises this
        raise ClassificationConflict("another proposal for this request was recorded first")
    record = _proposal(row)
    write_audit(
        conn,
        actor=actor,
        action="classification.proposed",
        entity_type=PROPOSAL_ENTITY,
        entity_id=str(record.id),
        after={
            "asset_id": str(record.asset_id),
            "version": record.version,
            "profile_version": record.profile_version,
            "prompt_version": record.prompt_version,
            "model_alias": record.model_alias,
            "status": record.status.value,
            "sensitive_columns": len(proposal.sensitive_columns),
        },
    )
    return record


def approve(
    conn: QueueConnection,
    proposal_id: UUID,
    *,
    decision: ReviewDecision,
    idempotency_key: str | None = None,
    actor: Actor,
) -> ProposalRecord:
    """Publish `proposal_id`, superseding whatever it replaces, atomically.

    The sequence, and each step is load-bearing:

    1. lock the asset's classification namespace, so decisions serialise;
    2. re-read the target under that lock, because what was pending when the
       operator looked may not be now;
    3. refuse unless it is still `pending_review`;
    4. refuse if the asset is inactive or its profile has moved on;
    5. refuse if a *newer* proposal is already approved -- approving an older
       one would roll the published classification backwards silently;
    6. append the decision;
    7. demote the incumbent, then promote the target (that order, for the
       index);
    8. audit both;
    9. return -- the caller commits, and all of it lands together or none.
    """
    _lock_by_proposal(conn, proposal_id)
    if idempotency_key is not None:
        replayed = _replayed(
            conn, idempotency_key, proposal_id=proposal_id, outcome=ReviewOutcome.APPROVED
        )
        if replayed is not None:
            return replayed

    target = _locked_proposal(conn, proposal_id)
    if target.status is not ProposalStatus.PENDING_REVIEW:
        raise ProposalNotPending(
            f"proposal {proposal_id} is {target.status.value}, not pending review"
        )
    _require_classifiable(conn, target)

    incumbent = _approved(conn, target.asset_id)
    if incumbent is not None and incumbent.version > target.version:
        raise StaleProposal(
            f"version {incumbent.version} is already approved for this asset; "
            f"approving version {target.version} would publish an older classification"
        )

    _record_decision(conn, target, decision, idempotency_key)
    if incumbent is not None:
        _set_status(conn, incumbent, ProposalStatus.SUPERSEDED, actor=actor)
    published = _set_status(conn, target, ProposalStatus.APPROVED, actor=actor)
    return published


def reject(
    conn: QueueConnection,
    proposal_id: UUID,
    *,
    decision: ReviewDecision,
    idempotency_key: str | None = None,
    actor: Actor,
) -> ProposalRecord:
    """Refuse `proposal_id`. The currently approved version is untouched.

    Rejection is not the inverse of approval: it decides one proposal and says
    nothing about what is published, so an asset keeps the classification it had.
    """
    _lock_by_proposal(conn, proposal_id)
    if idempotency_key is not None:
        replayed = _replayed(
            conn, idempotency_key, proposal_id=proposal_id, outcome=ReviewOutcome.REJECTED
        )
        if replayed is not None:
            return replayed

    target = _locked_proposal(conn, proposal_id)
    if target.status is not ProposalStatus.PENDING_REVIEW:
        raise ProposalNotPending(
            f"proposal {proposal_id} is {target.status.value}, not pending review"
        )
    _record_decision(conn, target, decision, idempotency_key)
    return _set_status(conn, target, ProposalStatus.REJECTED, actor=actor)


def current_classification(conn: QueueConnection, asset_id: UUID) -> ProposalRecord | None:
    """The approved classification of `asset_id`, or None if none is published."""
    return _approved(conn, asset_id)


def proposal_history(conn: QueueConnection, asset_id: UUID) -> tuple[ProposalRecord, ...]:
    """Every proposal for `asset_id`, newest version first."""
    rows = conn.execute(_sql.SELECT_PROPOSALS_FOR_ASSET, {"asset_id": asset_id}).fetchall()
    return tuple(_proposal(row) for row in rows)


def record_proposal_reviews(conn: QueueConnection, proposal_id: UUID) -> tuple[ReviewRecord, ...]:
    """Every decision recorded against `proposal_id`, oldest first."""
    rows = conn.execute(_sql.SELECT_REVIEWS_FOR_PROPOSAL, {"proposal_id": proposal_id}).fetchall()
    return tuple(_review(row) for row in rows)


def _require_resolvable_evidence(conn: QueueConnection, proposal: ClassificationProposal) -> None:
    """Every citation must name a column the cited profile actually contains.

    The type validator checks a citation against the proposal's own column name,
    which catches a model citing some *other* column but not one citing a column
    that never existed. #50 asks for evidence "resolvable back to that profile",
    and this is the only place that can resolve it: the profile is a row, and the
    proposal is text until it is checked against one.
    """
    row = conn.execute(
        _sql.SELECT_PROFILE_VERSION,
        {"asset_id": proposal.asset_id, "version": proposal.profile_version},
    ).fetchone()
    if row is None:
        raise EvidenceNotResolvable(
            f"asset {proposal.asset_id} has no profile version {proposal.profile_version} "
            "to resolve this proposal's evidence against"
        )
    profiled = {column["name"] for column in row[0].get("columns", ())}
    cited = {
        reference.column_name
        for column in proposal.columns
        for reference in column.evidence
    } | {column.column_name for column in proposal.columns}
    unknown = sorted(cited - profiled)
    if unknown:
        raise EvidenceNotResolvable(
            f"profile version {proposal.profile_version} has no column(s) "
            f"{', '.join(unknown)}; a citation that resolves to nothing is one a "
            "reviewer cannot check"
        )


def _lock(conn: QueueConnection, asset_id: UUID) -> None:
    conn.execute(_sql.LOCK_ASSET_CLASSIFICATION, {"asset_id": asset_id})


def _lock_by_proposal(conn: QueueConnection, proposal_id: UUID) -> None:
    """Take the asset's lock, given only a proposal.

    Read without a lock first, purely to learn which asset to lock -- the
    authoritative read happens afterwards, under it. A decision made on this
    first read would be exactly the race the lock exists to remove.
    """
    row = conn.execute(_sql.SELECT_PROPOSAL_FOR_UPDATE, {"id": proposal_id}).fetchone()
    if row is None:
        raise LookupError(f"no such proposal: {proposal_id}")
    _lock(conn, _proposal(row).asset_id)


def _locked_proposal(conn: QueueConnection, proposal_id: UUID) -> ProposalRecord:
    row = conn.execute(_sql.SELECT_PROPOSAL_FOR_UPDATE, {"id": proposal_id}).fetchone()
    if row is None:
        raise LookupError(f"no such proposal: {proposal_id}")
    return _proposal(row)


def _approved(conn: QueueConnection, asset_id: UUID) -> ProposalRecord | None:
    row = conn.execute(_sql.SELECT_APPROVED_PROPOSAL, {"asset_id": asset_id}).fetchone()
    return _proposal(row) if row is not None else None


def _require_classifiable(conn: QueueConnection, target: ProposalRecord) -> None:
    """The asset is still active and the cited profile is still its latest."""
    row = conn.execute(_sql.SELECT_ASSET_STATE, {"asset_id": target.asset_id}).fetchone()
    if row is None or row[0] != "active":
        raise AssetNotClassifiable(
            f"asset {target.asset_id} is inactive or absent; nothing about it can be published"
        )
    latest_profile = row[1]
    if latest_profile != target.profile_version:
        raise StaleProposal(
            f"proposal reads profile version {target.profile_version}, but the asset's "
            f"latest is {latest_profile}; the data it describes has changed"
        )


def _replayed(
    conn: QueueConnection,
    idempotency_key: str,
    *,
    proposal_id: UUID,
    outcome: ReviewOutcome,
) -> ProposalRecord | None:
    """The proposal a previous decision under this key settled, if it is *this* one.

    Both halves are checked, and both were missing. A key that settled another
    proposal, or the opposite outcome, is not a replay of this request: it is
    the same key used for a different one. Returning the earlier record then
    told a caller their decision had succeeded when nothing had happened to the
    proposal they named -- a rejection that silently did not reject.
    """
    row = conn.execute(
        _sql.SELECT_REVIEW_BY_KEY, {"idempotency_key": idempotency_key}
    ).fetchone()
    if row is None:
        return None
    review = _review(row)
    if review.proposal_id != proposal_id or review.outcome is not outcome:
        raise IdempotencyKeyReused(
            f"key {idempotency_key!r} already recorded {review.outcome.value} on proposal "
            f"{review.proposal_id}; it cannot also record {outcome.value} on {proposal_id}"
        )
    return _locked_proposal(conn, review.proposal_id)


def _record_decision(
    conn: QueueConnection,
    target: ProposalRecord,
    decision: ReviewDecision,
    idempotency_key: str | None,
) -> None:
    conn.execute(
        _sql.INSERT_REVIEW,
        {
            "id": uuid4(),
            "proposal_id": target.id,
            "outcome": decision.outcome.value,
            "actor": decision.actor,
            "reason": decision.reason,
            "policy_id": decision.policy_id,
            "idempotency_key": idempotency_key,
        },
    )


def _set_status(
    conn: QueueConnection, target: ProposalRecord, status: ProposalStatus, *, actor: Actor
) -> ProposalRecord:
    row = conn.execute(
        _sql.SET_PROPOSAL_STATUS, {"id": target.id, "status": status.value}
    ).fetchone()
    if row is None:  # pragma: no cover -- the row was read under lock above
        raise ClassificationConflict(f"proposal {target.id} vanished mid-decision")
    record = _proposal(row)
    write_audit(
        conn,
        actor=actor,
        action=f"classification.{status.value}",
        entity_type=PROPOSAL_ENTITY,
        entity_id=str(record.id),
        before={"status": target.status.value},
        after={"status": record.status.value, "version": record.version},
    )
    return record
