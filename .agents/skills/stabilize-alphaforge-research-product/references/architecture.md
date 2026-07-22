# Target Architecture and Reuse Boundary

## One product path

```text
User Alpha question
  -> deterministic/LLM-assisted task plan (DRAFT until confirmed)
  -> specialist selection and budgets
  -> Vibe Swarm for isolated branch scheduling
  -> Vibe AgentLoop inside each specialist
  -> wrapped read-only Vibe tools and loaders
  -> frozen raw responses + versioned time semantics
  -> isolated generated-code review and execution
  -> standard output contract
  -> deterministic VeriQuant evaluator and Evidence Court
  -> customer Chinese report and Workbench artifact view
```

The LLM is free within the research box. Financial authority stays outside the box.

## Reuse matrix

| Capability | Preferred source | Treatment | Reason |
|---|---|---|---|
| bounded specialist loop | Vibe `AgentLoop` | wrap/reuse | mature planning, tools, cancellation, trace |
| tool registration | Vibe `ToolRegistry` | strict wrapper | useful but too broad for trusted exposure |
| market and fundamental loaders | Vibe loaders | wrap/reuse | source reach and fallback behavior |
| model providers/session/trace | Vibe | reuse/wrap | avoid duplicate provider stacks |
| cross-group scheduling | Vibe Swarm/DAG | reuse | parallelism and dependency isolation |
| frozen identity and PIT | VeriQuant | retain authority | deterministic financial truth |
| generated-code execution | research sandbox | retain authority | no-network, read-only, resource-limited |
| official metrics/Evidence | VeriQuant | retain authority | replayable and proposer-independent |
| promotion/sealed/risk/ledger | VeriQuant | retain authority | must not be reachable by research agents |

## Tool wrapper contract

Every agent-facing Vibe tool is wrapped with stable JSON schema, specialist/campaign capability checks, argument bounds, timeout/retry/rate limits, secret redaction, raw-response freezing, source provenance, versioned time policy, authority classification, and a read-only data handle instead of an arbitrary path.

Fallback data sources must be recorded. A fallback must never silently inherit the provenance or authority of the requested provider.

## Research result classes

| Result | Meaning |
|---|---|
| `EVALUATED_RESEARCH_RESULT` | deterministic evaluation completed |
| `NEGATIVE_RESEARCH_RESULT` | evaluation completed and rejected/needs revision |
| `PARTIAL_RESEARCH_RESULT` | useful work exists, but an official gate is unavailable |
| `DATA_OR_CAPABILITY_GAP_REPORT` | missing source, timing policy, permission, or evaluator is explicit |
| `SYSTEM_FAILED` | infrastructure failed before a legitimate research outcome |

These classes guarantee a useful answer without guaranteeing a profitable Alpha.

