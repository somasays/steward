"""G5 — Commit discipline (commit-msg hook).

Conventional Commits, subject <= 100 chars. feat/fix/refactor/perf commits must
reference a GitHub issue (#N) somewhere in the message — work is issue-driven.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TYPES = "feat|fix|refactor|perf|test|docs|chore|ci|build|style|revert"
SUBJECT_RE = re.compile(rf"^({TYPES})(\([a-z0-9._/-]+\))?!?: \S.{{0,98}}$")
ISSUE_REQUIRED = {"feat", "fix", "refactor", "perf"}


def main(msg_file: str) -> int:
    message = Path(msg_file).read_text(encoding="utf-8")
    # Strip comment lines git adds
    lines = [l for l in message.splitlines() if not l.startswith("#")]
    if not lines or not lines[0].strip():
        print("commit-msg: empty message")
        return 1
    subject = lines[0].strip()
    if subject.startswith(("Merge ", "Revert ", "fixup!", "squash!")):
        return 0
    match = SUBJECT_RE.match(subject)
    if not match:
        print(f"commit-msg: subject must be Conventional Commits (<=100 chars):")
        print(f"  got:    {subject}")
        print(f"  wanted: <type>(<scope>): <description>   type in: {TYPES}")
        return 1
    if match.group(1) in ISSUE_REQUIRED and not re.search(r"#\d+", "\n".join(lines)):
        print(f"commit-msg: '{match.group(1)}' commits must reference a GitHub issue (#N).")
        print("  Work is issue-driven (CLAUDE.md): create one with `gh issue create` first.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
