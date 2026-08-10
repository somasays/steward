# CLAUDE.md — Steward

Steward is a multi-agent data management platform (catalog, classify, quality-monitor, answer). It is a public repo whose process is part of the product: claims are backed by evidence in `PROOFS.md`, and **architecture judgment is the primary output** — every non-trivial choice records what was chosen, what was rejected, and why (SPEC.md §13 for platform decisions; a sentence or two in the issue/PR for local ones).

## Source-of-truth documents (read before designing anything)

1. **`ARCHITECTURE.md`** — the system definition: functional requirements, quantified NFRs (N1–N10), technology decisions, invariants (I1–I15). Highest authority.
2. **`GUARDRAILS.md`** — the fitness functions derived from ARCHITECTURE.md (tiers S/H/B/P + hygiene G), the smell checklist, and enforcement status.
3. **`SPEC.md`** — component-level design and roadmap (M0–M6). Implement toward the spec; if implementation reveals the spec is wrong, update the spec in the same PR and say why.

If documents conflict: ARCHITECTURE > GUARDRAILS > SPEC > this file.

## The development loop (issue-driven, no exceptions)

Every change follows this cycle:

1. **Start from a GitHub issue.** No issue → create one first (`gh issue create`, or the `issue-planner` subagent for milestone breakdowns). Issues carry acceptance criteria including which invariants they touch.
2. **Branch** from `main`: `m<milestone>/<issue-number>-<slug>` (e.g. `m0/12-task-queue`).
3. **Implement in vertical slices** — each commit leaves the system working and the fitness gate green. Prefer several small commits over one large one.
   **No stale files:** before committing, look up every changed file in `scripts/fitness/filegraph.json` and update (or explicitly verify unaffected) each listed dependent — `PROOFS.md` and the docs are dependents of almost everything. New files must be added to the graph (S7 fails otherwise).
4. **Before every commit:** run `make fitness`. Before finishing a branch: run the **`architecture-guardian` subagent** on the diff (`git diff main...HEAD`) and address its findings — treat a FAIL verdict as a broken build.
5. **Commit format** (enforced by hook): Conventional Commits; `feat`/`fix`/`refactor`/`perf` must reference the issue: `feat(queue): claim tasks with SKIP LOCKED (#12)`.
6. **Prove it.** When acceptance criteria are met, produce a proof entry: the claim, the exact command to reproduce it, and the observed result. No adjectives — if it can't be demonstrated by a command, test, eval score, or CI run, it doesn't go in.
   **Where it goes depends on who you are.** Working solo on a branch, append it to `PROOFS.md` directly. Working as a dispatched agent (or alongside one), put it in the **PR body** instead and leave `PROOFS.md` untouched — concurrent branches all appending to one table conflict every time. The maintainer appends PR-body rows to `PROOFS.md` on merge, so the evidence still lands; the ledger just has a single writer. A branch instruction saying "do not edit PROOFS.md" is this rule, not a violation of it — reviewers should not flag it as one.
7. **PR** with: what changed, which invariants were touched, evidence. Close the issue via `Closes #N`.
   **Open the PR before waiting on CI, not after.** Push, dispatch CI, open the PR immediately, then wait. A branch with green CI and no PR is invisible work — three dispatched agents have ended mid-wait with nothing to review.

## Non-negotiables

The invariants are **I1–I15 in `ARCHITECTURE.md` §5** — read them there, not from memory; they are the working summary and this file deliberately does not duplicate them (single source, no staleness). The ones agents trip on most: no string-built SQL (I5), LangGraph/provider SDKs only in their home packages with types never leaking (I2/I9), typed seams everywhere (I3), idempotent handlers + transactional enqueue (I8), hard budgets on every run (I12).

When a task seems to require violating an invariant, **stop and redesign** — or, if the invariant is genuinely wrong, follow the amendment process in GUARDRAILS.md §7. Never "temporarily" violate one.

## Commands

| Command | What it does |
|---|---|
| `make fitness` | Run the full suite: S (static architecture), H (behavioral harnesses), B (evals), G (hygiene); stdlib checks always, tool checks when available |
| `make hooks` | Install git hooks (pre-commit fitness gate, commit-msg format check) — run once after clone |
| `make lint` / `make type` / `make test` | Individual gates (ruff / mypy --strict / pytest+coverage) |
| `scripts/fitness/fitness --json` | Fitness results as JSON (used by CI and subagents). Use the launcher, never `python3 scripts/fitness/run.py`: it selects the project interpreter, and an older system `python3` cannot parse this project's own syntax (#74) |

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
