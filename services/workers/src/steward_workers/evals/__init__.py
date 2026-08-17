"""The eval runner (SPEC.md §9, GUARDRAILS.md Tier B, issue #50's B2).

**Why this lives in a service.** An eval runs the *real* capability, which for
B2 means composing `AgentColumnClassifier` with a validated `GatewayConfig` and a
transport. That is composition-root work: I15 says only a root may decide which
gateway a process reaches, and I4 forbids a package from importing a service, so
a package home is not available — `AgentColumnClassifier` lives here. A dedicated
workspace member would cost import-linter contracts, a manifest and H12 wiring
for a single consumer; when M2's retrieval suites arrive and there are several,
that trade changes and this should move.

**What lives where.** Code here; *data* in `evals/` at the repo root — the
fixture, its labels, and the thresholds. That split is deliberate: a fixture is
reviewed by reading it, and burying it in a Python module makes a labelled
dataset something you diff through code.

The three states this runner can be in, and why the middle one exists
--------------------------------------------------------------------
A suite that cannot reach a model has not passed and has not failed. Collapsing
that into either is how a gate starts lying, and this repository has shipped the
collapse twice (`PROOFS.md` rows 21 and 23). So:

* **PASS** (exit 0) — the suite ran and met its thresholds, or nothing the change
  touched has a suite.
* **INCONCLUSIVE** (exit `EXIT_NO_ENDPOINT`) — a suite was selected and no
  gateway is configured. The fitness runner maps this exit code to `SKIP` with
  the reason printed here (`_tool_check`'s `skip_exits`), so it is visible and
  never reads as green. CI has no local model and must not be permanently red
  for that; a laptop without Ollama running must not be either.
* **FAIL** (exit 1) — the suite ran and missed a threshold, or was *required* and
  could not run. `STEWARD_EVALS_REQUIRED=1` promotes the middle state to this,
  which is what a release job is meant to set. That is #50's "INCONCLUSIVE
  locally and a failure in the designated integration/release job", as a flag —
  and the job does not exist yet (#88), so this promotion is currently reachable
  only by hand.

The distinction that matters: **INCONCLUSIVE is about this machine, FAIL is about
this code.** Issue #74 drew that line for the fitness runner and this is the same
line drawn one level down.
"""

from __future__ import annotations

__all__ = [
    "EXIT_FAILED",
    "EXIT_NO_ENDPOINT",
    "EXIT_NOTHING_SELECTED",
    "EXIT_OK",
    "REQUIRED_ENV",
]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_ENDPOINT = 3
EXIT_NOTHING_SELECTED = 4
"""Exit code for "no suite is affected by this change" (#90).

Distinct from `EXIT_OK` because they are different statements and the gate
renders the code, not the reason: a run that met its thresholds and a run that
did no work both exited 0, so `_tool_check` printed

    B*   eval gates   PASS   uv run steward evals run --changed

for a change that touched nothing an eval measures. On `main`, where the
merge-base is HEAD itself and `--changed` therefore selects nothing, that PASS is
what the headline gate output says while no suite ran, no model was reached and
no fixture was scored — beside S6, which handles the identical "no divergence to
compare" condition honestly with a SKIP.

This module invented `EXIT_NO_ENDPOINT` so "this machine cannot" would not share
a code with "this passed", then let "there was nothing to do" share one with it.
Four states, four codes.

Not 2, which argparse uses for a usage error.
"""
"""Exit code for "a suite was selected and no gateway is configured".

Distinct from 1 so the fitness runner can tell "this machine cannot" from "this
code is wrong" without parsing output. Not 2, which argparse uses for a usage
error — a mistyped command must not read as a missing endpoint.
"""

REQUIRED_ENV = "STEWARD_EVALS_REQUIRED"
"""Set to `1` where an eval is not allowed to be inconclusive.

**Nothing sets it yet — see #88.** A release job is meant to, and none exists:
`.github/workflows/ci.yml` runs the fitness gate and the secret scan, B* is SKIP
on every CI run, and the `live_gateway` marker is never selected. So the
promotion below is implemented and tested here and enforced nowhere, and saying
that plainly is the difference between a gate and a description of one.

A developer's laptop must not set it either: refusing to commit because Ollama is
not running would make the gate something to work around rather than trust.
"""
