import type {
  CandidateDecisionView,
  CandidateSelectionOutcome,
  CandidatePreflightOutcome,
  CandidatePreflightView,
  FormalResearchReportView,
  MachineValidationView,
  RawCandidateRef,
  RawResearchBranch,
  RawResearchOSV3Envelope,
  RawResearchRunResult,
  ResearchGroupOutcome,
  ResearchGroupResultView,
  ResearchOSV3RunView,
  ResearchRouteStepView,
  ResearchRunOutcome,
} from "./types";

const groupLabels: Record<string, { title: string; purpose: string }> = {
  fundamental: { title: "基本面研究组", purpose: "研究企业质量、估值、盈利变化与财务可得性。" },
  expectations_events: { title: "预期与事件研究组", purpose: "研究预期差、公告事件和信息进入市场后的反应。" },
  market_structure: { title: "市场行为与交易可行性研究组", purpose: "研究价格行为、成交结构、流动性与交易成本。" },
  alternative_data: { title: "另类数据研究组", purpose: "研究已批准外部资料的覆盖、时点语义与增量价值。" },
  macro_regime: { title: "宏观状态研究组", purpose: "研究宏观环境、市场状态和条件化有效性。" },
  meta_alpha: { title: "组合增量研究组", purpose: "研究候选与既有因子的相关性、冗余和增量贡献。" },
};

const branchOutcomes: Record<string, ResearchGroupOutcome> = {
  COMPLETED_DRAFT: "草案已提交",
  NEGATIVE_RESULT: "负向结果",
  DATA_GAP: "数据缺口",
  BUDGET_BLOCKED: "预算已用尽",
  PARTIAL_RESULT: "部分完成",
  FAILED: "系统异常",
  CANCELLED: "已取消",
};

const runOutcomes: Record<string, ResearchRunOutcome> = {
  COMPLETED: "已完成",
  PARTIAL: "部分完成",
  BLOCKED: "已阻断",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const preflightOutcomes: Record<string, CandidatePreflightOutcome> = {
  ACCEPTED: "预检通过",
  REVISE: "需要修订",
  CAPABILITY_REQUEST: "能力缺口",
  NOT_EVALUATED: "尚未预检",
};

const findingLabels: Record<string, string> = {
  INVALID_STATE: "本轮修订未形成可继续提交的草案。",
  NO_VALID_REVISED_CANDIDATE: "自我质疑后没有保留可提交的修订候选。",
  INVALID_FACTOR_DAG: "候选暂时不能安全转换为受限因子表达式。",
  REQUESTED_LOOKBACK_NOT_REPRESENTED: "候选没有完整表达用户要求的观察窗口。",
  NOT_EVALUATED: "候选尚未完成安全预检。",
  PROVIDER_TIMEOUT: "研究模型在本组预算时间内未返回。",
};

const capabilityLabels: Record<string, string> = {
  documents: "带可得时间的研究资料",
  entity_mapping: "资料与证券的实体映射",
  forecast: "带发布时间的业绩预期数据",
  express: "带发布时间的业绩快报数据",
};

const sourceLabels: Record<string, string> = {
  APPROVED_PROXY_DATA_VENDOR: "已批准代理数据源",
  DETERMINISTIC_OFFLINE_FIXTURE: "确定性离线数据",
};

const providerLabels: Record<string, string> = {
  configured_openai_compatible: "已配置的受限研究服务",
  deterministic: "确定性研究服务",
};

const selectionLabels: Record<string, CandidateSelectionOutcome> = {
  SELECTED_FOR_PROMOTION_REVIEW: "进入独立评审短名单",
  NOT_ELIGIBLE: "不符合选择条件",
  REJECTED: "不符合选择条件",
  REVISE: "需要修订",
  NOT_EVALUATED: "尚未选择",
};

const selectionReasonLabels: Record<string, string> = {
  PIT_NOT_VALIDATED: "尚未通过时点一致性检查",
  OFFICIAL_EVIDENCE_MISSING: "缺少官方可见机器证据",
  CRITIC_NOT_PASSED: "独立机器审查未通过",
  VISIBLE_SELECTION_METRICS_MISSING: "缺少可用于比较的公开指标",
  FAMILY_BUDGET_EXHAUSTED: "候选家族预算已用尽",
  NET_ICIR_BELOW_POLICY: "成本后信息比率低于研究政策",
  STABILITY_BELOW_POLICY: "跨时期或市场状态稳定性不足",
  COST_CAPACITY_BELOW_POLICY: "成本或容量条件不足",
  NOVELTY_BELOW_POLICY: "与已有研究过于相似",
  INCREMENTALITY_BELOW_POLICY: "对已有因子的增量贡献不足",
  CANDIDATE_SELECTION_BUDGET_LIMIT: "短名单名额已用尽",
  PIT_EVIDENCE_CRITIC_VISIBLE_SELECTION_PASS: "通过时点、机器证据与独立审查的可见选择条件",
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && Boolean(item.trim()));
}

function nonNegativeInteger(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function normalizeCandidate(value: unknown): CandidatePreflightView {
  const raw = record(value) as RawCandidateRef;
  const rawStatus = stringValue(raw.preflight_status, "NOT_EVALUATED");
  const outcome = preflightOutcomes[rawStatus] ?? "尚未预检";
  const failureCode = optionalString(raw.failure_code);
  const explanation = failureCode
    ? findingLabels[failureCode] ?? "候选未通过当前安全能力边界，原因已记录。"
    : outcome === "预检通过"
      ? "受限因子表达式已通过结构与能力预检，尚未计算官方机器证据。"
      : "候选尚未进入完整机器验证。";
  return {
    candidateId: stringValue(raw.candidate_id, "未命名候选"),
    candidateHash: stringValue(raw.candidate_hash, "未生成候选标识"),
    outcome,
    explanation,
    factorDagHash: outcome === "预检通过"
      ? optionalString(raw.formula_program_hash) ?? optionalString(raw.factor_dag_hash)
      : undefined,
  };
}

function normalizeBranch(value: unknown): ResearchGroupResultView {
  const raw = record(value) as RawResearchBranch;
  const groupId = stringValue(raw.specialist_id, "unknown_specialist");
  const copy = groupLabels[groupId] ?? { title: "未识别的专业研究组", purpose: "该研究组尚未登记中文能力说明。" };
  const typedFailures = stringArray(raw.typed_failures);
  return {
    groupId,
    ...copy,
    outcome: branchOutcomes[stringValue(raw.status, "FAILED")] ?? "系统异常",
    providerCalls: nonNegativeInteger(raw.provider_calls),
    candidates: Array.isArray(raw.candidate_refs) ? raw.candidate_refs.map(normalizeCandidate) : [],
    missingCapabilities: stringArray(raw.missing_capabilities).map((item) => capabilityLabels[item] ?? "所需研究数据或工具尚未就绪"),
    findings: typedFailures.map((failure) => findingLabels[failure] ?? "研究过程记录了一个待处理问题。"),
    resultHash: optionalString(raw.outcome_hash),
  };
}

function machineValidation(raw: RawResearchRunResult, runStatus: string): MachineValidationView {
  const evidenceHash = optionalString(raw.official_evidence_hash);
  const promotionHash = optionalString(raw.promotion_hash);
  const evidenceBlocked = !evidenceHash && ["BLOCKED", "FAILED", "CANCELLED"].includes(runStatus);
  return {
    evidence: evidenceHash ? "已生成" : evidenceBlocked ? "已阻断" : "尚未执行",
    evidenceHash,
    promotion: promotionHash ? "已完成" : "未申请",
    publicMessage: evidenceHash
      ? "官方机器证据已由确定性服务生成。"
      : evidenceBlocked
        ? "研究未能进入正式机器评价，已保留负向结果或缺口说明。"
      : "当前结果止于研究草案和安全预检，尚未计算官方指标。",
    sealedMessage: "封闭评审内容不可见；这里只显示是否已进入独立评审，不展示任何封闭指标、排名或原因。",
  };
}

function route(validation: MachineValidationView, groupCount: number): ResearchRouteStepView[] {
  const evidenceDone = validation.evidence === "已生成";
  const evidenceBlocked = validation.evidence === "已阻断";
  return [
    { id: "question", title: "研究问题与边界", description: "研究范围、数据和预算已冻结。", state: "已完成" },
    { id: "groups", title: "专业研究组", description: `${groupCount} 个被选中的专业研究组独立形成候选。`, state: "已完成" },
    { id: "preflight", title: "候选安全预检", description: "候选逐一接受 Qlib 表达式静态安全与能力预检。", state: "已完成" },
    { id: "evidence", title: "机器验证", description: evidenceDone ? "确定性机器证据已生成。" : evidenceBlocked ? "研究未能进入正式机器评价。" : "等待冻结输入上的正式机器评价。", state: evidenceDone ? "已完成" : evidenceBlocked ? "已阻断" : "当前阶段" },
    { id: "review", title: "独立评审", description: "通过研究验证的候选才可申请独立评审。", state: evidenceBlocked ? "已阻断" : "等待中" },
  ];
}

function normalizeDecisionCandidate(value: unknown, selected: Set<string>, best?: string): CandidateDecisionView | undefined {
  const raw = record(value);
  const candidateHash = optionalString(raw.candidate_hash);
  const candidateId = optionalString(raw.candidate_id);
  if (!candidateHash || !candidateId) return undefined;
  const machineStatus = stringValue(raw.machine_evidence_status, "NOT_RUN");
  const officialMetrics = machineStatus === "OFFICIAL_VISIBLE_EVALUATED" && Array.isArray(raw.official_metrics)
    ? raw.official_metrics.flatMap((metric) => {
      const item = record(metric);
      const name = optionalString(item.name);
      const metricValue = finiteNumber(item.value);
      return name && metricValue !== undefined ? [{ name, value: metricValue }] : [];
    })
    : [];
  const status = stringValue(raw.preflight_status, "NOT_EVALUATED");
  return {
    candidateId,
    candidateHash,
    groupLabel: stringValue(raw.group_label, "专业研究组"),
    hypothesis: stringValue(raw.hypothesis, "未提供候选假设。"),
    economicMechanism: stringValue(raw.economic_mechanism, "未提供经济机制说明。"),
    expression: stringValue(raw.expression, "未发布受限因子表达式。"),
    preflight: preflightOutcomes[status] ?? "尚未预检",
    machineEvidence: machineStatus === "OFFICIAL_VISIBLE_EVALUATED" ? "已生成" : machineStatus === "BLOCKED" ? "已阻断" : "尚未执行",
    metrics: officialMetrics,
    verdict: selectionLabels[stringValue(raw.selection_verdict, "NOT_EVALUATED")] ?? "尚未选择",
    score: finiteNumber(raw.selection_score),
    reasons: stringArray(raw.selection_reason_codes).map((code) => selectionReasonLabels[code] ?? "机器选择政策记录了一个未分类原因。"),
    isShortlisted: selected.has(candidateHash),
    isBest: best === candidateHash,
    factorDagHash: optionalString(raw.formula_program_hash) ?? optionalString(raw.factor_dag_hash),
  };
}

function normalizeReport(value: unknown): FormalResearchReportView | undefined {
  const raw = record(value);
  if (!Object.keys(raw).length) return undefined;
  const limitations = stringArray(raw.limitations);
  const nextActions = stringArray(raw.next_actions);
  const isCanonical = "executive_answer" in raw || "candidates" in raw;
  const selectedCandidateHashes = stringArray(raw.selected_candidate_hashes);
  const bestCandidateHash = optionalString(raw.best_candidate_hash);
  const candidates = isCanonical && Array.isArray(raw.candidates)
    ? raw.candidates.flatMap((candidate) => {
      const normalized = normalizeDecisionCandidate(candidate, new Set(selectedCandidateHashes), bestCandidateHash);
      return normalized ? [normalized] : [];
    })
    : [];
  const candidateCount = nonNegativeInteger(raw.candidate_count);
  if (isCanonical && candidateCount !== candidates.length) {
    throw new Error("正式研究报告候选明细不完整，已关闭展示");
  }
  const rawOutcome = String(raw.outcome ?? "");
  const outcomeMap: Record<string, FormalResearchReportView["outcome"]> = {
    EVALUATED_RESEARCH_RESULT: "正向结果",
    DRAFT_RESEARCH_RESULT: "部分结果",
    NEGATIVE_RESEARCH_RESULT: "负向结果",
    DATA_OR_CAPABILITY_GAP_REPORT: "数据或能力缺口",
    PARTIAL_RESEARCH_RESULT: "部分结果",
    SYSTEM_FAILURE_REPORT: "系统故障",
    CANCELLED_RESEARCH_REPORT: "已取消",
  };
  const sourceLabel = optionalString(raw.source_label);
  const dataStart = optionalString(raw.data_start);
  const dataEnd = optionalString(raw.data_end);
  return {
    title: stringValue(raw.title, "AlphaForge 正式研究报告"),
    question: stringValue(raw.question, "研究问题未提供"),
    conclusion: stringValue(raw.executive_answer ?? raw.conclusion, "本次研究尚未形成正式结论。"),
    outcome: outcomeMap[rawOutcome] ?? (["正向结果", "负向结果", "部分结果", "数据或能力缺口", "系统故障", "已取消"].includes(rawOutcome)
      ? rawOutcome as FormalResearchReportView["outcome"] : "部分结果"),
    dataSummary: stringValue(raw.data_summary, sourceLabel && dataStart && dataEnd ? `${sourceLabels[sourceLabel] ?? "已登记研究数据源"}；${dataStart} 至 ${dataEnd}` : "数据身份信息尚未发布。"),
    machineConclusion: stringValue(raw.machine_conclusion, "尚未完成机器验证。"),
    limitations,
    nextActions,
    reportHash: optionalString(raw.report_hash ?? raw.artifact_hash),
    sourceLabel: sourceLabel ? sourceLabels[sourceLabel] ?? "已登记研究数据源" : undefined,
    dataStart,
    dataEnd,
    modelLabel: optionalString(raw.model_id),
    funnel: isCanonical ? {
      proposed: candidateCount,
      preflightAccepted: nonNegativeInteger(raw.accepted_preflight_count),
      machineEvaluated: nonNegativeInteger(raw.evaluated_candidate_count),
      shortlisted: selectedCandidateHashes.length,
      rejected: nonNegativeInteger(raw.rejected_candidate_count),
    } : undefined,
    candidates,
    bestCandidateHash,
    selectedCandidateHashes,
  };
}

export function normalizeResearchOSV3Run(input: unknown): ResearchOSV3RunView {
  const envelope = record(input) as RawResearchOSV3Envelope;
  const raw = record(envelope.result ?? input) as RawResearchRunResult;
  const selectedGroups = Array.isArray(raw.branches) ? raw.branches.map(normalizeBranch) : [];
  const rawRunStatus = stringValue(raw.status, "FAILED");
  const validation = machineValidation(raw, rawRunStatus);
  const rawSourceLabel = stringValue(envelope.source_label, "");
  const rawProviderLabel = stringValue(envelope.provider, "");
  return {
    runId: stringValue(raw.run_id, "未生成运行标识"),
    outcome: runOutcomes[rawRunStatus] ?? "失败",
    sourceLabel: sourceLabels[rawSourceLabel] ?? (rawSourceLabel ? "已登记研究数据源" : "来源未标注"),
    providerLabel: providerLabels[rawProviderLabel] ?? (rawProviderLabel ? "已登记受限研究服务" : "研究模型未标注"),
    modelLabel: optionalString(envelope.model),
    authorityLabel: "研究草案",
    selectedGroups,
    route: route(validation, selectedGroups.length),
    candidateCount: nonNegativeInteger(raw.candidate_count),
    acceptedPreflightCount: nonNegativeInteger(raw.accepted_preflight_count),
    manifestHash: stringValue(raw.manifest_hash, "未生成运行清单标识"),
    campaignHash: stringValue(raw.campaign_hash, "未生成研究计划标识"),
    contextHash: stringValue(raw.context_hash, "未生成数据上下文标识"),
    machineValidation: validation,
    report: normalizeReport(envelope.report),
  };
}
