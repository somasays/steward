"""S8 -- Checker self-tests (guards S5, S6 and any future check with the same
pattern; the checker-of-the-checkers gap, issue #32).

check_contracts.py (~470 lines of breaking/stale/pass classification, four
baseline-resolution paths) and check_surface.py (AST-based leak scanning)
are real software, not mechanical scripts -- and until now their only proof
of correctness was `--selftest`, a flag nothing ran automatically. This
sweeps every fitness check that declares one and fails the suite if any
selftest regresses.

Discovery, not a hardcoded list: any `check_*.py` in this directory (other
than this file) whose source contains the literal `"--selftest" in
sys.argv` is treated as a checker with a selftest and run with that flag.
Grepping for the literal branch, not just the substring "selftest", avoids
a false positive on a script that merely mentions the word (e.g. in a
docstring) without wiring the flag -- and a script added later is picked
up the moment it wires the same flag, no edit here required (GUARDRAILS.md
"harnesses bind to registries").

Each selftest is already self-contained: stdlib-only, no repo state, no
uv/subprocess of its own (check_contracts.py's docstring notes this
explicitly). This sweep only adds the piece that was missing -- something
that runs them without a human remembering to.

Stdlib only, like every Tier S check (Tier S runs before the project can
install itself); the selftests it sweeps are stdlib-only too, so this
stays true transitively.

    python3 scripts/fitness/check_selftests.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

from common import CheckResult, repo_root

SELFTEST_MARKER = '"--selftest" in sys.argv'


def _discover(fitness_dir: Path) -> List[Path]:
    """Scripts in this directory that declare a `--selftest` branch.

    `check_*.py` plus `run.py`: the runner decides the suite's verdict, so its
    own logic is exactly the kind of "real software, not a mechanical script"
    S8 exists to sweep — and it was unswept while it was the one component that
    could turn a skipped check into a green line (issue #74).
    """
    found = []
    candidates = sorted(fitness_dir.glob("check_*.py")) + [fitness_dir / "run.py"]
    for path in candidates:
        if path.name == Path(__file__).name:
            continue
        if SELFTEST_MARKER in path.read_text(encoding="utf-8"):
            found.append(path)
    return found


def run() -> CheckResult:
    fitness_dir = Path(__file__).parent
    scripts = _discover(fitness_dir)
    if not scripts:
        return CheckResult("S8", "checker self-tests", "SKIP", [],
                           "no check_*.py declares --selftest")

    failures: List[str] = []
    passed: List[str] = []
    for script in scripts:
        proc = subprocess.run([sys.executable, str(script), "--selftest"],
                              cwd=repo_root(), capture_output=True, text=True)
        if proc.returncode == 0:
            passed.append(script.name)
        else:
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-20:]
            failures.append(f"{script.name} (exit {proc.returncode}):\n    " + "\n    ".join(tail))

    if failures:
        return CheckResult("S8", "checker self-tests", "FAIL", [], "\n  ".join(failures))
    return CheckResult("S8", "checker self-tests", "PASS", [],
                       f"{len(passed)} checker selftests passed: {', '.join(passed)}")


if __name__ == "__main__":
    result = run()
    print(f"S8 {result.status} ({result.detail})")
    # 0 PASS / 1 FAIL / 2 SKIP -- see run.py::_script_or_pending.
    sys.exit({"PASS": 0, "FAIL": 1, "SKIP": 2}[result.status])
