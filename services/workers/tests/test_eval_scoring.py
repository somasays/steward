"""B2's scorer, tested without a model (#50).

Scoring is pure — labels in, metrics out — so every rule is checkable here. A
gate whose arithmetic nobody verified is a number, not evidence.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from steward_schemas import (
    ClassificationProposal,
    ColumnClassification,
    ColumnProfile,
    EvidenceKind,
    EvidenceRef,
    SensitivityLabel,
    TableProfile,
)
from steward_workers.evals.scoring import ColumnOutcome, confusion, ratio, score_table

pytestmark = pytest.mark.invariants

PII = SensitivityLabel.PII
FIN = SensitivityLabel.FINANCIAL
NONE = SensitivityLabel.NONE
ASSET = __import__("uuid").UUID("88888888-8888-8888-8888-888888888888")


def profile(*names: str) -> TableProfile:
    return TableProfile(
        row_count=10,
        columns=tuple(
            ColumnProfile(
                name=name,
                data_type="text",
                null_count=0,
                null_ratio=Decimal("0.000000"),
                distinct_count=10,
                distinct_ratio=Decimal("1.000000"),
            )
            for name in names
        ),
    )


def proposal(*columns: ColumnClassification) -> ClassificationProposal:
    return ClassificationProposal(
        asset_id=ASSET,
        profile_version=1,
        prompt_version="p@v1",
        model_alias="steward-classify",
        columns=columns,
    )


def classified(name: str, *labels: SensitivityLabel, cite: bool = False) -> ColumnClassification:
    evidence = (
        (
            EvidenceRef(
                profile_version=1,
                column_name=name,
                kind=EvidenceKind.COLUMN_NAME,
                locator=name,
                detail="named so",
            ),
        )
        if cite
        else ()
    )
    return ColumnClassification(
        column_name=name,
        labels=labels or (NONE,),
        confidence=Decimal("0.9"),
        evidence=evidence,
    )


def test_a_correct_run_scores_clean() -> None:
    """The positive case. Every negative below is satisfied by a scorer that
    reports failure for everything."""
    score = score_table(
        proposal(classified("email", PII, cite=True), classified("id")),
        profile("email", "id"),
        {"email": frozenset({PII}), "id": frozenset()},
        table="t",
    )

    assert score.covered_exactly
    assert score.evidence_failures == ()
    assert all(outcome.correct for outcome in score.outcomes)


def test_a_dropped_and_an_invented_column_are_both_named() -> None:
    """Counts would agree with themselves here: one out, one in.

    This is the shape a model actually produces, and comparing lengths is how a
    coverage check passes it.
    """
    score = score_table(
        proposal(classified("email", PII, cite=True), classified("invented")),
        profile("email", "id", "invented"),
        {"email": frozenset({PII}), "id": frozenset()},
        table="t",
    )

    assert score.missing_columns == ("id",)
    assert score.invented_columns == ("invented",)
    assert not score.covered_exactly


def test_evidence_is_scored_by_the_function_production_enforces() -> None:
    """A sensitive label whose citation names nothing in the profile fails,
    even though the label itself is right."""
    score = score_table(
        proposal(
            ColumnClassification(
                column_name="email",
                labels=(PII,),
                confidence=Decimal("0.9"),
                evidence=(
                    EvidenceRef(
                        profile_version=1,
                        column_name="email",
                        kind=EvidenceKind.MASKED_SAMPLE,
                        locator="never-recorded",
                        detail="invented",
                    ),
                ),
            )
        ),
        profile("email"),
        {"email": frozenset({PII})},
        table="t",
    )

    assert score.outcomes[0].correct, "the label is right"
    assert score.evidence_failures, "and the citation is still a failure"


def test_none_is_not_a_predicted_label() -> None:
    """`none` is the assertion that nothing applies, so it must not count as a
    prediction — otherwise every negative column is a false positive for `none`
    and the metrics become meaningless."""
    score = score_table(
        proposal(classified("id")),
        profile("id"),
        {"id": frozenset()},
        table="t",
    )

    assert score.outcomes[0].predicted == frozenset()
    assert score.outcomes[0].correct


def test_each_label_of_a_multi_label_column_is_judged_separately() -> None:
    """A classifier that finds the card and misses the cardholder scores one
    hit and one miss, not a single wrong answer."""
    outcomes = (
        ColumnOutcome(
            column="card",
            expected=frozenset({FIN, PII}),
            predicted=frozenset({FIN}),
            confidence=Decimal("0.9"),
            evidence=(),
        ),
    )

    assert confusion(outcomes, FIN) == (1, 0, 0)
    assert confusion(outcomes, PII) == (0, 0, 1)


def test_an_undefined_metric_is_none_and_never_a_perfect_score() -> None:
    """The vacuity #50 forbids: a run that predicted nothing must not report
    precision 1.0."""
    assert ratio(0, 0) is None
    assert ratio(3, 4) == Decimal("0.7500")
