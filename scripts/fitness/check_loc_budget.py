"""S2 — Runtime size budget (enforces I9).

packages/steward-agents stays under BUDGET effective LOC (non-blank, non-comment,
tests excluded). If the runtime can't fit, simplify the design — raising the
budget is a GUARDRAILS.md amendment, not an edit here.
"""
from __future__ import annotations

import sys

from common import CheckResult, Finding, effective_loc, is_test_path, iter_python_files, repo_root

BUDGET = 2000
TARGET = "packages/steward-agents"


def run() -> CheckResult:
    base = repo_root() / TARGET
    if not base.exists():
        return CheckResult("S2", "runtime size budget", "SKIP", [], f"{TARGET} not created yet")
    total = sum(effective_loc(p) for p in iter_python_files(base) if not is_test_path(p))
    detail = f"{total}/{BUDGET} effective LOC"
    if total > BUDGET:
        return CheckResult("S2", "runtime size budget", "FAIL",
                           [Finding(TARGET, 0, f"runtime is {total} LOC, budget {BUDGET} (I9)")], detail)
    return CheckResult("S2", "runtime size budget", "PASS", [], detail)


if __name__ == "__main__":
    result = run()
    for f in result.findings:
        print(f"{f.path}: {f.message}")
    print(f"S2 {result.status} ({result.detail})")
    sys.exit(1 if result.status == "FAIL" else 0)
