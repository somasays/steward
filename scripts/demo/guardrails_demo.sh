#!/bin/sh
# Shows the fitness gate rejecting planted guardrail violations, then cleaning up.
# Reproduces PROOFS.md rows 2-4 (GUARDRAILS.md S1/S3). Run: make demo-guardrails
set -u

VICTIM_DIR="packages/steward-retrieval/src/steward_retrieval"
VICTIM="$VICTIM_DIR/_demo_violation.py"
HOME_DIR="packages/steward-agents/src/steward_agents"
HOME_FILE="$HOME_DIR/_demo_contained.py"
RULE="──────────────────────────────────────────────────────────────────────────────"

cleanup() { rm -f "$VICTIM" "$HOME_FILE"; }
trap cleanup EXIT INT TERM

printf '\n%s\n  Guardrails demo: plant violations, watch the gate reject them\n%s\n' "$RULE" "$RULE"

cat > "$VICTIM" <<'PY'
import crewai
import langgraph
from openai import OpenAI


def lookup(table: str, uid: str) -> str:
    return f"SELECT * FROM {table} WHERE id = {uid}"
PY

echo
echo "Planted in packages/steward-retrieval (a package that owns none of these):"
echo "  - import crewai      -> kitchen-sink framework, banned everywhere (I9)"
echo "  - import langgraph   -> contained to steward-agents (I9)"
echo "  - from openai import -> contained to steward-llm (I2)"
echo "  - f-string SQL       -> string-built SQL is banned outright (I5)"
echo
echo "\$ python3 scripts/fitness/run.py"
python3 scripts/fitness/run.py 2>&1 | grep -E "^  S1|^  S3|^FAIL" | head -8
echo
echo "What S1 and S3 actually saw:"
uv run ruff check --select TID251,S608 --output-format concise "$VICTIM" 2>&1 | sed 's/^/  /' | head -8
echo
echo "Exit code was non-zero, so the pre-commit hook and CI both refuse this commit."
echo "(G1/G2 also fail here -- hygiene noise from the planted file, not the point.)"

rm -f "$VICTIM"

printf '\n%s\n  The same langgraph import inside its home package is fine\n%s\n' "$RULE" "$RULE"
cat > "$HOME_FILE" <<'PY'
import langgraph  # noqa: F401  contained here by design
PY
echo
echo "\$ python3 scripts/fitness/run.py   # file now lives in packages/steward-agents"
python3 scripts/fitness/run.py 2>&1 | grep -E "^  S1|^  S5|fitness:" | head -5
echo
echo "Containment is a property of where code lives, checked mechanically -- not a code-review habit."
