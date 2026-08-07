"""Deterministic JSON Schema export for every published contract (SPEC.md
§8; GUARDRAILS.md S6, issue #7's contract-compatibility check consumes this
snapshot).

Run via `uv run --package steward-schemas steward-schemas-export-schemas`
(console script, `[project.scripts]`) or `python -m
steward_schemas.export_schemas`. Writes one file per `CONTRACTS` entry
(`<name>.json`) under `contracts/schemas/` by default; each schema is dumped
with sorted keys and stable separators so re-running produces byte-identical
output regardless of any internal, non-semantic dict-ordering differences.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from steward_schemas import CONTRACTS

DEFAULT_RELATIVE_PATH = Path("contracts") / "schemas"


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "GUARDRAILS.md").exists():
            return candidate
    raise RuntimeError("could not locate repo root (no GUARDRAILS.md found above " + str(start) + ")")


def schema_json(name: str) -> str:
    """A single contract's JSON Schema as deterministic, sorted-key text."""
    schema = CONTRACTS[name].model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    out_dir = Path(args[0]) if args else _find_repo_root(Path(__file__).resolve()) / DEFAULT_RELATIVE_PATH
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in sorted(CONTRACTS):
        (out_dir / f"{name}.json").write_text(schema_json(name))
    print(f"wrote {len(CONTRACTS)} schemas to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
