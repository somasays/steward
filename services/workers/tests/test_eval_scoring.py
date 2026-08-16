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
from steward_workers.evals import classification
from steward_workers.evals.classification import (
    PII_PRECISION_FLOOR,
    PII_RECALL_FLOOR,
    RunOutcome,
)
from steward_workers.evals.scoring import ColumnOutcome, TableScore, confusion, ratio, score_table

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


class TestRetryBoundary:
    """Only a typed transport failure may be retried (#50).

    The rule this replaces matched on message text, which made "a threshold miss
    is never retried" depend on how an exception happened to be worded. These
    assert on *types*, including through the wrapping the I4 seam imposes.
    """

    def test_a_transport_failure_under_the_seam_is_retryable(self) -> None:
        """`ClassifierFailed` is what the catalog seam allows through; the
        original transport error stays reachable on `__cause__`."""
        import httpx
        from steward_catalog import ClassifierFailed
        from steward_workers.evals.classification import EvaluationInfrastructureError
        from steward_workers.evals.harness import _classify_failure

        wrapped = ClassifierFailed("the gateway would not answer")
        wrapped.__cause__ = httpx.ConnectError("connection refused")

        assert isinstance(_classify_failure(wrapped), EvaluationInfrastructureError)

    @pytest.mark.parametrize(
        "message",
        [
            "the agent stopped without calling 'submit_result'",
            "email: masked_sample evidence cites 'nope', which profile version 1 does not contain",
            "connection to the truth was lost",  # names a signature, is not one
            "request timeout policy violated by the model's answer",
        ],
        ids=["malformed", "invalid-evidence", "says-connection", "says-timeout"],
    )
    def test_a_model_failure_is_a_result_however_it_is_worded(self, message: str) -> None:
        """The last two are the point: under message matching they would have
        been retried, because their text contains 'connection' and 'timeout'."""
        from steward_catalog import ClassifierFailed
        from steward_workers.evals.classification import EvaluationResult
        from steward_workers.evals.harness import _classify_failure

        assert isinstance(_classify_failure(ClassifierFailed(message)), EvaluationResult)

    def test_a_completed_5xx_fails_immediately_rather_than_retrying(self) -> None:
        """Documented, chosen behaviour — not an oversight.

        The transport turns any status >= 400 into `CompletionFailed`, a
        `steward-llm` type rather than a transport error, so a 502/503 from the
        proxy is a result and the run fails at once. Retrying it would mean
        recovering the status from a rendered message, which is exactly the
        inspection this boundary replaced. Supporting it properly needs a typed
        status on the failure.
        """
        from steward_catalog import ClassifierFailed
        from steward_workers.evals.classification import EvaluationResult
        from steward_workers.evals.harness import _classify_failure

        wrapped = ClassifierFailed("gateway returned 503 for 'steward-classify': unavailable")

        assert isinstance(_classify_failure(wrapped), EvaluationResult)

    def test_an_exhausted_budget_is_a_result_not_infrastructure(self) -> None:
        """A cap is the product working, and re-rolling it would spend the next
        cap too."""
        from steward_catalog import ClassifierBudgetExceeded
        from steward_workers.evals.classification import EvaluationResult
        from steward_workers.evals.harness import _classify_failure

        assert isinstance(
            _classify_failure(ClassifierBudgetExceeded("out of tokens")), EvaluationResult
        )


class TestTheVerdict:
    """B2's PASS/FAIL, which nothing exercised.

    The scorer was tested and the artifact was tested; the code that turns
    scores into a verdict was not. Measured by mutation: zeroing both thresholds,
    setting `RUNS = 1` and changing `all(...)` to `any(...)` in `_report` left
    **285 tests passing**. Every claim GUARDRAILS' B2 row makes — the 0.95 recall
    floor, the 0.90 precision floor, "three pinned runs, each independently over
    threshold" — could be deleted and this repository stayed green, in the gate
    this branch exists to build.

    None of it needs a model, which is the eval package's own argument for where
    the line falls: "the gate's own behaviour is testable where the thing it
    gates is not".
    """

    def outcome(
        self,
        name: str,
        expected: tuple[SensitivityLabel, ...],
        predicted: tuple[SensitivityLabel, ...],
        *,
        cited: tuple[str, ...] = (),
        confidence: str = "0.9",
    ) -> ColumnOutcome:
        return ColumnOutcome(
            column=name,
            expected=frozenset(expected),
            predicted=frozenset(predicted),
            confidence=Decimal(confidence),
            evidence=cited,
        )

    def scored(
        self,
        *outcomes: ColumnOutcome,
        table: str = "t",
        missing: tuple[str, ...] = (),
        invented: tuple[str, ...] = (),
        evidence_failures: tuple[str, ...] = (),
    ) -> TableScore:
        return TableScore(
            table=table,
            outcomes=outcomes,
            missing_columns=missing,
            invented_columns=invented,
            evidence_failures=evidence_failures,
        )

    def perfect(self) -> TableScore:
        """Four PII columns all found, one negative correctly left alone."""
        return self.scored(
            *(self.outcome(f"p{i}", (PII,), (PII,), cited=("column_name=p",)) for i in range(4)),
            self.outcome("n0", (), ()),
        )

    def test_a_clean_run_passes(self) -> None:
        """The positive case first: without it every assertion below is
        satisfied by a verdict that fails everything."""
        assert RunOutcome(index=1, scores=(self.perfect(),)).passed is True

    def test_a_missed_pii_column_fails_on_recall(self) -> None:
        """Three of four found is 0.75, and the floor is 0.95."""
        scores = self.scored(
            *(self.outcome(f"p{i}", (PII,), (PII,), cited=("column_name=p",)) for i in range(3)),
            self.outcome("p3", (PII,), ()),
        )
        run = RunOutcome(index=1, scores=(scores,))

        assert run.passed is False
        assert any("pii recall" in reason for reason in run.reasons), run.reasons

    def test_a_false_positive_fails_on_precision(self) -> None:
        """All four found plus one column wrongly called PII is 4/5 = 0.8,
        under the 0.90 floor — and recall is a perfect 1.0, so this can only
        fail through precision."""
        scores = self.scored(
            *(self.outcome(f"p{i}", (PII,), (PII,), cited=("column_name=p",)) for i in range(4)),
            self.outcome("n0", (), (PII,), cited=("column_name=n0",)),
        )
        run = RunOutcome(index=1, scores=(scores,))

        assert run.passed is False
        assert any("pii precision" in reason for reason in run.reasons), run.reasons

    def test_the_floors_are_the_published_ones(self) -> None:
        """GUARDRAILS' B2 row quotes these two numbers. A test that only
        compared against the constants would follow them down to zero."""
        assert (PII_RECALL_FLOOR, PII_PRECISION_FLOOR) == (Decimal("0.95"), Decimal("0.90"))

    def test_a_dropped_or_invented_column_fails_the_run(self) -> None:
        """Coverage is part of the verdict, not just of the score: an asset that
        reads as classified with a column nobody assessed is the defect the
        handler's guard exists for."""
        run = RunOutcome(
            index=1, scores=(self.scored(*self.perfect().outcomes, missing=("m",), invented=("i",)),)
        )

        assert run.passed is False
        assert any("missing ('m',)" in reason for reason in run.reasons), run.reasons

    def test_an_unresolvable_citation_fails_the_run(self) -> None:
        """A correct label with an unsupported citation still fails (#50)."""
        run = RunOutcome(
            index=1,
            scores=(self.scored(*self.perfect().outcomes, evidence_failures=("bad locator",)),),
        )

        assert run.passed is False
        assert any("unresolvable" in reason for reason in run.reasons), run.reasons

    def test_one_failing_run_of_three_fails_the_report(self) -> None:
        """**The averaging rule, as code.** #50 requires three runs *each
        independently* over threshold, because a model scoring 0.96 then 0.93
        has not met a 0.95 bar. Changing `all` to `any` in `_report` passes this
        set and fails only here."""
        good = RunOutcome(index=1, scores=(self.perfect(),))
        bad = RunOutcome(index=2, scores=(), result_error="unparseable submission")

        report = classification._report([good, bad, good], ())

        assert report.passed is False
        assert "NOT retried" in report.detail

    def test_three_clean_runs_pass(self) -> None:
        """The pair to the above, so it cannot be met by a report that always
        fails."""
        runs = [RunOutcome(index=i, scores=(self.perfect(),)) for i in (1, 2, 3)]

        assert classification._report(runs, ()).passed is True

    def test_no_runs_at_all_is_not_a_pass(self) -> None:
        """`all([])` is True. An empty run list must not report PASS — the
        purest form of this repository's signature defect."""
        assert classification._report([], ()).passed is False

    def test_the_report_names_the_column_the_runs_disagreed_on(self) -> None:
        """"The runs disagreed on 2 columns" tells a reader nothing; naming the
        column and what each run said is the reason #50 asks for three."""
        agree = self.outcome("stable", (PII,), (PII,), cited=("column_name=stable",))
        runs = [
            RunOutcome(index=1, scores=(self.scored(agree, self.outcome("wobbly", (), (PII,))),)),
            RunOutcome(index=2, scores=(self.scored(agree, self.outcome("wobbly", (), ())),)),
        ]

        detail = classification._report(runs, ()).detail

        assert "t.wobbly" in detail
        assert "t.stable" not in detail

    def test_runs_that_agree_report_no_disagreement(self) -> None:
        """Otherwise the test above is satisfied by naming every column."""
        runs = [RunOutcome(index=i, scores=(self.perfect(),)) for i in (1, 2)]

        assert classification._disagreements(runs) == ()

    def test_the_suite_runs_three_times(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`RUNS = 3` is a published claim, and the loop has to honour it.
        Asserting the constant alone would pass a loop that ran once."""
        calls: list[int] = []

        def fake_one_run(gateway: object, requests: object, *, index: int) -> RunOutcome:
            calls.append(index)
            return RunOutcome(index=index, scores=(self.perfect(),))

        monkeypatch.setattr(classification, "_require_gateway", lambda gateway: None)
        monkeypatch.setattr(classification, "_one_run", fake_one_run)

        report = classification.run_classification(object())  # type: ignore[arg-type]

        assert calls == [1, 2, 3] == list(range(1, classification.RUNS + 1))
        assert report.passed is True
