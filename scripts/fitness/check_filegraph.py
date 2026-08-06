"""S7 — File-graph coverage (staleness leash).

filegraph.json declares, for every file pattern, which files are impacted when
it changes. Propagation is workflow law (CLAUDE.md); this check enforces the
mechanical half: every tracked (and new untracked) file must match at least one
edge key or leaf pattern, so nothing can exist outside the graph and rot
silently.
"""
from __future__ import annotations

import json
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path
from typing import List

from common import CheckResult, Finding, repo_root


def _repo_files(root: Path) -> List[str]:
    tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                               cwd=root, capture_output=True, text=True)
    if tracked.returncode != 0:
        return []
    return [f for f in (tracked.stdout + untracked.stdout).splitlines() if f.strip()]


def run() -> CheckResult:
    root = repo_root()
    cfg = json.loads((Path(__file__).parent / "filegraph.json").read_text())
    patterns = list(cfg["edges"].keys()) + list(cfg["leaves"])
    files = _repo_files(root)
    if not files:
        return CheckResult("S7", "file-graph coverage", "SKIP", [], "not a git repo yet")
    findings = [Finding(f, 0, "not covered by any filegraph.json pattern — add it to the graph (with its dependents) or to leaves")
                for f in files if not any(fnmatch(f, p) for p in patterns)]
    status = "FAIL" if findings else "PASS"
    return CheckResult("S7", "file-graph coverage", status, findings,
                       f"{len(files)} files vs {len(patterns)} patterns")


if __name__ == "__main__":
    result = run()
    for f in result.findings:
        print(f"{f.path}: {f.message}")
    print(f"S7 {result.status} ({result.detail})")
    sys.exit(1 if result.status == "FAIL" else 0)
