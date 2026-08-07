# Fitness Functions & Enforcement

**Status:** Binding · applies to every commit · enforced by `make fitness`, git hooks, CI, and scheduled runs

`ARCHITECTURE.md` defines what must stay true: functional requirements (FR), quantified non-functionals (N1–N10), and invariants (I1–I14). This document defines how those properties are **continuously measured**. The fitness functions are the leash that lets the system evolve fast — with heavy agent assistance — without its architecture or its guarantees eroding.

Coverage rule: **every N-row and I-row in ARCHITECTURE.md is protected by at least one fitness function below** (see the matrix in §2). A change that adds an invariant or NFR without a protecting check is incomplete.

Checks are tiered by *how* they measure, because different properties fail at different speeds:

- **Tier S — static architecture checks**: seconds, every commit, no environment needed
- **Tier H — behavioral harnesses**: minutes, every PR, real components against an ephemeral Postgres (`pgserver` ships the binaries — no Docker, on a laptop or in CI)
- **Tier B — benchmarks & evals**: PR when affected paths change, plus nightly; golden datasets and load fixtures
- **Tier P — production fitness**: continuous, on the live system
- **Hygiene (G)** — generic code health. Blocking, but deliberately *not* called a fitness function: it protects code quality, not this system's architecture

## 1. The catalog

### Tier S — static architecture checks (every commit, seconds)

| ID | Fitness function | Protects | Measurement | Status |
|----|------------------|----------|-------------|--------|
| S1 | Boundaries & containment | I2, I4, I9, N9 | import-linter contracts (layers `services → packages`, declared package edges, schemas independence — `pyproject.toml` `[tool.importlinter]`) + ruff TID251 banned-api (kitchen-sink frameworks banned everywhere; `langgraph`/`litellm`/provider SDKs unbanned only in their home package's own `pyproject.toml`, which overrides the root banned-api table). Schemas purity additionally enforced by isolated `uv run --isolated --package steward-schemas` import | active (issue #9) |
| S2 | Runtime size budget | I9 | `check_loc_budget.py` — effective LOC of `packages/steward-agents` ≤ 2,000 (custom: no tool does per-package budgets). SKIPs while the package is a skeleton: a budget check over 5 lines would read as "the runtime fits" when there is no runtime (issue #21) | skips until M1 |
| S3 | SQL string-assembly ban | I5, N7 | ruff S608, selected globally (including `scripts/fitness`, which is otherwise style-exempt) | active (issue #9) |
| S4 | Prompt literal ban | I10 | `check_prompt_hygiene.py` — prompt-shaped literals outside `prompts/` (custom: domain-specific) | active |
| S5 | Public-surface lock | I9, I3 | `check_surface.py` — no contained-module type in any package's public signatures, class bases, or re-exports. The classification logic itself is covered by `--selftest` (planted-leak + clean fixtures), swept automatically by S8 | active |
| S6 | Contract compatibility | I3, N9 | `check_contracts.py` — regenerates JSON Schema for every published Pydantic contract and the exported OpenAPI spec, then diffs on two axes: against the snapshot **at the baseline commit** (read from git), which is the compatibility gate, and against the snapshot committed in the working tree, which catches a forgotten regeneration. A stdlib differ (no external oasdiff binary) classifies removed model/property/path/method, type changes, new-required properties and enum narrowing as breaking (FAIL) on either axis — so a breaking change committed together with its regenerated snapshot still FAILs; other drift from the working-tree snapshot is stale (FAIL); additive drift from the baseline is evolution (PASS). Baseline: PR base sha on `pull_request`, the previous tip (`event.before`) on a push to the default branch, merge-base with the default branch on other CI events, merge-base with `origin/main` locally; unresolvable — no baseline in the checkout, any candidate equal to HEAD (comparing a commit with itself, on every resolution path), or resolved-but-unreadable, e.g. a blobless clone — → SKIP with the reason locally, **FAIL in CI**, never PASS, and the detail line carries the baseline sha and how many contracts it compared. CI checks out with `fetch-depth: 0` for this. The classification logic (breaking/stale/pass, baseline resolution, the baseline-axis-vs-stale-axis split) is covered by `--selftest` against in-memory fixtures, swept automatically by S8 — this is the only local check that catches a regression here, since S6 itself SKIPs on an undiverged branch | active |
| S7 | File-graph coverage | doc consistency | `check_filegraph.py` — `scripts/fitness/filegraph.json` maps every file pattern to its impacted files; changing a file means updating/verifying its dependents (workflow law, CLAUDE.md); S7 fails if any tracked file is outside the graph | active |
| S8 | Checker self-tests | I3, I9, N9 (meta: correctness of S5, S6) | `check_selftests.py` — discovers every `check_*.py` that declares a `--selftest` branch (registry by convention, not a hardcoded list) and runs it; any nonzero exit FAILs the suite. Closes the gap where S5/S6's classification logic — real software, not mechanical scripts — had a selftest nothing ran automatically (issue #32) | active |

### Tier H — behavioral harnesses (every PR; `pytest -m invariants` / `-m acceptance` against an ephemeral Postgres)

These run real components — Postgres, the queue, the runtime with a stub LLM — and assert system behavior. They bind to *registries* (task handlers, repositories, agents), so a newly added component is on the leash automatically.

| ID | Fitness function | Protects | Measurement | Lands |
|----|------------------|----------|-------------|-------|
| H1 | Idempotency | I8 | every registered task handler executed twice with the same payload → byte-identical end state | active |
| H2 | Crash recovery | N1 | SIGKILL a worker mid-run → run completes after restart with ≤ 1 step re-executed | M0/M5 chaos |
| H3 | No lost/ghost tasks | I8, N1 | crash injection around enqueue/claim/complete → task set matches state-machine expectations exactly | active |
| H4 | Budget termination | I12, N6 | agent given an impossible goal with 1-step/1-cent budget → terminates `budget_exceeded`, never hangs, cost ≤ cap | wall-clock active (worker); step/token/cost with the M1 agent loop |
| H5 | Audit completeness | I7, N8 | every repository mutation produces an audit row in the same transaction (asserted over the repository registry) | M1 |
| H6 | Trace completeness | I7, N8 | a finished run's Langfuse trace contains the full expected span tree; every generation span carries a prompt version | partial: trace_id is NOT NULL and asserted end to end by H11; span-tree assertions land with the M1 agent loop |
| H7 | Masking canary | I6, N7 | canary secrets planted in fixture data; assert they never appear in any captured prompt or trace payload | M1 |
| H8 | Index rebuild convergence | I1, N9 | wipe Qdrant + ES, run rebuild job → search results identical to pre-wipe golden results | M2 |
| H9 | Citation resolution | N2, FR8 | every citation in every golden answer resolves to a live asset/document id | M3 |
| H10 | Governance gating | I13, FR9 | governance actions land in `pending_review` unless a policy explicitly auto-approves; the policy id is on the audit row | M1 |
| H11 | Milestone acceptance | FR1–FR10 | executable exit criterion of each shipped milestone (SPEC §12); once shipped, runs forever | active (M0: API → queue → worker → succeeded) |

### Tier B — benchmarks & evals (affected-path PRs + nightly)

Datasets live in Langfuse; runnable identically on a laptop and CI (`steward evals run`). Thresholds are ratchets: raising is routine, lowering follows §5 amendment.

| ID | Fitness function | Protects | Threshold | Lands |
|----|------------------|----------|-----------|-------|
| B1 | Retrieval quality | N3 | recall@8 ≥ 0.90, MRR@8 ≥ 0.75, no metric −2 pts vs main | M2 |
| B2 | Classification quality | N2 | PII recall ≥ 0.95, precision ≥ 0.90 | M1 |
| B3 | Doc groundedness | N2 | calibrated judge (κ ≥ 0.7 vs human subset): zero ungrounded claims, rubric mean ≥ 4.0/5 | M1 |
| B4 | Answer faithfulness | N2 | ≥ 0.95, every claim cited | M3 |
| B5 | Triage accuracy | N2 | root-cause hit@3 ≥ 0.8 on replayed incidents | M4 |
| B6 | Latency budgets | N4 | search P95 ≤ 150/400 ms; ask P50 ≤ 10 s; API P99 ≤ 500 ms — measured on the fixture stack | M2+ |
| B7 | Scan throughput | N5 | 500-table fixture scan ≤ 30 min | M5 |
| B8 | Cost regression | N6 | cost per task type ≤ 1.2× tracked baseline | M1+ |
| B9 | Provider independence | I14, N9 | B1–B5 re-run with aliases bound to the fallback provider — pass without code change | M3, nightly |

### Tier P — production fitness (continuous)

| ID | Fitness function | Protects | Measurement |
|----|------------------|----------|-------------|
| P1 | Online quality | N2, I11 | 10% of production runs judge-scored async; alert on degradation vs offline baseline |
| P2 | SLO burn | N4 | Prometheus SLO rules on the B6 budgets |
| P3 | Cost guard | N6 | gateway budget caps (hard 429) + daily cost trend alerts |
| P4 | Queue health | N1 | dead-task count, queue age > 10 min alerts |
| P5 | Derived-index drift | I1 | nightly reconciliation diff Postgres ↔ Qdrant/ES; alert on divergence |

### Hygiene (G) — blocking, but not fitness

| ID | Check | Mechanism |
|----|-------|-----------|
| G1 | Lint & format | ruff check + format --check |
| G2 | Strict types | mypy --strict on `packages/` and `services/` in one invocation — each member's `src/` is listed in `mypy_path` (with `explicit_package_bases`) so sibling modules with identical relative paths resolve to distinct dotted names instead of colliding (this is also what turns typed-contract conventions (I3, I6-by-construction) into compile-time enforcement) |
| G3 | Tests & coverage | pytest, branch coverage ≥ 85% on `packages/` |
| G4 | Secret scan | gitleaks — full history in the CI `gitleaks` job (hard gate); staged diff (`gitleaks protect --staged`) as a pre-commit pass-through when gitleaks is installed locally, otherwise the hook prints an install hint and continues. `make fitness` runs a repo-wide `gitleaks detect` when the binary is available, and reports `SKIP` (never a false `PASS`) when it isn't |
| G5 | Commit discipline | commit-msg hook: Conventional Commits; feat/fix/refactor/perf must reference an issue (custom: issue-ref rule is project policy) |

Build-vs-buy rule: a check is hand-rolled only when no maintained tool has the semantics (S2, S4, S5, G5). S1/S3/G4 are tool-backed (import-linter, ruff, gitleaks); their bootstrap stdlib implementations were deleted once parity was proven (issue #9, `PROOFS.md`).

## 2. Coverage matrix

| Property | Protected by |
|----------|--------------|
| I1 sole system of record | H8, P5 |
| I2 gateway-only model access | S1 |
| I3 typed, versioned contracts | S6, S5, G2 |
| I4 dependency flow | S1 |
| I5 read-only sources, no string SQL | S3 (+ read-only role: fixture harness asserts writes fail — part of H11 M1 scenario) |
| I6 masking | H7 (+ by construction via typed prompt-builder inputs, G2) |
| I7 traced & audited | H5, H6 |
| I8 idempotent, transactional tasks | H1, H3 |
| I9 framework containment & size | S1, S2, S5 |
| I10 versioned prompts | S4 |
| I11 eval-gated behavior | B1–B5 gates, P1 |
| I12 bounded autonomy | H4 |
| I13 policy-gated governance | H10 |
| I14 provider swap = config | B9 |
| N1 recoverability | H2, H3, P4 |
| N2 output correctness | B2–B5, H9, P1 |
| N3 retrieval quality | B1 |
| N4 latency | B6, P2 |
| N5 throughput | B7 |
| N6 cost | H4, B8, P3 |
| N7 privacy/security | H7, S3, G4 |
| N8 observability | H5, H6 |
| N9 evolvability | S1, S6, H8, B9 |
| N10 operability | M6 acceptance scenario (H11) + Argo rollout analysis |
| Doc/file consistency | S7 (coverage) + filegraph propagation in the workflow |

## 3. Rules of engagement

- **No bypass culture.** `git commit --no-verify` is for emergencies; CI re-runs everything, so a bypassed hook only delays the failure.
- **Suppressions are visible and justified.** Inline pragmas (`# fitness: allow-sql-string`, `# fitness: allow-prompt-literal`, ruff `noqa`) require a same-line reason; CI reports pragma counts and reviewers treat increases as design questions.
- **Checks fail loud, skip honest.** A check that can't run reports `SKIP` with the reason — never `PASS` for work it didn't do.
- **Thresholds only ratchet.** Coverage and eval thresholds go up in dedicated PRs; down follows §6.
- **Harnesses bind to registries**, not to lists in test files — a new handler/repository/agent is leashed the moment it's registered, with no test edit an agent could "forget."

## 4. Architecture & code smells — review checklist

Not everything reduces to a check. The `architecture-guardian` subagent and human review watch every diff for these:

| Smell | Early warning for |
|---|---|
| A "utils"/"helpers"/"common" module accreting unrelated functions | I4 (hidden coupling hub) |
| A Pydantic model with `dict[str, Any]` fields doing real work | I3 (type laundering) |
| Business logic in `services/api` route handlers instead of packages | I4 (inverted dependency gravity) |
| A tool function that both decides and acts (no seam to test the decision) | I3, testability |
| Retry/timeout/budget logic duplicated in an agent instead of the runtime | I9, I12 (runtime bypass) |
| A LangGraph type (graph, state, message) in a public signature of `steward-agents` | I9 (framework leak) |
| New state written to Qdrant/ES without a Postgres source of truth | I1 |
| A task handler that reads its own side effects to decide what to do | I8 (hidden non-idempotency) |
| Prompt fragments concatenated conditionally across call sites | I10 |
| A test asserting on mock call order instead of observable state | test smell (brittle coupling) |
| `# type: ignore` or pragma suppressions clustering in one module | design pressure — refactor, don't suppress |

## 5. Enforcement status

Active now: S1, S3–S8 (S1/S3 tool-backed: import-linter + ruff; S4–S8 stdlib), H1, H3, H4 (wall-clock; step/token/cost with the M1 agent loop), H11 (M0 exit criterion), G1–G5 (G2 covers `packages/` and `services/` in one `--strict` invocation, issue #17). S2 skips until there is a runtime to bound. Everything else lands with its milestone (tables above) — the runner (`scripts/fitness/run.py`) reports each pending check as `SKIP` with its reason, so the gap is always visible, never silent. Review-enforced in the meantime: I6 (until masking lands, M1) and the span-tree half of I7/H6 (until the M1 agent loop) — the `architecture-guardian`'s explicit responsibility.

## 6. Amendment process

1. Open an issue labeled `guardrails` stating the invariant/NFR/threshold to change and why.
2. PR touching only `ARCHITECTURE.md`/`GUARDRAILS.md` (+ the corresponding check), linking the issue.
3. The PR description must answer: *what does this make possible, what does it make possible to get wrong, and what replaces the lost protection?*
