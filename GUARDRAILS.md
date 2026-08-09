# Fitness Functions & Enforcement

**Status:** Binding · applies to every commit · enforced by `make fitness`, git hooks, CI, and scheduled runs

`ARCHITECTURE.md` defines what must stay true: functional requirements (FR), quantified non-functionals (N1–N10), and invariants (I1–I15). This document defines how those properties are **continuously measured**. The fitness functions are the leash that lets the system evolve fast — with heavy agent assistance — without its architecture or its guarantees eroding.

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
| S4 | Prompt literal ban | I10 | `check_prompt_hygiene.py` — prompt-shaped literals outside `prompts/` (custom: domain-specific). A file it cannot parse is reported, never counted clean: if any file is unparsable the check SKIPs with the count and interpreter version rather than PASSing on files it never read (issue #35). The suite launches via `scripts/fitness/fitness`, which prefers `.venv/bin/python` so the checks see the project's own 3.12 syntax, falling back to `python3` on a fresh clone | active |
| S5 | Public-surface lock | I9, I3 | `check_surface.py` — no contained-module type in any package's public signatures, class bases, or re-exports. The classification logic itself is covered by `--selftest` (planted-leak + clean fixtures), swept automatically by S8 | active |
| S6 | Contract compatibility | I3, N9 | `check_contracts.py` — regenerates JSON Schema for every published Pydantic contract and the exported OpenAPI spec, then diffs on two axes: against the snapshot **at the baseline commit** (read from git), which is the compatibility gate, and against the snapshot committed in the working tree, which catches a forgotten regeneration. A stdlib differ (no external oasdiff binary) classifies removed model/property/path/method, type changes, new-required properties and enum narrowing as breaking (FAIL) on either axis — so a breaking change committed together with its regenerated snapshot still FAILs; other drift from the working-tree snapshot is stale (FAIL); additive drift from the baseline is evolution (PASS). Baseline: PR base sha on `pull_request`, the previous tip (`event.before`) on a push to the default branch, merge-base with the default branch on other CI events, merge-base with `origin/main` locally; unresolvable — no baseline in the checkout, any candidate equal to HEAD (comparing a commit with itself, on every resolution path), or resolved-but-unreadable, e.g. a blobless clone — → SKIP with the reason locally, **FAIL in CI**, never PASS, and the detail line carries the baseline sha and how many contracts it compared. CI checks out with `fetch-depth: 0` for this. An intentional break can be declared in `contracts/BREAKING.md` (one entry per break: contracts affected, ground, migration note, decision — issue #24); a breaking finding is waived only when a complete entry names that exact contract label for this diff. Undeclared still FAILs, a declaration naming the wrong contract still FAILs (the real break stays undeclared, and the phantom entry FAILs as stale), and a declaration outliving its break FAILs as stale too — the waiver never becomes a standing blanket. The classification logic (breaking/stale/pass, baseline resolution, the baseline-axis-vs-stale-axis split, declared-break acceptance and its three failure modes) is covered by `--selftest` against in-memory fixtures, swept automatically by S8 — this is the only local check that catches a regression here, since S6 itself SKIPs on an undiverged branch | active |
| S7 | File-graph coverage | doc consistency | `check_filegraph.py` — `scripts/fitness/filegraph.json` maps every file pattern to its impacted files; changing a file means updating/verifying its dependents (workflow law, CLAUDE.md); S7 fails if any tracked file is outside the graph | active |
| S8 | Checker self-tests | I3, I9, N9 (meta: correctness of S5, S6) | `check_selftests.py` — discovers every `check_*.py` that declares a `--selftest` branch (registry by convention, not a hardcoded list) and runs it; any nonzero exit FAILs the suite. Closes the gap where S5/S6's classification logic — real software, not mechanical scripts — had a selftest nothing ran automatically (issue #32) | active |
| S9 | Inference endpoint allowlist | I15, N7 | `uv run python -m steward_llm.validate` — runs the **startup refusal itself** over the committed LiteLLM config and allowlist (`packages/steward-llm/src/steward_llm/defaults/`), so the gate and what a process does at boot are the same code, not a lint that approximates it. FAILs when any `model_list` entry — or any `general_settings.pass_through_endpoints` target, which is routing under another name — omits `api_base` (the quiet breach: the provider's own API becomes the destination), names a provider-routed model instead of a URL-addressed one, resolves to a base URL outside the allowlist, or when a production alias is unbound. Every entry is checked, not only the four aliases, because LiteLLM can reach any of them through a fallback chain. What it does not model is a future LiteLLM key that routes — §5 names that gap rather than letting the row imply exhaustiveness. SKIPs only when the project isn't installed (no uv), like every other tool check | active (issue #59) |

### Tier H — behavioral harnesses (every PR; `pytest -m invariants` / `-m acceptance` against an ephemeral Postgres)

These run real components — Postgres, the queue, the runtime with a stub LLM — and assert system behavior. They bind to *registries* (task handlers, repositories, agents), so a newly added component is on the leash automatically.

| ID | Fitness function | Protects | Measurement | Lands |
|----|------------------|----------|-------------|-------|
| H1 | Idempotency | I8 | every registered task handler executed twice with the same payload → byte-identical end state | active |
| H2 | Crash recovery | N1 | SIGKILL a worker mid-run → run completes after restart with ≤ 1 step re-executed | M0/M5 chaos |
| H3 | No lost/ghost tasks | I8, N1 | crash injection around enqueue/claim/complete → task set matches state-machine expectations exactly | active |
| H4 | Budget termination | I12, N6 | agent given an impossible goal with 1-step/1-cent budget → terminates `budget_exceeded`, never hangs, cost ≤ cap. **Wall-clock (issue #42, SPEC §13 D7):** a handler that overruns fails `budget_exceeded` within the cap plus a *bounded* margin — `DEADLINE_GRACE` (500 ms) + one terminate round trip + one bookkeeping transaction, independent of the handler, because nothing on the enforcement path waits on the handler thread. Asserted at the three shapes that defeat different mechanisms: one that awaits, one blocked in the driver (whose `QueryCanceled` is retyped as the overrun it is), and one blocked in Python, which no timeout can reach and which was previously unbounded. A handler spending the cap on each of the two connections a scan uses is asserted to cost **one** cap, not two; and the worker is asserted to keep reaping expired leases and to honour a stop *while* a handler runs (N1). **The verdict cannot be reached by a non-budget timeout (issue #57):** it now requires the timeout `_bounded` itself raised, or an elapsed time that reached the cap, so a `connect_timeout` firing well inside the budget is asserted to record `handler raised` — while every `TimeoutError` counted as an overrun (and since 3.11 `socket.timeout` *is* one), an unreachable customer database satisfied this row, which is §3's "passes for the wrong reason" in the wall-clock half itself. Before #42 the wall-clock half was hollow — `asyncio.timeout` wrapped handlers with no await point, so only `statement_timeout` bound anything, per statement, at the full budget each. **Steps, tokens and cost (issue #48, SPEC §13 D9):** the non-wall-clock half is no longer entirely deferred. Two properties are asserted now, without an agent loop. *Reservation:* a plan divides its run's budget — every planned task declares its own caps, and an expansion whose caps sum past the run's budget in any dimension is refused (`RunBudgetExceeded`) before a run row or a task row exists, asserted at the registry (`packages/steward-orchestration/tests`) and end to end over the API and a real Postgres (`services/api/tests/test_run_admission.py`, `test_run_budgets.py`: the refused fan-out leaves the run and task counts unchanged). *Reported-usage cap:* a succeeded result whose usage exceeds its own task budget is recorded `budget_exceeded` and its usage never reaches `runs.used_*`, so the run's totals cannot be walked past its budget one task at a time. Together those make the N-task assertion real: three tasks of one run are asserted to spend one run budget between them, not three. What still lands with the M1 agent loop is *in-loop* enforcement — stopping an agent at the step that would cross the cap, rather than failing the task that already did — and that is the half this row still calls pending. Not claimed either: retry spend, which is neither reserved nor recorded (D9 names the gap). Not claimed: a thread blocked on a third-party socket cannot be killed — its task is failed on time and its writes discarded, but the thread lingers until the connector's budget-derived timeouts fire | wall-clock active (worker); plan-time reservation and the reported-usage cap active (#48); in-loop step/token/cost with the M1 agent loop |
| H5 | Audit completeness | I7, N8 | every repository mutation produces an audit row in the same transaction (asserted over the repository registry) | M1 |
| H6 | Trace completeness | I7, N8 | a finished run's Langfuse trace contains the full expected span tree; every generation span carries a prompt version | partial: trace_id is NOT NULL and asserted end to end by H11; span-tree assertions land with the M1 agent loop |
| H7 | Masking canary | I6, N7 | canary secrets planted in the fixture source's *data* (an email, a Luhn-valid card, an opaque token, a value whose payload follows the last dot, and one whose payload precedes a `://`); the registered `profile_asset` handler is then executed the way production executes it — claimed off the queue by a real `Worker`, with the environment-backed resolver and the real profiler — and none of them may appear in **any row of any table** in Steward's database (the sweep enumerates `pg_tables`, so a table added later is covered without a test edit), **any log record** at any level from any logger, **stdout/stderr**, or **any span the worker opened**. Three guards keep a green result meaningful: the harness asserts the canary column really was profiled and carries the *masked* form (a run that profiled nothing fails); a planted leak in an audit row is asserted to be *found*; and the log half has its own planted leak plus a non-emptiness assertion, because `canary not in ""` passes on a capture of nothing. **Two canaries exist because the first three were all one shape:** they ended in `.test` or had no dot and none was URL-shaped, so when `_mask_email` published everything after the final dot verbatim, and later when `_mask_url` published the scheme, this harness watched payloads reach `profiles` and reported green. `CANARY_AFTER_LAST_DOT` and `CANARY_BEFORE_SCHEME` cover those regions, and each is *also* swept for by its payload alone (`CANARY_TAIL`, `CANARY_HEAD`) — a partial leak publishes the payload, not the whole canary, so a whole-string sweep returns nothing and the row would claim a region it does not cover. `packages/steward-catalog/tests/test_masking_canary.py`. **What it does not cover:** prompts, because there are none yet (#50) — the seam is instead closed by types, `MaskedSample` being the only shape a sampled value can take and `RawCell` satisfying no data-typed parameter (G2); one masker over one fixture estate rather than every value a source could hold — **this row's four escapes on #49 were all shape blindness**: the canaries are long, so a mask that revealed first and last character being the identity function on `M`, `42`, `9.5` was invisible here; they all ended in `.test`, so a verbatim TLD reveal was too; none was URL-shaped, so a verbatim scheme reveal was the third; and the card canary's last four digits were never swept for — a four-digit needle is too low-entropy to sweep at table level, which is exactly why that bound belongs in a unit property and not here. All four are now covered by exhaustive unit properties in `test_masking.py` (every string up to three characters, plus values whose payload follows the last dot), which is the right layer for "is this mask safe" — a canary proves a path, not a function; nothing about a dependency writing to a file descriptor of its own; and nothing about a column of **three or more** low-cardinality values, where the masks still differ and an attacker who knows the domain can often map them — suppression covers two-valued columns and the residue is a drawn line (SPEC.md D10) | active (issue #49) |
| H8 | Index rebuild convergence | I1, N9 | wipe Qdrant + ES, run rebuild job → search results identical to pre-wipe golden results | M2 |
| H9 | Citation resolution | N2, FR8 | every citation in every golden answer resolves to a live asset/document id | M3 |
| H10 | Governance gating | I13, FR9 | governance actions land in `pending_review` unless a policy explicitly auto-approves; the policy id is on the audit row | M1 |
| H11 | Milestone acceptance | FR1–FR10 | executable exit criterion of each shipped milestone (SPEC §12); once shipped, runs forever | active (M0: API → queue → worker → succeeded. M1 slice 1, issue #20: register a Postgres source → scan → assets and columns paged back over the API, against a real second database on a read-only role; includes the I5 write-fails proof, the byte-identical rescan, the `missing` lifecycle, and no credential readable from the database or any response) |

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
| B9 | OSS binding independence | I14, I15, N9 | B1–B5 re-run with aliases bound to a *second approved self-hosted* model — pass without code change. Since #59 this proves the OSS binding is the **only** binding rather than that provider swapping works: a hosted binding is not an accepted configuration, so the quality bar has to be met inside the allowlist | M3, nightly |

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
| I5 read-only sources, no string SQL | S3 (+ read-only role: H11's M1 scenario and `packages/steward-catalog/tests/test_read_only.py` both attempt a write on the connection a scan opens and assert Postgres' `42501`, not a session flag's `25006`) |
| I6 masking | H7 (active since #49: canaries planted in fixture *data* are asserted absent from every table, log line and span on the profiling path) + G2 by construction: `steward_catalog.masking.RawCell` is not a `str` and satisfies no parameter typed for data, and every value-carrying field of a profile is a `steward_schemas.MaskedSample`, so `mask()` is the only bridge and `mypy --strict` is what says so. Not yet covered: a prompt, since none exists until #50 — H7's row states that gap rather than implying the check is exhaustive |
| I7 traced & audited | H5, H6 |
| I8 idempotent, transactional tasks | H1, H3 |
| I9 framework containment & size | S1, S2, S5 |
| I10 versioned prompts | S4 |
| I11 eval-gated behavior | B1–B5 gates, P1 |
| I12 bounded autonomy | H4 (+ G3 for the plan-time half: `RunBudgetExceeded` refuses an expansion reserving more than its run's budget, and a run's `used_*` is the sum over its succeeded tasks, each capped at what it was reserved — issue #48, SPEC §13 D9. Not yet covered: in-loop enforcement at the step that would cross a cap, which lands with the M1 agent loop, and spend on retried attempts, which the failure path carries no usage to record) |
| I13 policy-gated governance | H10 |
| I14 model swap = config, inside the approved set | B9 |
| I15 self-hosted production inference | S9 (the startup refusal run over the committed config: no `api_base`, a provider-routed model, an unbound alias or an off-allowlist base URL each FAIL) + S1 (the `litellm` containment that keeps the one choke point) + G3 (`packages/steward-llm/tests/` — non-approved refused, approved accepted, hosted-mode opt-in). Not yet covered: that the config a cluster *mounts* is the one checked, and egress from worker/gateway pods — review-enforced, see §5 |
| N1 recoverability | H2, H3, P4 (+ worker responsiveness under a running handler: H4's wall-clock scenarios assert `reap_stale` and shutdown are bounded by a poll interval, not a task duration — issue #42. An attempt whose executor takes the handoff and then fails to write its terminal state is recovered by lease expiry rather than failed on the spot: the loop must not write a row the thread that beat it to the handoff may still be committing, so N1's re-execution *is* the answer there, not a fallback from one — SPEC §13 D7, issue #53. A handler leaking a bare `asyncio.CancelledError` from its own event loop is asserted to fail *that task* and leave the worker polling: grouped with `SystemExit`/`KeyboardInterrupt` as the process ending, it travelled out of the future the poll loop reads and one buggy handler killed the worker — issue #55. The two it was grouped with are now asserted the same way: a handler whose dependency calls `sys.exit()` fails *that task* and the worker goes on claiming, and an interrupt raised around the handler is the execution's failure rather than the worker's. A `SystemExit` on a non-main thread ends that thread, and a `KeyboardInterrupt` is delivered to the main thread only, so neither is the process ending when it arrives from a handler; a real shutdown reaches the loop as its stop event — issue #63, SPEC §13 D7) |
| N2 output correctness | B2–B5, H9, P1 |
| N3 retrieval quality | B1 |
| N4 latency | B6, P2 |
| N5 throughput | B7 |
| N6 cost | H4, B8, P3 (+ the reservation above: a run's cost cap now bounds the whole plan rather than each branch of it, so a fan-out cannot cost N times what the API advertised — issue #48) |
| N7 privacy/security | H7 (active, #49), S3 (which now also covers the composed identifiers profiling needs: the templates are static `psycopg.sql.SQL` constants and the only substitution is a `sql.Identifier` psycopg quotes — SPEC.md §13 D10), G4, S9 |
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

Active now: S1, S3–S9 (S1/S3/S9 tool-backed: import-linter, ruff, and `steward-llm`'s own startup check; S4–S8 stdlib), H1, H3, H4 (wall-clock, plan-time reservation and the reported-usage cap; in-loop step/token/cost with the M1 agent loop), H7 (issue #49), H11 (M0 and M1-slice-1 exit criteria), G1–G5 (G2 covers `packages/` and `services/` in one `--strict` invocation, issue #17). S2 skips until there is a runtime to bound. Everything else lands with its milestone (tables above) — the runner (`scripts/fitness/run.py`) reports each pending check as `SKIP` with its reason, so the gap is always visible, never silent. Review-enforced in the meantime: the span-tree half of I7/H6 (until the M1 agent loop) and **the deployment half of I15** — the `architecture-guardian`'s explicit responsibility.

**I6 is no longer on that list** (issue #49). It was review-enforced from M0 because there was nothing to mask: the catalog slice read metadata only. Profiling reads customer values, so the invariant is now carried by two mechanisms and it is worth being precise about which does what. The *type* half is total and compile-time: `RawCell` cannot be assigned to anything typed for data, so no raw value can be persisted, returned or handed to a future prompt builder, and G2 fails the build if one is. The *behavioural* half, H7, covers what types cannot see — a log line, a console write, a span payload — over one fixture estate, and it is the half that can only ever be evidence rather than proof. The prompt half of I6 has no subject yet: #50 brings the first prompt, and the harness that watches it is H7 extended, not a new tier.

I15's split is worth stating precisely, because the coverage rule is not satisfied by a check that measures the easy half. S9 proves the committed config and the refusal logic agree, and G3 proves the refusal fires; neither can see the config a cluster actually mounts, nor stop a pod from reaching a hosted API by some path that never goes through our gateway. A third gap sits inside the check itself: it reads `model_list` and `pass_through_endpoints`, the two keys that route today, and a future LiteLLM key that routes or exports prompts would be invisible to it — so a diff bumping LiteLLM is a diff that has to re-read this. Until then, reviewers check three things on any diff touching inference: that a config is never loaded outside `steward_llm.config`, that no deployment artifact ships a hosted default, and that a new gateway config key which can name a destination is added to the parser. The promotion path is concrete — a Tier H harness when #50 lands the gateway client (boot **every service entry point** against a config pointed at a non-approved base URL and assert each exits non-zero — the refusal is wired in the worker composition root today and nothing yet fails if a new entry point forgets it, which is the wiring half a harness closes), and a NetworkPolicy restricting egress from worker and gateway pods to the approved endpoints when the M6 Helm chart lands, which is the only mechanism that binds a process that bypasses our code entirely.

## 6. Guardrail freeze (LIFTED 2026-08-07 when the catalog shipped — kept for the record)

**Status: lifted.** #20 landed, so the condition below is met. The rule it leaves behind: a new check needs a concrete product change that demonstrates the gap, not an anticipated one. Re-read this section before adding a tier or generalising a check.

<details><summary>The freeze as it stood</summary>

**No new generic fitness infrastructure until the deterministic catalog exists** (issue #20). Measured at the freeze: 1,768 lines of fitness apparatus against 3,637 lines of product, with `check_contracts.py` (854) the largest single module in the repo — larger than anything it protects. S8 gives the checkers test coverage; it does not remove their maintenance cost.

Allowed during the freeze:
- finishing already-open guardrail issues (#21, #24)
- a check that a *concrete product change* proves is needed — the gap must be demonstrated by real code, not anticipated
- fixing a gate that is wrong or hollow

Not allowed: new checks for capabilities that do not exist yet, generalising a check beyond its current need, or new tiers.

The freeze lifts when #20 ships. It exists because this project's failure mode is strengthening the apparatus instead of shipping the capability — the invariants are already ahead of the system they govern.
</details>

## 7. Amendment process

1. Open an issue labeled `guardrails` stating the invariant/NFR/threshold to change and why.
2. PR touching only `ARCHITECTURE.md`/`GUARDRAILS.md` (+ the corresponding check), linking the issue.
3. The PR description must answer: *what does this make possible, what does it make possible to get wrong, and what replaces the lost protection?*
