# Demo

Two things to look at: the platform running, and the leash that keeps it honest. Both run on a laptop with no Docker and no API keys.

```
uv sync --all-packages
make demo              # M0 platform end to end on an ephemeral Postgres
make demo-guardrails   # plant guardrail violations, watch the gate reject them
make fitness           # the whole suite as CI runs it
```

## make demo

Starts an ephemeral Postgres (`pgserver` ships the binaries), migrates it, starts the real API on a real socket, and then drives the system the way a client does — `POST /v1/runs`, wait, `GET /v1/runs/{id}`. Nothing reaches past the API to move the run along; a worker does that. Everything printed is either an HTTP response or a row read back out of the database.

1. **`POST /v1/runs` (I8)** — 202, and the run row and its first task are already committed together. A 202 therefore means work is queued; there is no state where a run exists with nothing to execute it. Replaying the POST with the same `Idempotency-Key` returns the same run and does not enqueue a second task (the same key with a *different* body is a 409, not a silently ignored edit). The response carries the run's `trace_id` — generated locally, so it is on the row whether or not Langfuse credentials are configured (I7) — and its budget (I12).
2. **A worker claims and executes it** — `SELECT … FOR UPDATE SKIP LOCKED`, handler runs, checkpoint written, task `succeeded`, usage recorded. The run's own status follows its tasks in the same transaction, so `GET /v1/runs/{id}` returns `succeeded` — on the same trace id the POST returned.
3. **Audit trail (I7)** — eight rows, each written in the same transaction as the mutation it records: `run.created`, `task.enqueued`, `task.claimed`, `task.started`, `run.status_changed` (→ running), `task.succeeded`, `run.usage_recorded`, `run.status_changed` (→ succeeded).
4. **A second worker finds nothing** — the queue is drained; no double-claim.

That whole path is also the M0 exit criterion (SPEC.md §12) as an executable check — `uv run pytest -q -m acceptance` asserts it, and `make fitness` runs it as H11. What it proves is trace *correlation*: one id, carried from the POST response through every span and audit row, with no credentials involved. Proving Langfuse received a resolvable trace is a separate check (H6) that lands with the M1 agent loop.

## make demo-guardrails

Plants a file in `packages/steward-retrieval` that imports `crewai`, `langgraph`, and `openai`, and builds SQL with an f-string. The gate rejects it with the invariant named in each message:

```
TID251 `crewai` is banned: kitchen-sink framework banned everywhere (I9)
TID251 `langgraph` is banned: contained: steward-agents only (I2/I9)
TID251 `openai` is banned: contained: steward-llm only (I2/I9)
S608 Possible SQL injection vector through string-based query construction
```

Then it moves the same `langgraph` import into `packages/steward-agents`, where that dependency is contained by design, and the suite goes green. Containment is a property of where code lives, checked mechanically — not a code-review habit. The script cleans up after itself.

## make fitness

The whole suite, tiered by how it measures (GUARDRAILS.md §1). Currently active: S1–S5 and S7 (static architecture), H1/H3/H11 (behavioral harnesses — every registered handler run twice converges; crash injection leaves no lost or ghost tasks; M0's exit criterion runs API → queue → worker → `succeeded`), G1–G4 (hygiene). Checks that haven't landed report `SKIP` with a reason rather than a false `PASS`.

The same command runs in the pre-commit hook and in CI.

## Where to read next

- [ARCHITECTURE.md](./ARCHITECTURE.md) — requirements, NFRs, invariants I1–I14, technology decisions with what was rejected
- [GUARDRAILS.md](./GUARDRAILS.md) — every fitness function, and the matrix showing each invariant and NFR has one
- [PROOFS.md](./PROOFS.md) — each claim with the command that reproduces it
- [SPEC.md](./SPEC.md) §13 — the load-bearing design decisions (LangGraph contained rather than adopted or rebuilt; Postgres as queue; hybrid retrieval)
- The [issues and merged PRs](https://github.com/somasays/steward/pulls?q=is%3Apr+is%3Amerged) — every change is issue-driven, guardian-reviewed, and gated
