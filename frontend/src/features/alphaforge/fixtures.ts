import { advancedResearchModes, researchModes } from "./catalog";
import type {
  CampaignBudgetView,
  CampaignDeskView,
  CampaignDraftInput,
  CampaignDraftView,
  CampaignRunView,
  FactorLibraryCatalogView,
  FactorLibraryItemView,
  ResearchDepthId,
} from "./types";

const budgetByDepth: Record<ResearchDepthId, CampaignBudgetView> = {
  QUICK_EXPLORATION: { factorFamilies: 1, trials: 1, modelCalls: 4, runtimeMinutes: 1 },
  STANDARD_RESEARCH: { factorFamilies: 3, trials: 12, modelCalls: 12, runtimeMinutes: 5 },
  STRICT_VALIDATION: { factorFamilies: 4, trials: 24, modelCalls: 24, runtimeMinutes: 10 },
};

export function stableCanonicalJson(value: unknown): string {
  const normalize = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(normalize);
    if (item && typeof item === "object") {
      return Object.fromEntries(
        Object.entries(item as Record<string, unknown>)
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([key, child]) => [key, normalize(child)]),
      );
    }
    return item;
  };
  return JSON.stringify(normalize(value));
}

export function fixtureFingerprint(value: unknown): string {
  const source = stableCanonicalJson(value);
  let left = 0x811c9dc5;
  let right = 0x9e3779b9;
  for (let index = 0; index < source.length; index += 1) {
    const code = source.charCodeAt(index);
    left = Math.imul(left ^ code, 0x01000193) >>> 0;
    right = Math.imul(right ^ (code + index), 0x85ebca6b) >>> 0;
  }
  return `fixture:${left.toString(16).padStart(8, "0")}${right.toString(16).padStart(8, "0")}`;
}

function deskNameToId(name: string): string {
  const mapping: Record<string, string> = {
    基本面: "fundamental",
    预期事件: "expectations_events",
    市场结构: "market_structure",
    另类数据: "alternative_data",
    宏观状态: "macro_regime",
    元阿尔法: "meta_alpha",
    原始领域: "market_structure",
    相关领域: "fundamental",
  };
  return mapping[name] ?? "market_structure";
}

export function createOfflineDraft(input: CampaignDraftInput): CampaignDraftView {
  const mode = [...researchModes, ...advancedResearchModes].find((item) => item.id === input.mode) ?? researchModes[0];
  const payload = {
    question: input.question.trim(),
    mode: input.mode,
    depth: input.depth,
    interactionStyle: input.interactionStyle,
    methods: [...mode.methods],
    desks: [...mode.desks],
  };
  const fingerprint = fixtureFingerprint(payload);
  const quick = input.depth === "QUICK_EXPLORATION";
  const horizonMatch = /(\d+)\s*(?:日|天|d)/i.exec(payload.question);
  return {
    schemaVersion: "alphaforge.campaign-ui/v1",
    draftId: `draft_${fingerprint.slice(8)}`,
    state: "NEEDS_CONFIRMATION",
    question: payload.question,
    mode: input.mode,
    depth: input.depth,
    interactionStyle: input.interactionStyle,
    researchObject: mode.systemWork,
    universe: ["CN_ASHARE_APPROVED_PIT_SAMPLE"],
    horizon: horizonMatch ? `${horizonMatch[1]}日` : "20日",
    allowedData: ["价格与成交量", "历史股票池", "行业与市值", "可得时间字段"],
    methodModels: [...mode.methods],
    selectedDesks: mode.desks.map(deskNameToId).filter((desk, index, items) => items.indexOf(desk) === index),
    budget: { ...budgetByDepth[input.depth] },
    checkpoints: quick
      ? ["研究简报后"]
      : ["研究简报后", "候选短名单后", ...(input.depth === "STRICT_VALIDATION" ? ["封闭验证前", "独立审批前"] : [])],
    stopConditions: ["预算耗尽", "用户停止", "研究目标已完成"],
    runtimeMode: "DETERMINISTIC",
    allowsOfficialEvidence: !quick,
    allowsPromotion: false,
    fixtureFingerprint: fingerprint,
  };
}

function stage(id: string, title: string, status: CampaignDeskView["stages"][number]["status"], detail: string) {
  return { id, title, status, detail };
}

export const offlineRunFixture: CampaignRunView = {
  campaignId: "campaign_af_offline_20260719",
  state: "AWAITING_CHECKPOINT",
  runtimeMode: "DETERMINISTIC",
  selectedDesks: [
    {
      id: "fundamental",
      title: "基本面研究 Desk",
      status: "COMPLETED",
      runtimeMode: "DETERMINISTIC",
      budgetUsed: 3,
      budgetLimit: 4,
      stages: [
        stage("hypothesis", "提出可证伪假设", "COMPLETED", "已提交结构化研究草案。"),
        stage("construct", "构造因子草案", "COMPLETED", "只生成受限 FactorDAG 草案。"),
        stage("compile", "编译为 PIT 任务", "COMPLETED", "由确定性服务完成。"),
        stage("evaluate", "研究评价", "COMPLETED", "只展示允许公开的摘要。"),
        stage("feedback", "脱敏反馈", "COMPLETED", "反馈不含封闭信息。"),
      ],
    },
    {
      id: "market_structure",
      title: "市场结构 Desk",
      status: "FAILED",
      runtimeMode: "REAL_LLM",
      budgetUsed: 2,
      budgetLimit: 4,
      failure: "候选表达式包含未登记算子，已关闭该分支；不会伪造成完成。",
      stages: [
        stage("hypothesis", "提出可证伪假设", "COMPLETED", "已提交结构化假设。"),
        stage("construct", "构造因子草案", "COMPLETED", "草案进入编译边界。"),
        stage("compile", "编译为 PIT 任务", "FAILED", "未知算子被拒绝，失败照常消耗预算。"),
        stage("evaluate", "研究评价", "BLOCKED", "上游失败，未伪造评价结果。"),
        stage("feedback", "脱敏反馈", "BLOCKED", "依赖任务未启动。"),
      ],
    },
    {
      id: "meta_alpha",
      title: "元阿尔法 Desk",
      status: "RUNNING",
      runtimeMode: "DETERMINISTIC",
      budgetUsed: 1,
      budgetLimit: 4,
      stages: [
        stage("hypothesis", "分析组合缺口", "COMPLETED", "已读取允许公开的因子摘要。"),
        stage("construct", "构造增量候选", "RUNNING", "正在形成候选草案。"),
        stage("compile", "编译为 PIT 任务", "WAITING", "等待候选草案。"),
        stage("evaluate", "研究评价", "WAITING", "尚未启动。"),
        stage("feedback", "脱敏反馈", "WAITING", "尚未启动。"),
      ],
    },
  ],
  budget: { factorFamilies: 3, trials: 12, modelCalls: 12, runtimeMinutes: 5 },
  trialsUsed: 5,
  currentCheckpoint: "候选短名单后",
  checkpointState: "REACHED",
  messages: ["一个 Desk 失败，另外两个独立分支继续。", "系统已暂停，等待用户确认检查点。"],
  reportPath: "/reports",
  workbenchPath: "/veriquant/workbench",
};

type FamilySeed = {
  family: FactorLibraryItemView["sourceFamily"];
  count: number;
  title: string;
  themes: string[];
};

const familySeeds: FamilySeed[] = [
  { family: "academic", count: 11, title: "学术异象", themes: ["质量", "价值", "动量"] },
  { family: "alpha101", count: 101, title: "Alpha101", themes: ["价格", "成交量", "反转", "波动率"] },
  { family: "fundamental", count: 4, title: "PIT 基本面", themes: ["基本面", "质量"] },
  { family: "gtja191", count: 191, title: "GTJA191", themes: ["微观结构", "动量", "流动性"] },
  { family: "qlib158", count: 154, title: "Qlib158", themes: ["技术面", "量价", "风险"] },
];

function qualificationForIndex(index: number): Pick<
  FactorLibraryItemView,
  "eligibility" | "qualificationStatus" | "compatibilityClass" | "lineage" | "visibleSummary"
> {
  if (index < 21) {
    return {
      eligibility: "DAG_PORTED",
      qualificationStatus: "DIRECT_FACTOR_DAG",
      compatibilityClass: "directly_representable",
      lineage: "static_catalog -> direct_factor_dag_seed",
      visibleSummary: "可作为受限 FactorDAG 草案种子；仍需 Campaign、PIT、Trial Ledger 与官方 Evidence。",
    };
  }
  if (index < 411) {
    return {
      eligibility: "METADATA_VALIDATED",
      qualificationStatus: "NEEDS_DETERMINISTIC_OPERATOR",
      compatibilityClass: "needs_deterministic_operator",
      lineage: "static_catalog -> metadata_validated",
      visibleSummary: "静态元数据可浏览，但需要新增审计过的确定性算子后才能进入 FactorDAG。",
    };
  }
  return {
    eligibility: "AUXILIARY_ONLY",
    qualificationStatus: "UNSAFE_AUXILIARY_ONLY",
    compatibilityClass: "auxiliary_only",
    lineage: "static_catalog -> auxiliary_only_never_official",
    visibleSummary: "只能作为研究背景，不得直接运行源码、引用旧 Qlib/bench 结果或充当官方 Evidence。",
  };
}

function buildOfflineFactorLibrary(): FactorLibraryItemView[] {
  const items: FactorLibraryItemView[] = [];
  for (const seed of familySeeds) {
    for (let localIndex = 1; localIndex <= seed.count; localIndex += 1) {
      const globalIndex = items.length;
      const qualification = qualificationForIndex(globalIndex);
      const factorId = `${seed.family}_${String(localIndex).padStart(3, "0")}`;
      items.push({
        factorId,
        displayName: `${seed.title} ${String(localIndex).padStart(3, "0")} 静态目录项`,
        theme: seed.themes[globalIndex % seed.themes.length],
        sourceFamily: seed.family,
        actions: ["validate", "improve", "diagnose", "gap"],
        ...qualification,
      });
    }
  }
  return items;
}

export const offlineFactorLibrary: FactorLibraryItemView[] = buildOfflineFactorLibrary();

export const offlineFactorCatalog: FactorLibraryCatalogView = {
  schemaVersion: "alphaforge.factor-library-ui/v1",
  total: 461,
  generatedFrom: "static_factor_library_qualification",
  catalogHash: "sha256:264c9d9a5205e8379d7aeb7305923b5572e8e95bddb59c3b7e9abbd76ebc2104",
  qualificationHash: "sha256:48aaf38321ecdfe71064eabecc3785be08bdef2df12a37a38c5d259c8a4e9dd2",
  items: offlineFactorLibrary,
};
