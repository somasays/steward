"""B2 — classification quality (GUARDRAILS.md Tier B, issue #50).

The suite that gates `steward-classify`: PII recall ≥ 0.95, precision ≥ 0.90,
and evidence validity scored separately, over a versioned labelled fixture. Three
pinned runs, **each independently** over threshold, because a model that scores
0.96 once and 0.93 next has not met a 0.95 bar and averaging is how that gets
hidden (#50's nondeterminism amendment).

What this module is careful about
---------------------------------
* **It scores evidence with the code production enforces.**
  `steward_catalog.classification.evidence_problems` is the single definition of
  "this citation resolves"; a second one here could report a passing evidence
  score for citations the repository would refuse to persist.
* **It cannot report PASS without having done the work.** An absent fixture, a
  fixture with no columns, an empty prediction set and a run that reached no
  model are each a refusal, not a quiet zero. Every one of those has been shipped
  as a green check somewhere in this repository's history.
* **It says which machine it could not run on, rather than passing.** No gateway
  is `NoGatewayConfigured`, which the CLI turns into `EXIT_NO_ENDPOINT` — visible
  as a SKIP with its reason, and a hard failure under `STEWARD_EVALS_REQUIRED=1`.

The gateway check happens **before** the fixture is read, deliberately. "This
laptop has no model" and "this repository has no fixture" are different
statements, and reporting the second when the first is true would send whoever
runs it looking for a missing file.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import Field, ValidationError
from steward_catalog.models import CatalogModel
from steward_llm import GatewayConfig
from steward_schemas import (
    ColumnProfile,
    MaskedSample,
    SemanticType,
    SensitivityLabel,
    TableProfile,
    ValueFrequency,
)

from steward_workers.evals import REQUIRED_ENV
from steward_workers.evals.scoring import TableScore, confusion, ratio, score_table

__all__ = [
    "CLASSIFICATION_SUITE",
    "EvalReport",
    "NoFixture",
    "EvaluationInfrastructureError",
    "EvaluationResult",
    "NoGatewayConfigured",
    "Suite",
    "run_classification",
]

CLASSIFY_ALIAS = "steward-classify"

FIXTURE_DIR = Path("evals/classification")
"""Where the labelled fixture lives — *data*, at the repo root, not in this
package. A labelled dataset is reviewed by reading it, and burying it in a Python
module makes that a code review instead of a data review."""

AFFECTING_PATHS = (
    "services/workers/src/steward_workers/prompts/",
    "services/workers/src/steward_workers/classifier.py",
    "services/workers/src/steward_workers/evals/",
    "packages/steward-catalog/src/steward_catalog/classify_handler.py",
    "packages/steward-catalog/src/steward_catalog/classification.py",
    "evals/",
)
"""What makes the classification suite worth re-running.

Named paths rather than a dependency graph, because a graph nobody maintains
answers confidently and wrongly. The list is deliberately generous: a suite that
cannot decide is selected, which costs a run, where the other direction costs a
regression nobody measured.
"""


class NoGatewayConfigured(RuntimeError):
    """No model is reachable, so this suite has neither passed nor failed."""


class NoFixture(RuntimeError):
    """The labelled fixture is absent or empty.

    A failure, never a skip: #50 requires that an absent fixture cannot report
    PASS, and a suite that silently scored nothing would be the purest form of
    this repository's signature defect.
    """


@dataclass(frozen=True, slots=True)
class Suite:
    """One eval suite, and what makes it worth running."""

    name: str

    def affected_by_working_tree(self) -> bool:
        """Whether anything the diff touches could change this suite's score."""
        changed = _changed_paths()
        return any(path.startswith(AFFECTING_PATHS) for path in changed)


@dataclass(frozen=True, slots=True)
class EvalReport:
    """What one invocation of a suite concluded."""

    suite: str
    passed: bool
    detail: str

    def render(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return f"{self.suite}: {verdict}\n{self.detail}"


CLASSIFICATION_SUITE = Suite(name="classification")


PII_RECALL_FLOOR = Decimal("0.95")
PII_PRECISION_FLOOR = Decimal("0.90")
RUNS = 3
"""Three runs, each gated on its own.

A model that scores 0.96 once and 0.93 the next has not met a 0.95 bar, and an
average of the two says it has. Decoding is pinned as far as it can be — the
gateway config sets `temperature: 0` and a seed — but llama.cpp and vLLM batch,
so identical input is only approximately identical output. Three independent
verdicts is the honest response to that; one run plus a claim of determinism is
not.
"""

class EvaluationInfrastructureError(RuntimeError):
    """The run could not reach a working model. **The only retryable failure.**

    Raised by the gateway harness alone, from failure *types* it can name —
    a refused connection, a timeout, an unavailable endpoint. Never inferred
    from the text of an exception: a message-matching rule ("did the error
    mention 'timeout'?") makes "a threshold miss is never retried" a property of
    string formatting, and a model whose refusal happened to contain the word
    would be re-rolled until it read better. #50 forbids exactly that.
    """


class EvaluationResult(RuntimeError):
    """The run completed and the answer was unusable. **Never retryable.**

    Malformed output, an invalid citation, a missed threshold — these are what
    the evaluation *found*. Retrying them is not evaluation; it is sampling until
    the number is acceptable.
    """


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """One of the three runs."""

    index: int
    scores: tuple[TableScore, ...]
    infrastructure_error: str | None = None
    result_error: str | None = None
    """A completed run whose answer was unusable. Separate from
    `infrastructure_error` because only one of the two may be retried, and a
    single field would make that distinction a matter of how it was spelled."""

    @property
    def passed(self) -> bool:
        return self.infrastructure_error is None and not self.reasons

    @property
    def reasons(self) -> tuple[str, ...]:
        if self.infrastructure_error is not None:
            return (f"infrastructure: {self.infrastructure_error}",)
        if self.result_error is not None:
            return (f"result: {self.result_error}",)
        outcomes = tuple(o for score in self.scores for o in score.outcomes)
        failures: list[str] = []
        for score in self.scores:
            if not score.covered_exactly:
                failures.append(
                    f"{score.table}: missing {score.missing_columns}, "
                    f"invented {score.invented_columns}"
                )
            if score.evidence_failures:
                failures.append(
                    f"{score.table}: {len(score.evidence_failures)} unresolvable "
                    f"citation(s): {score.evidence_failures[0]}"
                )
        true_positive, false_positive, false_negative = confusion(outcomes, SensitivityLabel.PII)
        recall = ratio(true_positive, true_positive + false_negative)
        precision = ratio(true_positive, true_positive + false_positive)
        if recall is None or recall < PII_RECALL_FLOOR:
            failures.append(f"pii recall {recall} < {PII_RECALL_FLOOR}")
        if precision is None or precision < PII_PRECISION_FLOOR:
            failures.append(f"pii precision {precision} < {PII_PRECISION_FLOOR}")
        return tuple(failures)


def run_classification(gateway: GatewayConfig | None, *, artifacts: str | None = None) -> EvalReport:
    """Run B2, or say why it could not.

    The gateway is checked first and hardest: an eval that reaches no model has
    produced no evidence, and the one thing it must not do is return a report
    saying so with `passed=True`.
    """
    _require_gateway(gateway)
    assert gateway is not None  # `_require_gateway` refuses None
    fixture = load_fixture()
    requests = tuple(_request(table) for table in fixture.tables)

    runs: list[RunOutcome] = []
    for index in range(1, RUNS + 1):
        runs.append(_one_run(gateway, requests, index=index))
    report = _report(runs, requests)
    if artifacts is not None:
        _persist(runs, artifacts, fixture_version=fixture.version)
    return report


def _one_run(
    gateway: GatewayConfig,
    requests: tuple[_FixtureTable, ...],
    *,
    index: int,
) -> RunOutcome:
    """One independent pass over the whole fixture.

    Retried **once**, and only for infrastructure. #50 is explicit that a
    quality-threshold failure is not retried until green, and the same reasoning
    covers malformed output and invalid evidence: those are what the run found,
    and re-rolling them until they read better is not evaluation.
    """
    for attempt in (1, 2):
        try:
            scores = tuple(_classify_and_score(gateway, table) for table in requests)
        except EvaluationInfrastructureError as exc:
            if attempt == 2:
                return RunOutcome(index=index, scores=(), infrastructure_error=str(exc))
            continue
        except EvaluationResult as exc:
            # A completed run with an unusable answer. Not retried, and not
            # dressed up as infrastructure: it is this run's finding.
            return RunOutcome(index=index, scores=(), result_error=str(exc))
        return RunOutcome(index=index, scores=scores)
    raise AssertionError("unreachable")  # pragma: no cover


class FixtureSample(CatalogModel):
    masked: str
    semantic_type: SemanticType
    length: int


class FixtureColumn(CatalogModel):
    """One labelled column. `why` is prose for a reviewer and is not scored."""

    name: str
    data_type: str
    semantic_type: SemanticType
    null_ratio: Decimal
    distinct_ratio: Decimal
    masked_samples: tuple[FixtureSample, ...] = ()
    expected: tuple[SensitivityLabel, ...] = ()
    why: str


class FixtureTableSpec(CatalogModel):
    name: str
    row_count: int
    columns: tuple[FixtureColumn, ...] = Field(min_length=1)


class Fixture(CatalogModel):
    """The labelled dataset, validated on load.

    A Pydantic model rather than raw JSON because the fixture crosses a seam
    like anything else (I3), and because a malformed one must fail while being
    read rather than score strangely. `min_length=1` on both levels is the
    anti-vacuity rule from #50 expressed where it cannot be forgotten.
    """

    version: str
    description: str
    tables: tuple[FixtureTableSpec, ...] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _FixtureTable:
    """One fixture table: what the classifier is shown, and the right answer."""

    name: str
    profile: TableProfile
    expected: dict[str, frozenset[SensitivityLabel]]


def _request(table: FixtureTableSpec) -> _FixtureTable:
    """Turn a fixture entry into a real `TableProfile`.

    Built through the published contracts rather than hand-shaped dicts: a
    fixture that cannot be expressed as a `TableProfile` is one no classifier
    would ever be given, and this fails while reading it rather than while
    scoring against it.
    """
    profile = TableProfile(
        row_count=table.row_count,
        columns=tuple(
            ColumnProfile(
                name=column.name,
                data_type=column.data_type,
                null_count=int(column.null_ratio * table.row_count),
                null_ratio=column.null_ratio,
                distinct_count=int(column.distinct_ratio * table.row_count),
                distinct_ratio=column.distinct_ratio,
                semantic_type=column.semantic_type,
                top_values=tuple(
                    ValueFrequency(
                        value=MaskedSample(
                            masked=sample.masked,
                            semantic_type=sample.semantic_type,
                            length=sample.length,
                        ),
                        count=1,
                    )
                    for sample in column.masked_samples
                ),
            )
            for column in table.columns
        ),
    )
    return _FixtureTable(
        name=table.name,
        profile=profile,
        expected={column.name: frozenset(column.expected) for column in table.columns},
    )


def _classify_and_score(gateway: GatewayConfig, table: _FixtureTable) -> TableScore:
    """Run the real classifier over one fixture table and score what comes back."""
    from steward_workers.evals.harness import classify_once

    proposal = classify_once(gateway, table.profile)
    return score_table(proposal, table.profile, table.expected, table=table.name)


def _report(
    runs: tuple[RunOutcome, ...] | list[RunOutcome],
    requests: tuple[_FixtureTable, ...],
) -> EvalReport:
    """Every run's verdict, and where the runs disagreed.

    Disagreement is reported per column and label — with confidence and the
    citations each run gave — rather than as a variance figure. "The runs
    disagreed on 2 columns" tells a reader nothing actionable; naming
    `email_domain: run 1 said pii (0.8), runs 2-3 said none` tells them exactly
    which judgement is unstable.
    """
    lines: list[str] = []
    for run in runs:
        verdict = "PASS" if run.passed else "FAIL"
        lines.append(f"  run {run.index}: {verdict}" + ("" if run.passed else ""))
        for reason in run.reasons:
            lines.append(f"      {reason}")
    lines.append("  disagreement across runs:")
    disagreements = _disagreements(runs)
    lines.extend(f"      {line}" for line in disagreements or ("      none",))
    passed = bool(runs) and all(run.passed for run in runs)
    if not passed:
        lines.append(
            "  NOT retried: a quality-threshold failure, malformed output or an "
            "invalid citation is this run's result, not a flake (#50)."
        )
    return EvalReport(suite=CLASSIFICATION_SUITE.name, passed=passed, detail="\n".join(lines))


def _disagreements(runs: tuple[RunOutcome, ...] | list[RunOutcome]) -> tuple[str, ...]:
    """Columns whose label, confidence or citations differ between runs."""
    by_column: dict[str, list[tuple[int, str, str, str]]] = {}
    for run in runs:
        for score in run.scores:
            for outcome in score.outcomes:
                labels = ",".join(sorted(label.value for label in outcome.predicted)) or "none"
                by_column.setdefault(f"{score.table}.{outcome.column}", []).append(
                    (run.index, labels, str(outcome.confidence), "|".join(outcome.evidence))
                )
    lines: list[str] = []
    for column, seen in sorted(by_column.items()):
        distinct = {(labels, confidence, evidence) for _, labels, confidence, evidence in seen}
        if len(distinct) > 1:
            rendered = "; ".join(
                f"run {index}: {labels} (conf {confidence}, cited {evidence or 'nothing'})"
                for index, labels, confidence, evidence in seen
            )
            lines.append(f"{column}: {rendered}")
    return tuple(lines)


PROXY_IMAGE_ENV = "STEWARD_SMOKE_PROXY_IMAGE"
MODEL_REVISION_ENV = "STEWARD_SMOKE_MODEL_REVISION"
"""The same two names the live gateway smoke reads, deliberately.

One run of a release job pins one stack, and two different variable names for
"which proxy image" and "which model revision" is how the smoke and the eval end
up describing different ones while both look pinned.
"""


def _provenance(fixture_version: str) -> dict[str, object]:
    """What produced these numbers, and whether they may be quoted.

    An eval result is only readable next to the stack that produced it: the same
    prompt against a different model revision is a different measurement. The
    fields mirror the live gateway smoke's artifact (`test_live_gateway`), and
    the gateway config is hashed rather than copied because it carries an
    `api_key` reference.

    `release_evidence` is the field that matters most and it is computed, never
    asserted. Unless the stack is pinned *and* the run was required, this is a
    developer preflight — Ollama scores characterise a model no deployment runs
    (`litellm.preflight-ollama.yaml`), and an artifact that did not say so would
    eventually be read as if it were the release result. #50's evidence of record
    is LiteLLM -> vLLM with both pinned.
    """
    config_path = os.environ.get("STEWARD_LITELLM_CONFIG", "")
    digest = (
        hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
        if config_path and Path(config_path).exists()
        else None
    )
    proxy_image = os.environ.get(PROXY_IMAGE_ENV) or None
    model_revision = os.environ.get(MODEL_REVISION_ENV) or None
    required = os.environ.get(REQUIRED_ENV, "").strip() == "1"
    pinned = bool(proxy_image and model_revision)
    return {
        "fixture": fixture_version,
        "model_alias": CLASSIFY_ALIAS,
        "gateway_config_sha256": digest,
        "proxy_image": proxy_image or "unpinned (preflight)",
        "model_revision": model_revision or "unpinned (preflight)",
        "required": required,
        "release_evidence": pinned and required,
        "note": (
            "Release evidence: pinned proxy image and model revision, run with "
            f"{REQUIRED_ENV}=1."
            if pinned and required
            else "NOT release evidence — a developer preflight. These scores "
            "characterise whatever this machine routed to and must not be quoted "
            "as B2's result (#50)."
        ),
    }


def _persist(
    runs: tuple[RunOutcome, ...] | list[RunOutcome],
    target: str,
    *,
    fixture_version: str,
) -> None:
    """Write per-run results so a CI job can keep them as artifacts (#50)."""
    directory = Path(target)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        **_provenance(fixture_version),
        "runs": [
            {
                "index": run.index,
                "passed": run.passed,
                "reasons": list(run.reasons),
                "columns": [
                    {
                        "table": score.table,
                        "column": outcome.column,
                        "expected": sorted(label.value for label in outcome.expected),
                        "predicted": sorted(label.value for label in outcome.predicted),
                        "confidence": str(outcome.confidence),
                        "evidence": list(outcome.evidence),
                    }
                    for score in run.scores
                    for outcome in score.outcomes
                ],
            }
            for run in runs
        ],
        "disagreements": list(_disagreements(runs)),
    }
    (directory / "classification.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def load_fixture() -> Fixture:
    """The labelled fixture, refused rather than defaulted when it is not there.

    Three ways this could be vacuous and each is an error: the file missing, the
    file holding no tables, and a table holding no columns. The last two are the
    model's (`min_length=1`); this raises `NoFixture` for all of them, because
    #50 requires that an absent or empty fixture cannot report PASS.

    Returns the whole `Fixture` rather than its tables so `version` reaches the
    artifact from the file that was actually read. It used to be a literal in
    `_persist`, which would have gone on claiming `@v1` after the fixture was
    revised — evidence naming the wrong dataset is worse than evidence naming
    none, because it looks answerable.
    """
    path = FIXTURE_DIR / "fixture.v1.json"
    if not path.exists():
        raise NoFixture(f"{path} does not exist")
    try:
        fixture = Fixture.model_validate_json(path.read_text())
    except ValidationError as exc:
        raise NoFixture(f"{path} is not a usable fixture: {exc}") from exc
    return fixture


def _require_gateway(gateway: GatewayConfig | None) -> None:
    """Refuse unless a model is actually reachable for `steward-classify`.

    Three separate ways this can be unusable, and each gets its own sentence
    because they send whoever reads it somewhere different: no gateway config at
    all, a config that binds no classifier, and a binding with no endpoint.
    """
    if gateway is None:
        raise NoGatewayConfigured(
            "no gateway is configured (STEWARD_LITELLM_CONFIG is unset)"
        )
    bindings = [binding for binding in gateway.bindings if binding.alias == CLASSIFY_ALIAS]
    if not bindings:
        raise NoGatewayConfigured(
            f"the configured gateway binds no model for {CLASSIFY_ALIAS!r}"
        )
    unreachable = [binding for binding in bindings if binding.api_base is None]
    if len(unreachable) == len(bindings):
        raise NoGatewayConfigured(
            f"every {CLASSIFY_ALIAS!r} binding declares no api_base, so none addresses a server"
        )


def _changed_paths() -> tuple[str, ...]:
    """Repo-relative paths this working tree changes, against the default branch.

    A failure to ask git is not an answer: it returns everything-changed rather
    than nothing-changed, so a broken invocation over-selects instead of quietly
    running no evals at all.
    """
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        working = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return AFFECTING_PATHS
    if diff.returncode != 0 and working.returncode != 0:
        return AFFECTING_PATHS
    paths = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    paths.extend(line[3:].strip() for line in working.stdout.splitlines() if len(line) > 3)
    return tuple(paths)
