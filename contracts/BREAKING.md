# Declared breaking contract changes

S6 (`GUARDRAILS.md`) fails any breaking change to a published contract
unless it is declared here, and only while that break is actually present in
the diff — merge it, and the entry is stale and must be removed in the same
PR that removes the break. A declaration naming contracts that did not break
is rejected the same as no declaration at all.

Ground for pre-v1 breaks: `ARCHITECTURE.md`/`SPEC.md` have not tagged a 1.0;
contracts have no external consumers to protect yet. After 1.0 a break needs
a major version bump instead of an entry here.

| Contract | Ground | Migration | Decision |
|---|---|---|---|
