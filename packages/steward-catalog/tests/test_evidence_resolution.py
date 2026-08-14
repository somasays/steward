"""`evidence_problems` — the one definition of "this citation resolves" (#50).

Public and pure, because two callers depend on it agreeing with itself: the
repository refuses a proposal whose evidence does not resolve, and **B2 scores
evidence validity with the same function**. A second implementation in the eval
would let a run report a passing evidence score for citations production would
refuse to persist, which is worse than having no eval — it reports confidence.

Pure, so no database: the stored profile is passed in. The DB-fetching half is
covered against a real Postgres in `test_classification_lifecycle.py`.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from steward_catalog.classification import evidence_problems
from steward_schemas import (
    ClassificationProposal,
    ColumnClassification,
    ColumnProfile,
    EvidenceKind,
    EvidenceRef,
    MaskedSample,
    SemanticType,
    SensitivityLabel,
    TableProfile,
    ValueFrequency,
)

ASSET = UUID("88888888-8888-8888-8888-888888888888")
VERSION = 3
MASKED_EMAIL = "a***@e***.***"


def a_profile() -> TableProfile:
    return TableProfile(
        row_count=2,
        columns=(
            ColumnProfile(
                name="email",
                data_type="text",
                null_count=0,
                null_ratio=Decimal("0.000000"),
                distinct_count=2,
                distinct_ratio=Decimal("1.000000"),
                semantic_type=SemanticType.EMAIL,
                top_values=(
                    ValueFrequency(
                        value=MaskedSample(
                            masked=MASKED_EMAIL,
                            semantic_type=SemanticType.EMAIL,
                            length=15,
                        ),
                        count=1,
                    ),
                ),
            ),
            ColumnProfile(
                name="id",
                data_type="bigint",
                null_count=0,
                null_ratio=Decimal("0.000000"),
                distinct_count=2,
                distinct_ratio=Decimal("1.000000"),
            ),
        ),
    )


def a_reference(**overrides: object) -> EvidenceRef:
    fields: dict[str, object] = {
        "profile_version": VERSION,
        "column_name": "email",
        "kind": EvidenceKind.COLUMN_NAME,
        "locator": "email",
        "detail": "the column is named 'email'",
    }
    fields.update(overrides)
    return EvidenceRef.model_validate(fields)


def a_proposal(*columns: ColumnClassification) -> ClassificationProposal:
    return ClassificationProposal(
        asset_id=ASSET,
        profile_version=VERSION,
        prompt_version="classify_asset@v1",
        model_alias="steward-classify",
        columns=columns,
    )


def sensitive(*evidence: EvidenceRef, column: str = "email") -> ColumnClassification:
    return ColumnClassification(
        column_name=column,
        labels=(SensitivityLabel.PII,),
        confidence=Decimal("0.95"),
        evidence=evidence,
    )


def test_a_citation_of_a_recorded_fact_has_no_problems() -> None:
    """The positive case, first: every negative below is satisfied by a function
    that reports a problem for everything."""
    assert evidence_problems(a_proposal(sensitive(a_reference())), a_profile()) == ()


@pytest.mark.parametrize(
    ("kind", "locator"),
    [
        (EvidenceKind.COLUMN_NAME, "email"),
        (EvidenceKind.DATA_TYPE, "text"),
        (EvidenceKind.SEMANTIC_TYPE, "email"),
        (EvidenceKind.NULL_RATIO, "0.000000"),
        (EvidenceKind.DISTINCT_RATIO, "1.000000"),
        (EvidenceKind.MASKED_SAMPLE, MASKED_EMAIL),
    ],
    ids=lambda value: str(value),
)
def test_every_kind_resolves_against_the_fact_it_names(
    kind: EvidenceKind, locator: str
) -> None:
    """One case per `EvidenceKind`, so a kind added without a resolver is a
    failure here rather than a citation nobody checks."""
    reference = a_reference(kind=kind, locator=locator)

    assert evidence_problems(a_proposal(sensitive(reference)), a_profile()) == ()


def test_a_locator_the_profile_does_not_hold_is_a_problem() -> None:
    """An invented masked sample: the column is real, the value never was."""
    invented = a_reference(kind=EvidenceKind.MASKED_SAMPLE, locator="z***@z***.***")

    [problem] = evidence_problems(a_proposal(sensitive(invented)), a_profile())

    assert "masked_sample" in problem
    assert "z***@z***.***" in problem


def test_a_column_the_profile_lacks_is_a_problem() -> None:
    invented = sensitive(a_reference(column_name="ssn"), column="ssn")

    [problem] = evidence_problems(a_proposal(invented), a_profile())

    assert "has no column 'ssn'" in problem


def test_every_problem_is_reported_not_just_the_first() -> None:
    """The reason this returns a tuple rather than raising.

    A gate scoring a run needs the whole picture; a repository refusing a write
    needs only the first. Returning one problem where there are three would make
    B2's evidence-validity score depend on the order the columns happened to be
    in.
    """
    proposal = a_proposal(
        sensitive(a_reference(kind=EvidenceKind.MASKED_SAMPLE, locator="nope"), column="email"),
        sensitive(a_reference(column_name="ssn"), column="ssn"),
    )

    problems = evidence_problems(proposal, a_profile())

    assert len(problems) == 2
    assert any("masked_sample" in problem for problem in problems)
    assert any("has no column" in problem for problem in problems)


def test_a_column_with_no_evidence_is_not_a_problem_here() -> None:
    """`none` needs no citation; the type refuses a *sensitive* label without one.

    Stated because it is the boundary between the two checks: this function
    resolves citations that exist and says nothing about whether enough of them
    do. A `none` column carrying no evidence is well-formed.
    """
    unlabelled = ColumnClassification(
        column_name="id",
        labels=(SensitivityLabel.NONE,),
        confidence=Decimal("0.99"),
    )

    assert evidence_problems(a_proposal(unlabelled), a_profile()) == ()


def test_an_empty_profile_makes_every_citation_unresolvable() -> None:
    """The pathology guard: scoring against a profile with no columns must not
    read as "all citations resolved". A 0-column fixture agreeing with itself is
    this repository's signature defect."""
    problems = evidence_problems(a_proposal(sensitive(a_reference())), TableProfile(row_count=0))

    assert len(problems) == 1
    assert "has no column 'email'" in problems[0]


def test_the_asset_is_not_consulted() -> None:
    """Pure: the same proposal against the same profile scores the same however
    the asset id was generated. B2 has no database, so anything read off a row
    here would be a dependency the eval cannot satisfy."""
    proposal = a_proposal(sensitive(a_reference()))
    elsewhere = proposal.model_copy(update={"asset_id": uuid4()})

    assert evidence_problems(elsewhere, a_profile()) == evidence_problems(proposal, a_profile())
