"""S4 — Prompt hygiene (enforces I10).

Prompts are versioned artifacts in prompts/ directories (or Langfuse-managed),
not string literals in application code. Flags prompt-shaped literals — long
instruction-like strings — outside prompts/ and tests.

Narrow exemption: in a module whose whole job is holding static SQL (`_sql.py`,
or an Alembic revision under `versions/`), a literal that opens with a SQL
statement keyword is exempt from the *length* rule. I5/S3 require SQL to be a
parameterized constant rather than an assembled string, so a long SQL literal
there is the shape this architecture asks for, not a smuggled prompt. Both
conditions are required — a prompt prefixed with `SELECT 1;` in ordinary
application code is still flagged — and the instruction-shape rule applies
everywhere, including inside those modules.

Escape hatch (requires a written reason, counted in CI):
    text = "..."  # fitness: allow-prompt-literal <reason>
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import List

from common import CheckResult, Finding, is_test_path, iter_python_files, pragma_on_line, repo_root

INSTRUCTION_RE = re.compile(
    r"^\s*(You are\b|Your (task|job|role)\b|## (Instructions|Task|Role))", re.IGNORECASE | re.MULTILINE)
SQL_STATEMENT_RE = re.compile(
    r"^\s*(WITH\s|SELECT\s|INSERT\s+INTO\s|UPDATE\s|DELETE\s+FROM\s|TRUNCATE\s"
    r"|CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX|VIEW|SCHEMA|EXTENSION)\s"
    r"|DROP\s+(TABLE|INDEX|VIEW|SCHEMA|EXTENSION)\s|ALTER\s+TABLE\s)", re.IGNORECASE)
LONG_LITERAL = 600
INSTRUCTION_MIN = 150
PRAGMA = "allow-prompt-literal"


def _in_prompts_dir(path: Path) -> bool:
    return "prompts" in path.parts


def _sql_module(path: Path) -> bool:
    """Modules that exist to hold static SQL: the I5 constant-statement pattern."""
    return path.name == "_sql.py" or path.parent.name == "versions"


def run() -> CheckResult:
    root = repo_root()
    findings: List[Finding] = []
    pragmas = 0
    scanned = 0
    for base in (root / "packages", root / "services"):
        for path in iter_python_files(base):
            if _in_prompts_dir(path) or is_test_path(path):
                continue
            scanned += 1
            sql_module = _sql_module(path)
            source = path.read_text(encoding="utf-8", errors="replace")
            lines = source.splitlines()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            # Skip docstring nodes: they legitimately contain instruction-like prose.
            docstring_linenos = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    body = getattr(node, "body", [])
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                        docstring_linenos.add(body[0].value.lineno)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if node.lineno in docstring_linenos:
                    continue
                text = node.value
                exempt = sql_module and SQL_STATEMENT_RE.match(text)
                long_prose = len(text) >= LONG_LITERAL and not exempt
                prompt_shaped = long_prose or (
                    len(text) >= INSTRUCTION_MIN and INSTRUCTION_RE.search(text))
                if not prompt_shaped:
                    continue
                reason = pragma_on_line(lines, node.lineno, PRAGMA)
                if reason:
                    pragmas += 1
                    continue
                if reason == "":
                    findings.append(Finding(str(path.relative_to(root)), node.lineno,
                                            f"'# fitness: {PRAGMA}' pragma requires a reason"))
                    continue
                findings.append(Finding(str(path.relative_to(root)), node.lineno,
                                        "prompt-shaped literal in application code — move to prompts/ (I10)"))
    if scanned == 0:
        return CheckResult("S4", "prompt hygiene", "SKIP", [], "no Python code yet")
    status = "FAIL" if findings else "PASS"
    return CheckResult("S4", "prompt hygiene", status, findings,
                       f"{scanned} files scanned", pragma_count=pragmas)


if __name__ == "__main__":
    result = run()
    for f in result.findings:
        print(f"{f.path}:{f.line}: {f.message}")
    print(f"S4 {result.status} ({result.detail}, {result.pragma_count} pragmas)")
    sys.exit(1 if result.status == "FAIL" else 0)
