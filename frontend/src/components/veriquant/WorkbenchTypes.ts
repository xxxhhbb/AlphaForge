export type WorkbenchStatus =
  | "DRAFT"
  | "VALIDATED_RESEARCH"
  | "CHALLENGER"
  | "SHADOW_ACTIVE"
  | "REJECTED"
  | "INCOMPLETE"
  | "BLOCKED";

export const WORKBENCH_STATUSES: WorkbenchStatus[] = [
  "DRAFT",
  "VALIDATED_RESEARCH",
  "CHALLENGER",
  "SHADOW_ACTIVE",
  "REJECTED",
  "INCOMPLETE",
  "BLOCKED",
];

export const STATUS_LABELS: Record<WorkbenchStatus, string> = {
  DRAFT: "草案",
  VALIDATED_RESEARCH: "研究已验证",
  CHALLENGER: "挑战者",
  SHADOW_ACTIVE: "影子组合运行中",
  REJECTED: "已拒绝",
  INCOMPLETE: "未完成",
  BLOCKED: "已阻断",
};

export interface WorkbenchRunSummary {
  run_id: string;
  title?: string;
  status: WorkbenchStatus;
  decision_time?: string;
  updated_at?: string;
  candidate_count?: number;
  alert_count?: number;
}

export interface WorkbenchRunDetail {
  run?: Record<string, unknown>;
  report?: Record<string, unknown>;
  research_dag?: Record<string, unknown>;
  lineage?: Record<string, unknown>;
  evidence?: Record<string, unknown>;
  critics?: Record<string, unknown>;
  promotion?: Record<string, unknown>;
  citations?: Record<string, unknown>;
  shadow_portfolio?: Record<string, unknown>;
  nav_attribution?: Record<string, unknown>;
  drift_alerts?: Record<string, unknown>;
}

export interface WorkbenchRunsResponse {
  status: string;
  runs: WorkbenchRunSummary[];
}

export interface WorkbenchDetailResponse {
  status: string;
  detail: WorkbenchRunDetail;
}
