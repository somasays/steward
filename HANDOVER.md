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

M0 and M1 slices 1–3b are shipped — #50's persistence layer, Classifier and **review API** with them.
`main` is at `1be2a02`, green: `make fitness` re-run on merged main, all checks green (S6 SKIPs on main
by design — the merge-base is HEAD itself, so there is no divergence to compare).
Working end to end: register a Postgres source, scan it, persist assets/columns, profile every column
through a masking layer, run a **bounded agent** through API → queue → worker with per-attempt budget
accounting, reach a model through a real **LiteLLM proxy HTTP transport** — and now read a proposal, its
evidence and its review history over HTTP, approve or reject it, and read the asset's published
classification. Twenty fitness functions active (S1–S9, H1, H3, H4, H6, H7, H11, H12, G1–G5); B-tier eval
gates SKIP until M2.

**#69, #48 and #74 are closed.** PR #81 merged the classification schemas, migration `0006`, the
repository and the review lifecycle. **PR #82** merged #50 steps 1–6.

### Nothing is open

**PR #83 merged at `1be2a02`** (rebase, linear history, branch kept — the style #81 and #82 used). It
landed **#50 steps 7–8**. It went through **five review rounds**, and rounds 3, 4 and 5 each found a
defect in the previous round's *fix*, all on one line of `auth.py` — read the PR comments before
touching authentication, and read "the failure mode this project keeps hitting" below, which those
rounds extended.

Fourteen commits, `make fitness` green on each, `PROOFS.md` rows 118–136. What it landed:

- `GET /v1/assets/{id}/classification` (the approved version) and `/classifications` (every version).
- `GET /v1/reviews/{id}` and `POST /v1/reviews/{id}:approve|:reject`, with the standard `Idempotency-Key`.
- Eight new published contracts in `steward-schemas` (`review.py`), snapshotted by S6.
- The acceptance scenario: profile → agent → `pending_review` → approval → published version, against real
  PostgreSQL. The model is the only stub, and it names no column — it classifies whatever the real
  profiler put in the request.
- **API-key authentication on the two decision endpoints**, added after review. `X-API-Key` names a
  principal from `STEWARD_API_KEYS`, and that principal becomes the `Actor` — the repository refuses a
  caller-supplied actor precisely so the credential is the only thing that can say who approved a
  classification, and an unauthenticated endpoint recording a constant `human:api` made that whole chain
  terminate in a fiction. A key can only produce a *human* principal (a policy one would let anything
  holding a secret record an automatic approval). The credential is published as an OpenAPI **security
  scheme**, not an optional header — invisible at runtime, decisive in the contract, since SPEC §8
  generates the SDK from it. Reads and the older mutations are still unauthenticated — a real gap,
  stated in SPEC §8, not closed here.
- **`GET /v1/reviews/{id}` reads from one snapshot.** Its two statements under the default READ COMMITTED
  took two, so a decision committing between them returned `pending_review` beside an approval. The
  repository operation now refuses a transaction that cannot answer it.
- One refactor, in its own commit: `classification.py` decoded rows **positionally** across seven
  duplicated column lists. Now by name (`dict_row`), so a drifted projection is a `KeyError` rather than
  `status` read out of `model_alias`. The duplication itself is forced — ruff S608 flags a column list
  composed from a module constant exactly as it flags one composed from user input.

`PROOFS.md` rows 118–136 landed with it. **#50 itself is still open** — B2 and the live gateway smoke
test remain, which is why the PR said "Part of #50" rather than `Closes`.

**#84 is open and is a release blocker**: authenticate the rest of the API surface (`POST /v1/sources`,
the scan endpoint, `POST /v1/runs`), retire `API_ACTOR`, decide whether the reads need a credential, and
publish the resulting OpenAPI `security`. Steward should not be exposed outside a trusted local
environment until it is done. It is deliberately *not* a prerequisite for B2.

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

## What to do next: finish B2 — but read these two checks first

**#50 is open and must stay open.** B2 has a fixture, a scorer and a three-run gate; it has
**never executed against a model**, so there are no B2 quality results. Do not quote a number that
does not exist, and do not close #50 on the machinery being present.

Branch **`m1/50-b2-and-smoke`** (pushed, `make fitness` green on every commit) carries:

- `evidence_problems()` — one definition of "this citation resolves", shared by the repository and
  the scorer, so the eval cannot pass what production refuses.
- The eval gate invoking a command that exists, with tri-state semantics: nothing affected → 0;
  selected with no endpoint → 3, reported SKIP with its reason and **never PASS**; the same under
  `STEWARD_EVALS_REQUIRED=1` → 1, which is what the release job sets; an absent or empty fixture → 1
  regardless. 21 tests, no model needed.
- The LiteLLM proxy pinned **by digest** to its own runtime — running it inside Steward's environment
  fails outright on a FastAPI incompatibility, which is the argument for the pin.
- The live gateway smoke test asserting **durable state**: run succeeded within every budget
  dimension, persisted tokens and cost non-zero, exactly one `pending_review` proposal, exact column
  coverage, evidence resolving through production code, alias and prompt version, trace/run/task
  provenance, and an artifact naming the images and config digest it ran against.
- Checkpoint == task == run usage **exactly**, across steps, tokens, cost and recorded latency.
  Non-zero on all three would miss both a double charge and a missing debit; equality catches both,
  and both were mutation-proven.
- `steward_queue.ledger_cost()` — `ROUND_HALF_UP`, because PostgreSQL rounds ties away from zero and
  Python's default rounds to even. Two of four tie cases would have failed the accounting assertion
  **on a correct run**, intermittently. The scale is read from `information_schema` and ties are
  round-tripped through the real column, so a migration widening it fails loudly.

### Both pre-run checks are settled — two product decisions, now implemented

1. **The retry boundary is typed, not textual.** `EvaluationInfrastructureError` is the only
   retryable failure and is raised by the gateway harness alone, from failure *types*
   (`httpx.TransportError`, `TimeoutError`, `ConnectionError`) found by walking the `__cause__`
   chain — the seam wraps them in `ClassifierFailed` so `steward-catalog` never sees a
   `steward-agents` type (I4), and `raise ... from` keeps the original reachable. Everything else is
   `EvaluationResult`: a completed run with an unusable answer, never retried. Unknown exceptions
   fail immediately rather than being guessed at. The scorer produces results only.

   The old rule grepped the message, which made "a threshold miss is never retried" depend on
   wording. Two tests pin the difference: a model failure whose text *contains* "connection" and one
   containing "timeout" must both be results. Reverting to message matching fails exactly those two
   plus the wrapped-transport case.

2. **`test_card_number` is `financial`.** Steward classifies what a column is *for*, not whether
   today's sample is synthetic — which is what the prompt already says, so the prompt is unchanged
   and the fixture was the thing that was wrong. It is not `pii`: a fixture card is tied to no
   cardholder, which is what separates it from `card_number`. The synthetic hard negative it used to
   provide is now `synthetic_row_id` — a surrogate identifier with no meaning outside the dataset,
   expected `none`, testing the prompt's actual rule ("a surrogate key with no meaning outside this
   database is not pii") instead of one it contradicts.

   Fixture is now 15 columns: 7 sensitive, 8 negative, 6 hard negatives.

### Then, in order

1. Three preflight runs to prove the harness executes. **Label the artifacts non-release** — Ollama
   is a plumbing preflight and its scores characterise a model no deployment runs.
2. Inspect coverage, metrics, evidence validation, the disagreement output and retry behaviour.
3. Run against pinned LiteLLM → **vLLM**. vLLM is not an interchangeable backend: its chat template,
   tool parser, streamed frames, usage reporting and model revision are what the release evidence
   validates.
4. Require pinned provenance and **independently** passing thresholds on all three runs — recall
   ≥ 0.95, precision ≥ 0.90, evidence validity 100%, exact coverage, no infrastructure error. No
   averaging, no majority vote.
5. Commit the evidence of record, and only then close #50.

**#84** (authenticate the rest of the API surface) is a release blocker before external exposure, and
is deliberately not a prerequisite for B2. **#85** (a failed checkpoint save replaces the agent
failure that caused it) was found while running a real classification and is filed separately.

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

**#83 added a second, from its independent review — a claim the code did not keep:**

> **A constant-time comparison that raises is not constant-time, and not a comparison.**
> `hmac.compare_digest` *raises* `TypeError` on a `str` holding any character above U+007F rather
> than returning False. So a credential with one high byte aborted the "never breaks early" loop on
> its first iteration and escaped as a 500 — where the contract promises a 401, and where a
> deployment *with* keys answered 500 while one with *none* answered 401, handing an anonymous
> caller an oracle. `httpx` refuses to send such a header, so no test using the test client could
> reach it; it took a raw ASGI probe. The module's own docstring asserted the property the code
> broke. **Prose in a docstring is not enforcement — if a guarantee matters, something has to fail
> when it stops holding.**

> **A fix for a crash can be worse than the crash.** The repair for the above encoded the credential
> with `errors="replace"`, which maps every non-Latin-1 character to `?` — so `"ключ"` became
> `b"????"` and authenticated a principal whose secret was literally `"????"`. The crash failed
> closed; the fix authenticated the wrong person. Caught by the *second* bounded review of the delta,
> not the first. **When a repair touches a comparison that decides identity, ask what it now treats
> as equal**, and give the fix its own adversarial pass rather than assuming a fix inherits the
> scrutiny the bug got.

> **Fixing one operand of a comparison fixes half a defect.** The repair above settled the
> *presented* credential and left the *configured* one encoding inside the request loop, where a
> lone surrogate raises — restoring all three properties the original fix removed, through the
> operand nobody sends. Reachable, not theoretical: `os.environ` decodes an undecodable byte with
> `surrogateescape`, so a non-UTF-8 key file produces exactly that value. **When a defect is about
> normalising a value, enumerate every value that reaches the comparison, not the one in the bug
> report.** Three review rounds landed on the same line for three different reasons.

And a process one, which bit twice in one PR:

> **A count in a proof row is stale the moment you add a test.** Rows 123/124 said "8 passed" and
> "5 of the file's 8"; the file had grown to 11 by the time review ran. Both times the drift came
> from editing the suite *after* writing the row. Re-run every row's command against the tip before
> pushing, not once when the row is written. The same edit-after-claim drift put a scenario in
> GUARDRAILS' H11 row that `-m acceptance` does not execute.

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
