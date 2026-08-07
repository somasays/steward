# Steward — Architecture Definition

**Status:** Binding. `GUARDRAILS.md` derives its fitness functions from this document; `SPEC.md` details the component designs. If those documents conflict with this one, this one wins.

Steward is a multi-agent system that manages an organization's data estate: it catalogs and documents tables, classifies sensitive data, monitors quality, and answers questions about the estate with citations. It observes data sources read-only; it never moves or mutates customer data.

---

## 1. Functional requirements

| ID | Requirement |
|----|-------------|
| FR1 | Register data sources (read-only DSN) and discover their schemas continuously |
| FR2 | Profile every table and column: statistics, semantic types, samples (masked) |
| FR3 | Generate and maintain catalog documentation, every claim linked to evidence |
| FR4 | Classify column sensitivity (PII/PHI/financial) with confidence and evidence |
| FR5 | Propose, compile, and schedule data quality checks; detect schema/distribution drift |
| FR6 | Open incidents on failures with triage: severity, blast radius, ranked root-cause hypotheses |
| FR7 | Hybrid search (semantic + lexical) over the catalog corpus |
| FR8 | Agentic question answering with citations, streamed over SSE |
| FR9 | Human review queue for governance actions, with configurable approval policies |
| FR10 | Runs API: start, observe (status, cost, trace), cancel agent runs |

## 2. Non-functional requirements

Quantified; each is protected by at least one fitness function in `GUARDRAILS.md`.

| ID | Characteristic | Requirement |
|----|----------------|-------------|
| N1 | Recoverability | A worker killed mid-run loses ≤ 1 agent step; the run completes after restart. No task is ever lost or duplicated-with-effect under crash injection |
| N2 | Output correctness | PII classification recall ≥ 0.95, precision ≥ 0.90. Zero ungrounded claims in docs (judge-audited). Answer faithfulness ≥ 0.95. Triage root-cause hit@3 ≥ 0.8 |
| N3 | Retrieval quality | recall@8 ≥ 0.90, MRR@8 ≥ 0.75 on the golden query set |
| N4 | Latency | search P95 ≤ 150 ms (no rerank) / 400 ms (reranked); ask P50 ≤ 10 s; non-agent API P99 ≤ 500 ms |
| N5 | Throughput | 500-table source scanned ≤ 30 min; 50 concurrent `ask` sessions |
| N6 | Cost control | Every run has hard budgets; cost per task type ≤ 1.2× tracked baseline; workspace daily caps enforced at the gateway |
| N7 | Privacy & security | Raw sensitive values never reach a model or a trace; source connections are read-only at the DB-role level; no credentials in git |
| N8 | Observability | 100% of agent steps traced; 100% of mutations audited in-transaction; any output traceable to the exact prompt version that produced it |
| N9 | Evolvability | Provider swap = config change only. Agent-framework swap = one package's internals. Search index = rebuildable from Postgres by a job. Contract changes are visible and compatibility-checked |
| N10 | Operability | Fresh-cluster deploy is one command; code and prompts canary and roll back through the same pipeline |

## 3. Technology decisions

Full rationale for the load-bearing ones in `SPEC.md` §13 (D1–D7).

| Technology | Role | Why (one line) | Rejected |
|---|---|---|---|
| Python 3.12 + uv workspace | language & monorepo | typed, mature AI ecosystem; uv gives fast, reproducible multi-package builds | poetry/pip-tools (slower, weaker workspace story) |
| FastAPI + Pydantic v2 | API service & typed seams | contract-first, OpenAPI export feeds contract checks | Flask/Django (weaker typing story) |
| PostgreSQL | system of record + task queue | transactional enqueue with `SKIP LOCKED`; one less stateful system | Redis/RabbitMQ queue (loses transactional enqueue) — D2 |
| LangGraph (contained in `steward-agents`) | agent execution: checkpointing, interrupts, streaming | durable execution is undifferentiated to rebuild; containment caps the coupling | custom runtime (weeks of re-derivation); CrewAI/AutoGen (own our semantics) — D1 |
| LiteLLM proxy | provider gateway | aliases, fallback chains, budgets, caching at one choke point | per-provider SDKs in code (violates N9) |
| Qdrant + ElasticSearch | dense + lexical retrieval | estate search is bimodal (meaning + identifiers); RRF fusion is tuning-free | dense-only (fails identifier queries) — D3 |
| Langfuse | traces, prompt versions, eval datasets | semantic observability + evals in one place; native LangGraph/LiteLLM integration | hand-rolled trace store |
| Kubernetes + GitHub Actions + ArgoCD | delivery | GitOps promotion; prompts ride the same canary/rollback path as images — D6 | push-based deploys |
| ruff, mypy --strict, pytest, import-linter, gitleaks | enforcement toolchain | buy-over-build: hand-roll only checks with no maintained tool | bespoke checkers for solved problems; oasdiff for S6 (a Go binary would break Tier S's no-extra-toolchain guarantee — a stdlib differ does the same job) |

## 4. Architectural approaches

- **Planner/worker over a transactional queue.** Deterministic planners expand goals into task DAGs; stateless workers claim tasks (`SKIP LOCKED`) and run one bounded agent loop each. LLM-planned DAGs only for `ask`.
- **Containment pattern.** Third-party opinion (LangGraph, LiteLLM, provider SDKs) lives in exactly one package each, behind owned Pydantic contracts. Declared in `scripts/fitness/boundaries.json`.
- **Evidence-required outputs.** Documentation, classification, and answers must cite evidence (profile fields, masked samples, retrieved chunks); unsupported claims fail schema validation, and judges audit groundedness.
- **Policy-gated governance.** Actions with governance weight (publishing PII labels, activating rules) enter `pending_review`; auto-approval only via explicit policy, always auditable.
- **Append-only stewardship history.** Profiles, docs, and classifications are versioned, never overwritten; "what did this look like in March" is a query.
- **Eval-gated change.** Golden datasets + judges gate every prompt/model/retrieval change from M2 on; production samples feed the datasets back.

## 5. Invariants

Properties that hold at every commit, forever. Amendments follow `GUARDRAILS.md` §7.

| ID | Invariant |
|----|-----------|
| I1 | Postgres is the only system of record; Qdrant/ES/caches are derived and rebuildable |
| I2 | All model access goes through gateway aliases; provider SDKs and `litellm` only inside `steward-llm` |
| I3 | Typed contracts at every seam (API, tools, tasks, packages); published contracts are versioned and compatibility-checked |
| I4 | One-way dependency flow: `services → packages`; package edges are declared; `steward-schemas` = pydantic + stdlib |
| I5 | Sources are read-only at the role level; SQL is never assembled from strings; free-form SQL exists only inside the Librarian's bounded tool |
| I6 | Raw sensitive values never reach a model: masking is the only path from sampled data to a prompt |
| I7 | Every agent step is traced; every mutation writes its audit row in the same transaction |
| I8 | Task handlers are idempotent; task enqueue is transactional with the state change that caused it |
| I9 | Frameworks are contained: LangGraph only inside `steward-agents`, its types never in the public API; owned runtime code ≤ 2,000 LOC; kitchen-sink frameworks banned |
| I10 | Prompts are versioned artifacts, never inline literals |
| I11 | LLM-dependent behavior ships only with eval coverage; changes pass eval gates |
| I12 | Autonomy is bounded: hard step/token/cost/wall-clock budgets, enforced by the runtime; exceeding one is a typed, visible failure |
| I13 | Governance actions pass through policy-gated review states; every auto-approval traces to the policy that allowed it |
| I14 | Provider or model changes are configuration, not code |

## 6. Fitness functions

Derived from §2 and §5, defined and tracked in **`GUARDRAILS.md`** — including the coverage rule: every N-row and I-row above must be protected by at least one automated fitness function, and the mapping is explicit there.
