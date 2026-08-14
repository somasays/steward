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

import subprocess
from dataclasses import dataclass
from pathlib import Path

from steward_llm import GatewayConfig

__all__ = [
    "CLASSIFICATION_SUITE",
    "EvalReport",
    "NoFixture",
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


def run_classification(gateway: GatewayConfig | None, *, artifacts: str | None = None) -> EvalReport:
    """Run B2, or say why it could not.

    The gateway is checked first and hardest: an eval that reaches no model has
    produced no evidence, and the one thing it must not do is return a report
    saying so with `passed=True`.
    """
    _require_gateway(gateway)
    raise NoFixture(
        f"{FIXTURE_DIR} holds no labelled fixture, so there is nothing to score "
        "(an absent fixture cannot report PASS — #50)"
    )


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
