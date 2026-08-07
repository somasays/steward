# Demo

Two things to look at: the platform running, and the leash that keeps it honest. Both run on a laptop with no Docker and no API keys.

```
uv sync --all-packages
make demo              # M0 platform end to end on an ephemeral Postgres
make demo-guardrails   # plant guardrail violations, watch the gate reject them
make fitness           # the whole suite as CI runs it
```

## make demo

Starts an ephemeral Postgres (`pgserver` ships the binaries), migrates it, then walks the guarantees the architecture is built around. Everything printed is read back out of the database.

1. **Transactional enqueue (I8)** — the run row and its task commit in one transaction; until it commits no worker can see either. Replaying the same enqueue returns the same task id, because the dedup key is derived from `(task_type, payload)`.
2. **Worker claims and executes** — `SELECT … FOR UPDATE SKIP LOCKED`, handler runs, checkpoint written, task `succeeded`, usage recorded against the run's budget.
3. **Audit trail (I7)** — six rows, each written in the same transaction as the mutation it records: `run.created`, `task.enqueued`, `task.claimed`, `task.started`, `task.succeeded`, `run.usage_recorded`.
4. **A second worker finds nothing** — the queue is drained; no double-claim.

The run stays `pending` at the end: rolling task outcomes up to run status is issue #5, not merged yet. The demo says so rather than hiding it.

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

The whole suite, tiered by how it measures (GUARDRAILS.md §1). Currently active: S1–S5 and S7 (static architecture), H1/H3 (behavioral harnesses — every registered handler run twice converges; crash injection leaves no lost or ghost tasks), G1–G4 (hygiene). Checks that haven't landed report `SKIP` with a reason rather than a false `PASS`.

The same command runs in the pre-commit hook and in CI.

## Where to read next

- [ARCHITECTURE.md](./ARCHITECTURE.md) — requirements, NFRs, invariants I1–I14, technology decisions with what was rejected
- [GUARDRAILS.md](./GUARDRAILS.md) — every fitness function, and the matrix showing each invariant and NFR has one
- [PROOFS.md](./PROOFS.md) — each claim with the command that reproduces it
- [SPEC.md](./SPEC.md) §13 — the load-bearing design decisions (LangGraph contained rather than adopted or rebuilt; Postgres as queue; hybrid retrieval)
- The [issues and merged PRs](https://github.com/somasays/steward/pulls?q=is%3Apr+is%3Amerged) — every change is issue-driven, guardian-reviewed, and gated
