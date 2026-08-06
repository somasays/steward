"""Fitness suite orchestrator (GUARDRAILS.md).

Usage:
    python3 scripts/fitness/run.py [--json] [--stage pre-commit|ci]

Tiers (GUARDRAILS.md §1): S = static architecture checks (stdlib, always run),
H = behavioral harnesses (pytest markers), B = benchmarks/evals, G = hygiene.
Checks whose prerequisites don't exist yet SKIP with the reason. Exit 1 on FAIL.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

import check_boundaries
import check_filegraph
import check_loc_budget
import check_prompt_hygiene
import check_secrets
import check_sql_safety
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


def _script_or_pending(check_id: str, name: str, script: str) -> CheckResult:
    """Checks specced in GUARDRAILS.md that land via their own issue."""
    path = Path(__file__).parent / script
    if not path.exists():
        return CheckResult(check_id, name, "SKIP", [], f"{script} not implemented yet (tracked as an issue)")
    proc = subprocess.run([sys.executable, str(path)], cwd=repo_root(), capture_output=True, text=True)
    status = "PASS" if proc.returncode == 0 else "FAIL"
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
        check_boundaries.run(),          # S1
        check_loc_budget.run(),          # S2
        check_sql_safety.run(),          # S3
        check_prompt_hygiene.run(),      # S4
        _script_or_pending("S5", "public-surface lock", "check_surface.py"),
        _script_or_pending("S6", "contract compatibility", "check_contracts.py"),
        check_filegraph.run(),           # S7
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
        _tool_check("G2", "strict types", ["uv", "run", "mypy", "--strict", "packages"],
                    not_installed or ("" if has_packages else "no packages/ yet")),
        _tool_check("G3", "tests & coverage",
                    ["uv", "run", "pytest", "-q", "-m", "not acceptance",
                     "--cov=packages", "--cov-branch", "--cov-fail-under=85"],
                    not_installed or ("" if has_tests else "no tests yet")),
        check_secrets.run(),             # G4 (bootstrap until gitleaks wiring lands)
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
