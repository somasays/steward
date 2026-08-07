# Steward — Product Value

Steward continuously maintains an evidence-backed understanding of an organization's data estate. It reduces the recurring work required to discover, profile, classify, document, govern, and monitor data without moving or mutating customer data.

Steward is a data management system first. Natural-language access may become an interface to its catalog, but it is not the core product and does not define the near-term roadmap.

## The problem

Data estates change faster than teams can steward them manually:

- catalogs become incomplete or stale;
- sensitive data is discovered late or classified inconsistently;
- documentation depends on institutional knowledge and decays quickly;
- quality rules and incidents are managed reactively;
- governance decisions lack consistent evidence and audit history;
- specialists repeatedly perform work that is structured, judgment-heavy, and verifiable.

The result is slower delivery, avoidable compliance risk, unreliable data products, and excessive dependence on a small number of people who understand the estate.

## The outcome

Point Steward at approved read-only data sources and it builds and maintains a governed record of the estate:

```text
sources
  -> catalog discovery
  -> privacy-safe profiling
  -> evidence-backed classification
  -> maintained documentation
  -> quality and drift monitoring
  -> policy or human review
```

Teams gain a catalog that reflects the current estate, a history of how it changed, and an auditable explanation for every agent-produced classification, document, recommendation, and governance action.

## Value by user

### Data platform and engineering teams

- Reduce manual catalog and documentation maintenance.
- Detect schema and distribution changes before downstream failures spread.
- Standardize stewardship across databases and teams.
- Onboard engineers faster to unfamiliar data.
- Turn repeated operational work into durable, measurable workflows.

### Governance, privacy, and security teams

- Continuously discover PII, PHI, financial, and other governed data.
- Review classifications with confidence scores and resolvable evidence.
- Keep consequential actions behind explicit policy or human approval.
- Reconstruct what was known, proposed, approved, or rejected at any point in time.
- Keep source access read-only and raw sensitive values out of model prompts and traces.

### Data owners and domain experts

- Receive focused review requests instead of maintaining the whole catalog manually.
- Correct agent proposals and turn rejections into evaluation data.
- Preserve domain knowledge as versioned documentation and policies.
- See the source evidence behind every proposed description or label.

### Engineering leadership

- Reduce dependence on undocumented institutional knowledge.
- Measure automation quality, cost, latency, and review load.
- Operate bounded agents rather than opaque autonomous processes.
- Run production inference through LiteLLM and approved self-hosted vLLM endpoints.

## Why agents belong here

Steward does not make every operation agentic.

Deterministic workflows own discovery, SQL execution, sampling limits, masking, persistence, budgets, and policy enforcement. Specialized agents are introduced where interpretation is valuable: classifying ambiguous fields, grounding documentation in evidence, proposing quality rules, and triaging incidents.

An agent is justified only when it has:

- a specific data-management responsibility;
- typed inputs, outputs, and permitted tools;
- hard step, token, cost, and wall-clock limits;
- evidence requirements for its conclusions;
- independently measurable quality criteria;
- an auditable handoff to policy, a person, or another capability.

This division keeps routine operations predictable while applying models to the judgment-heavy work they can improve.

## Product guarantees

Steward's value depends on trust. The system therefore aims to make these properties structural:

- **Read-only sources:** Steward observes customer systems and never mutates their data.
- **Privacy before inference:** raw sensitive samples cannot reach a model, log, or trace.
- **Evidence before publication:** classifications and documentation link to the facts supporting them.
- **Governance before consequence:** consequential changes require an explicit approval policy or human review.
- **Bounded autonomy:** every run has enforced resource and time limits.
- **Recoverable execution:** retries and crashes do not create duplicate effects or lose tasks.
- **Historical accountability:** profiles, classifications, documentation, decisions, and audit records are versioned.
- **Controlled inference:** model access passes through owned contracts, LiteLLM aliases, and approved vLLM deployments.

## How value is proven

Progress is measured by operational outcomes rather than by the number of agents or infrastructure components.

| Capability | Proof of value |
|---|---|
| Catalog | Registered sources converge to an accurate inventory; rescans preserve identity and expose missing assets |
| Profiling | Useful statistics and masked exemplars are produced without leaking planted canaries |
| Classification | PII recall >= 0.95 and precision >= 0.90; every classification carries evidence |
| Documentation | No unsupported claims; unchanged assets converge to unchanged documentation |
| Governance | No governed result publishes without a recorded policy or human decision |
| Quality | Drift and failed rules produce actionable, deduplicated incidents with measurable detection time |
| Operations | Runs remain within budgets, recover after interruption, and are traceable end to end |

The first complete value flow is:

```text
register source
  -> discover catalog
  -> produce privacy-safe profiles
  -> propose sensitivity classifications
  -> review and approve
  -> retain the evidence and history
```

That flow proves Steward as a data-management product. Search and conversational access can build on the resulting trusted corpus later.

## Product promise

> Steward keeps an organization's data estate cataloged, understood, classified, documented, governed, and monitored—with evidence—while reducing the recurring manual work required from data teams.

