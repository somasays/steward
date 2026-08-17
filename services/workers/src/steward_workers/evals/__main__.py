"""`steward evals run [suite]` — the command Tier B's gate invokes.

The console script the fitness runner has always called (`uv run steward evals
run --changed`) and which, until now, did not exist: the B* check skipped on
"no `evals/` yet", so the command was never executed and its absence was never
noticed. Creating the directory was therefore enough to turn the gate red for a
reason unrelated to eval quality — which is what happened while this was being
built, and is recorded in `PROOFS.md`.

**The gateway config is validated before anything else happens**, exactly as
`steward_api.__main__` and `steward_workers.__main__` do it. Not decoration: H12
boots every `[project.scripts]` target in every service and asserts each refuses
a routing table that reaches off the allowlist (I15). A new entry point is on
that leash the moment it is declared, and it should be — this one reaches a model
by design.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from steward_llm import gateway_config_from_env

from steward_workers.evals import (
    EXIT_FAILED,
    EXIT_NO_ENDPOINT,
    EXIT_NOTHING_SELECTED,
    EXIT_OK,
    REQUIRED_ENV,
)
from steward_workers.evals.classification import (
    CLASSIFICATION_SUITE,
    CLASSIFY_ALIAS,
    NoFixture,
    NoGatewayConfigured,
    Suite,
    run_classification,
)

_logger = logging.getLogger(__name__)

SUITES = (CLASSIFICATION_SUITE,)
"""The suites that exist. B1/B3–B5 land with their milestones (GUARDRAILS.md)."""

DEFAULT_ARTIFACTS = "evals/artifacts"
"""Where per-run results go unless a caller says otherwise.

The help text has always named this path while the default was `None`, so no
invocation of the gate ever wrote one: a suite whose per-column verdicts and
per-run disagreement existed only in a terminal that has since scrolled. #50 asks
for those results as artifacts, and the run that produces them is expensive
enough that not keeping them is the wrong default. Ignored by git — an artifact
is written by whoever ran it, and one committed by accident is a preflight score
in the repository looking like a release result.
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="steward", description="Steward developer commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    evals = commands.add_parser("evals", help="evaluation suites (SPEC.md §9)").add_subparsers(
        dest="evals_command", required=True
    )
    run = evals.add_parser("run", help="run a suite and gate on its thresholds")
    run.add_argument(
        "suite",
        nargs="?",
        choices=[suite.name for suite in SUITES],
        help="which suite to run; omit with --changed to select by what the diff touches",
    )
    run.add_argument(
        "--changed",
        action="store_true",
        help="run only the suites the working tree's changes affect",
    )
    run.add_argument(
        "--artifacts",
        default=DEFAULT_ARTIFACTS,
        help=f"directory to write per-run results into (default: {DEFAULT_ARTIFACTS})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the gateway, select suites, run them, and report honestly.

    Returns rather than raises so the exit code is the whole contract: 0 ran and
    passed, `EXIT_NOTHING_SELECTED` nothing to run, `EXIT_NO_ENDPOINT` selected
    but no model reachable, 1 ran and failed a threshold. Four states, four
    codes — 0 used to cover the first two (#90).
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # I15, first and unconditionally -- **before the arguments are even parsed**.
    # A process handed a routing table that reaches off the allowlist must refuse
    # it whatever it was asked to do, and a usage error must not be able to
    # preempt that refusal. H12 boots every service entry point with a rogue
    # config and asserts each one refuses; when this parsed first, the boot died
    # on argparse's "invalid choice" and the gateway was never examined -- a
    # refusal that depended on the command line being well-formed.
    gateway = gateway_config_from_env()

    args = _parser().parse_args(list(argv) if argv is not None else sys.argv[1:])

    selected = _selected(args)
    if not selected:
        # Not `EXIT_OK` (#90). Nothing ran, so nothing passed, and the gate
        # renders the code rather than this sentence.
        _logger.info(
            "no eval suite is affected by this change — nothing ran, so nothing passed"
        )
        return EXIT_NOTHING_SELECTED

    required = os.environ.get(REQUIRED_ENV, "").strip() == "1"
    outcomes: list[bool] = []
    for suite in selected:
        try:
            report = run_classification(gateway, artifacts=args.artifacts)
        except NoGatewayConfigured as exc:
            if required:
                _logger.error("%s: REQUIRED and could not run — %s", suite.name, exc)
                return EXIT_FAILED
            _logger.warning(
                "%s: INCONCLUSIVE — %s\n"
                "    This is not a pass. The evidence of record is produced against the\n"
                "    deployed architecture, LiteLLM -> vLLM: point STEWARD_LITELLM_CONFIG at a\n"
                "    config binding %s to a vLLM endpoint, approve it via\n"
                "    STEWARD_LLM_APPROVED_ENDPOINTS, and set STEWARD_LLM_PROXY_URL.\n"
                "    evals/config/litellm.preflight-ollama.yaml is a *developer preflight* for\n"
                "    debugging the streaming tool-call path only; a score from it does not\n"
                "    characterise the model any deployment runs and is not this suite's result.\n"
                "    Set %s=1 where an eval is not allowed to be inconclusive.",
                suite.name,
                exc,
                CLASSIFY_ALIAS,
                REQUIRED_ENV,
            )
            return EXIT_NO_ENDPOINT
        except NoFixture as exc:
            # Never a skip: #50 requires that an absent fixture cannot report PASS.
            # Distinguished from the endpoint case because it is a fact about this
            # repository, not about this machine, and REQUIRED does not change it.
            _logger.error("%s: FAIL — %s", suite.name, exc)
            return EXIT_FAILED
        outcomes.append(report.passed)
        _logger.info("%s", report.render())

    return EXIT_OK if all(outcomes) else EXIT_FAILED


def _selected(args: argparse.Namespace) -> tuple[Suite, ...]:
    """Which suites this invocation runs.

    `--changed` is deliberately coarse for now: the classification suite is
    affected by the prompt artifact, the classifier adapter, the fixture and the
    scoring code, and naming those paths is more honest than a dependency graph
    nobody maintains. A suite that cannot decide is *selected*, never skipped —
    the failure direction that reports too much work rather than too little.
    """
    if args.suite is not None:
        return tuple(suite for suite in SUITES if suite.name == args.suite)
    if not args.changed:
        return SUITES
    return tuple(suite for suite in SUITES if suite.affected_by_working_tree())


if __name__ == "__main__":
    raise SystemExit(main())
