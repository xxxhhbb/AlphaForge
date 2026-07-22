export type ResearchRunOutcome = "已完成" | "部分完成" | "已阻断" | "失败" | "已取消";

export type ResearchGroupOutcome =
  | "草案已提交"
  | "负向结果"
  | "数据缺口"
  | "预算已用尽"
  | "部分完成"
  | "系统异常"
  | "已取消";

export type CandidatePreflightOutcome =
  | "预检通过"
  | "需要修订"
  | "能力缺口"
  | "尚未预检";

export type MachineEvidenceOutcome = "已生成" | "尚未执行" | "已阻断";

export type CandidateSelectionOutcome =
  | "进入独立评审短名单"
  | "不符合选择条件"
  | "需要修订"
  | "尚未选择";

export interface OfficialMetricView {
  name: string;
  value: number;
}

export interface CandidateDecisionView {
  candidateId: string;
  candidateHash: string;
  groupLabel: string;
  hypothesis: string;
  economicMechanism: string;
  expression: string;
  preflight: CandidatePreflightOutcome;
  machineEvidence: MachineEvidenceOutcome;
  metrics: OfficialMetricView[];
  verdict: CandidateSelectionOutcome;
  score?: number;
  reasons: string[];
  isShortlisted: boolean;
  isBest: boolean;
  factorDagHash?: string;
}

export interface CandidateFunnelView {
  proposed: number;
  preflightAccepted: number;
  machineEvaluated: number;
  shortlisted: number;
  rejected: number;
}

export interface CandidatePreflightView {
  candidateId: string;
  candidateHash: string;
  outcome: CandidatePreflightOutcome;
  explanation: string;
  factorDagHash?: string;
}

export interface ResearchGroupResultView {
  groupId: string;
  title: string;
  purpose: string;
  outcome: ResearchGroupOutcome;
  providerCalls: number;
  candidates: CandidatePreflightView[];
  missingCapabilities: string[];
  findings: string[];
  resultHash?: string;
}

export interface ResearchRouteStepView {
  id: string;
  title: string;
  description: string;
  state: "已完成" | "当前阶段" | "等待中" | "已阻断";
}

export interface MachineValidationView {
  evidence: MachineEvidenceOutcome;
  evidenceHash?: string;
  promotion: "未申请" | "评审中" | "已完成" | "已阻断";
  publicMessage: string;
  sealedMessage: string;
}

export interface FormalResearchReportView {
  title: string;
  question: string;
  conclusion: string;
  outcome: "正向结果" | "负向结果" | "部分结果" | "数据或能力缺口" | "系统故障" | "已取消";
  dataSummary: string;
  machineConclusion: string;
  limitations: string[];
  nextActions: string[];
  reportHash?: string;
  sourceLabel?: string;
  dataStart?: string;
  dataEnd?: string;
  modelLabel?: string;
  funnel?: CandidateFunnelView;
  candidates: CandidateDecisionView[];
  bestCandidateHash?: string;
  selectedCandidateHashes: string[];
}

export interface ResearchOSV3RunView {
  runId: string;
  outcome: ResearchRunOutcome;
  sourceLabel: string;
  providerLabel: string;
  modelLabel?: string;
  authorityLabel: "研究草案";
  selectedGroups: ResearchGroupResultView[];
  route: ResearchRouteStepView[];
  candidateCount: number;
  acceptedPreflightCount: number;
  manifestHash: string;
  campaignHash: string;
  contextHash: string;
  machineValidation: MachineValidationView;
  report?: FormalResearchReportView;
}

export interface ResearchOSV3ResultClient {
  getResearchResult(runId: string, signal?: AbortSignal): Promise<ResearchOSV3RunView>;
}

export interface RawCandidateRef {
  candidate_id?: unknown;
  candidate_hash?: unknown;
  preflight_status?: unknown;
  factor_dag_hash?: unknown;
  formula_program_hash?: unknown;
  formula_program_kind?: unknown;
  failure_code?: unknown;
}

export interface RawResearchBranch {
  specialist_id?: unknown;
  status?: unknown;
  provider_calls?: unknown;
  candidate_refs?: unknown;
  missing_capabilities?: unknown;
  typed_failures?: unknown;
  outcome_hash?: unknown;
}

export interface RawResearchRunResult {
  run_id?: unknown;
  status?: unknown;
  branches?: unknown;
  candidate_count?: unknown;
  accepted_preflight_count?: unknown;
  manifest_hash?: unknown;
  campaign_hash?: unknown;
  context_hash?: unknown;
  official_evidence_hash?: unknown;
  promotion_hash?: unknown;
}

export interface RawResearchOSV3Envelope {
  result?: unknown;
  source_label?: unknown;
  provider?: unknown;
  model?: unknown;
  report?: unknown;
}
