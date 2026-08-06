"""F1 — Import boundaries (enforces I2, I4, I9).

Rules, from scripts/fitness/boundaries.json:
  1. Banned modules (kitchen-sink frameworks) may not be imported anywhere,
     nor appear in any pyproject.toml dependency list.
  2. Contained modules may be imported only inside their declared home packages
     (langgraph -> steward-agents; provider SDKs / litellm -> steward-llm).
  3. Code under packages/ may not import from services/.
  4. A steward package may import another steward package only via a declared edge.
  5. The schemas package imports only the standard library plus an explicit
     third-party allowlist (pydantic and friends).
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Set

from common import CheckResult, Finding, iter_python_files, module_name_for_package, repo_root

# Fallback stdlib roots for Python < 3.10 (sys.stdlib_module_names preferred when present).
_STDLIB_FALLBACK = {
    "abc", "argparse", "asyncio", "base64", "collections", "contextlib", "contextvars",
    "copy", "csv", "dataclasses", "datetime", "decimal", "enum", "functools", "hashlib",
    "heapq", "hmac", "html", "http", "importlib", "inspect", "io", "itertools", "json",
    "logging", "math", "os", "pathlib", "pickle", "queue", "random", "re", "secrets",
    "shutil", "signal", "socket", "sqlite3", "statistics", "string", "struct", "subprocess",
    "sys", "tempfile", "textwrap", "threading", "time", "types", "typing", "unittest",
    "urllib", "uuid", "warnings", "weakref", "zlib", "zoneinfo", "tomllib", "__future__",
}
STDLIB: Set[str] = set(getattr(sys, "stdlib_module_names", _STDLIB_FALLBACK))
STDLIB.add("__future__")


def _import_roots(tree: ast.AST) -> List[tuple]:
    roots = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:  # absolute imports only
                roots.append((node.module.split(".")[0], node.lineno))
    return roots


def _owning_package(path: Path, packages_dir: Path) -> Optional[str]:
    try:
        rel = path.relative_to(packages_dir)
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def run() -> CheckResult:
    root = repo_root()
    cfg = json.loads((Path(__file__).parent / "boundaries.json").read_text())
    packages_dir = root / cfg["packages_dir"]
    findings: List[Finding] = []

    banned = set(cfg["banned_modules"])
    contained = {mod: set(homes) for mod, homes in cfg["contained_modules"].items()}
    schemas_pkg = cfg["schemas_package"]
    schemas_ok = set(cfg["schemas_allowed_thirdparty"])
    edges = {k: set(v) for k, v in cfg["allowed_package_edges"].items()}
    all_pkgs = set(edges) | {v for vs in edges.values() for v in vs} | {schemas_pkg} \
        | {h for hs in contained.values() for h in hs}
    steward_modules = {module_name_for_package(p): p for p in all_pkgs}
    services_module = cfg["services_dir"]

    scanned = 0
    for scan_base in (packages_dir, root / cfg["services_dir"]):
        for path in iter_python_files(scan_base):
            scanned += 1
            rel = str(path.relative_to(root))
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as exc:
                findings.append(Finding(rel, exc.lineno or 0, "unparseable Python file"))
                continue
            pkg = _owning_package(path, packages_dir)
            for mod, lineno in _import_roots(tree):
                if mod in banned:
                    findings.append(Finding(rel, lineno, f"banned framework import '{mod}' (I9)"))
                elif mod in contained and pkg not in contained[mod]:
                    homes = ", ".join(sorted(contained[mod]))
                    findings.append(Finding(rel, lineno, f"'{mod}' allowed only in {homes} (I2/I9)"))
                elif pkg is not None:
                    if mod == services_module:
                        findings.append(Finding(rel, lineno, "package imports from services/ (I4)"))
                    elif mod in steward_modules and steward_modules[mod] != pkg:
                        target = steward_modules[mod]
                        if target not in edges.get(pkg, set()):
                            findings.append(Finding(
                                rel, lineno,
                                f"undeclared package edge {pkg} -> {target}; declare in boundaries.json (I4)"))
                    if pkg == schemas_pkg and mod not in STDLIB and mod not in schemas_ok \
                            and mod not in steward_modules:
                        findings.append(Finding(rel, lineno, f"schemas package imports '{mod}' (I4: pydantic+stdlib only)"))

    # Rule 1b: banned frameworks must not appear in dependency manifests either.
    for manifest in [root / "pyproject.toml", *root.glob("packages/*/pyproject.toml"),
                     *root.glob("services/*/pyproject.toml")]:
        if manifest.exists():
            text = manifest.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                for mod in banned:
                    dep = re.escape(mod.replace("_", "-"))
                    # exact dep name (not a prefix of a longer, allowed dep like langchain-core)
                    if re.search(rf"[\"']{dep}\s*([\"'><=~!\[;]|$)", line):
                        findings.append(Finding(str(manifest.relative_to(root)), lineno,
                                                f"banned framework in dependencies (I9): {line.strip()}"))

    if scanned == 0 and not findings:
        return CheckResult("F1", "import boundaries", "SKIP", [], "no packages/ or services/ code yet")
    status = "FAIL" if findings else "PASS"
    return CheckResult("F1", "import boundaries", status, findings, f"{scanned} files scanned")


if __name__ == "__main__":
    result = run()
    for f in result.findings:
        print(f"{f.path}:{f.line}: {f.message}")
    print(f"F1 {result.status} ({result.detail})")
    sys.exit(1 if result.status == "FAIL" else 0)
