"""steward-workers: the processes that execute tasks.

SPEC.md §2 gives each agent type its own Deployment, so this service is a thin
composition root per worker type: read configuration, build a `Worker` from
`steward_queue`, run it until told to stop. All queue behaviour -- claiming,
leases, retries, budgets, audit, tracing -- belongs to the package; nothing
here is allowed to grow an opinion about it (GUARDRAILS.md §4: "retry/timeout/
budget logic duplicated in an agent instead of the runtime").

M0 has one worker, running whatever handlers the registry holds.
"""
