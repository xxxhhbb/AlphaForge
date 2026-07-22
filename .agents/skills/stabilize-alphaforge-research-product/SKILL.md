---
name: stabilize-alphaforge-research-product
description: Stabilize and converge AlphaForge or similar quantitative-research agent products. Use when a system has many partially connected runtimes, data tools, research agents, evidence paths, reports, or UI states; when representative Alpha questions fail despite many passing package tests; when Vibe-Trading AgentLoop, ToolRegistry, loaders, providers, or Swarm should be reused without granting them official financial authority; or when a recoverable harness must turn arbitrary research questions into honest, reproducible results.
---

# Stabilize AlphaForge Research Product

## Objective

Converge the product to one traceable path from a user question to a useful report. Give research agents broad freedom to plan, investigate, choose proxies, write experiments, critique, and revise. Enforce safety and financial authority mechanically at a small number of boundaries.

Never equate “a report was generated” with “an Alpha was validated.” A legitimate run ends as one of:

- evaluated research result;
- negative research result;
- partial research result;
- data or capability gap report;
- system failure only for genuine infrastructure failure.

## Read Before Acting

1. Read the applicable `AGENTS.md` and contributor guide.
2. Read the build constitution, current state, active harness receipts, and product architecture documents.
3. Inspect actual code and tests with `rg`/`rg --files`; do not accept README claims as implementation evidence.
4. Check current modifications and preserve unrelated work. If Git metadata is unavailable, inventory files and hashes instead.
5. Read [architecture.md](references/architecture.md), [correction-patterns.md](references/correction-patterns.md), and [harness-gates.md](references/harness-gates.md).

## Authority Boundary

Allow the research agent to own:

- research planning and decomposition;
- read-only investigation through approved tools;
- proxy-variable selection with explicit disclosure;
- multiple experiment drafts;
- generated research code inside its isolated workspace;
- self-critique, budgeted revisions, and prose explanation.

Do not allow the research agent to own:

- official metrics or Evidence authority;
- PIT acceptance;
- promotion or sealed-test access;
- portfolio, risk, order, execution, or ledger decisions;
- unrestricted filesystem, environment, network, shell, broker, or secret access.

Generated code may run only under the repository's approved research-sandbox policy: mechanical review, pinned no-network isolation, frozen read-only data handles, resource limits, standard output contracts, and independent deterministic evaluation. If the active constitution does not authorize this path, stop and request an explicit amendment rather than bypassing it.

## Reuse Before Rebuild

Prefer these Vibe-Trading capabilities:

- `AgentLoop` for bounded plan → investigate → draft → critique → revise work;
- filtered `ToolRegistry` for approved read-only research tools;
- market/fundamental loaders and data-source fallbacks for discovery;
- provider/session/trace for model calls and observability;
- Swarm/DAG runtime only for cross-specialist scheduling and dependency isolation.

Wrap rather than expose them directly. The wrapper must validate arguments, redact secrets, freeze returned data, record provenance and hashes, classify auxiliary versus official inputs, and prevent dangerous tools from entering the research context.

Keep VeriQuant deterministic services authoritative for frozen data identity, bitemporal/PIT semantics, Evidence, statistical decisions, promotion, risk, execution, and ledger truth.

## Product Convergence Workflow

### 1. Reconstruct the one true path

Trace the actual call graph:

`question → task plan → selected specialists → AgentLoop → tools/data → review gateway → isolated runner → standard output → deterministic evaluator → court/promotion boundary → report`

For every stage record the concrete file, symbol, state store, artifact root, and test. Mark alternatives as trusted, compatibility-only, audit-only, or disabled. Do not add another product runtime to fix a routing problem.

### 2. Establish a mechanical baseline

Run `scripts/audit_product_coverage.py` against the current workspace. Persist its canonical JSON output atomically under the active harness directory. Treat it as a diagnosis, not as proof of research quality.

The audit must distinguish static catalog coverage, task planning, lead-specialist routing, output-contract selection, data readiness, evaluator authority, specialist qualification, and representative real execution.

### 3. Fix shared blockers, not individual questions

Classify every failure into one primary cluster:

- product-path or state-source drift;
- task-planning/routing error;
- data discovery/freeze/PIT projection gap;
- research-agent runtime or provider failure;
- review/sandbox/output-contract failure;
- deterministic evaluator gap;
- Evidence/report traceability failure;
- UI/API presentation drift.

Choose the smallest shared fix that improves a whole family. Do not create question-specific `if/else` patches or fabricate data to make a test pass.

### 4. Preserve agent freedom

Programmatically require only the minimum investigation needed for the task. Then allow the agent to request more approved tools, select documented proxies, produce multiple methods, and revise after review.

Do not block the agent merely because a prebuilt panel lacks a field. Let it discover an approved source. A controller—not the LLM—must freeze the response, apply versioned time semantics, and decide whether it qualifies for official evaluation or only auxiliary research.

Do not require all six specialists for every question. Route one to six based on the task; every selected specialist must have an independent workspace, tool view, budget, and artifact namespace.

### 5. Evaluate by output contract

Support at least ScoreTable, RegressionTable, RegimeComparisonTable, EventTable, DiagnosticTable, and DatasetAssessmentTable. The task planner chooses the contract from research intent; the agent chooses the method.

Each deterministic evaluator must return either content-addressed evidence or a typed, non-empty diagnostic. Empty tables, NaN-only data, insufficient samples, zero variance, collinearity, missing event timing, or invalid PIT semantics must never become silent success.

### 6. Produce a customer report for every legitimate outcome

Reports lead with the answer, not hashes. Include the machine conclusion, evidence level, tested scope, data range and limits, hypotheses and variants, supporting and opposing evidence, costs/stability/incrementality or mode-appropriate diagnostics, explicit proxy caveats, a question-specific next experiment, and reproducibility links.

Every number must come from deterministic evidence. An LLM may explain but may not invent or alter metrics.

### 7. Run the harness loop

Use small, family-balanced batches. For each batch:

1. freeze case identity and inputs;
2. preflight actual consumable data, not only schema names;
3. run the real research path;
4. persist source, review, runner, output, evidence, and report receipts;
5. replay from frozen inputs and compare hashes;
6. cluster failures by typed code;
7. repair one shared blocker, up to three targeted rounds;
8. rerun the same cases plus adjacent regressions;
9. atomically update progress and next recovery cursor.

Pause expansion when the same blocker affects two cases in one capability or crosses two specialist groups. Fix the common path first.

## Stop Conditions

Do not claim completion until the active acceptance profile passes. For a broad-question product this normally requires:

- the question catalog is parseable and routable;
- representative families cover all research modes and output contracts;
- six specialist groups have real native-method evidence or an explicit blocked status;
- every supported standard output has positive and negative evaluator tests;
- all reports have evidence-bound numbers;
- replay hashes match;
- security, PIT, sealed, secret, path, arbitrary-execution, and live-broker scans pass;
- zero unknown states and zero auxiliary evidence presented as official.

Do not require a real run of every catalog question when a deterministic taxonomy audit plus a smaller balanced real suite gives stronger coverage. Do not call catalog coverage “solved” if representative execution, data, evaluator, or report gates are still missing.

## Deliverables

Always leave a machine-readable baseline and verdict, atomic progress/events, blocker clusters and recovery cursor, exact commands and test counts, architecture/reuse evidence, trusted/compatibility/audit/disabled path classification, and honest remaining gaps.

