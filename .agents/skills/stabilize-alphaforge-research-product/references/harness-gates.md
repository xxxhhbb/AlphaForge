# Harness Gates

## State model

Track execution, authority, verdict, and report separately:

```text
execution_status: NOT_RUN | PREFLIGHT_BLOCKED | RUNNING | PASSED | FAILED
evidence_authority: NONE | AUXILIARY_ONLY | OFFICIAL_EVIDENCE
research_verdict: SUPPORTED | CONDITIONALLY_SUPPORTED | REJECTED |
                  INSUFFICIENT_EVIDENCE | DATA_UNAVAILABLE
report_status: NOT_BUILT | BUILT | QUALIFIED | REJECTED
```

Also record failure stage/code, provider attempts, draft revisions, platform retries, trial id, idempotency key, lease, hashes, and recovery cursor.

## Full-catalog static gates

Every row parses; IDs and catalog hash are stable; each task compiles to goal, lead specialist, supporting roles, required data, output contract, budget, and report contract; horizons and subjects are preserved; unavailable data becomes a typed preflight outcome.

Static coverage proves reachability only, not execution or quality.

## Representative suite

Choose the smallest deterministic suite covering all research modes, all six specialist groups, all standard outputs, major data families, official/auxiliary/negative/data-gap/security outcomes, a cross-group conflict, replay, and resume. Prefer roughly 25–40 balanced cases over 500 near-duplicates.

## Data gates

For each field record frozen handle/root and hash, non-null/entity/time coverage, semantic mapping version, event/publication/availability/decision-time rules, revision lineage, universe/calendar policy, and derived-field lineage. A present field with zero usable coverage is unavailable.

## Evaluator gates

Each output contract has a real positive canary, negative fixture, empty/insufficient/NaN/zero-variance/mode-specific diagnostics, canonical Evidence hash, explicit authority, and deterministic replay. Official research evidence does not automatically imply promotion eligibility.

## Specialist qualification

Each specialist needs a native-data/native-method task, a robustness or specialized-output task, and a fail-closed safety/data-gap task. Qualification is an audit result, not a prerequisite for an unrelated user to receive a useful partial report.

## Security gates

Generated code cannot use network, shell, subprocess, dynamic imports, environment variables, arbitrary paths, broker, order, or ledger APIs. Tools are read-only and scoped. Sealed data and secrets never enter prompts, traces, outputs, reports, or feedback. The sandbox is pinned. Official evaluation independently replays standard tables. The proposer cannot approve itself.

## Final verdict

Use `QUALIFIED` only when catalog planning, representative execution, evaluators, specialist qualifications, reports, replay, and security pass. Otherwise use `PARTIAL`, `BLOCKED`, or `FAILED` with exact counts and recovery cursor. Never present auxiliary evidence as official.

