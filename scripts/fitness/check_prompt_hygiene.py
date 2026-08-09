"""S4 — Prompt hygiene (enforces I10).

Prompts are versioned artifacts in prompts/ directories (or Langfuse-managed),
not string literals in application code. Flags prompt-shaped literals — long
instruction-like strings — outside prompts/ and tests.

Narrow exemption: in a module whose whole job is holding static SQL (a *private*
module whose name ends in `_sql`, or an Alembic revision under `versions/`), a
literal that opens with a SQL statement keyword is exempt from the *length*
rule. I5/S3 require SQL to be a parameterized constant rather than an assembled
string, so a long SQL literal there is the shape this architecture asks for, not
a smuggled prompt. Both conditions are required — a prompt prefixed with
`SELECT 1;` in ordinary application code is still flagged — and the
instruction-shape rule applies everywhere, including inside those modules.

`--selftest` (swept by S8) runs the scan over planted fixtures: an exempt SQL
literal in a private `_sql` module, the same literal in a *public* one that must
not be exempt, and prose in a private one that must still be caught.

Escape hatch (requires a written reason, counted in CI):
    text = "..."  # fitness: allow-prompt-literal <reason>
"""
from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

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
    """Modules that exist to hold static SQL: the I5 constant-statement pattern.

    Any **private** module whose name ends in `_sql`, not only `_sql.py`
    exactly: a package with two SQL surfaces names the second one for what it
    holds (`_profile_sql.py`, the statements profiling runs against a
    *customer's* database, as opposed to `_sql.py`'s statements against
    Steward's own). The narrower spelling flagged the second module's first long
    statement as a prompt -- a check narrower than the rule it enforces, the
    same shape as issue #21's attribute-docstring gap.

    Private is a *condition*, not a description. It was written down in this
    docstring and in GUARDRAILS.md and enforced in neither, so a public
    `zzprobe_sql.py` holding an 833-character literal passed while the same
    literal in `zzprobe.py` failed -- an exemption defended by prose the code
    did not implement (PROOFS rows 69, 73 are the same shape). `--selftest`
    now plants that exact pair. The exemption stays tight on the other axis
    too: it applies only to literals that *begin* with a SQL keyword, so prose
    in such a module is still caught.
    """
    return (path.name.startswith("_") and path.name.endswith("_sql.py")) or path.parent.name == "versions"


def _scan_tree(bases: Tuple[Path, ...], root: Path) -> Tuple[List[Finding], int, List[str], int]:
    """Findings, files scanned, unparsable files and pragma count under `bases`.

    Shared between `run()` (the real repo) and `--selftest` (planted fixtures),
    so the selftest exercises the classification the suite actually runs rather
    than a re-derivation of it.
    """
    findings: List[Finding] = []
    unparsed: List[str] = []
    pragmas = 0
    scanned = 0
    for base in bases:
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
                # Cannot vouch for a file we could not parse. Usually the interpreter
                # running the checks is older than the project's target (see run.py,
                # which prefers the venv's Python), so 3.12 syntax reads as invalid.
                # Silently continuing counted the file as clean -- a PASS for work not
                # done (issue #35, GUARDRAILS §3).
                unparsed.append(str(path.relative_to(root)))
                continue
            # Skip docstring nodes: they legitimately contain instruction-like prose.
            docstring_linenos = set()
            # Exempt from the *length* rule only -- instruction-shaped text is still a
            # prompt wherever it sits. Same shape as the SQL-module exemption above.
            attribute_docstrings: set = set()
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                        docstring_linenos.add(body[0].value.lineno)
                # PEP 258 attribute docstrings: a bare string directly after an
                # assignment, at module or class level. Documentation, not a prompt --
                # missing these made S4 flag `__all__`'s own docstring (issue #21's
                # family: a check narrower than the rule it enforces).
                if isinstance(node, (ast.Module, ast.ClassDef)) and body:
                    for prev, cur in zip(body, body[1:]):
                        if (isinstance(prev, (ast.Assign, ast.AnnAssign))
                                and isinstance(cur, ast.Expr)
                                and isinstance(cur.value, ast.Constant)
                                and isinstance(cur.value.value, str)):
                            attribute_docstrings.add(cur.value.lineno)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if node.lineno in docstring_linenos:
                    continue
                text = node.value
                exempt = (sql_module and SQL_STATEMENT_RE.match(text)) or node.lineno in attribute_docstrings
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
    return findings, scanned, unparsed, pragmas


def run() -> CheckResult:
    root = repo_root()
    findings, scanned, unparsed, pragmas = _scan_tree((root / "packages", root / "services"), root)
    if scanned == 0:
        return CheckResult("S4", "prompt hygiene", "SKIP", [], "no Python code yet")
    if findings:
        return CheckResult("S4", "prompt hygiene", "FAIL", findings,
                           f"{scanned} files scanned", pragma_count=pragmas)
    if unparsed:
        # Some files were unreadable to this interpreter, so a PASS would vouch for
        # files never examined (issue #35). Skip honest instead.
        version = ".".join(str(n) for n in sys.version_info[:3])
        return CheckResult("S4", "prompt hygiene", "SKIP", [],
                           f"{scanned - len(unparsed)}/{scanned} files scanned; "
                           f"{len(unparsed)} unparsable by python {version} "
                           f"(e.g. {unparsed[0]}) — cannot vouch for them",
                           pragma_count=pragmas)
    return CheckResult("S4", "prompt hygiene", "PASS", [],
                       f"{scanned} files scanned", pragma_count=pragmas)


_LONG_SQL = (
    "SELECT a.attname FROM pg_catalog.pg_attribute AS a "
    + "JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid " * 12
)
"""One statement, over the length rule, opening with a SQL keyword."""

_LONG_PROSE = "Summarise the table for a data steward and list its likely owners. " * 12
"""Over the length rule and *not* opening with a SQL keyword: prose."""


def _fixture(module: Path, literal: str) -> None:
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(f'"""A module."""\n\nSTATEMENT = (\n    "{literal}"\n)\n', encoding="utf-8")


def _selftest() -> int:
    """Plant one case per branch of the exemption's wording.

    The second one is the case this check claimed and did not enforce: `private`
    was in the docstring and in GUARDRAILS.md while the code tested only the
    `_sql.py` suffix, so a *public* module with the same name shape carried a
    long literal past S4. The fourth pins the other branch — an Alembic revision
    under `versions/`, which is exempt by *directory* and is not private — so
    the sentence and the code stay pinned together on both.
    """
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pkg = root / "packages" / "steward-probe" / "src" / "steward_probe"
        _fixture(pkg / "_probe_sql.py", _LONG_SQL)              # exempt: private, SQL-opening
        _fixture(pkg / "probe_sql.py", _LONG_SQL)               # not exempt: public module
        _fixture(pkg / "_prose_sql.py", _LONG_PROSE)            # not exempt: prose, not SQL
        _fixture(pkg / "versions" / "0001_init.py", _LONG_SQL)  # exempt: Alembic revision
        findings, scanned, unparsed, _ = _scan_tree((root / "packages",), root)
        flagged = {Path(f.path).name for f in findings}
        if scanned != 4 or unparsed:
            print(f"selftest FAIL: scanned {scanned} files, {len(unparsed)} unparsable (expected 4, 0)")
            ok = False
        if "0001_init.py" in flagged:
            print("selftest FAIL: an Alembic revision's SQL literal was flagged (should be exempt)")
            ok = False
        if "_probe_sql.py" in flagged:
            print("selftest FAIL: a private _sql module's SQL literal was flagged (should be exempt)")
            ok = False
        if "probe_sql.py" not in flagged:
            print("selftest FAIL: a PUBLIC _sql module was exempted — the exemption says private")
            ok = False
        if "_prose_sql.py" not in flagged:
            print("selftest FAIL: prose in a private _sql module was exempted (only SQL is)")
            ok = False
        if ok:
            print(f"selftest: exemption is private-or-versions and SQL-only "
                  f"({scanned} fixtures, flagged {sorted(flagged)})")
    print(f"S4 selftest: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    result = run()
    for f in result.findings:
        print(f"{f.path}:{f.line}: {f.message}")
    print(f"S4 {result.status} ({result.detail}, {result.pragma_count} pragmas)")
    sys.exit({"FAIL": 1, "SKIP": 2}.get(result.status, 0))
