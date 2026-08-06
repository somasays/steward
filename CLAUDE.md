# CLAUDE.md — Steward

Steward is a multi-agent data management platform (catalog, classify, quality-monitor, answer). It is a public repo whose process is part of the product: claims are backed by evidence in `PROOFS.md`, and **architecture judgment is the primary output** — every non-trivial choice records what was chosen, what was rejected, and why (SPEC.md §13 for platform decisions; a sentence or two in the issue/PR for local ones).

## Source-of-truth documents (read before designing anything)

1. **`GUARDRAILS.md`** — binding architectural invariants (I1–I12) and fitness functions (F1–F10). Nothing here overrides it; if this file and GUARDRAILS.md ever conflict, GUARDRAILS.md wins.
2. **`SPEC.md`** — full technical specification and roadmap (M0–M6). Implement toward the spec; if implementation reveals the spec is wrong, update the spec in the same PR and say why in its description.

## The development loop (issue-driven, no exceptions)

Every change follows this cycle:

1. **Start from a GitHub issue.** No issue → create one first (`gh issue create`, or the `issue-planner` subagent for milestone breakdowns). Issues carry acceptance criteria including which invariants they touch.
2. **Branch** from `main`: `m<milestone>/<issue-number>-<slug>` (e.g. `m0/12-task-queue`).
3. **Implement in vertical slices** — each commit leaves the system working and the fitness gate green. Prefer several small commits over one large one.
4. **Before every commit:** run `make fitness`. Before finishing a branch: run the **`architecture-guardian` subagent** on the diff (`git diff main...HEAD`) and address its findings — treat a FAIL verdict as a broken build.
5. **Commit format** (enforced by hook): Conventional Commits; `feat`/`fix`/`refactor`/`perf` must reference the issue: `feat(queue): claim tasks with SKIP LOCKED (#12)`.
6. **Prove it.** When acceptance criteria are met, append an entry to `PROOFS.md` in the same branch: the claim, the exact command to reproduce it, and the observed result. No adjectives — if it can't be demonstrated by a command, test, eval score, or CI run, it doesn't go in.
7. **PR** with: what changed, which invariants were touched, evidence. Close the issue via `Closes #N`.

## Non-negotiables (summary — full text in GUARDRAILS.md)

- Postgres is the only system of record; Qdrant/ES are rebuildable derivatives (I1)
- LLM calls only via `steward-llm` aliases; provider SDKs and `litellm` imports only inside that package (I2)
- Pydantic-typed seams everywhere; `mypy --strict` on `packages/`; no `dict`/`Any` across boundaries (I3)
- Dependency flow: `services → packages`, edges declared in `scripts/fitness/boundaries.json`; `steward-schemas` = pydantic + stdlib only (I4)
- No string-built SQL, ever; sources are read-only (I5)
- Masked samples only in prompts (I6) · every step traced, every mutation audited in-transaction (I7)
- Task handlers idempotent; enqueue transactional (I8)
- LangGraph only inside `steward-agents`, its types never in the public API; steward-owned code there ≤ 2,000 LOC; crewai/autogen/llama-index/langchain(-community)/etc. banned outright (I9)
- Prompts live in `prompts/`, versioned, never inline string literals (I10)
- Model/prompt/retrieval changes pass eval gates from M2 (I11) · every run has hard budgets (I12)

When a task seems to require violating one of these, **stop and redesign** — or, if the invariant is genuinely wrong, follow the amendment process in GUARDRAILS.md §5. Never "temporarily" violate one.

## Commands

| Command | What it does |
|---|---|
| `make fitness` | Run the full suite: F1–F9 (architecture/contract/invariant/acceptance leashes) + H1–H4 (hygiene); stdlib checks always, tool checks when available |
| `make hooks` | Install git hooks (pre-commit fitness gate, commit-msg format check) — run once after clone |
| `make lint` / `make type` / `make test` | Individual gates (ruff / mypy --strict / pytest+coverage) |
| `python3 scripts/fitness/run.py --json` | Fitness results as JSON (used by CI and subagents) |

## Subagents

- **`architecture-guardian`** — adversarial review of a diff against GUARDRAILS.md (invariants + smell checklist). Use proactively before ending any branch, and whenever a design decision feels like it's bending an invariant.
- **`issue-planner`** — decomposes a SPEC.md milestone into scoped GitHub issues with acceptance criteria and invariant annotations. Use when opening a new milestone.

## Public-surface style (commits, issues, PRs, docs)

Everything on GitHub reads like a busy engineer wrote it:

- **Commit messages:** one line, Conventional Commits, no body unless genuinely needed. **Never add AI attribution, `Co-Authored-By: Claude`, "Generated with" footers, or emoji.**
- **Issues / PR descriptions / comments:** short and concrete. A sentence beats a boilerplate section; skip template headings that would be empty. No essays, no marketing language, no tall claims — link to `PROOFS.md` or CI instead of asserting quality.
- **Docs:** change only when behavior changes; state facts, not aspirations.

## Working style

- **Spec-first for anything non-obvious.** A paragraph of design in the issue before code; interfaces (Pydantic models, function signatures) before implementations.
- **Tests assert observable behavior** (state, outputs, emitted events), not implementation details or mock choreography.
- **Match altitude.** Small focused modules; business logic in `packages/`, orchestration wiring in `services/`; no grab-bag `utils.py`.
- **Escape-hatch pragmas** (`# fitness: allow-*`) require a reason on the same line and should be treated as design debt — prefer refactoring over suppressing.
- **Don't create docs nobody asked for**; the spec, guardrails, issues, and PR descriptions are the documentation surface.
