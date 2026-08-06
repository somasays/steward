"""F3 — SQL safety (enforces I5).

Flags SQL assembled from strings: f-strings, %-formatting, .format(), and
string concatenation where a literal contains SQL keywords. Parameterized
templates and static literals are fine.

Escape hatch (requires a written reason, counted in CI):
    query = f"..."  # fitness: allow-sql-string <reason>
"""
from __future__ import annotations

import ast
import re
import sys
from typing import List

from common import CheckResult, Finding, iter_python_files, pragma_on_line, repo_root

SQL_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|GRANT)\s", re.IGNORECASE)
PRAGMA = "allow-sql-string"


def _has_sql(value: object) -> bool:
    return isinstance(value, str) and bool(SQL_RE.search(value))


def _flag_nodes(tree: ast.AST) -> List[ast.AST]:
    flagged = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            # f-string with SQL in its literal parts and at least one interpolation
            literals = "".join(v.value for v in node.values
                               if isinstance(v, ast.Constant) and isinstance(v.value, str))
            has_interp = any(isinstance(v, ast.FormattedValue) for v in node.values)
            if has_interp and SQL_RE.search(literals):
                flagged.append(node)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant) and _has_sql(side.value):
                    other = node.right if side is node.left else node.left
                    if not isinstance(other, ast.Constant):
                        flagged.append(node)
                        break
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "format" \
                and isinstance(node.func.value, ast.Constant) and _has_sql(node.func.value.value):
            flagged.append(node)
    return flagged


def run() -> CheckResult:
    root = repo_root()
    findings: List[Finding] = []
    pragmas = 0
    scanned = 0
    for base in (root / "packages", root / "services", root / "scripts"):
        for path in iter_python_files(base):
            scanned += 1
            source = path.read_text(encoding="utf-8", errors="replace")
            lines = source.splitlines()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue  # F1 reports unparseable files
            for node in _flag_nodes(tree):
                lineno = getattr(node, "lineno", 0)
                reason = pragma_on_line(lines, lineno, PRAGMA)
                if reason:
                    pragmas += 1
                    continue
                if reason == "":
                    findings.append(Finding(str(path.relative_to(root)), lineno,
                                            f"'# fitness: {PRAGMA}' pragma requires a reason"))
                    continue
                findings.append(Finding(str(path.relative_to(root)), lineno,
                                        "SQL assembled from strings — use a parameterized template (I5)"))
    if scanned == 0:
        return CheckResult("F3", "sql safety", "SKIP", [], "no Python code yet")
    status = "FAIL" if findings else "PASS"
    return CheckResult("F3", "sql safety", status, findings,
                       f"{scanned} files scanned", pragma_count=pragmas)


if __name__ == "__main__":
    result = run()
    for f in result.findings:
        print(f"{f.path}:{f.line}: {f.message}")
    print(f"F3 {result.status} ({result.detail}, {result.pragma_count} pragmas)")
    sys.exit(1 if result.status == "FAIL" else 0)
