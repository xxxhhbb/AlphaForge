import type {
  V3CampaignPlanView,
  V3LaunchClient,
  V3LaunchInput,
  V3ResearchRunControlView,
} from "./types";

export class ResearchOSV3LaunchClientError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "ResearchOSV3LaunchClientError";
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function stringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

const AVAILABILITY_LABELS = new Set(["可用", "部分可用", "不可用"]);
const PRODUCT_AVAILABILITY_MAP: Record<string, V3CampaignPlanView["dataScope"]["availability"]> = {
  可正式评价: "可用",
  可进行部分研究: "部分可用",
  需要补充数据: "不可用",
};

function normalizePlan(value: unknown): V3CampaignPlanView | undefined {
  if (!record(value) || !record(value.dataScope) || !record(value.budget)) return undefined;
  const rawAvailability = value.dataScope.availability;
  if (typeof rawAvailability === "string" && rawAvailability in PRODUCT_AVAILABILITY_MAP) {
    value = {
      ...value,
      dataScope: {
        ...value.dataScope,
        availability: PRODUCT_AVAILABILITY_MAP[rawAvailability],
      },
    };
  }
  if (!isPlan(value)) return undefined;
  return value;
}

function isPlan(value: unknown): value is V3CampaignPlanView {
  if (!record(value) || !record(value.dataScope) || !record(value.budget)) return false;
  const dataScope = value.dataScope;
  const budget = value.budget;
  return ["campaignId", "campaignHash", "state", "question", "researchMode", "researchSubject", "depth", "interactionStyle"]
    .every((key) => typeof value[key] === "string")
    && ["待确认", "已确认"].includes(value.state as string)
    && ["快速探索", "标准研究", "严格验证"].includes(value.depth as string)
    && ["引导式", "标准式", "批处理式"].includes(value.interactionStyle as string)
    && stringArray(value.selectedSpecialists)
    && value.selectedSpecialists.length > 0
    && stringArray(value.plannedResearch)
    && stringArray(value.checkpoints)
    && stringArray(value.stopConditions)
    && ["sourceLabel", "universe", "dateRange", "availability"].every((key) => typeof dataScope[key] === "string")
    && AVAILABILITY_LABELS.has(dataScope.availability as string)
    && stringArray(dataScope.limitations)
    && ["specialistGroups", "candidateLimit", "trialLimit", "modelCallLimit"]
      .every((key) => typeof budget[key] === "number" && Number.isInteger(budget[key]) && (budget[key] as number) >= 0);
}

function isRun(value: unknown): value is V3ResearchRunControlView {
  if (!record(value)) return false;
  return ["runId", "campaignId", "state", "currentStage", "providerLabel"].every((key) => typeof value[key] === "string")
    && ["排队中", "运行中", "等待确认", "已完成", "部分完成", "已阻断", "失败", "已取消"].includes(value.state as string)
    && Array.isArray(value.selectedSpecialists)
    && value.selectedSpecialists.every((item) => record(item)
      && ["specialistId", "title", "state", "message"].every((key) => typeof item[key] === "string")
      && ["等待中", "运行中", "已完成", "部分完成", "已阻断", "失败", "已取消"].includes(item.state as string)
      && typeof item.candidateCount === "number" && Number.isInteger(item.candidateCount) && item.candidateCount >= 0)
    && typeof value.usedBudget === "number" && Number.isInteger(value.usedBudget) && value.usedBudget >= 0
    && typeof value.totalBudget === "number" && Number.isInteger(value.totalBudget) && value.totalBudget >= 0
    && value.usedBudget <= value.totalBudget
    && typeof value.resultReady === "boolean"
    && stringArray(value.typedFailures);
}

const GROUP_TITLES: Record<string, string> = {
  fundamental: "基本面研究组",
  expectations_events: "预期与事件研究组",
  market_structure: "市场行为与交易可行性研究组",
  alternative_data: "另类数据研究组",
  macro_regime: "宏观状态研究组",
  meta_alpha: "组合增量研究组",
};

const RUN_STATES: Record<string, V3ResearchRunControlView["state"]> = {
  PLANNING: "排队中",
  RESEARCHING: "运行中",
  CANDIDATE_PREFLIGHT: "运行中",
  AWAITING_EVIDENCE: "等待确认",
  COMPLETED: "已完成",
  PARTIAL: "部分完成",
  BLOCKED: "已阻断",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const BRANCH_STATES: Record<string, V3ResearchRunControlView["selectedSpecialists"][number]["state"]> = {
  WAITING: "等待中",
  RUNNING: "运行中",
  COMPLETED_DRAFT: "已完成",
  DATA_GAP: "已阻断",
  BUDGET_BLOCKED: "已阻断",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const FAILURE_LABELS: Record<string, string> = {
  DATA_GAP: "部分研究所需的数据或已登记证据不足，相关分支未启动。",
  UNAUTHORIZED_CAPABILITY: "部分研究输出触发能力边界，已安全停止且未获得额外权限。",
  PROVIDER_FAILED: "研究模型调用失败，相关分支未伪造结果。",
  ADAPTER_UNAVAILABLE: "研究模型适配器不可用，相关分支未启动。",
};

function normalizeRun(value: unknown): V3ResearchRunControlView | undefined {
  if (isRun(value)) return value;
  if (!record(value)) return undefined;
  const stage = typeof value.current_stage === "string" ? value.current_stage : "";
  const state = RUN_STATES[stage];
  const groups = value.selected_group_ids;
  const summaries = Array.isArray(value.specialist_summaries) ? value.specialist_summaries : [];
  const failures = value.typed_failures;
  const candidateCount = value.candidate_count;
  const trialCount = value.trial_count;
  const remaining = value.remaining_candidate_slots;
  if (!state || typeof value.run_id !== "string" || typeof value.campaign_hash !== "string"
    || !stringArray(groups) || !stringArray(failures)
    || typeof candidateCount !== "number" || !Number.isInteger(candidateCount) || candidateCount < 0
    || typeof trialCount !== "number" || !Number.isInteger(trialCount) || trialCount < 0
    || typeof remaining !== "number" || !Number.isInteger(remaining) || remaining < 0) return undefined;
  const terminalGroupState = state === "已完成" ? "已完成"
    : state === "部分完成" ? "部分完成"
      : state === "已阻断" ? "已阻断"
        : state === "失败" ? "失败"
          : state === "已取消" ? "已取消"
            : state === "运行中" ? "运行中" : "等待中";
  return {
    runId: value.run_id,
    campaignId: typeof value.plan_id === "string" ? value.plan_id : value.campaign_hash,
    state,
    currentStage: ({
      PLANNING: "正在准备研究计划与数据边界",
      RESEARCHING: "专业研究组正在形成候选",
      CANDIDATE_PREFLIGHT: "候选正在接受安全预检",
      AWAITING_EVIDENCE: "等待机器证据服务",
      COMPLETED: "研究与报告已完成",
      PARTIAL: "已保留可用的部分研究结果",
      BLOCKED: "研究因受控条件不足而停止",
      FAILED: "研究基础设施异常",
      CANCELLED: "用户已取消本次研究",
    } as Record<string, string>)[stage],
    providerLabel: typeof value.provider_id === "string" ? value.provider_id : "受限研究服务",
    modelLabel: typeof value.model_id === "string" && value.model_id !== "none" ? value.model_id : undefined,
    selectedSpecialists: groups.map((specialistId) => {
      const summary = summaries.find((item) => record(item) && item.specialist_id === specialistId);
      const branchStatus = summary && typeof summary.status === "string" ? BRANCH_STATES[summary.status] : undefined;
      const branchCount = summary && typeof summary.candidate_count === "number" && Number.isInteger(summary.candidate_count) && summary.candidate_count >= 0
        ? summary.candidate_count : 0;
      const branchMessage = summary && typeof summary.message === "string" ? summary.message
        : terminalGroupState === "运行中" ? "本组正在执行有界研究步骤。" : "分组明细将在正式结果中展示。";
      return {
        specialistId,
        title: GROUP_TITLES[specialistId] ?? "专业研究组",
        state: branchStatus ?? terminalGroupState,
        candidateCount: branchCount,
        message: branchMessage,
      };
    }),
    usedBudget: trialCount,
    totalBudget: trialCount + remaining,
    resultReady: (state === "已完成" || state === "部分完成")
      && typeof value.report_hash === "string"
      && (candidateCount === 0 || trialCount > 0 || typeof value.official_evidence_hash === "string"),
    typedFailures: failures.map((item) => FAILURE_LABELS[item] ?? `受控停止代码：${item}`),
  };
}

async function request<T>(path: string, init: RequestInit, parse: (value: unknown) => T | undefined): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      headers: { "content-type": "application/json", ...(init.headers ?? {}) },
    });
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "AbortError") throw reason;
    throw new ResearchOSV3LaunchClientError("研究服务当前不可用。已停止本次操作，请稍后重试。");
  }
  if (!response.ok) throw new ResearchOSV3LaunchClientError(`研究服务拒绝了请求（${response.status}）。`, response.status);
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ResearchOSV3LaunchClientError("研究服务没有返回可解析的数据。", response.status);
  }
  const parsed = parse(payload);
  if (parsed === undefined) throw new ResearchOSV3LaunchClientError("研究服务返回的数据不完整，页面已停止继续运行。", response.status);
  return parsed;
}

async function requestText(path: string, init: RequestInit = {}): Promise<string> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, credentials: "same-origin" });
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "AbortError") throw reason;
    throw new ResearchOSV3LaunchClientError("研究报告服务当前不可用。请稍后重试。");
  }
  if (!response.ok) throw new ResearchOSV3LaunchClientError(`研究报告暂不可读（${response.status}）。`, response.status);
  const text = await response.text();
  if (!text.trim()) throw new ResearchOSV3LaunchClientError("研究报告为空，页面已停止展示。", response.status);
  return text;
}

export function createResearchOSV3LaunchHttpClient(basePath = "/api/veriquant/research-os/v3"): V3LaunchClient {
  const base = basePath.replace(/\/$/, "");
  const requireId = (value: string, label: string) => {
    const normalized = value.trim();
    if (!normalized) throw new ResearchOSV3LaunchClientError(`缺少${label}。`);
    return encodeURIComponent(normalized);
  };
  return {
    createPlan(input: V3LaunchInput) {
      return request(`${base}/plans`, { method: "POST", body: JSON.stringify(input) }, normalizePlan);
    },
    preparePlanData(campaignId: string, endDate: string) {
      const normalizedDate = endDate.split("-").join("").trim();
      if (!/^\d{8}$/.test(normalizedDate)) throw new ResearchOSV3LaunchClientError("真实数据截止日期格式无效。");
      return request(
        `${base}/plans/${requireId(campaignId, "研究计划标识")}/prepare-data`,
        { method: "POST", body: JSON.stringify({ allowNetwork: true, endDate: normalizedDate }) },
        (value) => record(value) ? normalizePlan(value.plan) : undefined,
      );
    },
    confirmPlan(campaignId: string) {
      return request(`${base}/plans/${requireId(campaignId, "研究计划标识")}/confirm`, { method: "POST" }, normalizePlan);
    },
    startRun(campaignId: string) {
      return request(`${base}/runs`, { method: "POST", body: JSON.stringify({ plan_id: campaignId }) }, normalizeRun);
    },
    getRun(runId: string, signal?: AbortSignal) {
      return request(`${base}/runs/${requireId(runId, "研究运行标识")}/status`, { method: "GET", signal }, normalizeRun);
    },
    getReportMarkdown(runId: string, signal?: AbortSignal) {
      return requestText(`${base}/runs/${requireId(runId, "研究运行标识")}/report.md`, { method: "GET", signal });
    },
    cancelRun(runId: string) {
      return request(`${base}/runs/${requireId(runId, "研究运行标识")}/cancel`, { method: "POST" }, normalizeRun);
    },
    resumeRun(runId: string, campaignId: string) {
      return request(`${base}/runs/${requireId(runId, "研究运行标识")}/resume`, { method: "POST", body: JSON.stringify({ plan_id: campaignId }) }, normalizeRun);
    },
  };
}

export const defaultResearchOSV3LaunchClient = createResearchOSV3LaunchHttpClient();
