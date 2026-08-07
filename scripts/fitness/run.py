"""Fitness suite orchestrator (GUARDRAILS.md).

Usage:
    python3 scripts/fitness/run.py [--json] [--stage pre-commit|ci]

Tiers (GUARDRAILS.md §1): S = static architecture checks (some stdlib-only, some
tool-backed via uv), H = behavioral harnesses (pytest markers), B = benchmarks/evals,
G = hygiene. Checks whose prerequisites don't exist yet SKIP with the reason (tool
checks SKIP when the project isn't installed). Exit 1 on FAIL.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

import check_filegraph
import check_loc_budget
import check_prompt_hygiene
from common import CheckResult, repo_root

PYTEST_NO_TESTS_COLLECTED = 5


def _tool_check(check_id: str, name: str, cmd: List[str], skip_reason: str = "",
                skip_exits: Optional[Dict[int, str]] = None) -> CheckResult:
    if skip_reason:
        return CheckResult(check_id, name, "SKIP", [], skip_reason)
    proc = subprocess.run(cmd, cwd=repo_root(), capture_output=True, text=True)
    if proc.returncode == 0:
        return CheckResult(check_id, name, "PASS", [], " ".join(cmd))
    if skip_exits and proc.returncode in skip_exits:
        return CheckResult(check_id, name, "SKIP", [], skip_exits[proc.returncode])
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-15:]
    return CheckResult(check_id, name, "FAIL", [], " ".join(cmd) + "\n    " + "\n    ".join(tail))


def _check_s1(not_installed: str) -> CheckResult:
    """S1 -- boundaries & containment (I2, I4, I9): import-linter contracts for
    layers/edges/schemas-independence, ruff TID251 for framework containment, and an
    isolated import proving steward-schemas pulls in nothing but pydantic + stdlib."""
    if not_installed:
        return CheckResult("S1", "import boundaries", "SKIP", [], not_installed)
    steps = [
        ("lint-imports", ["uv", "run", "lint-imports"]),
        ("ruff TID251", ["uv", "run", "ruff", "check", "--select", "TID251", "."]),
        ("schemas isolation", ["uv", "run", "--isolated", "--package", "steward-schemas",
                                "--no-default-groups", "python3", "-c", "import steward_schemas"]),
    ]
    tails = []
    for label, cmd in steps:
        proc = subprocess.run(cmd, cwd=repo_root(), capture_output=True, text=True)
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
            tails.append(f"{label} ({' '.join(cmd)}):\n    " + "\n    ".join(tail))
    if tails:
        return CheckResult("S1", "import boundaries", "FAIL", [], "\n  ".join(tails))
    return CheckResult("S1", "import boundaries", "PASS", [],
                        "lint-imports + ruff TID251 + isolated steward-schemas import")


def _check_g4() -> CheckResult:
    """G4 -- secret scan. gitleaks is the hard gate in CI (full history); here we run it
    when available locally too, and skip honestly (never PASS) when it isn't installed."""
    if shutil.which("gitleaks") is None:
        return CheckResult("G4", "secret scan", "SKIP", [],
                           "gitleaks not installed locally; CI enforces (full history)")
    proc = subprocess.run(["gitleaks", "detect", "--no-banner"],
                          cwd=repo_root(), capture_output=True, text=True)
    if proc.returncode == 0:
        return CheckResult("G4", "secret scan", "PASS", [], "gitleaks detect")
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-15:]
    return CheckResult("G4", "secret scan", "FAIL", [], "gitleaks detect\n    " + "\n    ".join(tail))


SCRIPT_SKIP_EXIT = 2


def _checker_python() -> str:
    """The interpreter the stdlib checks run under.

    They must work on a fresh clone before `uv sync`, so they stay 3.9-compatible
    and fall back to whatever python3 launched the runner. But an older
    interpreter cannot parse the project's own 3.12 syntax, and a checker that
    cannot parse a file cannot vouch for it (issue #35). Prefer the venv's
    interpreter whenever it exists so the checks actually see the code.
    """
    venv = repo_root() / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _script_or_pending(check_id: str, name: str, script: str) -> CheckResult:
    """Checks specced in GUARDRAILS.md that land via their own issue.

    Exit codes: 0 PASS, 1 FAIL, 2 SKIP. Without the SKIP code a check that
    couldn't run (no toolchain, no baseline) exits 0 and reads as PASS in
    this table — green for work it didn't do (GUARDRAILS.md §3)."""
    path = Path(__file__).parent / script
    if not path.exists():
        return CheckResult(check_id, name, "SKIP", [], f"{script} not implemented yet (tracked as an issue)")
    proc = subprocess.run([_checker_python(), str(path)], cwd=repo_root(), capture_output=True, text=True)
    status = {0: "PASS", SCRIPT_SKIP_EXIT: "SKIP"}.get(proc.returncode, "FAIL")
    detail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else script
    return CheckResult(check_id, name, status, [], detail)


def main() -> int:
    as_json = "--json" in sys.argv
    stage = "ci"
    if "--stage" in sys.argv:
        stage = sys.argv[sys.argv.index("--stage") + 1]

    root = repo_root()
    installed = (root / "pyproject.toml").exists() and shutil.which("uv") is not None
    not_installed = "" if installed else "project not installed yet (needs pyproject.toml + uv)"
    has_packages = (root / "packages").exists()
    has_tests = has_packages and any((root / "packages").rglob("test_*.py"))
    has_evals = (root / "evals").exists()
    no_harness = {PYTEST_NO_TESTS_COLLECTED: "no tests with this marker yet"}

    results: List[CheckResult] = [
        # Tier S — static architecture checks
        _check_s1(not_installed),        # S1
        check_loc_budget.run(),          # S2
        _tool_check("S3", "sql safety", ["uv", "run", "ruff", "check", "--select", "S608", "."],
                    not_installed),
        check_prompt_hygiene.run(),      # S4
        _script_or_pending("S5", "public-surface lock", "check_surface.py"),
        _script_or_pending("S6", "contract compatibility", "check_contracts.py"),
        check_filegraph.run(),           # S7
        _script_or_pending("S8", "checker self-tests", "check_selftests.py"),
        # S9 -- inference endpoint allowlist (I15). Not a lint: this runs the startup
        # refusal itself over the committed LiteLLM config, so the check and the thing
        # a process does at boot are the same code.
        _tool_check("S9", "inference endpoints", ["uv", "run", "python", "-m", "steward_llm.validate"],
                    not_installed),
        # Tier H — behavioral harnesses
        _tool_check("H*", "invariant harnesses", ["uv", "run", "pytest", "-q", "-m", "invariants"],
                    not_installed or ("" if has_tests else "no tests yet"), no_harness),
        _tool_check("H11", "acceptance scenarios", ["uv", "run", "pytest", "-q", "-m", "acceptance"],
                    not_installed or ("" if has_tests else "no tests yet"), no_harness),
        # Tier B — benchmarks & evals
        _tool_check("B*", "eval gates", ["uv", "run", "steward", "evals", "run", "--changed"],
                    "" if has_evals else "activates in M2 (no evals/ yet)"),
        # Hygiene
        _tool_check("G1", "lint & format", ["uv", "run", "ruff", "check", "."], not_installed),
        _tool_check("G2", "strict types", ["uv", "run", "mypy", "--strict", "packages", "services"],
                    not_installed or ("" if has_packages else "no packages/ yet")),
        _tool_check("G3", "tests & coverage",
                    ["uv", "run", "pytest", "-q", "-m", "not acceptance",
                     "--cov=packages", "--cov-branch", "--cov-fail-under=85"],
                    not_installed or ("" if has_tests else "no tests yet")),
        _check_g4(),                     # G4
    ]
    if not installed and stage == "ci" and (root / "pyproject.toml").exists():
        # uv missing in CI would silently skip real gates — that is a failure, not a skip.
        results.append(CheckResult("G", "toolchain", "FAIL", [], "pyproject.toml exists but uv not found in CI"))

    if as_json:
        print(json.dumps([r._asdict() for r in results], default=lambda o: o._asdict(), indent=2))
    else:
        print(f"Fitness suite (stage: {stage})")
        print("-" * 64)
        for r in results:
            print(f"  {r.check_id:<4} {r.name:<24} {r.status:<5} {r.detail}")
            for f in r.findings:
                print(f"       {f.path}:{f.line}: {f.message}")
        pragmas = sum(r.pragma_count for r in results)
        if pragmas:
            print(f"  escape-hatch pragmas in use: {pragmas} (each requires a reason; increases need review)")
        print("-" * 64)
    failed = [r for r in results if r.status == "FAIL"]
    if failed:
        print(f"FAIL: {', '.join(r.check_id for r in failed)} — see GUARDRAILS.md")
        return 1
    print("fitness: all checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
