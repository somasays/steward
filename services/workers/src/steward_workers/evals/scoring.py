"""How a classification run is scored (B2, issue #50).

Separate from the runner because scoring is pure: labels in, metrics out, no
gateway and no database. That makes every rule here testable without a model,
which matters — a scorer nobody can test is a gate nobody should trust.

Three properties are scored, and they are deliberately not combined into one
number:

* **Precision and recall for PII.** A single "accuracy" figure lets a classifier
  that labels everything `pii` look competent on a fixture that is mostly
  sensitive, and lets one that labels nothing look competent on a fixture that is
  mostly not.
* **Evidence validity, separately.** #50 is explicit: a correct label with an
  unsupported citation still fails. Folding it into the label score would let a
  model guess well and cite nothing, which is precisely the output human review
  cannot check.
* **Exact column coverage.** An asset that reads as classified with a column
  nobody assessed is the defect the handler's coverage guard exists for; the
  eval scores it too, because a run that silently dropped a column would
  otherwise post *better* precision for having answered less.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from steward_catalog.classification import evidence_problems
from steward_schemas import ClassificationProposal, SensitivityLabel, TableProfile

__all__ = [
    "ColumnOutcome",
    "TableScore",
    "confusion",
    "score_table",
]


@dataclass(frozen=True, slots=True)
class ColumnOutcome:
    """What a classifier said about one column, against what it should have."""

    column: str
    expected: frozenset[SensitivityLabel]
    predicted: frozenset[SensitivityLabel]
    confidence: Decimal
    evidence: tuple[str, ...]
    """Each citation rendered as `kind=locator`, so two runs can be compared by
    what they *cited* and not merely by what they concluded."""

    @property
    def correct(self) -> bool:
        return self.expected == self.predicted


@dataclass(frozen=True, slots=True)
class TableScore:
    """One table's outcome in one run."""

    table: str
    outcomes: tuple[ColumnOutcome, ...]
    missing_columns: tuple[str, ...]
    invented_columns: tuple[str, ...]
    evidence_failures: tuple[str, ...] = field(default=())

    @property
    def covered_exactly(self) -> bool:
        return not self.missing_columns and not self.invented_columns


def score_table(
    proposal: ClassificationProposal,
    profile: TableProfile,
    expected: dict[str, frozenset[SensitivityLabel]],
    *,
    table: str,
) -> TableScore:
    """Score one proposal against the fixture's labels for that table.

    Coverage is compared by **name**, never by count: a run that drops one column
    and invents another has the right number of answers and is wrong about two of
    them, and that is the shape a model is most likely to produce.

    Evidence is checked with `steward_catalog.classification.evidence_problems`
    — the same function the repository refuses a write with. A private copy here
    could score a citation valid that production would reject, and a gate that
    disagrees with the product reports confidence in nothing.
    """
    predicted = {column.column_name: column for column in proposal.columns}
    missing = tuple(sorted(set(expected) - set(predicted)))
    invented = tuple(sorted(set(predicted) - set(expected)))

    outcomes = tuple(
        ColumnOutcome(
            column=name,
            expected=expected[name],
            predicted=frozenset(
                label for label in predicted[name].labels if label is not SensitivityLabel.NONE
            ),
            confidence=predicted[name].confidence,
            evidence=tuple(
                sorted(f"{ref.kind.value}={ref.locator}" for ref in predicted[name].evidence)
            ),
        )
        for name in sorted(set(expected) & set(predicted))
    )
    return TableScore(
        table=table,
        outcomes=outcomes,
        missing_columns=missing,
        invented_columns=invented,
        evidence_failures=evidence_problems(proposal, profile),
    )


def confusion(
    outcomes: tuple[ColumnOutcome, ...], label: SensitivityLabel
) -> tuple[int, int, int]:
    """True positives, false positives and false negatives for one label.

    Per label rather than per column: a column that is `financial` and `pii` is
    two independent judgements, and scoring the pair as one all-or-nothing answer
    would hide a classifier that reliably finds payment instruments and never
    notices the cardholder behind them.
    """
    true_positive = sum(1 for o in outcomes if label in o.expected and label in o.predicted)
    false_positive = sum(1 for o in outcomes if label not in o.expected and label in o.predicted)
    false_negative = sum(1 for o in outcomes if label in o.expected and label not in o.predicted)
    return true_positive, false_positive, false_negative


def ratio(numerator: int, denominator: int) -> Decimal | None:
    """`numerator / denominator`, or None when the question does not arise.

    **None, never 1.0.** Precision with no positive predictions and recall with no
    positive examples are undefined, and returning a perfect score for them is
    how a gate passes a run that predicted nothing at all — the exact vacuity #50
    forbids. The caller decides what an undefined metric means; it must not
    silently mean "met the threshold".
    """
    if denominator == 0:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))
