"""What the review API returns and accepts (SPEC.md §8, issue #50 step 7).

`classification` holds the domain: what a classifier may propose and what a
review decision means. This module holds the *published projections* of those
things -- a stored proposal as a reader sees it, the decisions recorded against
it, and the one thing a reviewer may send.

Separate from `classification` for the reason `catalog` is separate from
`asset`: a projection composes the domain models and adds the storage identity
they deliberately lack (which row, which version, which run produced it). Keeping
them apart means a change to how the API renders a proposal is not a change to
what a classifier may propose.

Two absences are the design:

* **A reader never sees a proposal without its status.** `Classification` has no
  representation in which "what was proposed" appears without "whether it is
  published", so a client cannot render an unapproved classification as the
  asset's answer by forgetting a field.
* **A reviewer sends a reason and nothing else.** Outcome, actor and time are
  refused on the way in for the reasons `ReviewCommand` states, and `policy_id`
  is refused for one more, stated on `ReviewRequest`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from steward_schemas._base import SchemaModel
from steward_schemas.classification import (
    ClassificationProposal,
    ProposalStatus,
    ReviewOutcome,
)

__all__ = [
    "Classification",
    "ClassificationDetail",
    "ClassificationHistory",
    "ClassificationReview",
    "ReviewRequest",
    "ReviewerKind",
]


class ReviewerKind(StrEnum):
    """Who recorded a decision, as the API publishes it.

    A deliberate duplicate of `steward_queue.ActorKind`, which this package may
    not import (I4: `steward-schemas` depends on pydantic and the standard
    library, and everything depends on it). The duplication is not left to
    drift: `services/api/tests/test_review_routes.py` asserts the two enums have
    the same members, so adding an actor kind on one side fails on the other
    rather than rendering as a value no client's generated types know.
    """

    HUMAN = "human"
    AGENT = "agent"
    POLICY = "policy"
    SYSTEM = "system"


class Classification(SchemaModel):
    """One stored proposal, projected: what was proposed, and where it stands.

    The proposal is nested rather than flattened because it is already the
    contract for "what a classifier proposed", validated on the way in and
    stored verbatim. Flattening would republish every one of its fields under a
    second name, and the day the two disagreed there would be no answer to which
    one the reviewer read.

    What the projection adds is everything the proposal cannot know about
    itself: which row it is, which version of this asset's classification, what
    review has decided so far, and which run, task and trace produced it. That
    last group is the provenance an operator follows backwards -- from a label
    on a column to the model call that proposed it (I7).
    """

    id: UUID
    asset_id: UUID
    version: int = Field(ge=1)
    """This asset's classification version. Append-only and never reused: a
    superseded version keeps its number and its row."""

    status: ProposalStatus
    proposal: ClassificationProposal
    run_id: UUID
    task_id: UUID
    trace_id: str = Field(min_length=1)
    created_at: datetime


class ClassificationReview(SchemaModel):
    """One recorded decision. An event: never edited, never deleted.

    `actor_kind`/`actor_id` are the trusted actor the repository was given, not
    anything a caller supplied, and they are the same pair the audit row carries
    for the same action.
    """

    id: UUID
    proposal_id: UUID
    outcome: ReviewOutcome
    actor_kind: ReviewerKind
    actor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    policy_id: str | None = None
    """Present only on an automatic approval, naming the policy that made it."""

    decided_at: datetime


class ClassificationDetail(SchemaModel):
    """A proposal and every decision recorded against it, oldest first.

    The history is part of the *detail* view and not of the list view because a
    reviewer deciding one proposal needs to see that someone already rejected
    it, while a client listing an asset's versions does not -- and fetching it
    for each would be a query per row.
    """

    classification: Classification
    reviews: tuple[ClassificationReview, ...]


class ClassificationHistory(SchemaModel):
    """Every classification version of one asset, newest first.

    Unpaged, deliberately: versions are bounded by how often an asset is
    re-profiled, so this is tens of rows and not thousands. If that stops being
    true it gains a cursor exactly as `AssetPage` has one.
    """

    items: tuple[Classification, ...]


class ReviewRequest(SchemaModel):
    """What a reviewer sends to approve or reject: why, and nothing else.

    Outcome comes from the endpoint (`:approve` / `:reject`), actor from the
    authenticated caller, time from the database -- the three reasons
    `ReviewCommand` refuses them.

    `policy_id` is absent for a fourth reason, specific to HTTP. An automatic
    approval must be attributable to the policy that made it, and the repository
    enforces that by requiring the policy actor's own id to match; a request
    arriving over this endpoint is attributed to the API's human actor, so any
    policy id a body could carry is one the repository would refuse. Publishing
    the field would advertise a way to record a policy decision that cannot
    work. Auto-approval is a configured policy calling the repository, not a
    request claiming to be one (SPEC.md §3.3).
    """

    reason: str = Field(min_length=1, max_length=1000)
