# Architecture Guardrails

**Status:** Binding · applies to every commit · enforced by `make fitness`, git hooks, and CI

This document is the constitution of the Steward codebase. It defines:

- **Invariants (I1–I12):** properties of the architecture that must hold at every commit, forever. Changing an invariant requires editing this file in a dedicated PR that explains why — never as a side effect of a feature.
- **Fitness functions (F1–F10):** automated, machine-checkable tests of those invariants. They run locally via `make fitness`, on every commit via the pre-commit hook, and on every push/PR via CI. **A commit that fails a fitness function does not merge.**

The relationship is strict: every invariant is either enforced by a fitness function, enforced by construction (it cannot be violated without failing an existing check), or explicitly listed as **review-enforced** (checked by the `architecture-guardian` subagent and human review, because it isn't mechanically checkable yet). Review-enforced is a debt state — the roadmap for promoting each one to a fitness function is tracked in [§4](#4-enforcement-roadmap).

---

## 1. Invariants

### I1 — Postgres is the only system of record
Qdrant and ElasticSearch are derived indexes, rebuildable from Postgres at any time. No business fact may exist only in a vector store, search index, or cache. Corollary: any indexing operation must be re-runnable and convergent (deterministic document IDs, upserts).

### I2 — All model access goes through the gateway
Agent and service code calls LLMs only through `steward-llm` using **model aliases** (`steward-reasoning`, `steward-fast`, …). Provider SDKs (`openai`, `anthropic`, `google-generativeai`, `mistralai`, `cohere`) and `litellm` itself may be imported **only** inside `packages/steward-llm`. Swapping providers must remain a config change.

### I3 — Typed boundaries, everywhere
Every seam — API request/response, tool input/output, task payload, task result, inter-package call — is a Pydantic model or a typed function signature. Raw `dict`/`Any` does not cross a package boundary. `mypy --strict` passes on `packages/`.

### I4 — One-way dependency flow
`services/*` may import `packages/*`. `packages/*` may never import `services/*` or each other's internals, and never form cycles. `steward-schemas` depends on **pydantic and the standard library only** — it is importable by anything, dependent on nothing. Allowed package-to-package edges are declared in `scripts/fitness/boundaries.json`; any new edge is a reviewed change to that file.

### I5 — Sources are read-only; SQL is never assembled from strings
Connections to customer data sources use read-only roles, enforced at the database level. In code, SQL is either (a) a parameterized template from the connector library, or (b) composed by the Librarian **only** through the bounded `run_readonly_sql` tool (read-only role + `LIMIT` + statement timeout + cost cap + masking). String-formatted SQL (f-strings, `%`, `+`, `.format`) is banned everywhere.

### I6 — Raw sensitive values never reach a model
Any value sampled from a customer source passes through the masking layer before entering a prompt. Prompts are constructed from schemas, statistics, formats, and masked exemplars — never raw payloads. **(Review-enforced until the masking layer lands in M1; then enforced by construction: prompt-building APIs only accept `MaskedSample` types — I3 makes the type system the enforcement.)**

### I7 — Nothing happens invisibly
Every agent step (generation, tool call) emits a Langfuse span. Every state-changing action writes `audit_log` **in the same transaction** as the mutation. A feature that can't answer "who/what/why" from its traces and audit rows is not done.

### I8 — Tasks are idempotent; enqueue is transactional
Running any task handler twice with the same payload converges to the same state (upserts on natural keys, deterministic derived IDs). Tasks are enqueued in the same Postgres transaction as the state change that caused them — no ghost tasks, no lost tasks.

### I9 — Frameworks are contained; the contract is owned
Agent execution may use LangGraph — but only inside `packages/steward-agents`, and no LangGraph type appears in that package's public API: callers see our Pydantic contracts (`AgentSpec`, tool defs, budgets, `TaskResult`) only. Third-party module homes are declared in the `contained_modules` map in `scripts/fitness/boundaries.json` (`langgraph` → `steward-agents`; provider SDKs and `litellm` → `steward-llm`); imports outside a module's home are violations. Steward-owned code in `steward-agents` stays under **2,000 effective LOC** (non-blank, non-comment, tests excluded) — containment plus the size budget keeps the wrapper honest: if it balloons, we're rebuilding the framework inside the wrapper; if types leak, we're coupled to it. Kitchen-sink frameworks (`langchain`, `langchain-community`, `crewai`, `llama-index`, `autogen`, `semantic-kernel`, `pydantic-ai`, `haystack`) are banned outright as dependencies and imports.

### I10 — Prompts are versioned artifacts
Prompts live in `prompts/` files (or Langfuse-managed versions referenced by ID) and are loaded by name+version. Prompt text is not scattered through application code as string literals. Changing a prompt is a diff a reviewer can see and an eval gate can test.

### I11 — LLM-dependent behavior is eval-gated
Every capability whose output depends on a model (documentation, classification, retrieval, answering, triage) has an eval suite with golden data. From M2 onward, changes to prompts, model bindings, or retrieval parameters must pass the affected suites in CI before merge. A behavior without an eval is a prototype, not a feature.

### I12 — Autonomy is bounded
Every agent run has hard limits — max steps, max tokens, max cost, max wall-clock — enforced by the runtime, not by convention. Exceeding a budget is a visible, typed failure (`budget_exceeded`), never a silent truncation or an unbounded loop.

---

## 2. Fitness functions

Fitness functions are **the leash that lets this system evolve fast — with heavy agent assistance — without the architecture eroding**. They are specific to Steward's invariants and promises, not generic hygiene: each one pins a property an agent (or a tired human) could otherwise silently break while "just implementing a feature." Generic hygiene (lint, types, coverage) still gates every commit, but it's table stakes and listed separately as H-checks.

Structural checks are stdlib-only Python in `scripts/fitness/` (no venv — they must run before the project can even install itself); harness checks run via pytest markers. `make fitness` runs everything; individual checks are runnable directly.

### Tier A — Architecture standards (structural, always on)

| ID | Name | Enforces | Mechanism |
|----|------|----------|-----------|
| **F1** | Import boundaries | I2, I4, I9 | `check_boundaries.py` — AST walk: no `packages → services` imports, no undeclared package edges, contained modules only in their declared homes (`langgraph` → `steward-agents`, providers/`litellm` → `steward-llm`), `steward-schemas` purity, banned frameworks nowhere (incl. `pyproject.toml` deps) — all vs `boundaries.json` |
| **F2** | Runtime size budget | I9 | `check_loc_budget.py` — effective LOC of `packages/steward-agents` ≤ 2,000 |
| **F3** | SQL safety | I5 | `check_sql_safety.py` — AST: flags f-strings/`%`/`format`/`+`-concat producing SQL |
| **F4** | Prompt hygiene | I10 | `check_prompt_hygiene.py` — prompt-shaped literals outside `prompts/` |
| **F5** | Public-surface lock | I9, I3 | `check_surface.py` — AST over each package's public modules: no contained-module type (langgraph, litellm, provider SDKs) in a public signature, class base, or re-export; the framework stays an implementation detail |

### Tier B — Contracts (what other code may rely on)

| ID | Name | Enforces | Mechanism |
|----|------|----------|-----------|
| **F6** | Contract compatibility | I3 | `check_contracts.py` — JSON Schema snapshots of published Pydantic contracts (and the OpenAPI spec, once `services/api` exists) live in `contracts/`; regenerated on each run and diffed. **Breaking** (removed field/path, type change, new required field) fails; additive changes require the snapshot update in the same commit, which makes every contract change visible in review |

### Tier C — Behavioral invariants (harness tests over real components)

| ID | Name | Enforces | Mechanism |
|----|------|----------|-----------|
| **F7** | Invariant harness | I7, I8, I12 | `pytest -m invariants` — generic harnesses that hold for *every* registered component, present and future: each task handler executed twice with the same payload converges to identical state (I8); each repository mutation produces an audit row in the same transaction (I7); each agent run with a 1-step/1-cent budget terminates with `budget_exceeded`, never hangs (I12). New components are picked up by registration, so an agent adding a handler cannot skip the leash |

### Tier D — Functional acceptance (does the system do its job)

| ID | Name | Enforces | Mechanism |
|----|------|----------|-----------|
| **F8** | Acceptance scenarios | SPEC §12 exit criteria | `pytest -m acceptance` against the Dockerized fixture warehouse — executable versions of each milestone's exit criterion (M0: a run flows API → queue → worker → done with a trace; M1: scan yields reviewed docs + classifications; M4: injected faults are detected and triaged). Once a milestone ships, its scenario runs forever — regressions in old capability fail new work |
| **F9** | Eval gates | I11 | `steward evals run` on suites affected by the diff; thresholds from SPEC §9. Activates in M2 |

### Hygiene (generic, still blocking)

| ID | Name | Mechanism |
|----|------|-----------|
| **H1** | Lint & format | `ruff check` + `ruff format --check` |
| **H2** | Strict types | `mypy --strict` on `packages/` |
| **H3** | Tests & coverage | `pytest` with branch coverage ≥ 85% on `packages/` |
| **H4** | Secret scan | `check_secrets.py` — credential patterns (keys, tokens, DSNs with passwords) |
| **H5** | Commit discipline | `commit-msg` hook — Conventional Commits; `feat`/`fix`/`refactor`/`perf` must reference an issue (`#N`) |

All of Tier A + H4 run stdlib-only and gate pre-commit; everything runs in CI. Tool-backed checks skip honestly until their prerequisites exist (see Rules below).

Rules of engagement:

- **No bypass culture.** `git commit --no-verify` is for emergencies; CI re-runs everything anyway, so a bypassed hook only delays the failure to the PR.
- **Suppressions are visible and justified.** The only escape hatches are inline pragmas (`# fitness: allow-sql-string`, `# fitness: allow-prompt-literal`) which **require a trailing reason** and are counted: CI reports total pragma count per check, and reviewers treat any increase as a design question.
- **Checks fail loud, skip honest.** A check that can't run (tooling not installed, directory not created yet) reports `SKIP` with the reason — it never reports `PASS` for work it didn't do.
- **Thresholds only ratchet.** Coverage and eval thresholds may go up in a dedicated PR; going down requires the same invariant-change process as §1.

---

## 3. Architecture & code smells — review checklist

Not everything reduces to a script. The `architecture-guardian` subagent (and human reviewers) check every diff for these, mapped to the invariant they usually precede violating:

| Smell | Early warning for |
|---|---|
| A "utils"/"helpers"/"common" module accreting unrelated functions | I4 (hidden coupling hub) |
| A Pydantic model with `dict[str, Any]` fields doing real work | I3 (type laundering) |
| Business logic appearing in `services/api` route handlers instead of packages | I4 (inverted dependency gravity) |
| A tool function that both decides and acts (no seam to test the decision) | I3, testability |
| Retry/timeout/budget logic duplicated in an agent instead of the runtime | I9, I12 (runtime bypass) |
| A LangGraph type (graph, state, message) in a public signature of `steward-agents` | I9 (framework leak) |
| New state written to Qdrant/ES without a Postgres source of truth | I1 |
| A task handler that reads its own side effects to decide what to do | I8 (hidden non-idempotency) |
| Prompt fragments concatenated conditionally across call sites | I10 |
| A test asserting on mock call order instead of observable state | test smell (brittle coupling) |
| `# type: ignore` or pragma suppressions clustering in one module | design pressure — refactor, don't suppress |

---

## 4. Enforcement roadmap

Review-enforced invariants and their promotion path to mechanical enforcement:

| Invariant | Currently | Promotion plan |
|---|---|---|
| I6 masking | review-enforced | M1: prompt-builder APIs accept only `MaskedSample` types → enforced by H2 (mypy strict) |
| I7 tracing/audit | review-enforced | M1: audit-row-per-mutation harness in the invariant suite → F7 |
| I8 idempotency | review-enforced | M1: twice-run harness over every registered task handler → F7 |
| I12 budgets | review-enforced | M0: budgets constructor-required in the runtime (no default = unlimited) + termination harness → F7 |
| F5, F6 | specced, not yet implemented | tracked as `guardrails` issues; runner reports them SKIP until they land |
| F9 evals | inactive | M2: suites + thresholds land with the retrieval milestone |

---

## 5. Amendment process

1. Open an issue labeled `guardrails` stating the invariant/threshold to change and why.
2. PR touching only `GUARDRAILS.md` (+ corresponding fitness script), linking the issue.
3. The PR description must answer: *what does this make possible, what does it make possible to get wrong, and what replaces the lost protection?*
