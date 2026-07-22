export type VeriQuantRunStatus =
  | "DRAFT"
  | "DATA_VALIDATED"
  | "PIT_VALIDATED"
  | "EVALUATED"
  | "REVISE"
  | "REJECTED"
  | "VALIDATED_RESEARCH"
  | "CHALLENGER"
  | "SHADOW_ACTIVE"
  | "RETIRED"
  | "INCOMPLETE"
  | "BLOCKED"
  | "COMPLETED"
  | "FAILED"
  | "PENDING"
  | "RUNNING"
  | "CANCELLED"
  | "INTERRUPTED";

export interface VeriQuantRunSummary {
  run_id: string;
  title?: string;
  status: VeriQuantRunStatus;
  decision_time?: string;
  updated_at?: string;
  candidate_count?: number;
  alert_count?: number;
}

export interface VeriQuantRunDetail {
  run?: Record<string, unknown>;
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

export const STATUS_ZH: Record<string, string> = {
  DRAFT: "草案",
  DATA_VALIDATED: "数据已验证",
  PIT_VALIDATED: "时点已验证",
  EVALUATED: "已评估",
  REVISE: "需要修订",
  REJECTED: "已拒绝",
  VALIDATED_RESEARCH: "研究已验证",
  CHALLENGER: "挑战者",
  SHADOW_ACTIVE: "影子组合运行中",
  RETIRED: "已退役",
  INCOMPLETE: "未完成",
  BLOCKED: "已阻断",
  COMPLETED: "已完成",
  FAILED: "失败",
  PENDING: "等待中",
  RUNNING: "运行中",
  CANCELLED: "已取消",
  INTERRUPTED: "已中断",
  NEEDS_CONFIRMATION: "待确认",
  CONFIRMED: "已确认",
  ROUTED: "已路由",
  AWAITING_CHECKPOINT: "等待检查点",
  VALIDATING: "验证中",
  STOPPED: "已停止",
};

export function statusZh(status?: string): string {
  if (!status) return "未知";
  return STATUS_ZH[status] ?? status;
}

async function readJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { headers: { Accept: "application/json" }, signal });
  if (!response.ok) throw new Error(`接口暂不可用（${response.status}）`);
  return response.json() as Promise<T>;
}

export async function listVeriQuantRuns(signal?: AbortSignal): Promise<VeriQuantRunSummary[]> {
  const payload = await readJson<{ runs?: VeriQuantRunSummary[] }>("/veriquant/workbench/runs?limit=100", signal);
  return Array.isArray(payload.runs) ? payload.runs : [];
}

export async function getVeriQuantRun(runId: string, signal?: AbortSignal): Promise<VeriQuantRunDetail | null> {
  const payload = await readJson<{ detail?: VeriQuantRunDetail }>(
    `/veriquant/workbench/runs/${encodeURIComponent(runId)}`,
    signal,
  );
  return payload.detail ?? null;
}

export async function getVeriQuantStatus(signal?: AbortSignal): Promise<Record<string, unknown>> {
  return readJson<Record<string, unknown>>("/veriquant/status", signal);
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function valueText(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}
