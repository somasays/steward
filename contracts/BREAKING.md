# Declared breaking contract changes

S6 (`GUARDRAILS.md`) fails any breaking change to a published contract
unless it is declared here, and only while that break is actually present in
the diff. Add the entry in the same PR that makes the break; once that PR
merges, the break is behind the new baseline and the entry goes stale — the
first branch to notice must delete it. A declaration naming contracts that
did not break is rejected the same as no declaration at all.

Ground for pre-v1 breaks: `ARCHITECTURE.md`/`SPEC.md` have not tagged a 1.0;
contracts have no external consumers to protect yet. After 1.0 a break needs
a major version bump instead of an entry here.

| Contract | Ground | Migration | Decision |
|---|---|---|---|
