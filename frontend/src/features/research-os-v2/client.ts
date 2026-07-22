import type {
  CreateSuggestionInput,
  ResearchOSV2Client,
  ResearchPlanView,
  ResearchRunView,
  ResearchSuggestionView,
} from "./types";

export class ResearchOSClientError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "ResearchOSClientError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasStrings(value: Record<string, unknown>, keys: string[]): boolean {
  return keys.every((key) => typeof value[key] === "string");
}

function hasStringArray(value: unknown): boolean {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isDataPlan(value: unknown): boolean {
  if (!isRecord(value) || !isRecord(value.universe)) return false;
  return hasStrings(value, ["planHash", "readiness"])
    && hasStrings(value.universe, ["kind"])
    && hasStringArray(value.requestedApis)
    && hasStringArray(value.requestedFields)
    && hasStringArray(value.missingRequirements)
    && hasStringArray(value.degradationSuggestions)
    && hasStringArray(value.researchableParts)
    && typeof value.minimumSessions === "number"
    && Number.isFinite(value.minimumSessions)
    && value.minimumSessions > 0
    && typeof value.currentSessions === "number"
    && Number.isFinite(value.currentSessions)
    && value.currentSessions >= 0
    && typeof value.acquisitionRequired === "boolean"
    && ["READY", "PARTIAL", "UNAVAILABLE"].includes(value.readiness as string);
}

function isSuggestion(value: unknown): value is ResearchSuggestionView {
  return isRecord(value)
    && hasStrings(value, ["suggestionId", "question", "researchMode", "researchObject", "answerLevel", "rationale"])
    && typeof value.canRun === "boolean"
    && Array.isArray(value.selectedGroups)
    && Array.isArray(value.plannedExperiments)
    && Array.isArray(value.dataCoverage)
    && Array.isArray(value.dataGaps)
    && (value.dataPlan === undefined || isDataPlan(value.dataPlan))
    && isRecord(value.provider);
}

function isPlan(value: unknown): value is ResearchPlanView {
  if (!isSuggestion(value)) return false;
  const record = value as unknown as Record<string, unknown>;
  return hasStrings(record, ["planId", "state", "depth", "collaborationStyle", "universe", "horizon", "planHash"])
    && isRecord(record.budget)
    && Array.isArray(record.checkpoints)
    && Array.isArray(record.stopConditions);
}

function isRun(value: unknown): value is ResearchRunView {
  return isRecord(value)
    && hasStrings(value, ["runId", "planId", "state", "currentCheckpoint", "sealedReview", "sealedMessage"])
    && isRecord(value.provider)
    && Array.isArray(value.groups)
    && Array.isArray(value.limitations)
    && typeof value.trialsUsed === "number"
    && typeof value.trialLimit === "number";
}

async function request<T>(path: string, init: RequestInit, validate: (value: unknown) => value is T): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "content-type": "application/json", ...(init.headers ?? {}) },
      credentials: "same-origin",
    });
  } catch {
    throw new ResearchOSClientError("研究服务当前不可用，请稍后重试或使用已批准的离线模式。");
  }
  if (!response.ok) throw new ResearchOSClientError(`研究服务拒绝了请求（${response.status}）。`, response.status);
  const payload: unknown = await response.json();
  if (!validate(payload)) throw new ResearchOSClientError("研究服务返回了不完整或不受支持的数据，页面已停止处理。");
  return payload;
}

export function createResearchOSV2HttpClient(basePath = "/api/veriquant/research-os/v2"): ResearchOSV2Client {
  const base = basePath.replace(/\/$/, "");
  return {
    createSuggestion(input: CreateSuggestionInput) {
      return request(`${base}/suggestions`, { method: "POST", body: JSON.stringify(input) }, isSuggestion);
    },
    confirmPlan(suggestion, input) {
      return request(`${base}/plans/confirm`, { method: "POST", body: JSON.stringify({ suggestion, ...input }) }, isPlan);
    },
    startResearch(planId) {
      return request(`${base}/runs`, { method: "POST", body: JSON.stringify({ planId }) }, isRun);
    },
    getResearchStatus(runId, signal) {
      return request(`${base}/runs/${encodeURIComponent(runId)}`, { method: "GET", signal }, isRun);
    },
  };
}

export const defaultResearchOSV2Client = createResearchOSV2HttpClient();
