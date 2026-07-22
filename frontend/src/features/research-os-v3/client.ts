import { normalizeResearchOSV3Run } from "./normalize";
import type { RawResearchOSV3Envelope, ResearchOSV3ResultClient, ResearchOSV3RunView } from "./types";

export class ResearchOSV3ClientError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "ResearchOSV3ClientError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isSupportedEnvelope(value: unknown): value is RawResearchOSV3Envelope {
  if (!isRecord(value)) return false;
  const result = isRecord(value.result) ? value.result : value;
  return typeof result.run_id === "string"
    && typeof result.status === "string"
    && Array.isArray(result.branches);
}

async function fetchResult(path: string, signal?: AbortSignal): Promise<ResearchOSV3RunView> {
  let response: Response;
  try {
    response = await fetch(path, { method: "GET", credentials: "same-origin", signal });
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "AbortError") throw reason;
    throw new ResearchOSV3ClientError("研究结果服务当前不可用，请稍后重试。");
  }
  if (!response.ok) throw new ResearchOSV3ClientError(`研究结果服务拒绝了请求（${response.status}）。`, response.status);
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ResearchOSV3ClientError("研究结果服务没有返回可解析的数据。", response.status);
  }
  if (!isSupportedEnvelope(payload)) {
    throw new ResearchOSV3ClientError("研究结果缺少运行标识、状态或专业研究组记录，页面已停止处理。", response.status);
  }
  return normalizeResearchOSV3Run(payload);
}

export function createResearchOSV3HttpClient(basePath = "/api/veriquant/research-os/v3"): ResearchOSV3ResultClient {
  const base = basePath.replace(/\/$/, "");
  return {
    getResearchResult(runId, signal) {
      const normalized = runId.trim();
      if (!normalized) return Promise.reject(new ResearchOSV3ClientError("缺少研究运行标识。"));
      return fetchResult(`${base}/runs/${encodeURIComponent(normalized)}`, signal);
    },
  };
}

export const defaultResearchOSV3Client = createResearchOSV3HttpClient();
