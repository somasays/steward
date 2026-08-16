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
5. `PROOFS.md` — 146 rows, each a claim plus the command that reproduces it.

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

### Open: #50, #84, #85, #86, #88–#95 — and see the two review sections below

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

## What to do next: finish B2

**#50 is open and must stay open.** The harness has now **executed against a model twice** and
**neither pass reached scoring** — both died on defects. So there are still **no B2 quality
results**. Do not quote a number that does not exist, and do not close #50 on the machinery
being present.

### What the two preflight passes found (2026-08-16)

**Pass 1 — a production accounting defect, now fixed.** All three runs failed identically:

```
result: run 4d73dcab... had less left than this step spent:
        requested 1 steps / 3096 tokens / 0.00007528, applied 1 / 3096 / 0.000075
```

Not a budget breach — the **seventh decimal place**. `record_step_usage` differenced the gateway's
unrounded cost against the delta the ledger stored, and `used_cost_usd` is `numeric(14, 6)`. Steps
and tokens applied unclamped, against a $0.50 cap on a $0.000075 step. Direction decides it: a cost
rounding *up* floors to no breach, one rounding *down* reports one, so a **correct run failed or
passed on what the model happened to charge**. `LEDGER_COST_SCALE` already stated the rule this code
broke. Fixed in `fix(queue): a cost below the ledger's resolution is not a budget breach (#50)`;
`budget_cost_usd` is at the same scale, so both operands of "did this exceed what is left" now agree,
and the SQL clamp — which is what I12 actually rests on — was never involved.

It reached `DurableCheckpointStore` on every agent step, so **every real classification run tripped
it**, and the live gateway smoke test would have too. Neither had ever run against a model, which is
why nobody had seen it.

**Pass 2 — the preflight model, not Steward.** Every run then died on `encounters`, one of the three
fixture tables, reported as "the agent stopped without calling `submit_result` ... never as prose".
It produced no prose. It produced **nothing**: `finish_reason: stop`, empty content, no tool call,
240–480 billed completion tokens. Narrowed a layer at a time with the byte-identical body from
`steward_llm.wire.request_body`:

| layer | result |
|---|---|
| eval → runtime → transport → LiteLLM → Ollama | empty, `stop`, 479 tokens |
| raw SSE from LiteLLM (streaming) | empty content, no tool call, 479 tokens |
| same body non-streaming through LiteLLM | empty content, 479 tokens |
| **direct to Ollama, bypassing LiteLLM** | **empty content, 479 tokens** |

Not the streaming path, not LiteLLM, not Steward's transport — the transport parsed exactly what
arrived. Not a decoding parameter either: `{temperature:0,seed:N}`, `{temperature:0}`, `{seed:N}`
and `{}` all reproduce it; one earlier call did return a valid tool call, so it is stochastic and
mostly failing. `customers` and `payments` produce valid submissions with **exact column coverage on
every attempt**, so the preflight did the job its own config claims — it proved the streaming
tool-call path works and found where it does not.

Filed **#86**: the runtime asserts prose that did not exist, and a completed response with no
content, no tool calls and non-zero usage deserves its own message. The #85 shape — a later, less
informative failure replacing the real one. It cost this session an hour.

### What an independent review of PR #87 found — four merge blockers, all real, all fixed

Verified against the code rather than accepted, then fixed in `29884c8`. Worth reading because two
of them are the signature pathology again:

1. **Suite selection failed open.** `_changed_paths` tested `diff.returncode != 0 **and**
   working.returncode != 0`. The common case is one of the two: a clone with no `origin/main` fails
   the diff with rc 128 while `git status` returns rc 0 and, on a clean worktree, nothing — so paths
   came out empty, the suite was not selected, and the runner printed "no eval suite is affected by
   this change" and exited 0. **A green B\* that measured nothing**, contradicting the docstring
   directly above it, which already promised over-selection. Now `or`, with a positive case beside
   the three failure cases so it cannot be satisfied by always selecting.
2. **The selector was blind to what decides a B2 run.** It named prompts, the classifier, the
   handler and the fixture — not the binding table, the transport, the agent loop or the ledger. A
   change to the model `steward-classify` resolves to did not trigger classification evals. Now
   includes `steward-llm/`, `steward-agents/`, and `steward_queue/runs.py` + `usage.py` — that last
   pair on evidence, since the rounding defect above failed every B2 run without touching anything
   the old list named.
3. **A missing proxy escaped the tri-state.** `_require_gateway` checked bindings, not the proxy, and
   `classify_once`'s `ClassifierFailed` is caught by nothing — `_one_run` handles only
   `EvaluationInfrastructureError` and `EvaluationResult`. A valid gateway with no
   `STEWARD_LLM_PROXY_URL` produced a traceback and exit 1 where the contract promises
   `EXIT_NO_ENDPOINT`. The line carried `# pragma: no cover -- the caller checks first`; **no caller
   did.** A pragma is a claim, and that one was false.
4. **`release_evidence` was two unvalidated strings.** `litellm.production.yaml` falls
   `steward-classify` back to `steward-fast` and `CompletionResult` carries the alias, not what
   answered — so a run served entirely by the fallback was indistinguishable from one served by the
   classifier model, and any non-empty pair of env vars turned the claim on. Now fails closed on
   three conditions: immutable digests (a moving tag is rejected), `REQUIRED`, and the responding
   model on record. The third is **#89** and is not satisfiable today, so the claim is refused.

And the process one, which is why "the release job" now reads differently everywhere:

> **A guardrail describing enforcement that does not exist is not a guardrail.** Six places said "the
> designated release job sets `STEWARD_EVALS_REQUIRED=1`". `.github/workflows/ci.yml` has the fitness
> gate and the secret scan; the flag appears nowhere in `.github/`. B\* is SKIP on every CI run and
> the `live_gateway` marker is never selected — so **CI green on this branch means B2 skipped, not B2
> passed.** Tracked as **#88**; every mention now says the job does not exist yet.

### What the independent architecture-guardian pass found — the gate did not gate

Two reviewers over the full `main...HEAD` diff, one invariant-led and one hunting hollow checks.
**Both returned FAIL.** Every violation was re-verified against the code before being acted on; all
are fixed in `75ba2c1`. The headline is the worst shape this repository has produced yet:

> **B2's verdict had no tests.** Zeroing `PII_RECALL_FLOOR` *and* `PII_PRECISION_FLOOR`, setting
> `RUNS = 1`, and changing `all(...)` to `any(...)` in `_report` left **285 tests passing**. Every
> claim GUARDRAILS' B2 row makes — both floors, and "three pinned runs, each independently over
> threshold" — could be deleted and the whole repository stayed green. The scorer was covered and the
> artifact was covered; the code that turns scores into PASS/FAIL was not. `all([])` is `True`, so an
> empty run list reported PASS as well. **The gate this branch exists to build did not gate.**

`TestTheVerdict` in `test_eval_scoring.py` closes it — 13 tests, no model, because the eval package's
own argument is that "the gate's own behaviour is testable where the thing it gates is not". Each of
the four mutations now fails. Re-run them before touching thresholds or the run loop.

Three invariant violations, all in the fitness suite's known blind spots — which is the lesson:

- **I5** — `conn.execute(f"SELECT {raw}::numeric(14,6)")`. S608 cannot see it (no `FROM`/`WHERE` for
  its regex), and it was the only `conn.execute(f"` in the repo. Parameterised.
- **I7** — `harness.py` mutated `tasks` with its own `UPDATE ... SET state = 'running'`, skipping
  `claimed` in the state machine and writing no audit rows. Invisible to H5, which sweeps the
  repository registry and cannot see a service issuing raw SQL. Now `claim` + `mark_running`;
  verified against a real database, the trail gains `task.claimed`, `task.started` and
  `run.status_changed`, and the run carries a real trace id where the old code hardcoded `"0" * 32`.
- **I4** — `test_live_gateway.py` imports `steward_orchestration`, undeclared in
  `services/workers/pyproject.toml`. S1's `root_packages` are the `src/` trees, so a `tests/` import
  is outside every contract — the third time this exact blind spot has bitten. Declared.

**Read this if you take one thing from the pass:** all four findings sat where a green suite cannot
look. Twenty fitness functions were green over a gate that measured nothing and three invariant
breaches. A self-review by the working session had already passed over the same diff and found none
of them.

Filed rather than fixed: **#90** (B* prints PASS both when a suite passed and when none was
selected — the same two-states-one-code shape `EXIT_NO_ENDPOINT` exists to prevent), **#91** (at 4
PII columns "recall ≥ 0.95" is really "miss nothing" and "precision ≥ 0.90" is "zero false
positives"; the quoted tolerance is not expressible at this fixture size), **#92** (SPEC and
GUARDRAILS still describe a Dockerized fixture warehouse and Langfuse datasets), **#93**
(`pgserver`/`httpx` imported in `src/` undeclared), **#94** (the runner ignores which suite was
selected), **#95** (the live smoke can pass its provenance check writing no evidence file).

That gap is now closed: PR #87's body carries a second proof table for the earlier harness — the
shared evidence resolver, the tri-state gate, the typed retry boundary, the fixture and scorer, the
three-run verdict and the state-machine claim — with every command re-run against the tip. It also
says plainly what is **not** proven, because it does not exist: no B2 quality result and no
live-vLLM proof.

A third review round asked for the one thing missing from the I7 fix, and it is worth carrying:

> **A fix verified by hand is not a fix with a test.** `_claimed_task` was corrected and checked
> against a real database in the session that changed it — and nothing was committed, so restoring
> the raw `UPDATE` stayed green. `test_eval_claim.py` pins it now. Note which assertion does the
> work: the fencing-pair test passes against the defect too, because the raw UPDATE also produced
> `running`/`b2-eval`/attempts=1. Only `task.claimed` preceding `task.started` in the audit log, and
> the real trace id, actually catch it. **When a fix restores something invisible, assert the
> invisible thing.**

### It is the `encounters` table, not the model — two models, two parsers, same table

`llama3.1:8b` was pulled to get past it. It does not, and **how it fails is the finding**:

| table | qwen2.5:14b-instruct | llama3.1:8b |
|---|---|---|
| `customers` | valid tool call, exact coverage | valid tool call |
| `payments` | valid tool call, exact coverage | (not separately probed) |
| `encounters` | **empty** — no content, no tool call, 240–480 tokens billed | **prose** — the tool call emitted *as text*, `columns` a stringified array |

Two different chat templates and two different tool parsers, failing on the same table and only that
table — and it is the *smallest* of the three (4 columns against 5 and 6), so it is not a length
effect. That points at the table's content, or at how the prompt and the `ProposedColumns` schema
interact on clinical multi-label columns, rather than at one flaky model.

**Do not "fix" this by editing the fixture.** That is step 4 below, and `encounters` is where the
multi-label `phi`+`pii` case and the sparse-evidence hard negative live — the rows most worth
measuring. If the eventual vLLM run also degrades there, that is a product finding about the prompt
and deserves a recorded decision, not a quieter fixture.

### What has now executed against a real model, and what still has not

`score_table` and `evidence_problems` **have** now run on live output, over the two tables that
work (one run, `encounters` skipped — **not a B2 result**, and the numbers below are not quotable
as one):

```
customers  missing=() invented=() evidence_failures=()   5/5 correct
payments   missing=() invented=() evidence_failures=()   5/6 correct
  MISS card_number  expected=[financial, pii]  predicted=[financial]
PII over 11 columns: tp=2 fp=0 fn=1  recall=0.6667 precision=1.0000
```

Worth knowing: **every citation resolved** through the production `evidence_problems`, coverage was
exact by name on both tables, and all six hard negatives were correct. The one miss is the
multi-label `card_number` — the case the fixture was built to catch.

Still never executed against a model: `_report`, `_disagreements` across three runs, the threshold
gate, and `_persist` on real runs. `_persist` is unit-tested; `_report`, `_disagreements` and the
threshold comparison were **not tested at all** until PR #87's guardian pass found it — see below. A gate
whose green path is untested is this repository's signature pathology wearing a new coat.

Branch **`m1/50-b2-and-smoke`** (pushed, `make fitness` green on every commit) carries:

- **The artifact now says what produced it and whether it may be quoted.** `--artifacts` documented a
  default of `evals/artifacts` while defaulting to `None`, so no invocation of the gate had ever
  written one. It writes by default (git-ignored) with the fixture version *read from the file* rather
  than a literal, the model alias, a digest of the gateway config, and the proxy image and model
  revision where pinned. `release_evidence` is **computed and fails closed** on three
  conditions — immutably pinned digests (a moving tag is rejected), `STEWARD_EVALS_REQUIRED=1`,
  and the responding model on record. The third is #89 and is not satisfiable today, so the
  claim is currently refused however the run is configured, and the note names what is missing.

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

   **Exercised against the real proxy**, not only in unit tests:

   | shape | observed | classification |
   |---|---|---|
   | connection refused (closed port) | `ConnectError` | infrastructure, retryable |
   | proxy killed mid-stream | `RemoteProtocolError` | infrastructure, retryable |
   | completed 502/503 response | `CompletionFailed` | **result — fails immediately** |

   The third is deliberate and documented. `LiteLLMProxyTransport` turns any status >= 400 into
   `CompletionFailed`, a `steward-llm` type rather than an `httpx` transport error, so a 5xx from the
   proxy is not retried. That is the conservative direction; making it retryable requires a **typed
   status** on the failure, not reading the code back out of a message — which is the inspection this
   boundary exists to remove.

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

### The retry policy, as characterised against the real proxy

- Connection and streaming transport failures **retry**.
- Completed HTTP failures (any status >= 400) **do not retry**.
- **No message text influences classification** — only failure types found in the `__cause__` chain.
- The conservative direction cannot inflate B2 quality: a misread infrastructure blip fails the
  evaluation, and no quality failure can be retried into a better answer.

One negative result is worth keeping. A first attempt at the 502/503 shape used a *stub* HTTP server
and reported `ReadError` → retryable, which is the **opposite** of what the real transport does: the
stub's non-streaming body broke the stream read, so it exercised a transport path rather than a
completed response. Taking that at face value would have recorded 5xx as already-retryable. Classify
failures from the real transport path; an approximate server fixture answers a different question.

### Then, in order

1. **Get one preflight pass all the way through scoring.** The blocker is `encounters` (above), not the harness — `score_table` and `evidence_problems` are now proven on live output. Reaching `_report`/`_disagreements`/`_persist` needs a model that answers that table; the honest next move is the vLLM run rather than a third local model, since two have now failed the same way.
2. ~~Confirm every artifact is explicitly non-release.~~ **Done, and it now fails closed** —
   `release_evidence` requires immutable digests, `STEWARD_EVALS_REQUIRED=1` **and** the responding
   model on record; the third is #89, so the claim is refused today and the note names every missing
   condition. Landing #89 is what makes a true `release_evidence` reachable at all. Still worth
   reading the first artifact a real run produces: that path has not executed against real runs.
3. **Inspect each independent verdict**, coverage, evidence validity, metrics, retries and
   column-level disagreements. Read the disagreement output specifically: it is the part no single
   run can show, and the reason #50 asks for three.
4. **Resolve harness defects only.** Do **not** tune the prompt or the fixture in response to
   preflight scores without recording a product/evaluation decision first. Changing the expected
   labels until the model agrees with them is how an eval stops measuring anything — and this
   fixture has already had one expectation corrected *on policy grounds* (`test_card_number`), which
   is the legitimate version of that move: the prompt was the authority, not the score.
5. **Run the same gate against pinned LiteLLM → vLLM.** Not an interchangeable backend: its chat
   template, tool parser, streamed frames, usage reporting and model revision are what the release
   evidence validates.
6. **Commit immutable provenance and all three independently passing release verdicts** — pinned
   proxy image digest, vLLM image and model revision, gateway config digest, fixture version, prompt
   version, per-run metrics and per-column disagreement.
7. **Close #50 only then.**

**#84** (authenticate the rest of the API surface) is a release blocker before external exposure, and
is deliberately not a prerequisite for B2. **#85** (a failed checkpoint save replaces the agent
failure that caused it) and **#86** (an empty completion is reported as the model answering in prose)
were both found while running a real classification. **#88** (no release job runs B2 or the live smoke) and **#89** (an artifact cannot say which model actually served a run) came out of PR #87's review. All four are filed separately.

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

**The first two live B2 passes added three, and the first two are about code that had never run:**

> **A check nothing has ever executed is not a check.** The accounting defect in `record_step_usage`
> failed *every* real agent step and sat in `main` unnoticed, because the only things that exercise it
> — B2 and the live gateway smoke — had never been run against a model. Twenty green fitness functions
> did not touch it. When a component's only real exercise is gated behind "no endpoint is reachable",
> the SKIP is not neutral: it is the untested half of the system, and it grows.

> **Two numbers compared at different precisions is a coin flip, not a check.** The unrounded cost from
> the gateway against the six-decimal figure the ledger stored: rounding down reported an overspend,
> rounding up reported none. Right answer roughly half the time, on a *correct* run, decided by the
> seventh decimal place of whatever the model charged. The helper that fixes it (`ledger_cost`) already
> existed on the same branch, for the same reason, applied to a different comparison — so the lesson is
> not "round consistently" but **when you write a rounding helper, find every comparison it governs.**

> **An error message that names a behaviour is a claim, and it can be false.** "stopped without calling
> `submit_result` ... never as prose" asserts prose. There was none — the model returned nothing at all
> while billing 479 tokens. An hour went into looking for a chatty model. When you write the message for
> a failure branch, check it is true of *every* state that reaches it, not the one you had in mind (#86).

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
