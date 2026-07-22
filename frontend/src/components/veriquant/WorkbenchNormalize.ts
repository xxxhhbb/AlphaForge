import type {
  WorkbenchRunDetail,
  WorkbenchRunSummary,
  WorkbenchRunsResponse,
  WorkbenchStatus,
} from "./WorkbenchTypes";

type JsonRecord = Record<string, unknown>;
const KNOWN_STATUSES: readonly WorkbenchStatus[] = [
  "DRAFT",
  "VALIDATED_RESEARCH",
  "CHALLENGER",
  "SHADOW_ACTIVE",
  "REJECTED",
  "INCOMPLETE",
  "BLOCKED",
];

function record(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function status(value: unknown): WorkbenchStatus {
  const candidate = typeof value === "string" ? value.toUpperCase() : "";
  return KNOWN_STATUSES.includes(candidate as WorkbenchStatus)
    ? candidate as WorkbenchStatus
    : "INCOMPLETE";
}

function normalizeResearchDag(value: unknown): JsonRecord {
  const dag = record(value);
  if (Array.isArray(dag.nodes)) return dag;
  const selectedGroups = array(dag.selected_groups)
    .filter((item): item is string => typeof item === "string");
  if (selectedGroups.length) {
    return {
      ...dag,
      nodes: selectedGroups.map((groupId) => ({
        id: groupId,
        role: "专业研究小组",
        status: "VALIDATED_RESEARCH",
      })),
    };
  }
  return {
    ...dag,
    nodes: Object.entries(dag).map(([id, nodeStatus]) => ({
      id: `desk:${id}`,
      role: id,
      status: status(nodeStatus),
    })),
  };
}

function normalizeLineage(value: unknown): JsonRecord {
  const lineage = record(value);
  if (Array.isArray(lineage.candidates) || Array.isArray(lineage.items)) return lineage;

  const selected = typeof lineage.selected_desk === "string" ? lineage.selected_desk : "";
  const completed = array(lineage.completed_desks).filter((item): item is string => typeof item === "string");
  const incomplete = array(lineage.failed_or_incomplete_desks).filter((item): item is string => typeof item === "string");
  const desks = [...new Set([...completed, ...incomplete])];

  return {
    ...lineage,
    items: desks.map((desk) => ({
      candidate_id: `desk:${desk}`,
      family_id: desk === selected ? "selected research desk" : "research desk branch",
      status: incomplete.includes(desk) ? "INCOMPLETE" : "DRAFT",
      operator: "fixed five-stage research desk",
      factor_dag_hash: desk === selected ? lineage.factor_dag_hash : undefined,
    })),
  };
}

function normalizeEvidence(value: unknown): JsonRecord {
  const evidence = record(value);
  if (Array.isArray(evidence.items) || Array.isArray(evidence.metrics)) return evidence;

  const items: JsonRecord[] = [];
  for (const candidate of array(evidence.candidates)) {
    const row = record(candidate);
    const metrics = record(row.metrics);
    if (Object.keys(metrics).length === 0) {
      items.push({
        evidence_id: row.candidate_id,
        metric: row.group,
        value: row.status,
        summary: row.summary_hash,
      });
      continue;
    }
    for (const [metric, metricValue] of Object.entries(metrics)) {
      items.push({
        evidence_id: `${String(row.candidate_id ?? "candidate")}.${metric}`,
        metric,
        value: metricValue,
        scope: "VISIBLE_RESEARCH",
      });
    }
  }
  for (const partition of ["train", "validation"] as const) {
    const partitionEvidence = record(evidence[partition]);
    const metrics = record(partitionEvidence.metrics);
    for (const [metric, metricValue] of Object.entries(metrics)) {
      if (metric === "contract_type" || metric === "schema_version") continue;
      items.push({
        evidence_id: `${partition}.${metric}`,
        metric,
        value: metricValue,
        scope: partition.toUpperCase(),
      });
    }
  }
  return { ...evidence, items };
}

function normalizeCritics(value: unknown): JsonRecord {
  const critics = record(value);
  const findings = [...array(critics.findings), ...array(critics.items)];
  const robustness = record(critics.statistical_robustness);
  for (const finding of array(robustness.findings)) {
    const row = record(finding);
    findings.push({
      critic: `StatisticalRobustness:${String(row.check_name ?? "check")}`,
      verdict: row.passed === true ? "PASS" : "FAIL",
      message: row.message,
      failure_code: row.code,
    });
  }
  for (const [desk, failureCode] of Object.entries(record(critics.branch_failures))) {
    findings.push({
      critic: `ResearchDesk:${desk}`,
      verdict: "INCOMPLETE",
      failure_code: failureCode,
      message: "Branch failed closed; no result was fabricated.",
    });
  }
  return { ...critics, findings };
}

function normalizePromotion(value: unknown): JsonRecord {
  const promotion = record(value);
  const decision = record(promotion.decision);
  const audit = record(promotion.audit);
  return {
    ...promotion,
    verdict: promotion.verdict ?? decision.verdict,
    status: promotion.status ?? decision.to_state ?? audit.status,
    failure_code: promotion.failure_code ?? decision.failure_code ?? audit.failure_code,
    sealed_status: promotion.sealed_status ?? "redacted",
  };
}

function normalizeCitations(value: unknown): JsonRecord {
  const citations = record(value);
  if (Array.isArray(citations.items) || Array.isArray(citations.citations)) return citations;
  const hypothesis = record(citations.hypothesis);
  return { ...citations, items: array(hypothesis.citations) };
}

function normalizeShadowPortfolio(value: unknown): JsonRecord {
  const shadow = record(value);
  if (Array.isArray(shadow.vintages) || Array.isArray(shadow.items)) return shadow;
  if (Object.keys(shadow).length === 0) return shadow;
  return {
    ...shadow,
    items: [{
      vintage_id: shadow.vintage_id ?? shadow.run_id,
      date: shadow.report_time,
      status: shadow.status,
      vintage_hash: shadow.vintage_hash,
      gross_exposure: shadow.gross_exposure,
      net_exposure: shadow.net_exposure,
    }],
  };
}

const NAV_METRICS = [
  "nav_start",
  "nav_end",
  "gross_pnl",
  "net_pnl",
  "return_pct",
  "transaction_cost",
  "slippage_cost",
  "turnover",
  "gross_exposure",
  "beta_exposure",
  "data_coverage",
] as const;

function normalizeNavAttribution(value: unknown): JsonRecord {
  const nav = record(value);
  if (Array.isArray(nav.attribution) || Array.isArray(nav.items)) return nav;
  const attribution: JsonRecord[] = [];
  for (const label of NAV_METRICS) {
    if (Object.prototype.hasOwnProperty.call(nav, label)) {
      attribution.push({ label, value: nav[label] });
    }
  }
  for (const [label, contribution] of Object.entries(record(nav.challenger_contribution))) {
    attribution.push({ label: `challenger:${label}`, value: contribution });
  }
  for (const [label, exposure] of Object.entries(record(nav.industry_exposure))) {
    attribution.push({ label: `industry:${label}`, value: exposure });
  }
  return { ...nav, attribution };
}

export function normalizeWorkbenchRuns(payload: WorkbenchRunsResponse): WorkbenchRunSummary[] {
  return array(payload.runs)
    .map((value) => record(value))
    .filter((run) => typeof run.run_id === "string" && run.run_id.length > 0)
    .map((run) => ({
      ...run,
      run_id: String(run.run_id),
      status: status(run.status),
    } as WorkbenchRunSummary));
}

export function normalizeWorkbenchDetail(value: unknown): WorkbenchRunDetail {
  const detail = record(value);
  const run = record(detail.run);
  return {
    ...detail,
    run,
    report: record(detail.report),
    research_dag: normalizeResearchDag(detail.research_dag ?? run.research_dag),
    lineage: normalizeLineage(detail.lineage),
    evidence: normalizeEvidence(detail.evidence),
    critics: normalizeCritics(detail.critics),
    promotion: normalizePromotion(detail.promotion),
    citations: normalizeCitations(detail.citations),
    shadow_portfolio: normalizeShadowPortfolio(detail.shadow_portfolio),
    nav_attribution: normalizeNavAttribution(detail.nav_attribution),
    drift_alerts: record(detail.drift_alerts),
  };
}
