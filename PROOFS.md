# Proof log

Append-only. Every entry: a claim, the command that demonstrates it, the observed result. If it can't be reproduced by running the command, it doesn't belong here. Newest entries at the bottom. (Check IDs follow the current GUARDRAILS.md scheme; entries predating a renumber are restated in current IDs — the claims are re-runnable.)

| # | Date | Claim | Reproduce | Observed |
|---|------|-------|-----------|----------|
| 1 | 2026-08-06 | Static fitness checks run with no venv, no dependencies (stdlib Python 3.9+) | `python3 scripts/fitness/run.py` | S1–S4, S7, G4 execute; not-yet-landed checks report `SKIP` with reasons; exit 0 |
| 2 | 2026-08-06 | Guardrail violations fail the gate with file:line, mapped to invariants | plant a file in `packages/` importing `crewai` + an f-string SQL query, run `python3 scripts/fitness/run.py` | S1 FAIL (`banned framework import 'crewai' (I9)`), S3 FAIL (`SQL assembled from strings (I5)`); exit 1 |
| 3 | 2026-08-06 | Framework containment is mechanical: `langgraph` passes in `steward-agents`, fails anywhere else | same file importing `langgraph` under `steward-agents/` vs `steward-retrieval/` | agents: no finding; retrieval: `'langgraph' allowed only in steward-agents (I2/I9)` |
| 4 | 2026-08-06 | Provider SDK containment: `openai` import outside `steward-llm` is rejected | file importing `openai` in `steward-retrieval/`, run S1 | `'openai' allowed only in steward-llm (I2/I9)`; exit 1 |
| 5 | 2026-08-06 | Commit gate enforces issue-driven work | `echo "feat(x): thing" > m && python3 scripts/fitness/check_commit_msg.py m` | rejected (feat needs `#N`); with `(#12)` appended: exit 0; malformed subject: rejected |
| 6 | 2026-08-06 | uv workspace bootstraps clean from scratch, with S1/S2/G1/G2/G3 active and passing | `rm -rf .venv && uv sync --all-packages && python3 scripts/fitness/run.py` | `uv sync` installs 7 workspace members (5 packages + 2 services) plus dev tools with no errors; fitness suite: S1 PASS (12 files scanned), S2 PASS (5/2000 effective LOC), G1 PASS (`ruff check .`), G2 PASS (`mypy --strict packages`, no issues in 5 source files), G3 PASS (5 passed, 100% branch coverage on `packages/`); exit 0 |
| 7 | 2026-08-06 | Every tracked file is covered by the file-dependency graph; an unmapped file fails the gate | `touch orphan.md && python3 scripts/fitness/check_filegraph.py; rm orphan.md` | `orphan.md: not covered by any filegraph.json pattern`; S7 FAIL, exit 1; after removal S7 PASS |
