"""Shared helpers for fitness checks. Stdlib only; must run on Python 3.9+."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, NamedTuple, Optional

EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".ruff_cache", ".pytest_cache", "dist", "build", ".eggs", "htmlcov",
}

PRAGMA_RE = re.compile(r"#\s*fitness:\s*(allow-[a-z-]+)\b\s*(.*)")


class Finding(NamedTuple):
    path: str
    line: int
    message: str


class CheckResult(NamedTuple):
    check_id: str
    name: str
    status: str  # PASS | FAIL | SKIP
    findings: List[Finding]
    detail: str = ""
    pragma_count: int = 0


def repo_root() -> Path:
    """The repo root is two levels above this file (scripts/fitness/)."""
    return Path(__file__).resolve().parent.parent.parent


def iter_python_files(base: Path) -> Iterator[Path]:
    if not base.exists():
        return
    for path in sorted(base.rglob("*.py")):
        if not any(part in EXCLUDED_DIRS for part in path.parts):
            yield path


def is_test_path(path: Path) -> bool:
    return any(part in ("tests", "test") for part in path.parts) or path.name.startswith("test_")


def effective_loc(path: Path) -> int:
    """Non-blank, non-comment-only lines."""
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def pragma_on_line(source_lines: List[str], lineno: int, pragma: str) -> Optional[str]:
    """Return the pragma reason if `# fitness: <pragma> <reason>` is on the given 1-based line.

    A pragma with no reason returns "" (which callers treat as invalid: reasons are required).
    """
    if 1 <= lineno <= len(source_lines):
        match = PRAGMA_RE.search(source_lines[lineno - 1])
        if match and match.group(1) == pragma:
            return match.group(2).strip()
    return None


def module_name_for_package(package_dir_name: str) -> str:
    """Distribution name -> import name: steward-schemas -> steward_schemas."""
    return package_dir_name.replace("-", "_")
