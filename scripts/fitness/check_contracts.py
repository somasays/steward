"""S6 -- Contract compatibility (enforces I3, N9).

Regenerates the current JSON Schema for every `steward_schemas.CONTRACTS`
entry plus the exported OpenAPI spec (via the packages' own console scripts,
run through `uv run` into a temp directory) and diffs each along two axes:

  1. baseline axis -- the fresh export against the snapshot as it exists at
     the BASELINE commit (read straight out of git with `git show`, so no
     second checkout is needed). This is the compatibility gate: a breaking
     change committed together with its regenerated snapshot is
     self-consistent and invisible to axis 2, but still breaks consumers who
     are on the baseline (#25).
  2. stale axis -- the fresh export against the snapshot committed in the
     working tree. This catches the other mistake: changed the code, forgot
     to regenerate.

Classification (GUARDRAILS.md S6):
  - breaking: a removed model/property/path/method, a type change, a newly
    -required property, or enum narrowing (values removed) -> FAIL, findings
    carry a JSON-pointer-shaped path to the exact spot. Reported against the
    baseline ("breaking vs baseline") and against the committed snapshot.
  - stale: any other difference (added property, widened enum, description
    change, ...) where the committed snapshot doesn't match a fresh export
    -> FAIL "snapshot stale -- regenerate and commit". Additive drift away
    from the baseline is not stale: that is contract evolution.
  - identical: PASS.

Baseline resolution (`_resolve_baseline`) is explicit, because origin/main is
not reliably present in a shallow CI checkout or an offline clone:
  - CI, pull_request event: the base SHA from the event payload, falling back
    to `origin/$GITHUB_BASE_REF`.
  - CI, push to the default branch: the ref's previous tip (`event.before`).
    Merge-base is HEAD itself there, which would compare a commit with itself
    and call it a compatibility check.
  - CI, any other event (push to another ref, workflow_dispatch): merge-base
    with the repository default branch.
  - local: merge-base with origin/main, when that ref exists.
  - any merge-base that equals HEAD (a dispatch run on the default branch, an
    undiverged ref) is rejected: comparing a commit with itself is not a
    compatibility check, whatever the event was.
  - unresolvable -- including resolved but unreadable, e.g. a blobless clone
    where `git show` cannot produce the blob: SKIP with the reason locally
    (exit 2), FAIL in CI. Never
    PASS -- a comparison that could not run is not a comparison that
    succeeded (GUARDRAILS.md §3 "checks fail loud, skip honest"; PROOFS rows
    21 and 23 are what this rule is paying for). The stale axis still runs
    and can still FAIL on its own.

CI must check out with `fetch-depth: 0`: at depth 1 neither the PR base SHA
nor origin/<default branch> is present, and every run would FAIL unresolvable.

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
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from common import CheckResult, Finding, repo_root

JSONDict = Dict[str, Any]
GitFn = Callable[[Sequence[str]], Optional[str]]
# A differ: two contract fragments -> (json-pointer, message) per breaking change.
BreakingFn = Callable[[Any, Any], List[Tuple[str, str]]]

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

STALE_MESSAGE = "snapshot stale — regenerate and commit"
FETCH_DEPTH_HINT = "CI needs actions/checkout with fetch-depth: 0"

SCHEMAS_PREFIX = "contracts/schemas/"
OPENAPI_PATH = "contracts/openapi.json"


class Baseline(NamedTuple):
    """What the check compares against. `sha` is None when unresolvable."""

    sha: Optional[str]
    method: str  # how it was resolved (or, when unresolved, why not)


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
    old: Optional[JSONDict], new: Optional[JSONDict], breaking_fn: BreakingFn,
) -> Tuple[str, List[Tuple[str, str]]]:
    """pass | breaking | stale, plus (pointer, message) findings."""
    if old is None and new is None:
        return "pass", []  # gone from tree and export alike; only the baseline axis can judge it
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


# --- baseline resolution ----------------------------------------------

def _git(root: Path) -> GitFn:
    """A git runner returning stdout, or None when the command fails --
    injected so `_resolve_baseline` is testable without a repo."""

    def run(args: Sequence[str]) -> Optional[str]:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    return run


def _in_ci(env: Mapping[str, str]) -> bool:
    return env.get("GITHUB_ACTIONS") == "true" or env.get("CI", "").lower() in {"1", "true"}


def _load_event(env: Mapping[str, str]) -> JSONDict:
    path = env.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).exists():
        return {}
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _commit_present(git: GitFn, sha: str) -> bool:
    return git(["cat-file", "-e", f"{sha}^{{commit}}"]) is not None


def _resolve_baseline(
    env: Mapping[str, str],
    git: GitFn,
    load_event: Callable[[Mapping[str, str]], JSONDict] = _load_event,
) -> Baseline:
    """Resolve what "before this change" means, per environment. Returns a
    Baseline whose `sha` is None (with the reason in `method`) when no
    baseline exists in this checkout -- the caller decides SKIP vs FAIL."""
    if _in_ci(env):
        if env.get("GITHUB_EVENT_NAME") == "pull_request":
            payload = load_event(env)
            pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
            base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
            sha = base.get("sha")
            if isinstance(sha, str) and sha and _commit_present(git, sha):
                return Baseline(sha, "CI pull_request: base sha from the event payload")
            base_ref = env.get("GITHUB_BASE_REF") or ""
            if base_ref:
                resolved = git(["rev-parse", "--verify", f"origin/{base_ref}^{{commit}}"])
                if resolved:
                    return Baseline(resolved, f"CI pull_request: origin/{base_ref}")
            return Baseline(None, "CI pull_request: neither the payload base sha nor "
                                  f"origin/{base_ref or '<base ref>'} is in this checkout ({FETCH_DEPTH_HINT})")
        default_branch = _default_branch(env, load_event)
        event = env.get("GITHUB_EVENT_NAME") or "push"
        if event == "push" and env.get("GITHUB_REF_NAME") == default_branch:
            # On the default branch the merge-base IS HEAD, so it would compare the
            # commit with itself and report a compatibility check that never ran.
            # What main had before this push is the push event's `before` sha.
            before = load_event(env).get("before")
            if isinstance(before, str) and set(before) != {"0"} and _commit_present(git, before):
                return Baseline(before, f"CI push to {default_branch}: previous tip (event.before)")
            return Baseline(None, f"CI push to {default_branch}: event.before is unavailable or not in "
                                  f"this checkout, and merge-base would be HEAD itself ({FETCH_DEPTH_HINT})")
        merge_base = git(["merge-base", "HEAD", f"origin/{default_branch}"])
        if merge_base:
            return _unless_head(git, merge_base, f"CI {event}: merge-base with origin/{default_branch}")
        return Baseline(None, f"CI {event}: no merge-base with "
                              f"origin/{default_branch} in this checkout ({FETCH_DEPTH_HINT})")

    merge_base = git(["merge-base", "HEAD", "origin/main"])
    if merge_base:
        return _unless_head(git, merge_base, "local: merge-base with origin/main")
    return Baseline(None, "local: origin/main is not present (offline clone or no remote-tracking ref)")


def _unless_head(git: GitFn, merge_base: str, method: str) -> Baseline:
    """A merge-base equal to HEAD -- on the default branch, or any ref that
    hasn't diverged -- would compare the commit with itself and call it a
    compatibility check. That is not a baseline; route it to the unresolved
    policy (SKIP locally, FAIL in CI) instead of vouching for nothing."""
    head = git(["rev-parse", "HEAD"])
    if head is not None and head == merge_base:
        return Baseline(None, f"{method} is HEAD itself — no divergence to compare")
    return Baseline(merge_base, method)


def _default_branch(env: Mapping[str, str], load_event: Callable[[Mapping[str, str]], JSONDict]) -> str:
    payload = load_event(env)
    repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    branch = repo.get("default_branch")
    return branch if isinstance(branch, str) and branch else "main"


class BaselineContracts(NamedTuple):
    """What the baseline commit published. `error` distinguishes "git could
    not read it" from "the baseline had none" -- collapsing those two would
    disable the compatibility gate while still reporting a baseline sha."""

    schemas: Dict[str, JSONDict]
    openapi: Optional[JSONDict]
    error: Optional[str]


def _baseline_contracts(git: GitFn, sha: str) -> BaselineContracts:
    """The committed contracts as of `sha`, read from git rather than from a
    working-tree checkout. A file absent from the tree listing predates the
    baseline (the baseline axis treats it as "new since"); a file that is
    listed but unreadable -- blobless/partial clone, corrupt blob -- is an
    error, and the caller downgrades the whole baseline to unresolved."""
    listing = git(["ls-tree", "--name-only", "-r", sha, "--", "contracts/"])
    if listing is None:
        return BaselineContracts({}, None, f"git ls-tree failed at {sha[:9]}")
    schemas: Dict[str, JSONDict] = {}
    openapi: Optional[JSONDict] = None
    for line in listing.splitlines():
        path = line.strip()
        if not (path == OPENAPI_PATH or (path.startswith(SCHEMAS_PREFIX) and path.endswith(".json"))):
            continue
        blob = git(["show", f"{sha}:{path}"])
        if blob is None:
            return BaselineContracts({}, None, f"git show {sha[:9]}:{path} failed (blobless or partial clone?)")
        try:
            value = json.loads(blob)
        except ValueError:
            return BaselineContracts({}, None, f"{path} at {sha[:9]} is not valid JSON")
        if path == OPENAPI_PATH:
            openapi = value
        else:
            # Keyed by the path under contracts/schemas/ minus .json, matching
            # `_read_dir`: both sides of a comparison must traverse alike, or a
            # nested schema reads as "removed since baseline".
            schemas[path[len(SCHEMAS_PREFIX):-len(".json")]] = value
    return BaselineContracts(schemas, openapi, None)


def _classify_baseline(
    old: Optional[JSONDict], new: Optional[JSONDict], breaking_fn: BreakingFn,
) -> List[Tuple[str, str]]:
    """Breaking differences between the baseline snapshot and a fresh export.
    Non-breaking drift is not a finding here: evolving away from the baseline
    is the point. A contract that didn't exist at the baseline can't break."""
    if old is None:
        return []
    if new is None:
        return [("", "removed since baseline")]
    if old == new:
        return []
    return list(breaking_fn(old, new))


class Artifact(NamedTuple):
    label: str
    breaking_fn: BreakingFn
    baseline: Optional[JSONDict]
    committed: Optional[JSONDict]
    regenerated: Optional[JSONDict]


def _artifacts(
    baseline_schemas: Dict[str, JSONDict],
    committed_schemas: Dict[str, JSONDict],
    regenerated_schemas: Dict[str, JSONDict],
    baseline_openapi: Optional[JSONDict],
    committed_openapi: Optional[JSONDict],
    regenerated_openapi: Optional[JSONDict],
) -> List[Artifact]:
    items = [
        Artifact(f"{SCHEMAS_PREFIX}{name}.json", _diff_schema_breaking,
                 baseline_schemas.get(name), committed_schemas.get(name), regenerated_schemas.get(name))
        for name in sorted(set(baseline_schemas) | set(committed_schemas) | set(regenerated_schemas))
    ]
    items.append(Artifact(OPENAPI_PATH, _diff_openapi_breaking,
                          baseline_openapi, committed_openapi, regenerated_openapi))
    return items


def _evaluate(artifacts: List[Artifact], baseline_available: bool) -> Tuple[List[Finding], str, int]:
    """Both axes over every artifact: breaking vs the baseline (only when a
    baseline was resolved) and breaking/stale vs the committed snapshot.
    Returns the findings, a summary, and how many artifacts the baseline axis
    actually compared -- a zero there must reach the detail line, or "the
    baseline had nothing" reads exactly like "nothing broke". Pure: the
    selftest drives it with in-memory contracts."""
    findings: List[Finding] = []
    breaking_files: List[str] = []
    stale_files: List[str] = []
    compared = 0

    for art in artifacts:
        if baseline_available and art.baseline is not None:
            compared += 1
        baseline_items = (
            _classify_baseline(art.baseline, art.regenerated, art.breaking_fn) if baseline_available else []
        )
        if baseline_items:
            breaking_files.append(art.label)
            for pointer, msg in baseline_items:
                findings.append(Finding(art.label, 0, f"{pointer}: {msg} (breaking vs baseline)"))

        status, items = _classify(art.committed, art.regenerated, art.breaking_fn)
        fresh = [item for item in items if item not in baseline_items]
        if status == "breaking":
            if art.label not in breaking_files:
                breaking_files.append(art.label)
            for pointer, msg in fresh:
                findings.append(Finding(art.label, 0, f"{pointer}: {msg} (breaking)"))
        elif status == "stale":
            stale_files.append(art.label)
            for pointer, msg in fresh:
                findings.append(Finding(art.label, 0, f"{pointer}: {msg}" if pointer else msg))

    if breaking_files:
        return findings, "breaking: " + ", ".join(breaking_files), compared
    if stale_files:
        return findings, STALE_MESSAGE + " (" + ", ".join(stale_files) + ")", compared
    schema_count = sum(1 for art in artifacts if art.label != OPENAPI_PATH)
    return findings, f"{schema_count} schemas + openapi.json match their snapshots", compared


def _read_dir(schemas_dir: Path) -> Dict[str, JSONDict]:
    """Schemas on disk, keyed by path under the directory minus .json --
    recursive, to match how the baseline side lists them."""
    loaded: Dict[str, JSONDict] = {}
    if not schemas_dir.exists():
        return loaded
    for path in sorted(schemas_dir.rglob("*.json")):
        value = _load_json(path)
        if value is not None:
            loaded[path.relative_to(schemas_dir).with_suffix("").as_posix()] = value
    return loaded


def run() -> CheckResult:
    root = repo_root()
    if shutil.which("uv") is None:
        return CheckResult("S6", "contract compatibility", "SKIP", [], "uv not available")
    schemas_dir = root / "contracts" / "schemas"
    openapi_path = root / OPENAPI_PATH
    if not schemas_dir.exists() or not any(schemas_dir.glob("*.json")) or not openapi_path.exists():
        return CheckResult("S6", "contract compatibility", "SKIP", [], "contracts/ snapshots not generated yet")

    git = _git(root)
    baseline = _resolve_baseline(os.environ, git)
    contracts = BaselineContracts({}, None, None)
    if baseline.sha is not None:
        contracts = _baseline_contracts(git, baseline.sha)
        if contracts.error:
            # Resolved but unreadable is not a comparison either.
            baseline = Baseline(None, f"{baseline.method}, but {contracts.error}")

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

        artifacts = _artifacts(contracts.schemas, _read_dir(schemas_dir), _read_dir(tmp_schemas),
                               contracts.openapi, _load_json(openapi_path), _load_json(tmp_openapi))
        findings, detail, compared = _evaluate(artifacts, baseline.sha is not None)

    if baseline.sha is None:
        detail = f"{detail}; baseline unresolved — {baseline.method}"
    else:
        detail = (f"{detail} [baseline {baseline.sha[:9]}: {compared}/{len(artifacts)} "
                  f"contracts compared — {baseline.method}]")
    status = _status_for(baseline.sha is not None, bool(findings), _in_ci(os.environ))
    return CheckResult("S6", "contract compatibility", status, findings, detail)


def _status_for(baseline_resolved: bool, has_findings: bool, in_ci: bool) -> str:
    """FAIL on findings; otherwise PASS only when a baseline was actually
    compared. An unresolved baseline is a SKIP locally (with the reason) and
    a FAIL in CI -- never PASS (GUARDRAILS.md §3, PROOFS rows 21 and 23)."""
    if has_findings:
        return "FAIL"
    if baseline_resolved:
        return "PASS"
    return "FAIL" if in_ci else "SKIP"


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

    ok = _selftest_baseline_resolution(ok)
    ok = _selftest_baseline_axis(ok)

    print(f"S6 selftest: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _fake_git(answers: Dict[str, Optional[str]]) -> GitFn:
    """A git stub: first key that is a prefix of the joined argv wins."""

    def run(args: Sequence[str]) -> Optional[str]:
        joined = " ".join(args)
        for prefix, value in answers.items():
            if joined.startswith(prefix):
                return value
        return None

    return run


def _check(ok: bool, label: str, condition: bool, actual: Any) -> bool:
    if condition:
        print(f"selftest: {label}")
        return ok
    print(f"selftest FAIL: {label} — got {actual!r}")
    return False


def _selftest_baseline_resolution(ok: bool) -> bool:
    """All four resolution cases, including the unresolvable one."""
    pr_env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": "main"}
    payload = {"pull_request": {"base": {"sha": "b" * 40}}}

    # 1. CI pull_request -> base sha from the event payload
    base = _resolve_baseline(pr_env, _fake_git({"cat-file -e": ""}), lambda _env: payload)
    ok = _check(ok, "CI pull_request resolves the payload base sha",
                base.sha == "b" * 40 and "event payload" in base.method, base)

    # 1b. payload sha absent from a shallow checkout -> origin/<base ref> fallback
    base = _resolve_baseline(pr_env, _fake_git({"rev-parse --verify origin/main": "c" * 40}), lambda _env: {})
    ok = _check(ok, "CI pull_request falls back to origin/<base ref>",
                base.sha == "c" * 40 and "origin/main" in base.method, base)

    # 2. CI push (branch != default) -> merge-base with the default branch
    push_env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "topic"}
    base = _resolve_baseline(push_env, _fake_git({"merge-base HEAD origin/trunk": "d" * 40,
                                                 "rev-parse HEAD": "a" * 40}),
                             lambda _env: {"repository": {"default_branch": "trunk"}})
    ok = _check(ok, "CI push resolves merge-base with the default branch",
                base.sha == "d" * 40 and "merge-base with origin/trunk" in base.method, base)

    # 2a. CI push TO the default branch -> event.before; merge-base would be HEAD
    # itself, i.e. a compatibility check comparing the commit with itself.
    main_push = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "main"}
    base = _resolve_baseline(main_push, _fake_git({"cat-file -e": "", "merge-base": "h" * 40}),
                             lambda _env: {"before": "9" * 40})
    ok = _check(ok, "CI push to the default branch uses event.before, not merge-base",
                base.sha == "9" * 40 and "event.before" in base.method, base)
    base = _resolve_baseline(main_push, _fake_git({"merge-base": "h" * 40}), lambda _env: {"before": "0" * 40})
    ok = _check(ok, "CI push to the default branch with no usable event.before is unresolved",
                base.sha is None, base)

    # 2b. workflow_dispatch takes the same path (it is neither push nor pull_request)
    dispatch_env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"}
    base = _resolve_baseline(dispatch_env, _fake_git({"merge-base HEAD origin/main": "e" * 40,
                                                     "rev-parse HEAD": "a" * 40}), lambda _env: {})
    ok = _check(ok, "CI workflow_dispatch resolves merge-base with origin/main",
                base.sha == "e" * 40, base)

    # 3. local -> merge-base with origin/main
    base = _resolve_baseline({}, _fake_git({"merge-base HEAD origin/main": "f" * 40,
                                            "rev-parse HEAD": "a" * 40}), lambda _env: {})
    ok = _check(ok, "local resolves merge-base with origin/main",
                base.sha == "f" * 40 and base.method.startswith("local"), base)

    # 3b. a merge-base equal to HEAD (dispatch on the default branch, an
    # undiverged ref) is X-vs-X, not a baseline -- whatever the event is
    same = _fake_git({"merge-base": "a" * 40, "rev-parse HEAD": "a" * 40})
    for label, env_ in (("CI workflow_dispatch", dispatch_env), ("local", {})):
        base = _resolve_baseline(env_, same, lambda _env: {})
        ok = _check(ok, f"{label}: merge-base equal to HEAD is not accepted as a baseline",
                    base.sha is None and "HEAD itself" in base.method, base)

    # 4. unresolvable -> no sha, reason carried; SKIP locally, FAIL in CI
    nothing = _fake_git({})
    local_unresolved = _resolve_baseline({}, nothing, lambda _env: {})
    ci_unresolved = _resolve_baseline(pr_env, nothing, lambda _env: {})
    ok = _check(ok, "unresolvable baseline reports no sha with a reason, locally and in CI",
                local_unresolved.sha is None and "origin/main" in local_unresolved.method
                and ci_unresolved.sha is None and FETCH_DEPTH_HINT in ci_unresolved.method,
                (local_unresolved, ci_unresolved))
    ok = _check(ok, "unresolvable baseline is a SKIP locally and a FAIL in CI",
                _status_for(False, False, in_ci=False) == "SKIP"
                and _status_for(False, False, in_ci=True) == "FAIL"
                and _status_for(True, False, in_ci=True) == "PASS"
                and _status_for(False, True, in_ci=False) == "FAIL",
                [_status_for(r, f, c) for r in (True, False) for f in (True, False) for c in (True, False)])
    ok = _check(ok, "CI is detected from GITHUB_ACTIONS or CI",
                not _in_ci({}) and _in_ci({"GITHUB_ACTIONS": "true"}) and _in_ci({"CI": "true"}),
                (_in_ci({}), _in_ci({"GITHUB_ACTIONS": "true"})))

    # 5. baseline resolved but unreadable (blobless/partial clone) -> error, which
    # run() folds into the unresolved path rather than comparing nothing quietly
    listed = f"{SCHEMAS_PREFIX}widget.json\n{OPENAPI_PATH}\n"
    unreadable = _baseline_contracts(_fake_git({"ls-tree": listed}), "a" * 40)
    ok = _check(ok, "baseline whose blobs cannot be read is an error, not an empty baseline",
                unreadable.error is not None and not unreadable.schemas, unreadable)
    listable = _baseline_contracts(_fake_git({"ls-tree": listed, "show": "{}"}), "a" * 40)
    ok = _check(ok, "readable baseline blobs load with no error",
                listable.error is None and set(listable.schemas) == {"widget"} and listable.openapi == {},
                listable)
    empty = _baseline_contracts(_fake_git({"ls-tree": ""}), "a" * 40)
    ok = _check(ok, "baseline predating contracts/ is empty without an error",
                empty.error is None and not empty.schemas and empty.openapi is None, empty)
    broken = _baseline_contracts(_fake_git({}), "a" * 40)
    ok = _check(ok, "baseline whose tree cannot be listed is an error",
                broken.error is not None, broken)
    return ok


def _selftest_baseline_axis(ok: bool) -> bool:
    """The headline case (#25): a breaking change committed TOGETHER with its
    regenerated snapshot is self-consistent, so the stale axis sees nothing --
    the baseline axis must still FAIL it."""
    broken = json.loads(json.dumps(_BASE_SCHEMA))
    del broken["properties"]["status"]
    arts = [Artifact("contracts/schemas/widget.json", _diff_schema_breaking, _BASE_SCHEMA, broken, broken)]

    findings, detail, compared = _evaluate(arts, baseline_available=True)
    ok = _check(ok, "breaking change + regenerated snapshot in one commit FAILs against the baseline",
                any("removed property (breaking vs baseline)" in f.message for f in findings)
                and detail.startswith("breaking:") and compared == 1, (findings, detail, compared))

    findings, detail, compared = _evaluate(arts, baseline_available=False)
    ok = _check(ok, "without a baseline the same commit looks clean (the hole #25 closes)",
                not findings and compared == 0, (findings, detail, compared))

    # additive drift away from the baseline is evolution, not a finding
    widened = json.loads(json.dumps(_BASE_SCHEMA))
    widened["properties"]["note"] = {"type": "string"}
    findings, _, _ = _evaluate(
        [Artifact("contracts/schemas/widget.json", _diff_schema_breaking, _BASE_SCHEMA, widened, widened)],
        baseline_available=True)
    ok = _check(ok, "additive change against the baseline is not a finding", not findings, findings)

    # a contract removed since the baseline is breaking even if nothing else moved
    findings, _, _ = _evaluate(
        [Artifact("contracts/schemas/widget.json", _diff_schema_breaking, _BASE_SCHEMA, None, None)],
        baseline_available=True)
    ok = _check(ok, "contract removed since the baseline is breaking",
                any("removed since baseline" in f.message for f in findings), findings)

    # the stale axis survives: changed code, snapshot not regenerated
    findings, detail, _ = _evaluate(
        [Artifact("contracts/schemas/widget.json", _diff_schema_breaking, _BASE_SCHEMA, _BASE_SCHEMA, widened)],
        baseline_available=True)
    ok = _check(ok, "stale snapshot still reported when the change itself is compatible",
                any(STALE_MESSAGE in f.message for f in findings), (findings, detail))
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    result = run()
    for f in result.findings:
        print(f"{f.path}: {f.message}")
    print(f"S6 {result.status} ({result.detail})")
    # 0 PASS / 1 FAIL / 2 SKIP — the runner needs to tell a skip from a pass.
    sys.exit({"PASS": 0, "FAIL": 1, "SKIP": 2}[result.status])
