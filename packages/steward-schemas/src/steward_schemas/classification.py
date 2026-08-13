"""Sensitivity classification: what a Classifier proposes, and how it is judged.

The product shape #50 asks for, as types. Four ideas carry the weight, and each
exists because its absence is a way the feature could be wrong rather than
merely incomplete:

* **A label without evidence is an opinion.** Every sensitive label carries at
  least one `EvidenceRef` naming the profile and column it was read from, and
  validation refuses one that does not. A classifier that says "this is PII"
  and cannot say why is exactly the output a reviewer cannot check, which makes
  human review theatre.
* **`NONE` is exclusive.** "Not sensitive" and "PII" are not two findings about
  one column; they are a contradiction, and a model emitting both is a model
  whose output nobody should publish.
* **A proposal is a version, not a value.** It is tied to the profile, prompt
  and model that produced it, so "why does this column say PII" is answerable
  years later, and a new profile supersedes rather than overwrites.
* **Publication is a review decision, never an agent decision.** The state
  machine has no transition from proposed to published that does not pass
  through a recorded human or policy review (SPEC §3.3).

Deliberately not here: how a proposal is stored, how it is served, or how the
agent produces it. Those are `steward-catalog`, `steward-api` and
`steward-agents` respectively; this package stays free of all three (I3, I4).
The *shapes* the review API publishes -- a stored proposal with its status and
review history -- are contracts and so do live in this package, one module over
in `review`, composed from these models rather than restating them.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import Field, model_validator

from steward_schemas._base import SchemaModel

__all__ = [
    "ClassificationProposal",
    "ColumnClassification",
    "EvidenceKind",
    "EvidenceRef",
    "ProposalStatus",
    "ReviewCommand",
    "ReviewOutcome",
    "SensitivityLabel",
]


class SensitivityLabel(StrEnum):
    """What a column may be classified as (#50).

    Four values, and `NONE` is not one of the sensitive three: it is the
    assertion that none apply. Multiple sensitive labels on one column are
    allowed -- a payments table's `card_holder_name` is plausibly `PII` and
    `FINANCIAL` at once -- because forcing a single label would make the
    classifier choose between two true statements.
    """

    PII = "pii"
    PHI = "phi"
    FINANCIAL = "financial"
    NONE = "none"

    @property
    def is_sensitive(self) -> bool:
        return self is not SensitivityLabel.NONE


class EvidenceKind(StrEnum):
    """Which part of a profile a citation points at.

    The kinds are closed on purpose: an evidence reference has to be resolvable
    back to the exact input, and a free-text "kind" would let the model cite
    something the resolver cannot look up -- an unresolvable citation being
    indistinguishable, to a reviewer, from a fabricated one.
    """

    COLUMN_NAME = "column_name"
    DATA_TYPE = "data_type"
    MASKED_SAMPLE = "masked_sample"
    DISTINCT_RATIO = "distinct_ratio"
    NULL_RATIO = "null_ratio"
    SEMANTIC_TYPE = "semantic_type"


class EvidenceRef(SchemaModel):
    """A pointer into the profile that justified a label.

    `profile_version` and `column_name` are part of the reference rather than
    implied by context, so a reference can be checked against the input it
    claims to come from. Cross-profile and cross-column citations are the two
    failure modes worth catching by construction: a model that cites a column
    it was not asked about, or a profile version it never saw, is one whose
    output is unverifiable however plausible it reads.
    """

    profile_version: int = Field(ge=1)
    column_name: str = Field(min_length=1)
    kind: EvidenceKind
    locator: str = Field(min_length=1, max_length=200)
    """The value being cited, exactly as the profile stores it.

    A *locator*, not a description: `detail` is prose and cannot be resolved, so
    a citation carrying only prose proves the column exists and nothing else --
    `MASKED_SAMPLE` passed whether or not the profile held any such sample. This
    field is checked against the stored profile, per kind:

    | kind | resolves against |
    |---|---|
    | `COLUMN_NAME` | the column's name |
    | `DATA_TYPE` | its `data_type` |
    | `NULL_RATIO` / `DISTINCT_RATIO` | the stored ratio |
    | `SEMANTIC_TYPE` | the stored semantic type |
    | `MASKED_SAMPLE` | one of the column's masked `top_values` |

    So a reviewer following a citation lands on the fact it was drawn from,
    rather than on the classifier's account of it.
    """

    detail: str = Field(min_length=1, max_length=500)
    """Why that value supports the label, in the classifier's words.

    Bounded because it is model output that will be shown to a reviewer and
    stored forever; unbounded prose here is how a proposal row becomes a place
    to hide a paragraph.
    """


class ColumnClassification(SchemaModel):
    """One column's proposed labels, with the evidence for each."""

    column_name: str = Field(min_length=1)
    labels: tuple[SensitivityLabel, ...] = Field(min_length=1)
    confidence: Decimal = Field(ge=0, le=1)
    """How sure the classifier is, as a `Decimal` for the reason every ratio in
    this package is one: two runs over identical input must serialise
    identically, and binary floating point does not promise that."""

    evidence: tuple[EvidenceRef, ...] = ()

    @model_validator(mode="after")
    def _labels_and_evidence_agree(self) -> ColumnClassification:
        """The two rules that make a proposal reviewable.

        Enforced on the type rather than at the persistence boundary so that an
        unreviewable proposal cannot be *constructed*, let alone stored -- the
        agent runtime validates the model's submission against this model, so
        the refusal happens before anything is written (I3).
        """
        if len(set(self.labels)) != len(self.labels):
            raise ValueError(f"{self.column_name}: duplicate labels")
        if SensitivityLabel.NONE in self.labels and len(self.labels) > 1:
            raise ValueError(
                f"{self.column_name}: 'none' says no label applies, so it cannot "
                f"accompany {', '.join(label.value for label in self.labels if label.is_sensitive)}"
            )
        if any(label.is_sensitive for label in self.labels) and not self.evidence:
            raise ValueError(
                f"{self.column_name}: a sensitive label needs at least one evidence "
                "reference; a label nobody can check is not a finding"
            )
        if len(set(self.evidence)) != len(self.evidence):
            raise ValueError(
                f"{self.column_name}: the same evidence is cited twice; a repeated "
                "citation is not a second reason"
            )
        for reference in self.evidence:
            if reference.column_name != self.column_name:
                raise ValueError(
                    f"{self.column_name}: cites column {reference.column_name!r}; "
                    "evidence for a column must come from that column"
                )
        return self

    @property
    def is_sensitive(self) -> bool:
        return any(label.is_sensitive for label in self.labels)


class ProposalStatus(StrEnum):
    """Where a proposal sits in review.

    `PENDING_REVIEW` is the only state a proposal is created in. There is no
    transition to `APPROVED` that is not a recorded `ReviewDecision`, which is
    what makes "no proposal publishes without a review" a property of the state
    machine rather than a rule the API remembers to apply.
    """

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ClassificationProposal(SchemaModel):
    """One classification of one asset, from one profile, by one prompt.

    The provenance fields are not decoration: they are the answer to "why does
    this column say PII", and they are what makes a proposal reproducible. An
    asset re-profiled next month produces a *new* proposal against the new
    profile version; this one keeps saying what was true of the old one.
    """

    asset_id: UUID
    profile_version: int = Field(ge=1)
    prompt_version: str = Field(min_length=1)
    model_alias: str = Field(min_length=1)
    columns: tuple[ColumnClassification, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _columns_are_distinct_and_cite_this_profile(self) -> ClassificationProposal:
        names = [column.column_name for column in self.columns]
        if len(set(names)) != len(names):
            raise ValueError("a column cannot be classified twice in one proposal")
        for column in self.columns:
            for reference in column.evidence:
                if reference.profile_version != self.profile_version:
                    raise ValueError(
                        f"{column.column_name}: cites profile version "
                        f"{reference.profile_version}, but this proposal classifies "
                        f"version {self.profile_version}"
                    )
        return self

    @property
    def sensitive_columns(self) -> tuple[ColumnClassification, ...]:
        return tuple(column for column in self.columns if column.is_sensitive)


class ReviewOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewCommand(SchemaModel):
    """What a reviewer *asks for*, and nothing more.

    Deliberately not a decision: it carries no outcome, no actor and no
    timestamp, because a caller must not be able to supply any of them.

    * **Outcome** comes from the method called. When it lived here,
      `approve(decision=ReviewDecision(outcome=REJECTED))` was representable: it
      recorded a rejection and published the proposal, so the review table and
      the proposal disagreed about what happened.
    * **Actor** comes from the trusted `Actor` the repository is given, the same
      one the audit row uses. When both existed the tests attributed one action
      to `"reviewer"` in the review table and to the system in the audit log --
      the same decision, two different authors.
    * **Time** comes from the database. A caller-supplied timestamp was accepted
      and then ignored, which is worse than refusing it: the field looked
      authoritative and was decoration.

    What remains is what only the reviewer knows: why, and under which policy.
    """

    reason: str = Field(min_length=1, max_length=1000)
    policy_id: str | None = None
    """Set only by an automatic approval, and only by a `POLICY` actor.

    SPEC §3.3 allows auto-approval through an explicit configured policy and
    requires it to be auditable back to that policy. A human decision carrying a
    policy id would be a person claiming a policy approved something -- the one
    attribution this table exists to keep honest -- so the repository refuses it.
    """
