"""Deterministic OpenAPI export (SPEC.md §8: "OpenAPI-first"; GUARDRAILS.md
S6, issue #7's future contract-compatibility check consumes this snapshot).

Run via `uv run --package steward-api steward-api-export-openapi` (console
script, `[project.scripts]`) or `python -m steward_api.export_openapi`.
Writes `contracts/openapi.json` by default; the schema is dumped with sorted
keys so re-running produces byte-identical output regardless of any
internal, non-semantic dict-ordering differences.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from steward_llm import gateway_config_from_env

from steward_api.app import create_app

DEFAULT_RELATIVE_PATH = Path("contracts") / "openapi.json"


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "GUARDRAILS.md").exists():
            return candidate
    raise RuntimeError("could not locate repo root (no GUARDRAILS.md found above " + str(start) + ")")


def openapi_json() -> str:
    """The app's OpenAPI schema as deterministic, sorted-key JSON text."""
    schema = create_app().openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Write the schema, having first refused a gateway config that routes off
    the allowlist (I15).

    An exporter cannot call a model, so the check is not protecting this process
    from itself: it is what makes "every entry point refuses" a property of the
    repo rather than of the two entry points someone thought of. Exempting the
    ones that look harmless is how the next one gets exempted too, and H12 boots
    what `[project.scripts]` declares, not a list of the interesting ones.
    """
    gateway_config_from_env()
    args = sys.argv[1:] if argv is None else argv
    out_path = Path(args[0]) if args else _find_repo_root(Path(__file__).resolve()) / DEFAULT_RELATIVE_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(openapi_json())
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
