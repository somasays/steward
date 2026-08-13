# Handover prompt — paste this into a new session

I'm building **Steward**, a multi-agent data management platform, at `~/code/data-management-agent`
(GitHub: **somasays/steward**, public). It's a portfolio piece for Staff/Senior AI Engineer applications,
so the engineering process is part of the product: every claim is backed by a reproducible command.

## Read these first, in this order — they are binding, not background

1. `ARCHITECTURE.md` — requirements, NFRs N1–N10, invariants **I1–I15**. Highest authority.
2. `GUARDRAILS.md` — the fitness functions derived from it (tiers S/H/B/P + hygiene G), the coverage
   matrix, the enforcement status, and the amendment process in §7.
3. `SPEC.md` — component design, roadmap M0–M6, and the decision records **D1–D15** in §13.
4. `CLAUDE.md` — the development workflow. Follow it exactly; it encodes rules this project paid for.
5. `PROOFS.md` — 117 rows, each a claim plus the command that reproduces it.

If they conflict: ARCHITECTURE > GUARDRAILS > SPEC > CLAUDE.md. **SPEC also outranks anything I tell a
subagent in a dispatch brief** — if SPEC says something is permitted, an agent will believe it over the
brief, so fix SPEC first.

## State

M0 and M1 slices 1–3a are shipped, and #50's persistence layer and Classifier with them. `main` is at
`0619822`. **#50 steps 7–8 — the review API — are written and green but not merged**: they are on
`m1/50-review-api`, open as **PR #83**, CI green on the tip. Everything below that describes the review
API describes that branch, not `main`.
Working end to end: register a Postgres source, scan it, persist assets/columns, profile every column
through a masking layer, run a **bounded agent** through API → queue → worker with per-attempt budget
accounting, reach a model through a real **LiteLLM proxy HTTP transport** — and now read a proposal, its
evidence and its review history over HTTP, approve or reject it, and read the asset's published
classification. Twenty fitness functions active (S1–S9, H1, H3, H4, H6, H7, H11, H12, G1–G5); B-tier eval
gates SKIP until M2.

**#69, #48 and #74 are closed.** PR #81 merged the classification schemas, migration `0006`, the
repository and the review lifecycle. **PR #82** merged #50 steps 1–6.

### Open: PR #83 (#50 steps 7–8)

Eight commits, `make fitness` green on each, `PROOFS.md` rows 118–126. What it landed:

- `GET /v1/assets/{id}/classification` (the approved version) and `/classifications` (every version).
- `GET /v1/reviews/{id}` and `POST /v1/reviews/{id}:approve|:reject`, with the standard `Idempotency-Key`.
- Eight new published contracts in `steward-schemas` (`review.py`), snapshotted by S6.
- The acceptance scenario: profile → agent → `pending_review` → approval → published version, against real
  PostgreSQL. The model is the only stub, and it names no column — it classifies whatever the real
  profiler put in the request.
- One refactor, in its own commit: `classification.py` decoded rows **positionally** across seven
  duplicated column lists. Now by name (`dict_row`), so a drifted projection is a `KeyError` rather than
  `status` read out of `model_alias`. The duplication itself is forced — ruff S608 flags a column list
  composed from a module constant exactly as it flags one composed from user input.

**#50 is still open** after this: B2 and the live gateway smoke test remain, which is why the PR says
"Part of #50".

## What #82 decided, so you don't relitigate it

`classify_asset` had to be a goal the shipped registry carries, and the goal/handler seam check requires a
registered goal's task types to have handlers reachable **by importing packages alone** — which an agent
handler cannot be, because it needs a gateway only a composition root may validate (I15). That is exactly
why `agent_echo`'s goal was kept out of the registry (SPEC §13 D13's closing note).

Resolved by splitting the workflow from the capability:

- **`steward-catalog` owns the workflow** and registers `classify_asset` at import, beside `scan_source`
  and `profile_asset`. It loads the one immutable **current** profile version, decides what a classifier
  may see, validates what comes back, and persists via `classification.propose`. It imports no gateway and
  calls no model.
- **The model sits behind `ColumnClassifier`** — `classify(run, request)`, evidence in and proposed columns
  out — implemented by `services/workers/classifier.py` with the #69 runtime, the `steward-classify` alias,
  the reservation, an **empty** tool allowlist and the versioned prompt.
- **Registration is systemwide; claiming is per process.** A worker without a classifier narrows its claim
  list (`Worker(task_types=...)`); a worker started *with* a transport and left unbound refuses to boot.
- The classifier is given `ClassificationRun` (ids, fencing pair, trace, budget) and **never** `TaskContext`,
  which would hand a model-facing adapter the catalog's open connection.
- Provenance is the code's, not the model's: `ProposedColumns` carries columns only.

Rejected, with reasons in D15: a `packages/steward-classifier`, and teaching the goal registry about
entry-point-provided task types.

## What to do next: B2 and the live gateway smoke test

Both were deferred because both need the completed agent path, and both are now unblocked. **Do not put
them in one PR with anything else.** #50's own text specifies them in detail — read the issue, not this
summary:

1. **B2.** A versioned labelled fixture with difficult negatives (`ssn_hash`, `email_domain`, synthetic
   data, misleading names, sparse evidence); PII recall ≥ 0.95 and precision ≥ 0.90; evidence validity
   scored **separately**, so a correct label with an unsupported citation still fails. Three pinned runs,
   **each independently** over threshold, 100% evidence validity in every run, per-column disagreement
   reported rather than averaged away. An absent fixture, an empty prediction set or a skipped model
   execution must not report PASS. This is the one place the shipped work bends an invariant (I11).
2. **The live smoke test.** One environment-gated test calling `steward-classify` through the real
   LiteLLM proxy: alias routing, auth, streaming tool calls, usage extraction, **non-zero cost**, typed
   completion. Missing proxy configuration is INCONCLUSIVE locally and a **failure** in the release job —
   #74's distinction, applied here.

**Nothing else belongs in either increment.** No lifecycle redesign, no new runtime abstraction, no
automatic scheduling (#72), no Documentarian (#51).

## How this project works — the rules that matter

- **Issue-driven.** No issue, no branch. The commit hook enforces Conventional Commits and requires
  `feat`/`fix`/`refactor`/`perf` to reference an issue.
- **Run `make fitness`, never `python3 scripts/fitness/run.py`.** The launcher selects the project
  interpreter (#74).
- **Verify the claims that matter yourself before merging.** Not the whole diff — the security behaviour,
  the central acceptance property, and any assertion that a test is non-vacuous. Re-run the command.
- **Mutate the code to prove a test is load-bearing.** The single most valuable habit on this repo. Delete
  the feature, run the test, confirm it fails, restore. It earned its keep three more times on #82.
- **Check every proof row's command actually selects what the row claims.** A `-k` selector that
  over-matches reports a bigger number than the claim describes; #82 shipped one (`-k "citation"` also
  matched the parametrised citation test, claiming 3 while selecting 9) and it was caught only by running
  every row before pushing.
- **Run the `architecture-guardian` on every branch.** It is **not registered as a dispatchable subagent
  type** — the definition lives at `.claude/agents/architecture-guardian.md` and must be handed to a
  general-purpose agent to follow verbatim. On #82 it was applied by the working session itself, which is a
  self-review and was labelled as one in the PR. Say which you did; never describe a review that did not
  run as if it did.
- **Never write `Closes #N`** for work that doesn't fully satisfy the issue. #82 says "Part of #50".
- **Open the PR before waiting on CI.** CI must be dispatched manually on branches
  (`gh workflow run ci --ref <branch>`); verify the conclusion *before* merging, and re-run `make fitness`
  on merged main afterwards.

## The failure mode this project keeps hitting — watch for it

**A check that reports green while measuring nothing.** Fifteen-plus instances now, each a different shape:
gitleaks scanning 0 bytes; a LOC budget over an empty package; a contract check comparing a commit to
itself; a runner mapping exit 0 to PASS so SKIP displayed as PASS; a fixture with 0 rows agreeing with
itself; an autouse fixture whose docstring promised no network and whose body was a bare `yield`; a
tool-call test that passed only because it put the whole call in one delta; a concurrency test that
asserted a lock and measured an index; a lifecycle fixture that profiled *no columns*, so every evidence
citation in the suite was unresolvable and unchecked; and a regression test deleted by an unrelated text
edit, which merged missing and surfaced only when its proof row's command selected nothing.

**#82 added four more shapes, and they are the ones to read:**

> **A negative-only guard cannot distinguish "rejects invalid input" from "rejects every input".** Every
> rejection boundary needs a nearby positive case proving valid input crosses it. This one hid a live
> defect through two review rounds on #81.

> **A completeness check that only inspects what is present cannot see what is missing.** Evidence
> resolution walked the citations a proposal carried, so a three-column table accepted a one-column
> proposal as `SUCCEEDED` / `pending_review` — the asset read as classified with two columns never
> assessed. The same check caught an invented column only when it was labelled *sensitive*, because `none`
> requires no evidence: it caught the careless model and missed the plausible one.

> **A new guard can silently disarm the tests that came before it.** Adding exact-coverage would have
> refused every existing single-column stub *before* its citation was ever resolved — the evidence tests
> would have stayed green while testing nothing. Fixed by routing all stubs through one `covering()` helper
> built from the request, and *verified* by neutralising `_resolve` and confirming the evidence test still
> fails. When you add a guard upstream of existing assertions, prove the downstream ones still run.

> **Compare names, not counts.** A coverage check comparing lengths agrees with itself whenever a model
> drops one column and invents another — which is the shape a model is most likely to produce.

**#83 added one, and it is a gate blind spot rather than a hollow check:**

> **An import in a `tests/` tree is invisible to the check that forbids it.** The acceptance scenario
> imported `steward_workers.__main__` from `services/api/tests` to assert the worker's claim list —
> a services-import-services edge I4 forbids. S1 never saw it: import-linter's `root_packages` are the
> `src/` trees, and the import resolves anyway because uv installs every workspace member into one venv.
> `make fitness` was green on it through four commits. Found by reading the diff against I4 by hand,
> which is what the `architecture-guardian`'s own instructions warn to do — two of #49's rounds found the
> same shape. The property was already proven where it belongs
> (`services/workers/tests/test_worker_capabilities.py`), so the fix was deleting the test.

And one that is not a hollow check but a contradiction worth naming:

> **An unsatisfiable contract fails in the wrong place.** Postgres permits a relation with no columns and
> this catalog profiles one, but a proposal must classify exactly the profiled columns *and* carry at least
> one. No output satisfies both, so a cooperative classifier burned a model call and its one correction
> before failing as `classifier-failed` — an error naming the classifier for a property of the asset. When
> two constraints cannot both hold, refuse before the expensive step, not after.

Guard the **pathology** (baseline == HEAD, 0 bytes scanned, 0 rows, 0 entry points enumerated, 0 columns
covered), never an allowlist of known-safe cases. Treat a count offered as evidence as suspect: on the
broken path it will vouch for the hole.

## Known gaps, stated rather than hidden

**New with #83:**

- **The `ruff` gap below is unchanged and still needs an issue.** It was not touched in #83 either, for
  the same reason: 19 files of formatting churn would bury a feature diff.

**Carried from #82:**

- **B2 is not implemented.** #50 asks for the eval gate with this capability and I11 asks for eval coverage
  on LLM-dependent behaviour. The B tier activates in M2 and the build order deferred B2 to the increment
  after. Nothing published without human review, but this is the one place the branch bends an invariant,
  and it is now the next thing.
- **H7's canary sweep does not cover the classification path's logs or stdout.** The prompt *input* is
  covered by composition — what reaches the model is asserted to be the static artifact plus the
  `ClassificationRequest`, and that request is asserted canary-free against a real profile of the
  canary-planted table — and the span half follows, since a generation span's `input` is those message
  contents. GUARDRAILS §1 now says this rather than the stale "there are no prompts yet".
- **`make lint` fails on `main`.** `ruff>=0.6` is unpinned, `uv` resolved 0.16.1, and `ruff format --check`
  now wants 19 pre-existing files reformatted. `make fitness`'s G1 runs only `ruff check`, so the
  documented gate is green while the Makefile target is not. Needs an issue: pin ruff, or reformat, or
  both. **Not** fixed inside a feature branch — 19 files of churn would bury the diff.
- **`pgserver` can fail to start under a sandboxed environment** that blocks its lockfile under
  `~/Library/Caches`. The PostgreSQL-backed tests then cannot run at all; the non-PostgreSQL ones still do.
  If you see that, say so rather than reporting a partial run as a pass.

**Carried forward:**

- The prompt-token ceiling is UTF-8 bytes over the serialised request. `tokens ≤ bytes` holds for
  byte-level BPE, which every model behind our aliases uses; a tokenizer-owned bound would replace it.
- An interrupted gateway call is charged its **proven upper bound** (the preflight's ceiling), not its
  exact spend — the protocol reports usage only in a terminal frame.
- A task abandoned at its wall-clock cap records a **lower bound**: its thread may spend more after the
  worker reads the ledger.
- `runs.used_*` is clamped at the cap, so **no assertion about overspend can be made against it** — it
  reads "exactly the cap" whether or not more was spent. Use `tasks.used_*` or the audit row
  (`requested`/`applied`/`overspend`). A later fix silently disarmed an earlier test this way.
- The `x-litellm-response-cost` header is not read — cost is computed locally from validated prices.
- The lock-ordering comment in `runs.record_usage` reports evidence, not a proven mechanism: reversing the
  order deadlocks reliably, but the precise trigger was not isolated.
- `policy_id == actor.id` is enforced for policy approvals. If actor identity and policy identity are ever
  meant to differ, that needs a trusted mapping, not two free-form strings allowed to disagree.

## Style — non-negotiable on anything public

Commits, issues, PR bodies and comments read like a busy engineer wrote them. Short, concrete, no essays
or marketing. **Never any AI attribution, `Co-Authored-By`, "Generated with" footers, or emoji.** Claims
go in `PROOFS.md` with a command, not in prose.
