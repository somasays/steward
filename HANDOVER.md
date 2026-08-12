# Handover prompt — paste this into a new session

I'm building **Steward**, a multi-agent data management platform, at `~/code/data-management-agent`
(GitHub: **somasays/steward**, public). It's a portfolio piece for Staff/Senior AI Engineer applications,
so the engineering process is part of the product: every claim is backed by a reproducible command.

## Read these first, in this order — they are binding, not background

1. `ARCHITECTURE.md` — requirements, NFRs N1–N10, invariants **I1–I15**. Highest authority.
2. `GUARDRAILS.md` — the fitness functions derived from it (tiers S/H/B/P + hygiene G), the coverage
   matrix, the enforcement status, and the amendment process in §7.
3. `SPEC.md` — component design, roadmap M0–M6, and the decision records **D1–D13** in §13.
4. `CLAUDE.md` — the development workflow. Follow it exactly; it encodes rules this project paid for.
5. `PROOFS.md` — 105 rows, each a claim plus the command that reproduces it.

If they conflict: ARCHITECTURE > GUARDRAILS > SPEC > CLAUDE.md. **SPEC also outranks anything I tell a
subagent in a dispatch brief** — if SPEC says something is permitted, an agent will believe it over the
brief, so fix SPEC first.

## State

M0 and M1 slices 1–3a are shipped, and #50's persistence layer with them. `main` is at `74132ba`, green. Working end to end: register a Postgres
source, scan it, persist assets/columns, profile every column through a masking layer, run a **bounded
agent** through API → queue → worker with per-attempt budget accounting, durable checkpoints and tracing,
and reach a model through a real **LiteLLM proxy HTTP transport**. Twenty fitness functions active
(S1–S9, H1, H3, H4, H6, H7, H11, H12, G1–G5); B-tier eval gates SKIP until M2.

**#69 (bounded agent loop + gateway client) and #48 (run budget accounting) are closed.** #74 is closed —
the documented fitness command can no longer print "all checks green" while skipping a check this machine
could not run.

### Nothing is open

PR #81 merged at `74132ba`: classification schemas, migration `0006`, the repository, and the review
lifecycle — 273 catalog tests, `make fitness` green on merged `main`. It went through four review rounds
and every one found something real; read its body if you want the reasoning behind the approval
transaction, the advisory lock, and the evidence locators.

## What to do next: #50, in increments

#50 (Sensitivity Classifier) is the first real product capability and the milestone that matters:

> Connect a source, profile one table, classify its sensitive columns with evidence, and let a human
> approve the result.

**Do not put the rest of #50 into one PR.** #69 became enormous and took seven review rounds; each round
found real defects, but the branch was far too large to review well. Steps 3–5 merged as PR #81. **The
active work is steps 6–8**, branched from `main`.

### The order to build it in

Straight down the vertical path — each step is worth having on its own, and none of them is a layer
built for a later step's benefit:

1. Register the classifier goal and its worker task type.
2. Load one immutable current profile version.
3. Build the evidence-only agent input from it.
4. Add the versioned prompt artifact.
5. Run it through the existing #69 runtime.
6. Persist the typed proposal as `pending_review` (`steward_catalog.classification.propose`).
7. Add start / read / history / current / review API behaviour.
8. Prove API → queue → worker → agent → review → published classification against real PostgreSQL.

**Nothing else belongs in this increment.** No lifecycle redesign, no new runtime abstraction, no B2
evaluation, no automatic scheduling (#72), no Documentarian (#51). B2 and the live proxy smoke test are
the increment *after*, because both need the completed agent path to exist first.

### The contracts for steps 6–8

- **Register `steward-classify`; do not build another runtime.** The bounded loop, budgets, checkpointing,
  tracing and tool allowlists all exist in `steward-agents` (#69). Use them.
- **Consume one immutable current profile version.** The request names an asset and a profile version.
- **Pass only** schema, statistics, masked samples, and evidence identifiers to the model.
- **No** source connection, raw values, arbitrary SQL, or catalog-mutation tools in the allowlist.
- **Use a versioned prompt artifact** under `prompts/`, and record its version on the result.
- **Produce the existing typed `ClassificationProposal`** — it is already defined and already enforces
  `none`-exclusivity, evidence-per-sensitive-label, and same-column/same-profile citations.
- **Persist initially as `pending_review`** through `steward_catalog.classification.propose`.
- **Start through `POST /v1/runs`.**
- **Add API behaviour**: read a proposal, its history, the current published version, and approve/reject
  using the API's idempotency convention.
- **Exercise API → queue → worker → classifier → pending review → approval → current published version
  against real PostgreSQL.**
- **Keep B2 and the live proxy smoke test for the increment after** — both need the completed agent path.

The approval lifecycle is done and specified in **SPEC §13 D14**: approval is one atomic supersession,
serialised per asset by an advisory lock, with the partial unique index as the final fence. The
repository owns outcome, actor and decision time — `ReviewCommand` carries only a reason and an optional
policy id — and evidence carries a kind-specific `locator` checked against the stored profile. Don't
re-litigate any of it; build the agent to produce a `ClassificationProposal` and call `propose`.

## How this project works — the rules that matter

- **Issue-driven.** No issue, no branch. The commit hook enforces Conventional Commits and requires
  `feat`/`fix`/`refactor`/`perf` to reference an issue.
- **Run `make fitness`, never `python3 scripts/fitness/run.py`.** The launcher selects the project
  interpreter; the raw invocation under an older system Python now reports INCONCLUSIVE rather than green,
  but the launcher is the documented command (#74).
- **Verify the claims that matter yourself before merging.** Not the whole diff — the security behaviour,
  the central acceptance property, and any assertion that a test is non-vacuous. Re-run the command.
- **Mutate the code to prove a test is load-bearing.** This is the single most valuable habit on this
  repo. Delete the feature, run the test, confirm it fails, restore. It has caught a hollow test written
  *by the person guarding against hollow tests* — twice.
- **Run the `architecture-guardian` on every branch.** Note: it is **not registered as a dispatchable
  subagent type** in the Claude Code harness — the definition lives at `.claude/agents/architecture-guardian.md`
  and must be handed to a general-purpose agent to follow verbatim. Say so when reporting; never describe
  a review that did not run as if it did.
- **Never write `Closes #N`** for work that doesn't fully satisfy the issue.
- **`PROOFS.md` has a single writer** — the maintainer, on merge. Dispatched agents put proof rows in the
  PR body.
- **Open the PR before waiting on CI.** CI must be dispatched manually on branches
  (`gh workflow run ci --ref <branch>`); verify the conclusion *before* merging, and re-run `make fitness`
  on merged main afterwards.

## The failure mode this project keeps hitting — watch for it

**A check that reports green while measuring nothing.** Ten-plus instances now, each a different shape:
gitleaks scanning 0 bytes; a LOC budget over an empty package; a contract check comparing a commit to
itself; a runner mapping exit 0 to PASS so SKIP displayed as PASS; a fixture with 0 rows agreeing with
itself; an autouse fixture whose docstring promised no network and whose body was a bare `yield`; a
tool-call test that passed only because it put the whole call in one delta, which is not how a gateway
streams; a concurrency test that asserted a lock and measured an index; a lifecycle fixture that profiled
*no columns*, so every evidence citation in the suite was unresolvable and unchecked; and a regression
test deleted by an unrelated text edit, which merged missing and surfaced only when its proof row's
command selected nothing.

**The named shape worth remembering above all the others:**

> A negative-only guard test cannot distinguish "rejects invalid input" from "rejects every input". Every
> rejection boundary needs at least one nearby positive case proving valid input crosses it.

That one hid a live defect through two review rounds: masked-sample evidence could *never* resolve --
its locators were built from a model's `repr` -- and "rejects an invented sample" passed the whole time.

Guard the **pathology** (baseline == HEAD, 0 bytes scanned, 0 rows, 0 entry points enumerated), never an
allowlist of known-safe cases. Treat a count offered as evidence ("11/11 contracts compared") as suspect:
on the broken path it will vouch for the hole.

## Known gaps, stated rather than hidden

- The prompt-token ceiling is UTF-8 bytes over the serialised request. `tokens ≤ bytes` holds for
  byte-level BPE, which every model behind our aliases uses; a tokenizer-owned bound would replace it.
- An interrupted gateway call is charged its **proven upper bound** (the preflight's ceiling), not its
  exact spend — the protocol reports usage only in a terminal frame.
- A task abandoned at its wall-clock cap records a **lower bound**: its thread may spend more after the
  worker reads the ledger.
- `runs.used_*` is clamped at the cap; the full attempted amount lives in the audit row
  (`requested`/`applied`/`overspend`).
- The `x-litellm-response-cost` header is not read — cost is computed locally from validated prices.
- The lock-ordering comment in `runs.record_usage` reports evidence, not a proven mechanism: reversing the
  order deadlocks reliably, but the precise trigger was not isolated.
- `runs.used_*` is clamped, so **no assertion about overspend can be made against it** — it reads "exactly
  the cap" whether or not more was spent. Use `tasks.used_*` or the audit row. A later fix silently
  disarmed an earlier test this way.
- `policy_id == actor.id` is enforced for policy approvals. If actor identity and policy identity are ever
  meant to differ, that needs a trusted mapping, not two free-form strings allowed to disagree.

## Style — non-negotiable on anything public

Commits, issues, PR bodies and comments read like a busy engineer wrote them. Short, concrete, no essays
or marketing. **Never any AI attribution, `Co-Authored-By`, "Generated with" footers, or emoji.** Claims
go in `PROOFS.md` with a command, not in prose.
