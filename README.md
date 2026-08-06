# Steward — an agentic data management platform

Steward is a multi-agent system that autonomously **catalogs, documents, classifies, and monitors** an organization's data estate — and lets anyone **ask questions about it** in natural language.

Point it at a database and a team of specialized AI agents will:

- **Profile and document** every table and column, generating and maintaining a searchable data catalog
- **Classify sensitive data** (PII/PHI/financial) with evidence-backed confidence scores
- **Propose, compile, and run data quality checks**, detect schema drift, and open triaged incidents with root-cause hypotheses
- **Answer questions** ("where do we store customer revenue, and can I join it to subscriptions?") via agentic hybrid retrieval over the catalog, with citations

Every agent step is traced, evaluated against golden datasets, and gated in CI — because an agent you can't measure is an agent you can't ship.

## Why this project exists

Data stewardship work — writing docs that go stale, hand-maintaining quality checks, answering "where is X?" in Slack — is language-heavy, judgment-heavy, and verifiable. That is the shape of work agentic systems handle well, provided they're built with production discipline: typed tool contracts, checkpointed orchestration, hybrid retrieval, evals, observability. This repo builds that system and shows the discipline; claims about it are logged with reproduction steps in [PROOFS.md](./PROOFS.md).

## Stack

| Concern | Technology |
|---|---|
| API service | Python 3.12, FastAPI, Pydantic v2 (uv workspace monorepo) |
| Agent runtime | LangGraph for execution (contained in `steward-agents`); owned contracts for tools/budgets/results; Postgres-backed task queue |
| LLM access | LiteLLM gateway — Anthropic, OpenAI, Qwen, OSS models via vLLM |
| Retrieval | Qdrant (dense) + ElasticSearch (BM25) with reciprocal-rank fusion + reranking |
| System of record | PostgreSQL |
| Observability & evals | Langfuse (traces, prompt mgmt, LLM-as-judge), OpenTelemetry, Prometheus |
| Delivery | Docker, Helm, Kubernetes, GitHub Actions (CI + eval gates), ArgoCD (GitOps CD) |

## Documentation

- **[ARCHITECTURE.md](./ARCHITECTURE.md)** — the system definition: functional requirements, quantified NFRs, technology decisions, invariants (I1–I14).
- **[GUARDRAILS.md](./GUARDRAILS.md)** — the fitness functions derived from the architecture: static checks, behavioral harnesses, benchmarks/evals, production fitness — enforced per commit via git hooks and CI.
- **[SPEC.md](./SPEC.md)** — component-level design: agents, retrieval, data model, API surface, eval framework, deployment, roadmap.
- **[CLAUDE.md](./CLAUDE.md)** — the development workflow: issue-driven iteration, per-commit fitness gates, and an adversarial `architecture-guardian` subagent that reviews every branch against the guardrails.
- **[PROOFS.md](./PROOFS.md)** — running evidence log: each claim with the command that reproduces it.

## How this repo is built

1. Every change starts from a GitHub issue with acceptance criteria and the invariants it touches.
2. Every commit passes the fitness suite (`make fitness`): import boundaries, runtime size budget, SQL string-assembly ban, prompt hygiene, secret scan, strict typing, coverage — pre-commit hook, re-run in CI.
3. Every branch gets an architecture review against the invariants and smell checklist before merge.
4. From M2, prompt/model/retrieval changes must pass eval gates (golden datasets, Langfuse) in CI.

## Status

Spec and guardrails done. Implementation iterating through the [roadmap](./SPEC.md#12-roadmap) (M0–M6) via GitHub issues.
