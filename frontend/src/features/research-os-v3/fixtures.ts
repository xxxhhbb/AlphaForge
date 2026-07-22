import { normalizeResearchOSV3Run } from "./normalize";

export const researchOSV3Fixture = normalizeResearchOSV3Run({
  source_label: "APPROVED_PROXY_DATA_VENDOR",
  provider: "configured_openai_compatible",
  model: "deepseek-v4-flash-260425",
  result: {
    run_id: "u5-fixture-momentum-50",
    status: "PARTIAL",
    candidate_count: 5,
    accepted_preflight_count: 2,
    manifest_hash: "sha256:manifest-u5-fixture",
    campaign_hash: "sha256:campaign-u5-fixture",
    context_hash: "sha256:context-u5-fixture",
    official_evidence_hash: null,
    promotion_hash: null,
    branches: [
      {
        specialist_id: "market_structure",
        status: "COMPLETED_DRAFT",
        provider_calls: 5,
        outcome_hash: "sha256:market-result",
        candidate_refs: [
          { candidate_id: "momentum_50", candidate_hash: "sha256:candidate-1", preflight_status: "ACCEPTED", factor_dag_hash: "sha256:dag-1" },
          { candidate_id: "momentum_volume", candidate_hash: "sha256:candidate-2", preflight_status: "ACCEPTED", factor_dag_hash: "sha256:dag-2" },
        ],
      },
      {
        specialist_id: "alternative_data",
        status: "DATA_GAP",
        provider_calls: 0,
        missing_capabilities: ["缺少带可得时间的事件资料"],
        candidate_refs: [],
      },
      {
        specialist_id: "meta_alpha",
        status: "NEGATIVE_RESULT",
        provider_calls: 4,
        typed_failures: ["NO_VALID_REVISED_CANDIDATE"],
        candidate_refs: [
          { candidate_id: "incremental_1", candidate_hash: "sha256:candidate-3", preflight_status: "REVISE", failure_code: "REQUESTED_LOOKBACK_NOT_REPRESENTED" },
          { candidate_id: "incremental_2", candidate_hash: "sha256:candidate-4", preflight_status: "CAPABILITY_REQUEST", failure_code: "INVALID_FACTOR_DAG" },
          { candidate_id: "incremental_3", candidate_hash: "sha256:candidate-5", preflight_status: "NOT_EVALUATED", failure_code: "NOT_EVALUATED" },
        ],
      },
    ],
  },
});

export const researchOSV3ReportFixture = normalizeResearchOSV3Run({
  ...{
    source_label: "APPROVED_PROXY_DATA_VENDOR",
    provider: "configured_openai_compatible",
    model: "deepseek-v4-flash-260425",
  },
  result: {
    run_id: "u5-fixture-report",
    status: "COMPLETED",
    candidate_count: 4,
    accepted_preflight_count: 2,
    manifest_hash: "sha256:manifest-report",
    campaign_hash: "sha256:campaign-report",
    context_hash: "sha256:context-report",
    official_evidence_hash: "sha256:official-evidence",
    promotion_hash: null,
    branches: [],
  },
  report: {
    title: "A 股 50 日动量自动研究报告",
    question: "挖一下最近 50 天的大动量因子",
    conclusion: "候选已完成可见研究验证，但尚未申请独立封闭评审，不构成挑战者结论。",
    outcome: "部分结果",
    data_summary: "已批准代理数据源；历史股票池与交易日历按决策时点冻结。",
    machine_conclusion: "2 个候选通过可见研究标准，成本与增量性仍需独立复核。",
    limitations: ["封闭指标对研究者与页面不可见。", "结果不构成投资建议。"],
    next_actions: ["申请独立封闭评审。", "若未通过，按脱敏结论决定停止或修订。"],
    report_hash: "sha256:formal-report-u5",
    sealed_metrics: { rank: 1, secret: "NEVER_RENDER" },
  },
});

function canonicalEnvelope(outcome: string, overrides: Record<string, unknown> = {}) {
  return {
    source_label: "APPROVED_PROXY_DATA_VENDOR",
    provider: "configured_openai_compatible",
    model: "deepseek-v4-flash-260425",
    result: {
      run_id: `fixture-${outcome.toLowerCase()}`,
      status: "COMPLETED",
      candidate_count: 2,
      accepted_preflight_count: 2,
      manifest_hash: "sha256:manifest-canonical",
      campaign_hash: "sha256:campaign-canonical",
      context_hash: "sha256:context-canonical",
      official_evidence_hash: "sha256:official-canonical",
      promotion_hash: null,
      branches: [],
    },
    report: {
      question: "寻找成本后仍稳定的 50 日动量候选",
      executive_answer: "两项候选完成机器评价，其中一项进入独立评审短名单；这不是晋级结论。",
      outcome,
      source_label: "APPROVED_PROXY_DATA_VENDOR",
      data_start: "2025-01-02",
      data_end: "2026-06-30",
      model_id: "deepseek-v4-flash-260425",
      candidate_count: 2,
      accepted_preflight_count: 2,
      evaluated_candidate_count: 2,
      rejected_candidate_count: 1,
      selected_candidate_hashes: ["sha256:selected-candidate"],
      best_candidate_hash: "sha256:selected-candidate",
      machine_evidence_status: "OFFICIAL_VISIBLE_EVALUATED",
      candidates: [
        {
          specialist_id: "market_structure", group_label: "市场行为与交易可行性研究组", candidate_id: "momentum_cost_aware", candidate_hash: "sha256:selected-candidate",
          hypothesis: "经成交活跃度约束的 50 日动量可能保留成本后预测力。", economic_mechanism: "缓慢信息扩散与交易容量约束。",
          expression: "rank_cross_section(pct_change(close, 50))", preflight_status: "ACCEPTED", factor_dag_hash: "sha256:dag-selected",
          machine_evidence_status: "OFFICIAL_VISIBLE_EVALUATED", official_metrics: [{ name: "validation_icir", value: 0.72, evidence_hash: "sha256:e1" }],
          selection_verdict: "SELECTED_FOR_PROMOTION_REVIEW", selection_score: 0.63, selection_reason_codes: ["PIT_EVIDENCE_CRITIC_VISIBLE_SELECTION_PASS"],
        },
        {
          specialist_id: "meta_alpha", group_label: "组合增量研究组", candidate_id: "momentum_plain", candidate_hash: "sha256:rejected-candidate",
          hypothesis: "原始 50 日动量可能带来增量预测。", economic_mechanism: "价格延续。", expression: "pct_change(close, 50)", preflight_status: "ACCEPTED",
          machine_evidence_status: "OFFICIAL_VISIBLE_EVALUATED", official_metrics: [{ name: "validation_icir", value: -0.2, evidence_hash: "sha256:e2" }],
          selection_verdict: "NOT_ELIGIBLE", selection_score: null, selection_reason_codes: ["NET_ICIR_BELOW_POLICY", "INCREMENTALITY_BELOW_POLICY"],
        },
      ],
      limitations: ["独立封闭评审尚未执行。"], next_actions: ["对短名单候选申请独立评审。"], artifact_hash: "sha256:canonical-report",
      sealed_payload: "NEVER_RENDER",
      ...overrides,
    },
  };
}

export const researchOSV3EvaluatedFixture = normalizeResearchOSV3Run(canonicalEnvelope("EVALUATED_RESEARCH_RESULT"));
export const researchOSV3RejectedFixture = normalizeResearchOSV3Run(canonicalEnvelope("NEGATIVE_RESEARCH_RESULT", {
  executive_answer: "全部候选均未达到机器选择政策，本轮形成负向研究结果。",
  selected_candidate_hashes: [], best_candidate_hash: null,
}));
export const researchOSV3DataGapFixture = normalizeResearchOSV3Run(canonicalEnvelope("DATA_OR_CAPABILITY_GAP_REPORT", {
  executive_answer: "批准数据缺少可确认发布时间的预期字段，无法执行官方评价。",
  evaluated_candidate_count: 0, selected_candidate_hashes: [], best_candidate_hash: null, machine_evidence_status: "NOT_RUN",
  candidates: [
    { specialist_id: "expectations_events", group_label: "预期与事件研究组", candidate_id: "earnings_revision", candidate_hash: "sha256:gap-1", hypothesis: "盈利上调可能预测收益。", economic_mechanism: "预期修正。", expression: "未形成", preflight_status: "CAPABILITY_REQUEST", machine_evidence_status: "BLOCKED", official_metrics: [], selection_verdict: "NOT_EVALUATED", selection_reason_codes: ["OFFICIAL_EVIDENCE_MISSING"] },
    { specialist_id: "fundamental", group_label: "基本面研究组", candidate_id: "fundamental_proxy", candidate_hash: "sha256:gap-2", hypothesis: "基本面代理可能提供替代信息。", economic_mechanism: "质量变化。", expression: "未形成", preflight_status: "CAPABILITY_REQUEST", machine_evidence_status: "BLOCKED", official_metrics: [], selection_verdict: "NOT_EVALUATED", selection_reason_codes: ["PIT_NOT_VALIDATED"] },
  ],
}));
export const researchOSV3PartialFixture = normalizeResearchOSV3Run(canonicalEnvelope("PARTIAL_RESEARCH_RESULT", {
  executive_answer: "部分候选完成预检，但机器评价尚未全部完成。",
  evaluated_candidate_count: 1, selected_candidate_hashes: [], best_candidate_hash: null,
}));
