import { createOfflineDraft, offlineFactorLibrary, offlineRunFixture } from "./fixtures";
import type {
  CampaignApiAdapter,
  CampaignDraftInput,
  CampaignDraftView,
  CampaignRecommendation,
  CampaignProviderStatusView,
  CampaignRunView,
  FactorLibraryItemView,
} from "./types";

export const ALPHAFORGE_HTTP_ENDPOINTS = {
  draft: "/veriquant/campaign/drafts",
  recommendation: "/veriquant/campaign/recommendations",
  confirmation: "/veriquant/campaign/confirmations",
  runPrefix: "/veriquant/campaign/runs/",
  runStartSuffix: "/start",
  providerStatus: "/veriquant/campaign/llm/provider-status",
  factorLibrary: "/veriquant/factor-library",
} as const;

const forbiddenKey =
  /(?:sealed|secret|token|api[_-]?key|authorization|cookie|provider[_-]?raw|raw[_-]?response|official[_-]?metrics?|promotion|position(?:s)?|target[_-]?weights?|orders?|fills?|broker|risk[_-]?approval|ledger)/i;
const forbiddenText =
  /(?:sealed[_ -]?(?:metric|test|rank|result)|封闭测试(?:指标|排名|内容|结果)?|official[_ -]?metrics?|promotion[_ -]?(?:decision|status)?|target[_ -]?weights?|orders?|positions?|authorization\s*:|api[_ -]?key\s*:|bearer\s+[a-z0-9._~+/=-]+)/i;

export function sanitizePublicPayload<T>(value: T): T {
  const visit = (item: unknown): unknown => {
    if (typeof item === "string") {
      return forbiddenText.test(item) ? "[敏感内容已移除]" : item;
    }
    if (Array.isArray(item)) return item.map(visit);
    if (item && typeof item === "object") {
      return Object.fromEntries(
        Object.entries(item as Record<string, unknown>)
          .filter(([key]) => !forbiddenKey.test(key))
          .map(([key, child]) => [key, visit(child)]),
      );
    }
    return item;
  };
  return visit(value) as T;
}

function copy<T>(value: T): T {
  return sanitizePublicPayload(structuredClone(value));
}

async function readFixedJson<T>(url: string, init: RequestInit = {}, fetchFn: typeof fetch = fetch): Promise<T> {
  const response = await fetchFn(url, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) throw new Error(`AlphaForge 接口暂不可用（${response.status}）`);
  return sanitizePublicPayload(await response.json()) as T;
}

function postJson<T>(url: string, body: unknown, fetchFn?: typeof fetch): Promise<T> {
  return readFixedJson<T>(
    url,
    { method: "POST", body: JSON.stringify(sanitizePublicPayload(body)) },
    fetchFn,
  );
}

export class OfflineCampaignApiAdapter implements CampaignApiAdapter {
  async createDraft(input: CampaignDraftInput): Promise<CampaignDraftView> {
    if (!input.question.trim()) throw new Error("请先填写研究问题");
    return copy(createOfflineDraft(input));
  }

  async recommend(question: string): Promise<CampaignRecommendation> {
    const text = question.trim();
    const diagnosis = /为什么|失效|回撤|衰减|诊断/.test(text);
    return copy({
      recommendedMode: diagnosis ? "DIAGNOSE_FACTOR_PORTFOLIO" : "NEW_ALPHA_EXPLORATION",
      explanation: diagnosis
        ? "你的问题更像因子或组合诊断，需要先定位表现变化与根因。"
        : "可以先从新 Alpha 探索开始，但必须确认研究对象、数据范围和预算。",
      questions: ["你希望研究单个因子、已有组合，还是新的股票信号？", "预测周期和股票池是什么？"],
      canRun: false,
    });
  }

  async confirm(draft: CampaignDraftView): Promise<CampaignDraftView> {
    if (draft.state !== "NEEDS_CONFIRMATION") throw new Error("Campaign 状态不允许重复确认");
    if (!draft.universe.length || !draft.horizon || draft.budget.trials < 1 || !draft.stopConditions.length) {
      throw new Error("Campaign 缺少股票池、周期、预算或停止条件");
    }
    return copy({ ...draft, state: "CONFIRMED", campaignId: `campaign_${draft.draftId.replace(/^draft_/, "")}` });
  }

  async getRun(campaignId: string): Promise<CampaignRunView> {
    if (!campaignId.trim()) throw new Error("缺少 Campaign 标识");
    return copy({ ...offlineRunFixture, campaignId });
  }

  async getProviderStatus(): Promise<CampaignProviderStatusView> {
    return { status: "UNAVAILABLE", reasonCode: "OFFLINE_RUNTIME" };
  }

  async startRun(campaignId: string, runtimeMode: "DETERMINISTIC" | "REAL_LLM" = "DETERMINISTIC"): Promise<CampaignRunView> {
    if (!campaignId.trim()) throw new Error("缺少 Campaign 标识");
    if (runtimeMode !== "DETERMINISTIC") throw new Error("离线模式不提供真实 LLM 研究");
    return copy({ ...offlineRunFixture, campaignId, state: "COMPLETED", checkpointState: "REACHED" });
  }

  async listFactors(): Promise<FactorLibraryItemView[]> {
    return copy(offlineFactorLibrary);
  }
}

export class HttpCampaignApiAdapter implements CampaignApiAdapter {
  constructor(private readonly fetchFn: typeof fetch = fetch) {}

  createDraft(input: CampaignDraftInput): Promise<CampaignDraftView> {
    return postJson<CampaignDraftView>(ALPHAFORGE_HTTP_ENDPOINTS.draft, {
      question: input.question,
      mode: input.mode,
      depth: input.depth,
      interactionStyle: input.interactionStyle,
    }, this.fetchFn);
  }

  recommend(question: string): Promise<CampaignRecommendation> {
    return postJson<CampaignRecommendation>(ALPHAFORGE_HTTP_ENDPOINTS.recommendation, { question }, this.fetchFn);
  }

  confirm(draft: CampaignDraftView): Promise<CampaignDraftView> {
    return postJson<CampaignDraftView>(ALPHAFORGE_HTTP_ENDPOINTS.confirmation, {
      draftId: draft.draftId,
      campaignId: draft.campaignId,
      state: draft.state,
      question: draft.question,
      mode: draft.mode,
      depth: draft.depth,
      interactionStyle: draft.interactionStyle,
      researchObject: draft.researchObject,
      universe: draft.universe,
      horizon: draft.horizon,
      allowedData: draft.allowedData,
      methodModels: draft.methodModels,
      selectedDesks: draft.selectedDesks,
      budget: draft.budget,
      checkpoints: draft.checkpoints,
      stopConditions: draft.stopConditions,
      runtimeMode: draft.runtimeMode,
      allowsOfficialEvidence: draft.allowsOfficialEvidence,
      allowsPromotion: false,
      fixtureFingerprint: draft.fixtureFingerprint,
    }, this.fetchFn);
  }

  getRun(campaignId: string): Promise<CampaignRunView> {
    const safeId = encodeURIComponent(campaignId.trim());
    if (!safeId) throw new Error("缺少 Campaign 标识");
    return readFixedJson<CampaignRunView>(`${ALPHAFORGE_HTTP_ENDPOINTS.runPrefix}${safeId}`, {}, this.fetchFn);
  }

  getProviderStatus(): Promise<CampaignProviderStatusView> {
    return readFixedJson<CampaignProviderStatusView>(ALPHAFORGE_HTTP_ENDPOINTS.providerStatus, {}, this.fetchFn);
  }

  startRun(campaignId: string, runtimeMode: "DETERMINISTIC" | "REAL_LLM" = "DETERMINISTIC"): Promise<CampaignRunView> {
    const safeId = encodeURIComponent(campaignId.trim());
    if (!safeId) throw new Error("缺少 Campaign 标识");
    return readFixedJson<CampaignRunView>(
      `${ALPHAFORGE_HTTP_ENDPOINTS.runPrefix}${safeId}${ALPHAFORGE_HTTP_ENDPOINTS.runStartSuffix}`,
      { method: "POST", body: JSON.stringify({ runtimeMode }) },
      this.fetchFn,
    );
  }

  async listFactors(): Promise<FactorLibraryItemView[]> {
    const payload = await readFixedJson<{ items?: FactorLibraryItemView[]; factors?: FactorLibraryItemView[] }>(
      ALPHAFORGE_HTTP_ENDPOINTS.factorLibrary,
      {},
      this.fetchFn,
    );
    return Array.isArray(payload.items) ? payload.items : Array.isArray(payload.factors) ? payload.factors : [];
  }
}

export class AutoCampaignApiAdapter implements CampaignApiAdapter {
  private readonly http: HttpCampaignApiAdapter;
  private readonly offline: OfflineCampaignApiAdapter;

  constructor(fetchFn: typeof fetch = fetch) {
    this.http = new HttpCampaignApiAdapter(fetchFn);
    this.offline = new OfflineCampaignApiAdapter();
  }

  private async preferHttp<T>(operation: () => Promise<T>, fallback: () => Promise<T>): Promise<T> {
    try {
      return await operation();
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      const unavailable =
        reason instanceof TypeError ||
        /(?:Failed to fetch|NetworkError|Load failed|接口暂不可用.*(?:404|500|502|503|504)|鎺ュ彛鏆備笉鍙敤.*(?:404|500|502|503|504))/i.test(message);
      if (!unavailable) throw reason;
      return fallback();
    }
  }

  createDraft(input: CampaignDraftInput): Promise<CampaignDraftView> {
    return this.preferHttp(() => this.http.createDraft(input), () => this.offline.createDraft(input));
  }

  recommend(question: string): Promise<CampaignRecommendation> {
    return this.preferHttp(() => this.http.recommend(question), () => this.offline.recommend(question));
  }

  confirm(draft: CampaignDraftView): Promise<CampaignDraftView> {
    return this.preferHttp(() => this.http.confirm(draft), () => this.offline.confirm(draft));
  }

  getRun(campaignId: string): Promise<CampaignRunView> {
    // Runtime state is financial-research control state. Never replace a
    // missing or failed server run with the demonstration fixture.
    return this.http.getRun(campaignId);
  }

  getProviderStatus(): Promise<CampaignProviderStatusView> {
    return this.preferHttp(() => this.http.getProviderStatus(), () => this.offline.getProviderStatus());
  }

  startRun(campaignId: string, runtimeMode: "DETERMINISTIC" | "REAL_LLM" = "DETERMINISTIC"): Promise<CampaignRunView> {
    // Starting a Campaign must fail closed when the backend is unavailable.
    return this.http.startRun(campaignId, runtimeMode);
  }

  listFactors(): Promise<FactorLibraryItemView[]> {
    return this.preferHttp(() => this.http.listFactors(), () => this.offline.listFactors());
  }
}

export function createCampaignApiAdapter(runtime: "offline" | "http" | "auto" = "auto"): CampaignApiAdapter {
  if (runtime === "http") return new HttpCampaignApiAdapter();
  if (runtime === "offline") return new OfflineCampaignApiAdapter();
  return new AutoCampaignApiAdapter();
}

// The deployed product always reflects the backend of record. Tests and
// deliberate offline demos can still inject OfflineCampaignApiAdapter.
export const defaultCampaignApi: CampaignApiAdapter = new HttpCampaignApiAdapter();
