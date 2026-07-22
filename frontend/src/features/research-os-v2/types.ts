export type ResearchPlanState =
  | "待确认"
  | "已确认"
  | "排队中"
  | "运行中"
  | "等待检查点"
  | "已完成"
  | "已阻断"
  | "失败";

export type ResearchDepth = "快速探索" | "标准研究" | "严格验证";
export type CollaborationStyle = "引导式" | "标准式" | "批处理式";
export type ProviderMode = "真实模型" | "确定性回退" | "不可用";
export type DataAvailability = "可用" | "部分可用" | "不可用";
export type BranchState = "等待中" | "运行中" | "已完成" | "需修订" | "已阻断" | "失败";
export type CandidateDecision =
  | "未评价"
  | "拒绝"
  | "需修订"
  | "研究验证通过"
  | "挑战者";
export type SealedReviewState = "未申请" | "等待独立评审" | "评审完成" | "不可用";

export interface ProviderProvenanceView {
  mode: ProviderMode;
  providerLabel: string;
  modelLabel?: string;
  reason?: string;
}

export interface DataCoverageItemView {
  field: string;
  availability: DataAvailability;
  coverageLabel: string;
  sourceLabel: string;
  limitation?: string;
}

export interface DynamicDataPlanView {
  planHash: string;
  requestedApis: string[];
  requestedFields: string[];
  minimumSessions: number;
  currentSessions: number;
  universe: {
    kind: string;
    label?: string;
    symbolCount?: number;
    indexCode?: string;
  };
  acquisitionRequired: boolean;
  readiness: "READY" | "PARTIAL" | "UNAVAILABLE";
  missingRequirements: string[];
  degradationSuggestions: string[];
  researchableParts: string[];
}

export interface ResearchSuggestionView {
  suggestionId: string;
  question: string;
  researchMode: string;
  researchObject: string;
  canRun: boolean;
  answerLevel: "概念研究" | "辅助研究" | "机器评价";
  rationale: string;
  selectedGroups: string[];
  plannedExperiments: string[];
  dataCoverage: DataCoverageItemView[];
  dataGaps: string[];
  dataPlan?: DynamicDataPlanView;
  provider: ProviderProvenanceView;
}

export interface ResearchPlanView extends ResearchSuggestionView {
  planId: string;
  state: ResearchPlanState;
  depth: ResearchDepth;
  collaborationStyle: CollaborationStyle;
  universe: string;
  horizon: string;
  budget: {
    groups: number;
    candidates: number;
    trials: number;
  };
  checkpoints: string[];
  stopConditions: string[];
  planHash: string;
}

export interface CandidateEvidenceView {
  candidateId: string;
  title: string;
  hypothesis: string;
  expression: string;
  decision: CandidateDecision;
  decisionExplanation: string;
  metricSummary?: {
    netIcir: number;
    stability: number;
    turnover: number;
    incrementalScore: number;
  };
  evidenceHash?: string;
}

export interface ResearchGroupView {
  groupId: string;
  title: string;
  purpose: string;
  state: BranchState;
  budgetUsed: number;
  budgetLimit: number;
  progress: string[];
  candidates: CandidateEvidenceView[];
  failure?: string;
}

export interface ResearchRunView {
  runId: string;
  planId: string;
  state: ResearchPlanState;
  provider: ProviderProvenanceView;
  groups: ResearchGroupView[];
  trialsUsed: number;
  trialLimit: number;
  currentCheckpoint: string;
  sealedReview: SealedReviewState;
  sealedMessage: string;
  formalReportPath?: string;
  reportHash?: string;
  limitations: string[];
}

export interface CreateSuggestionInput {
  question: string;
  depth: ResearchDepth;
  collaborationStyle: CollaborationStyle;
}

export interface ResearchOSV2Client {
  createSuggestion(input: CreateSuggestionInput): Promise<ResearchSuggestionView>;
  confirmPlan(
    suggestion: ResearchSuggestionView,
    input: Pick<CreateSuggestionInput, "depth" | "collaborationStyle">,
  ): Promise<ResearchPlanView>;
  startResearch(planId: string): Promise<ResearchRunView>;
  getResearchStatus(runId: string, signal?: AbortSignal): Promise<ResearchRunView>;
}
