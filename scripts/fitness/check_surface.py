"""S5 — Public-surface lock (enforces I3, I9).

For every (contained module, home package) pair declared in
boundaries.json's `contained_modules` (e.g. `langgraph` homed in
`steward-agents`), the home package's PUBLIC surface must never reference
that module:

  (a) in annotations (parameter, return, or class-attribute) of a top-level
      def or class in a public module,
  (b) as a base class of a top-level class in a public module,
  (c) via `import X` / `from X import ...` of the contained module in a
      public `__init__.py` (the classic re-export vector).

Public surface = every `.py` file under a package's `src/<module>/` tree
whose relative path has no leading-underscore path component (single
underscore only — dunders like `__init__.py` stay public), excluding
tests. A framework type can flow freely through the package's *private*
modules (e.g. `_internal.py`, `_runtime/graph.py`) — that is containment
(I9) working as intended — it just cannot surface in what other packages
or services can import.

Alias tracking: `import langgraph as lg`, `import langgraph.graph as gg`,
and `from langgraph.graph import StateGraph` are all resolved back to
their root module (`langgraph`) so a bare `StateGraph` or `lg.graph.Foo`
used in an annotation is caught. Quoted / forward-ref annotations
(`x: "StateGraph"`) are parsed and checked too.

Known conservative bound: privacy is judged at the module-path level only,
not per-symbol — a leading-underscore *helper* defined inside an otherwise
public module is still checked. If that trips a false positive, move the
helper into a private module; that is the containment pattern this check
is enforcing.

Self-test (stdlib only, no pytest — a script under scripts/ isn't on the
packages test suite's collection path):

    python3 scripts/fitness/check_surface.py --selftest

builds two temp fixture package trees (one with a planted leak covering
all three rules, one clean) and asserts the core scan logic FAILs the
first and PASSes the second, including that a private module doing the
exact same thing is never flagged.
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from common import CheckResult, Finding, is_test_path, module_name_for_package, repo_root


def _is_public_component(part: str) -> bool:
    return not (part.startswith("_") and not part.startswith("__"))


def _is_public_module(rel_parts: Tuple[str, ...]) -> bool:
    return all(_is_public_component(p) for p in rel_parts)


def _iter_public_modules(src_dir: Path) -> Iterator[Path]:
    if not src_dir.exists():
        return
    for path in sorted(src_dir.rglob("*.py")):
        if is_test_path(path):
            continue
        rel = path.relative_to(src_dir)
        if _is_public_module(rel.parts):
            yield path


def _alias_map(tree: ast.Module) -> Dict[str, str]:
    """Name bound in this module's namespace -> root module it came from."""
    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                bound = alias.asname or alias.name.split(".")[0]
                aliases[bound] = root
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                root = node.module.split(".")[0]
                for alias in node.names:
                    bound = alias.asname or alias.name
                    aliases[bound] = root
    return aliases


def _annotation_refs(node: Optional[ast.expr], aliases: Dict[str, str]) -> List[Tuple[int, str, str]]:
    """Every reference inside `node` that resolves, via `aliases`, to a tracked root module.

    Returns (lineno, referenced-name, root-module) triples. Handles plain
    names, attribute chains (`lg.graph.Foo`), subscripted generics
    (`Optional[Foo]`), and quoted forward-ref annotations.
    """
    if node is None:
        return []
    refs: List[Tuple[int, str, str]] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            try:
                parsed = ast.parse(sub.value, mode="eval").body
            except SyntaxError:
                continue
            for inner in ast.walk(parsed):
                if isinstance(inner, ast.Name) and inner.id in aliases:
                    refs.append((sub.lineno, inner.id, aliases[inner.id]))
        elif isinstance(sub, ast.Name) and sub.id in aliases:
            refs.append((sub.lineno, sub.id, aliases[sub.id]))
    return refs


def _all_args(fn: "ast.FunctionDef | ast.AsyncFunctionDef") -> List[ast.arg]:
    a = fn.args
    extra = ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else [])
    return list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs) + extra


def _function_refs(fn: "ast.FunctionDef | ast.AsyncFunctionDef", aliases: Dict[str, str]) -> List[Tuple[int, str, str]]:
    refs: List[Tuple[int, str, str]] = []
    for arg in _all_args(fn):
        refs.extend(_annotation_refs(arg.annotation, aliases))
    refs.extend(_annotation_refs(fn.returns, aliases))
    return refs


def _class_refs(cls: ast.ClassDef, aliases: Dict[str, str]) -> List[Tuple[int, str, str, str]]:
    refs: List[Tuple[int, str, str, str]] = []
    for base in cls.bases:
        for lineno, name, root in _annotation_refs(base, aliases):
            refs.append((lineno, name, root, "class base"))
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign):
            for lineno, name, root in _annotation_refs(stmt.annotation, aliases):
                refs.append((lineno, name, root, "attribute annotation"))
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for lineno, name, root in _function_refs(stmt, aliases):
                refs.append((lineno, name, root, f"method '{stmt.name}' signature"))
    return refs


def _reexport_refs(tree: ast.Module, targets: Set[str]) -> List[Tuple[int, str]]:
    refs: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in targets:
                    refs.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                root = node.module.split(".")[0]
                if root in targets:
                    names = ", ".join(a.name for a in node.names)
                    refs.append((node.lineno, f"from {node.module} import {names}"))
    return refs


def _scan_module(path: Path, rel_root: Path, targets: Set[str]) -> List[Finding]:
    rel = str(path.relative_to(rel_root))
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding(rel, exc.lineno or 0, "unparseable Python file")]

    aliases = _alias_map(tree)
    findings: List[Finding] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for lineno, name, root in _function_refs(node, aliases):
                if root in targets:
                    findings.append(Finding(
                        rel, lineno,
                        f"'{name}' ({root}) leaks into public signature of '{node.name}' (I9/I3)"))
        elif isinstance(node, ast.ClassDef):
            for lineno, name, root, ctx in _class_refs(node, aliases):
                if root in targets:
                    findings.append(Finding(
                        rel, lineno,
                        f"'{name}' ({root}) leaks into {ctx} of class '{node.name}' (I9/I3)"))

    if path.name == "__init__.py":
        for lineno, imported in _reexport_refs(tree, targets):
            findings.append(Finding(
                rel, lineno, f"public __init__.py re-exports contained module ({imported}) (I9/I3)"))

    return findings


def _scan_tree(packages_dir: Path, rel_root: Path, contained_modules: Dict[str, List[str]]) -> Tuple[List[Finding], int]:
    """Core logic: scan every package's public surface for leaks of its home-only modules.

    Shared between `run()` (real repo) and `--selftest` (temp fixtures) so the
    self-test genuinely exercises the same code path the gate runs.
    """
    home_targets: Dict[str, Set[str]] = {}
    for mod, homes in contained_modules.items():
        for home in homes:
            home_targets.setdefault(home, set()).add(mod)

    findings: List[Finding] = []
    scanned = 0
    if not packages_dir.exists():
        return findings, scanned

    for pkg_dir in sorted(p for p in packages_dir.iterdir() if p.is_dir()):
        targets = home_targets.get(pkg_dir.name)
        if not targets:
            continue
        src_dir = pkg_dir / "src" / module_name_for_package(pkg_dir.name)
        for path in _iter_public_modules(src_dir):
            scanned += 1
            findings.extend(_scan_module(path, rel_root, targets))

    return findings, scanned


def run() -> CheckResult:
    root = repo_root()
    cfg = json.loads((Path(__file__).parent / "boundaries.json").read_text())
    packages_dir = root / cfg["packages_dir"]
    if not packages_dir.exists():
        return CheckResult("S5", "public-surface lock", "SKIP", [], "no packages/ yet")

    findings, scanned = _scan_tree(packages_dir, root, cfg["contained_modules"])
    if scanned == 0:
        return CheckResult("S5", "public-surface lock", "SKIP", [], "no package owns a contained module yet")
    status = "FAIL" if findings else "PASS"
    return CheckResult("S5", "public-surface lock", status, findings, f"{scanned} public modules scanned")


# --- self-test ---------------------------------------------------------

_LEAK_INIT = """\
from langgraph.graph import StateGraph

__all__ = ["StateGraph"]
"""

_LEAK_PUBLIC = """\
import langgraph as lg
from langgraph.graph import StateGraph


class Runtime(lg.graph.StateGraph):
    def build(self) -> StateGraph:
        raise NotImplementedError
"""

_LEAK_PRIVATE = """\
from langgraph.graph import StateGraph


def _helper() -> StateGraph:
    raise NotImplementedError
"""

_CLEAN_INIT = """\
__all__: list = []
"""

_CLEAN_PUBLIC = """\
class Runtime:
    def build(self) -> str:
        return "ok"
"""

_CLEAN_PRIVATE = """\
from langgraph.graph import StateGraph


def _helper() -> StateGraph:
    raise NotImplementedError
"""


def _write_fixture(pkg_root: Path, init_src: str, public_src: str, private_src: str) -> Path:
    src_dir = pkg_root / "packages" / "steward-agents" / "src" / "steward_agents"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text(init_src)
    (src_dir / "graph_runtime.py").write_text(public_src)
    (src_dir / "_internal.py").write_text(private_src)
    return pkg_root / "packages"


def _selftest() -> int:
    cfg = {"langgraph": ["steward-agents"]}
    ok = True

    with tempfile.TemporaryDirectory() as td:
        leak_root = Path(td) / "leak"
        leak_root.mkdir()
        leak_packages = _write_fixture(leak_root, _LEAK_INIT, _LEAK_PUBLIC, _LEAK_PRIVATE)
        findings, scanned = _scan_tree(leak_packages, leak_root, cfg)
        private_flagged = any("_internal.py" in f.path for f in findings)
        if not findings:
            print("selftest FAIL: planted leak produced no findings")
            ok = False
        elif private_flagged:
            print("selftest FAIL: private module was flagged (should be exempt)")
            ok = False
        elif len(findings) < 3:
            print(f"selftest FAIL: expected findings for all three rules (a/b/c), got {len(findings)}")
            ok = False
        else:
            print(f"selftest: planted-leak fixture correctly FAILs ({scanned} modules scanned, {len(findings)} findings)")
            for f in findings:
                print(f"  {f.path}:{f.line}: {f.message}")

        clean_root = Path(td) / "clean"
        clean_root.mkdir()
        clean_packages = _write_fixture(clean_root, _CLEAN_INIT, _CLEAN_PUBLIC, _CLEAN_PRIVATE)
        findings2, scanned2 = _scan_tree(clean_packages, clean_root, cfg)
        if findings2:
            print("selftest FAIL: clean fixture produced findings:")
            for f in findings2:
                print(f"  {f.path}:{f.line}: {f.message}")
            ok = False
        elif scanned2 == 0:
            print("selftest FAIL: clean fixture scanned zero modules (test is vacuous)")
            ok = False
        else:
            print(f"selftest: clean fixture correctly PASSes ({scanned2} modules scanned)")

    print(f"S5 selftest: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    result = run()
    for f in result.findings:
        print(f"{f.path}:{f.line}: {f.message}")
    print(f"S5 {result.status} ({result.detail})")
    sys.exit(1 if result.status == "FAIL" else 0)
