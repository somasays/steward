"""S6 -- Contract compatibility (enforces I3, N9).

Regenerates the current JSON Schema for every `steward_schemas.CONTRACTS`
entry plus the exported OpenAPI spec (via the packages' own console scripts,
run through `uv run` into a temp directory) and diffs each against its
committed snapshot (`contracts/schemas/*.json`, `contracts/openapi.json`).

Classification (GUARDRAILS.md S6):
  - breaking: a removed model/property/path/method, a type change, a newly
    -required property, or enum narrowing (values removed) -> FAIL, findings
    carry a JSON-pointer-shaped path to the exact spot.
  - stale: any other difference (added property, widened enum, description
    change, ...) where the committed snapshot doesn't match a fresh export
    -> FAIL "snapshot stale -- regenerate and commit".
  - identical: PASS.

No external oasdiff binary: oasdiff is a Go binary, not a Python package,
so shelling out to it would break Tier S's zero-extra-toolchain guarantee
(GUARDRAILS.md build-vs-buy rule already prefers a maintained *library*,
and none exists for this in pure Python) -- a small stdlib differ covers
the breaking-change taxonomy this project actually needs.

Self-test (stdlib only, no pytest, no uv/subprocess -- exercises the pure
diff/classify logic directly against in-memory fixtures, mirroring
check_surface.py's pattern):

    python3 scripts/fitness/check_contracts.py --selftest
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import CheckResult, Finding, repo_root

JSONDict = Dict[str, Any]

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

STALE_MESSAGE = "snapshot stale — regenerate and commit"


def _diff_schema_breaking(old: Any, new: Any, pointer: str = "") -> List[Tuple[str, str]]:
    """Breaking differences between two JSON Schema fragments: removed
    property, type change, newly-required property, enum narrowing --
    recursing through `properties` and `$defs` (nested models/enums)."""
    findings: List[Tuple[str, str]] = []
    if not isinstance(old, dict) or not isinstance(new, dict):
        return findings

    old_type, new_type = old.get("type"), new.get("type")
    if old_type is not None and new_type is not None and old_type != new_type:
        findings.append((pointer or "/", f"type changed '{old_type}' -> '{new_type}'"))

    old_enum, new_enum = old.get("enum"), new.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        removed_values = [v for v in old_enum if v not in new_enum]
        if removed_values:
            findings.append((f"{pointer}/enum", f"enum narrowed, removed {removed_values!r}"))

    old_required = set(old.get("required") or [])
    new_required = set(new.get("required") or [])
    for prop in sorted(new_required - old_required):
        findings.append((f"{pointer}/required/{prop}", f"property '{prop}' newly required"))

    old_props = old.get("properties")
    if isinstance(old_props, dict):
        new_props = new.get("properties") if isinstance(new.get("properties"), dict) else {}
        for prop in sorted(old_props):
            if prop not in new_props:
                findings.append((f"{pointer}/properties/{prop}", "removed property"))
        for prop in sorted(set(old_props) & set(new_props)):
            findings.extend(_diff_schema_breaking(
                old_props[prop], new_props[prop], f"{pointer}/properties/{prop}"))

    old_defs = old.get("$defs")
    if isinstance(old_defs, dict):
        new_defs = new.get("$defs") if isinstance(new.get("$defs"), dict) else {}
        for name in sorted(old_defs):
            if name not in new_defs:
                findings.append((f"{pointer}/$defs/{name}", "removed nested model/enum"))
        for name in sorted(set(old_defs) & set(new_defs)):
            findings.extend(_diff_schema_breaking(
                old_defs[name], new_defs[name], f"{pointer}/$defs/{name}"))

    return findings


def _diff_operation_breaking(old_op: JSONDict, new_op: JSONDict, pointer: str) -> List[Tuple[str, str]]:
    """Breaking differences on a single operation object that the generic
    schema differ can't see: inline `parameters` (path/query/header/cookie
    -- FastAPI emits these directly on the operation, not via `$ref`, so a
    removed or newly-required path param would otherwise pass silently) and
    inline `requestBody` media-type schemas."""
    findings: List[Tuple[str, str]] = []

    def _param_key(p: JSONDict) -> Tuple[Any, Any]:
        return (p.get("name"), p.get("in"))

    old_params = {_param_key(p): p for p in (old_op.get("parameters") or []) if isinstance(p, dict)}
    new_params = {_param_key(p): p for p in (new_op.get("parameters") or []) if isinstance(p, dict)}
    for key in sorted(old_params, key=repr):
        label = f"{pointer}/parameters/{key[1]}.{key[0]}"
        if key not in new_params:
            findings.append((label, "removed parameter"))
            continue
        old_p, new_p = old_params[key], new_params[key]
        if bool(old_p.get("required")) is False and bool(new_p.get("required")) is True:
            findings.append((label, "parameter newly required"))
        findings.extend(_diff_schema_breaking(old_p.get("schema"), new_p.get("schema"), f"{label}/schema"))
    for key in sorted(set(new_params) - set(old_params), key=repr):
        if new_params[key].get("required") is True:
            findings.append((f"{pointer}/parameters/{key[1]}.{key[0]}", "new required parameter"))

    old_content = ((old_op.get("requestBody") or {}).get("content")) or {}
    new_content = ((new_op.get("requestBody") or {}).get("content")) or {}
    for media in sorted(old_content):
        label = f"{pointer}/requestBody/content/{media}"
        if media not in new_content:
            findings.append((label, "removed request media type"))
            continue
        findings.extend(_diff_schema_breaking(
            (old_content[media] or {}).get("schema"), (new_content[media] or {}).get("schema"), f"{label}/schema"))

    return findings


def _diff_openapi_breaking(old: JSONDict, new: JSONDict) -> List[Tuple[str, str]]:
    """Breaking differences at the OpenAPI level: removed path, removed
    method on a surviving path, removed/newly-required/type-changed inline
    parameters and request bodies on a surviving method (`_diff_operation_
    breaking`), and (via the schema differ) removed/changed fields on
    `components.schemas` entries -- the same models published in
    contracts/schemas/, so this mostly reconfirms them under their spec
    names, plus catches path/method removal the schema snapshots can't see.

    Known conservative bound: response-body schemas aren't diffed here --
    a narrower response is a break for consumers too, but producers loosen
    responses far more often than they break them, and every response
    schema in this API is a `$ref` into `components.schemas`, which the
    walk below already covers. Inline (non-`$ref`) response schemas would
    slip through; none exist in this API today."""
    findings: List[Tuple[str, str]] = []

    old_paths = old.get("paths") if isinstance(old.get("paths"), dict) else {}
    new_paths = new.get("paths") if isinstance(new.get("paths"), dict) else {}
    for path in sorted(old_paths):
        if path not in new_paths:
            findings.append((f"/paths{path}", "removed path"))
            continue
        old_methods = {k: v for k, v in old_paths[path].items() if k in HTTP_METHODS}
        new_methods = {k: v for k, v in new_paths[path].items() if k in HTTP_METHODS}
        for method in sorted(set(old_methods) - set(new_methods)):
            findings.append((f"/paths{path}/{method}", "removed method"))
        for method in sorted(set(old_methods) & set(new_methods)):
            findings.extend(_diff_operation_breaking(
                old_methods[method], new_methods[method], f"/paths{path}/{method}"))

    old_schemas = (old.get("components") or {}).get("schemas")
    old_schemas = old_schemas if isinstance(old_schemas, dict) else {}
    new_schemas = (new.get("components") or {}).get("schemas")
    new_schemas = new_schemas if isinstance(new_schemas, dict) else {}
    for name in sorted(old_schemas):
        if name not in new_schemas:
            findings.append((f"/components/schemas/{name}", "removed model"))
        else:
            findings.extend(_diff_schema_breaking(
                old_schemas[name], new_schemas[name], f"/components/schemas/{name}"))

    return findings


def _classify(
    old: Optional[JSONDict], new: Optional[JSONDict], breaking_fn: Any,
) -> Tuple[str, List[Tuple[str, str]]]:
    """pass | breaking | stale, plus (pointer, message) findings."""
    if old is None and new is not None:
        return "stale", [("", "new, not yet committed")]
    if old is not None and new is None:
        return "breaking", [("", "removed")]
    assert old is not None and new is not None
    if old == new:
        return "pass", []
    breaking = breaking_fn(old, new)
    if breaking:
        return "breaking", breaking
    return "stale", [("", STALE_MESSAGE)]


def _load_json(path: Path) -> Optional[JSONDict]:
    if not path.exists():
        return None
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def _classify_all(
    schemas_dir: Path, tmp_schemas_dir: Path, openapi_path: Path, tmp_openapi_path: Path,
) -> Tuple[List[Finding], str]:
    """Run `_classify` over every committed/regenerated schema file plus
    openapi.json; return (findings, overall_status). Pure once its inputs
    are on disk -- shared between `run()` and integration-style manual
    checks; the selftest exercises `_classify`/`_diff_*` directly instead,
    since those don't need real files at all."""
    findings: List[Finding] = []
    breaking_files: List[str] = []
    stale_files: List[str] = []

    committed_names = {p.stem for p in schemas_dir.glob("*.json")}
    regenerated_names = {p.stem for p in tmp_schemas_dir.glob("*.json")} if tmp_schemas_dir.exists() else set()
    for name in sorted(committed_names | regenerated_names):
        old = _load_json(schemas_dir / f"{name}.json") if name in committed_names else None
        new = _load_json(tmp_schemas_dir / f"{name}.json") if name in regenerated_names else None
        status, items = _classify(old, new, _diff_schema_breaking)
        label = f"contracts/schemas/{name}.json"
        if status == "breaking":
            breaking_files.append(label)
            for pointer, msg in items:
                findings.append(Finding(label, 0, f"{pointer}: {msg} (breaking)"))
        elif status == "stale":
            stale_files.append(label)
            for pointer, msg in items:
                findings.append(Finding(label, 0, f"{pointer}: {msg}" if pointer else msg))

    old_openapi = _load_json(openapi_path)
    new_openapi = _load_json(tmp_openapi_path)
    status, items = _classify(old_openapi, new_openapi, _diff_openapi_breaking)
    label = "contracts/openapi.json"
    if status == "breaking":
        breaking_files.append(label)
        for pointer, msg in items:
            findings.append(Finding(label, 0, f"{pointer}: {msg} (breaking)"))
    elif status == "stale":
        stale_files.append(label)
        for pointer, msg in items:
            findings.append(Finding(label, 0, f"{pointer}: {msg}" if pointer else msg))

    if breaking_files:
        return findings, "breaking: " + ", ".join(breaking_files)
    if stale_files:
        return findings, STALE_MESSAGE + " (" + ", ".join(stale_files) + ")"
    return findings, f"{len(committed_names)} schemas + openapi.json match their snapshots"


def run() -> CheckResult:
    root = repo_root()
    if shutil.which("uv") is None:
        return CheckResult("S6", "contract compatibility", "SKIP", [], "uv not available")
    schemas_dir = root / "contracts" / "schemas"
    openapi_path = root / "contracts" / "openapi.json"
    if not schemas_dir.exists() or not any(schemas_dir.glob("*.json")) or not openapi_path.exists():
        return CheckResult("S6", "contract compatibility", "SKIP", [], "contracts/ snapshots not generated yet")

    with tempfile.TemporaryDirectory() as td:
        tmp_schemas = Path(td) / "schemas"
        tmp_openapi = Path(td) / "openapi.json"
        exports = [
            ("schema export", ["uv", "run", "--package", "steward-schemas",
                                "steward-schemas-export-schemas", str(tmp_schemas)]),
            ("openapi export", ["uv", "run", "--package", "steward-api",
                                 "steward-api-export-openapi", str(tmp_openapi)]),
        ]
        for label, cmd in exports:
            proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
            if proc.returncode != 0:
                tail = (proc.stdout + proc.stderr).strip().splitlines()[-15:]
                return CheckResult("S6", "contract compatibility", "FAIL", [],
                                   f"{label} failed ({' '.join(cmd)}):\n    " + "\n    ".join(tail))

        findings, detail = _classify_all(schemas_dir, tmp_schemas, openapi_path, tmp_openapi)

    status = "FAIL" if findings else "PASS"
    return CheckResult("S6", "contract compatibility", status, findings, detail)


# --- self-test ---------------------------------------------------------

_BASE_SCHEMA: JSONDict = {
    "title": "Widget",
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "status": {"type": "string", "enum": ["active", "retired"]},
    },
    "required": ["id"],
}


def _selftest() -> int:
    ok = True

    # 1. removed property -> breaking
    new_missing_prop = json.loads(json.dumps(_BASE_SCHEMA))
    del new_missing_prop["properties"]["status"]
    status, items = _classify(_BASE_SCHEMA, new_missing_prop, _diff_schema_breaking)
    if status != "breaking" or not any("removed property" in m for _, m in items):
        print(f"selftest FAIL: removed property expected breaking, got {status} {items}")
        ok = False
    else:
        print("selftest: removed property correctly classified breaking")

    # 2. type change -> breaking
    new_type_change = json.loads(json.dumps(_BASE_SCHEMA))
    new_type_change["properties"]["id"]["type"] = "integer"
    status, items = _classify(_BASE_SCHEMA, new_type_change, _diff_schema_breaking)
    if status != "breaking" or not any("type changed" in m for _, m in items):
        print(f"selftest FAIL: type change expected breaking, got {status} {items}")
        ok = False
    else:
        print("selftest: type change correctly classified breaking")

    # 2b. enum narrowing -> breaking
    new_enum_narrow = json.loads(json.dumps(_BASE_SCHEMA))
    new_enum_narrow["properties"]["status"]["enum"] = ["active"]
    status, items = _classify(_BASE_SCHEMA, new_enum_narrow, _diff_schema_breaking)
    if status != "breaking" or not any("enum narrowed" in m for _, m in items):
        print(f"selftest FAIL: enum narrowing expected breaking, got {status} {items}")
        ok = False
    else:
        print("selftest: enum narrowing correctly classified breaking")

    # 2c. newly-required property -> breaking
    new_required = json.loads(json.dumps(_BASE_SCHEMA))
    new_required["required"] = ["id", "status"]
    status, items = _classify(_BASE_SCHEMA, new_required, _diff_schema_breaking)
    if status != "breaking" or not any("newly required" in m for _, m in items):
        print(f"selftest FAIL: newly-required property expected breaking, got {status} {items}")
        ok = False
    else:
        print("selftest: newly-required property correctly classified breaking")

    # 3. added optional property, snapshot not regenerated -> stale
    new_added_prop = json.loads(json.dumps(_BASE_SCHEMA))
    new_added_prop["properties"]["note"] = {"type": "string"}
    status, items = _classify(_BASE_SCHEMA, new_added_prop, _diff_schema_breaking)
    if status != "stale" or not any(STALE_MESSAGE in m for _, m in items):
        print(f"selftest FAIL: added optional property expected stale, got {status} {items}")
        ok = False
    else:
        print("selftest: added optional property with stale snapshot correctly classified stale")

    # 4. updated snapshot (committed == regenerated, including the added field) -> pass
    status, items = _classify(new_added_prop, new_added_prop, _diff_schema_breaking)
    if status != "pass" or items:
        print(f"selftest FAIL: updated snapshot expected pass, got {status} {items}")
        ok = False
    else:
        print("selftest: updated snapshot correctly classified pass")

    # 5. removed model (file gone from the regenerated set) -> breaking
    status, items = _classify(_BASE_SCHEMA, None, _diff_schema_breaking)
    if status != "breaking":
        print(f"selftest FAIL: removed model expected breaking, got {status} {items}")
        ok = False
    else:
        print("selftest: removed model (missing file) correctly classified breaking")

    # 6. removed path / removed method -> breaking (OpenAPI-level)
    old_api: JSONDict = {"paths": {"/v1/runs": {"get": {}, "post": {}}}, "components": {"schemas": {}}}
    new_api_no_method: JSONDict = {"paths": {"/v1/runs": {"get": {}}}, "components": {"schemas": {}}}
    status, items = _classify(old_api, new_api_no_method, _diff_openapi_breaking)
    if status != "breaking" or not any("removed method" in m for _, m in items):
        print(f"selftest FAIL: removed method expected breaking, got {status} {items}")
        ok = False
    else:
        print("selftest: removed OpenAPI method correctly classified breaking")

    # 7. removed inline path parameter -> breaking (FastAPI emits these
    # directly on the operation, not via $ref -- the schema differ alone
    # can't see them)
    old_api_param: JSONDict = {
        "paths": {"/v1/runs/{run_id}": {"get": {
            "parameters": [{"name": "run_id", "in": "path", "required": True,
                             "schema": {"type": "string"}}]}}},
        "components": {"schemas": {}},
    }
    new_api_param_removed: JSONDict = {
        "paths": {"/v1/runs/{run_id}": {"get": {"parameters": []}}},
        "components": {"schemas": {}},
    }
    status, items = _classify(old_api_param, new_api_param_removed, _diff_openapi_breaking)
    if status != "breaking" or not any("removed parameter" in m for _, m in items):
        print(f"selftest FAIL: removed path parameter expected breaking, got {status} {items}")
        ok = False
    else:
        print("selftest: removed inline path parameter correctly classified breaking")

    print(f"S6 selftest: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    result = run()
    for f in result.findings:
        print(f"{f.path}: {f.message}")
    print(f"S6 {result.status} ({result.detail})")
    sys.exit(1 if result.status == "FAIL" else 0)
