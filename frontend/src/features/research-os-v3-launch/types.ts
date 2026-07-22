export type V3ResearchDepth = "快速探索" | "标准研究" | "严格验证";
export type V3InteractionStyle = "引导式" | "标准式" | "批处理式";

export type V3PlanState = "待确认" | "已确认";
export type V3RunState =
  | "排队中"
  | "运行中"
  | "等待确认"
  | "已完成"
  | "部分完成"
  | "已阻断"
  | "失败"
  | "已取消";

export interface V3LaunchInput {
  question: string;
  depth: V3ResearchDepth;
  interactionStyle: V3InteractionStyle;
}

export interface V3DataScopeView {
  sourceLabel: string;
  universe: string;
  dateRange: string;
  snapshotHash?: string;
  availability: "可用" | "部分可用" | "不可用";
  limitations: string[];
}

export interface V3BudgetView {
  specialistGroups: number;
  candidateLimit: number;
  trialLimit: number;
  modelCallLimit: number;
}

export interface V3CampaignPlanView {
  campaignId: string;
  campaignHash: string;
  state: V3PlanState;
  question: string;
  researchMode: string;
  researchSubject: string;
  depth: V3ResearchDepth;
  interactionStyle: V3InteractionStyle;
  selectedSpecialists: string[];
  plannedResearch: string[];
  dataScope: V3DataScopeView;
  budget: V3BudgetView;
  checkpoints: string[];
  stopConditions: string[];
}

export interface V3SpecialistProgressView {
  specialistId: string;
  title: string;
  state: "等待中" | "运行中" | "已完成" | "部分完成" | "已阻断" | "失败" | "已取消";
  candidateCount: number;
  message: string;
}

export interface V3ResearchRunControlView {
  runId: string;
  campaignId: string;
  state: V3RunState;
  currentStage: string;
  providerLabel: string;
  modelLabel?: string;
  selectedSpecialists: V3SpecialistProgressView[];
  usedBudget: number;
  totalBudget: number;
  resultReady: boolean;
  typedFailures: string[];
}

export interface V3LaunchClient {
  createPlan(input: V3LaunchInput): Promise<V3CampaignPlanView>;
  preparePlanData(campaignId: string, endDate: string): Promise<V3CampaignPlanView>;
  confirmPlan(campaignId: string): Promise<V3CampaignPlanView>;
  startRun(campaignId: string): Promise<V3ResearchRunControlView>;
  getRun(runId: string, signal?: AbortSignal): Promise<V3ResearchRunControlView>;
  getReportMarkdown(runId: string, signal?: AbortSignal): Promise<string>;
  cancelRun(runId: string): Promise<V3ResearchRunControlView>;
  resumeRun(runId: string, campaignId: string): Promise<V3ResearchRunControlView>;
}
