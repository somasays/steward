"""G4 — Secret scan.

High-signal credential patterns only (low-noise by design; gitleaks can be
layered on in CI later). Scans all tracked-ish text files.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List

from common import EXCLUDED_DIRS, CheckResult, Finding, repo_root

# Patterns built with concatenation so this file never matches itself.
PATTERNS = [
    ("openai-style key", re.compile(r"\bsk-" + r"[A-Za-z0-9_-]{20,}")),
    ("anthropic key", re.compile(r"\bsk-ant-" + r"[A-Za-z0-9_-]{20,}")),
    ("github token", re.compile(r"\bgh[pousr]_" + r"[A-Za-z0-9]{30,}")),
    ("aws access key", re.compile(r"\bAKIA" + r"[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-" + r"[A-Za-z0-9-]{10,}")),
    ("password in DSN", re.compile(r"\b(postgres(?:ql)?|mysql|amqp|redis)://" + r"[^:/\s]+:[^@\s]{3,}@")),
    ("private key block", re.compile(r"-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]
TEXT_SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".json", ".md", ".env", ".sh", ".sql", ".txt", ".cfg", ".ini", ""}
PLACEHOLDER_RE = re.compile(r"(example|placeholder|changeme|xxxx|<[^>]+>|\$\{)", re.IGNORECASE)


def run() -> CheckResult:
    root = repo_root()
    findings: List[Finding] = []
    scanned = 0
    self_path = Path(__file__).resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts) or path.resolve() == self_path:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS:
                if pattern.search(line) and not PLACEHOLDER_RE.search(line):
                    findings.append(Finding(str(path.relative_to(root)), lineno,
                                            f"possible {label} committed to the repo"))
    status = "FAIL" if findings else "PASS"
    return CheckResult("G4", "secret scan", status, findings, f"{scanned} files scanned")


if __name__ == "__main__":
    result = run()
    for f in result.findings:
        print(f"{f.path}:{f.line}: {f.message}")
    print(f"G4 {result.status} ({result.detail})")
    sys.exit(1 if result.status == "FAIL" else 0)
