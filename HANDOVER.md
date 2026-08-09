# Handover prompt — paste this into a new session

I'm building **Steward**, a multi-agent data management platform, at `~/code/data-management-agent`
(GitHub: **somasays/steward**, public). It's a portfolio piece for Staff/Senior AI Engineer applications,
so the engineering process is part of the product: every claim is backed by a reproducible command.

## Read these first, in this order — they are binding, not background

1. `ARCHITECTURE.md` — requirements, NFRs N1–N10, invariants **I1–I15**. Highest authority.
2. `GUARDRAILS.md` — the fitness functions derived from it (tiers S/H/B/P + hygiene G), the coverage
   matrix, the enforcement status, and the amendment process in §7.
3. `SPEC.md` — component design, roadmap M0–M6, and the decision records D1–D10 in §13.
4. `CLAUDE.md` — the development workflow. Follow it exactly; it encodes rules this project paid for.
5. `PROOFS.md` — 77 rows, each a claim plus the command that reproduces it.

If they conflict: ARCHITECTURE > GUARDRAILS > SPEC > CLAUDE.md. **SPEC also outranks anything I tell a
subagent in a dispatch brief** — if SPEC says something is permitted, an agent will believe it over the
brief, so fix SPEC first.

## State

M0 and M1 slices 1–2 are shipped. `main` is green, no open PRs. Working: register a Postgres source,
scan it, persist assets/columns, profile every column through a masking layer, read it all back over
HTTP. Sixteen fitness functions active (S1, S3–S9, H1, H3, H4, H11, G1–G5); S2 and the B-tier eval
gates SKIP with stated reasons. No LLM call exists yet.

## What to do next

**#69 — bounded agent loop and LiteLLM client.** It gates everything model-backed (#50, #51). Two
design decisions are already made; do not relitigate them:

- **Budget enforcement must be incremental, inside the loop**: check remaining, reserve the next
  operation, perform it, debit actual usage, release the unused reservation. The outer `TaskResult`
  check stays a consistency fence only. #48 landed *reservation, not accounting* — failed attempts,
  retries and spend-before-failure are not debited, so a run with `max_attempts=3` can consume ~3x its
  reservation and `runs.used_*` is a lower bound. A cap discovered after the tokens are spent is an
  audit fence, not a budget (I12).
- **The client must take a validated `GatewayConfig`, not a path.** I15 (production inference resolves
  only to approved self-hosted vLLM endpoints) is enforced today by a startup refusal that only the
  worker composition root calls. Making the client's parameter the validated type means a service that
  skips validation cannot construct a client at all. GUARDRAILS §5 names this as the promotion path.

Then: #50 classification (needs #69 + the review queue), #51 documentation, #71 (profiler fails at
exactly 416 columns — batch the aggregate pass, don't rewrite it), #47 (idempotency key→run mapping),
#48's remaining accounting, #21 (gate-honesty audit), #74, #72 (mislabelled: it's a budget-accounting
problem, not scheduling), #67.

## How this project works — the rules that matter

- **Issue-driven.** No issue, no branch. The commit hook enforces Conventional Commits and requires
  `feat`/`fix`/`refactor`/`perf` to reference an issue.
- **Dispatch implementation to subagents** in isolated worktrees; the maintainer (you, with me) reviews
  and merges. Give each one a written brief: the design decisions already made, the constraints, and
  what must be proven.
- **Run the `architecture-guardian` on every branch.** It has found a real defect on nearly every PR —
  a credential leak, a live regression, a tautological test. If it doesn't return, report it as absent;
  **never describe a review that didn't run** (an agent fabricated one once; PROOFS row 47).
- **Verify the claims that matter yourself before merging.** Not the whole diff — the security
  behaviour, the central acceptance property, and any assertion that a test is non-vacuous. Re-run the
  command. Several agent reports have been wrong or overstated.
- **Never write `Closes #N` for work that doesn't fully satisfy the issue.** Two PRs had to be corrected
  for claiming closure while documenting why they didn't close. Merge the improvement, leave the issue
  open with the remaining scope recorded.
- **`PROOFS.md` has a single writer** — the maintainer, on merge. Dispatched agents put proof rows in
  the PR body. A row made false by a diff must be struck or annotated, not left.
- **Open the PR before waiting on CI.** CI must be dispatched manually on branches
  (`gh workflow run ci --ref <branch>`); verify the conclusion *before* merging, and re-run
  `make fitness` on merged main afterwards — cross-PR breakage is real (PROOFS row 11).

## The failure mode this project keeps hitting — watch for it

**A check that reports green while measuring nothing.** Seven-plus instances, each a different shape:
gitleaks scanning 0 bytes; a LOC budget over an empty package; a contract check comparing a commit to
itself; a runner mapping exit 0 to PASS so SKIP displayed as PASS; a proof row whose command collected
two unrelated tests; a masking exemption defended by prose the regex never enforced; and the subtlest —
a test whose probe table had no rows, so it asked the same catalog question as the code it was checking
and agreed with itself forever.

Guard the **pathology** (baseline == HEAD, 0 bytes scanned, 0 rows in the fixture), never an allowlist
of known-safe cases. When you fix a false positive by widening an exemption, probe both directions —
I did that once and created a blind spot in the same commit that fixed one. And treat a count offered
as evidence ("11/11 contracts compared") as suspect: on the broken path it will vouch for the hole.

## Style — non-negotiable on anything public

Commits, issues, PR bodies and comments read like a busy engineer wrote them. Short, concrete, no
essays or marketing. **Never any AI attribution, `Co-Authored-By`, "Generated with" footers, or emoji.**
Claims go in `PROOFS.md` with a command, not in prose.
