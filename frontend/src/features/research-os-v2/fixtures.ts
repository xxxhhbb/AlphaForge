import type {
  CreateSuggestionInput,
  ResearchOSV2Client,
  ResearchPlanView,
  ResearchRunView,
  ResearchSuggestionView,
} from "./types";

const wait = () => Promise.resolve();

function makeSuggestion(input: CreateSuggestionInput): ResearchSuggestionView {
  const normalized = input.question.trim();
  const hasNorthbound = /北向|陆股通/.test(normalized);
  return {
    suggestionId: "suggestion-momentum-50-v2",
    question: normalized,
    researchMode: /失效|回撤|诊断/.test(normalized) ? "因子与组合诊断" : "新 Alpha 探索",
    researchObject: /50|五十/.test(normalized) ? "A 股 50 日价格动量" : "A 股横截面 Alpha",
    canRun: true,
    answerLevel: hasNorthbound ? "辅助研究" : "机器评价",
    rationale: hasNorthbound
      ? "已批准数据中缺少可确认发布时间的北向资金字段；仍可提供机制分析、数据补充计划和可验证代理变量。"
      : "价格、成交量、历史股票池和交易日历可支持受控计算，并可由机器证据服务验证候选。",
    selectedGroups: ["市场行为研究组", "交易可行性研究组", "组合增量研究组"],
    plannedExperiments: [
      "比较原始动量与行业中性动量",
      "检验换手、冲击成本和子时期稳定性",
      "检验与已登记动量基线的相关性和增量性",
    ],
    dataCoverage: [
      { field: "价格与复权", availability: "可用", coverageLabel: "50 日观察窗及预热期", sourceLabel: "已批准代理数据源" },
      { field: "成交量与成交额", availability: "可用", coverageLabel: "与价格同频", sourceLabel: "已批准代理数据源" },
      { field: "历史股票池与交易日历", availability: "可用", coverageLabel: "按决策时点冻结", sourceLabel: "SSE / SZSE 冻结日历" },
      { field: "行业与市值", availability: "部分可用", coverageLabel: "样本覆盖随日期变化", sourceLabel: "已批准代理数据源", limitation: "缺失记录不会被静默填充" },
    ],
    dataGaps: hasNorthbound ? ["北向资金逐笔可得时间尚未冻结，不能生成官方评价。"] : [],
    dataPlan: {
      planHash: "sha256:dynamic-data-plan-momentum-50-v1",
      requestedApis: hasNorthbound
        ? ["trade_cal", "stock_basic", "daily", "adj_factor", "daily_basic", "moneyflow"]
        : ["trade_cal", "stock_basic", "daily", "adj_factor", "daily_basic"],
      requestedFields: hasNorthbound
        ? ["trade_date", "ts_code", "close", "vol", "amount", "adj_factor", "circ_mv", "net_mf_amount"]
        : ["trade_date", "ts_code", "close", "vol", "amount", "adj_factor", "circ_mv"],
      minimumSessions: 295,
      currentSessions: hasNorthbound ? 260 : 360,
      universe: { kind: "HISTORICAL_ALL_A_SHARE", label: "A 股历史可交易股票池" },
      acquisitionRequired: hasNorthbound,
      readiness: hasNorthbound ? "PARTIAL" : "READY",
      missingRequirements: hasNorthbound ? ["北向资金逐笔数据及其可得时间语义"] : [],
      degradationSuggestions: hasNorthbound ? ["先以已冻结的日频资金流代理变量进行辅助研究"] : [],
      researchableParts: ["价格动量", "成交与换手约束", "行业与市值中性化"],
    },
    provider: {
      mode: "确定性回退",
      providerLabel: "离线可复现研究建议",
      reason: "测试 fixture 未调用外部模型服务",
    },
  };
}

function makePlan(suggestion: ResearchSuggestionView, input: Pick<CreateSuggestionInput, "depth" | "collaborationStyle">): ResearchPlanView {
  return {
    ...suggestion,
    planId: "plan-momentum-50-v2",
    state: "已确认",
    depth: input.depth,
    collaborationStyle: input.collaborationStyle,
    universe: "A 股历史可交易股票池（按日冻结）",
    horizon: "50 日观察窗 / 5 日预测期",
    budget: { groups: 3, candidates: 6, trials: 12 },
    checkpoints: ["候选短名单", "机器评价完成", "独立封闭评审前"],
    stopConditions: ["试验预算耗尽", "没有可安全编译的候选", "用户停止", "验证完成"],
    planHash: "sha256:4c8cf1f8195f3c9a",
  };
}

const completedGroup = {
  groupId: "market-behaviour",
  title: "市场行为研究组",
  purpose: "从价格延续、反转边界和状态依赖提出候选。",
  state: "已完成" as const,
  budgetUsed: 2,
  budgetLimit: 4,
  progress: ["提出可证伪假设", "形成受限表达式", "完成机器评价"],
  candidates: [
    {
      candidateId: "momentum-50-neutral",
      title: "行业中性 50 日动量",
      hypothesis: "剔除行业共同波动后，中期价格延续仍包含横截面预测信息。",
      expression: "行业中性化(横截面排序(50日收益率), 行业)",
      decision: "研究验证通过" as const,
      decisionExplanation: "可见训练与验证证据达到研究验证标准；尚未完成独立封闭评审。",
      metricSummary: { netIcir: 0.61, stability: 0.68, turnover: 0.27, incrementalScore: 0.43 },
      evidenceHash: "sha256:evidence-visible-01",
    },
  ],
};

export function makeCompletedRun(planId = "plan-momentum-50-v2"): ResearchRunView {
  return {
    runId: "run-momentum-50-v2",
    planId,
    state: "已完成",
    provider: {
      mode: "确定性回退",
      providerLabel: "离线可复现研究建议",
      modelLabel: "测试 fixture；不冒充真实模型",
    },
    groups: [
      completedGroup,
      {
        groupId: "trade-feasibility",
        title: "交易可行性研究组",
        purpose: "评估换手、流动性、交易成本和容量。",
        state: "需修订",
        budgetUsed: 2,
        budgetLimit: 4,
        progress: ["提出可证伪假设", "表达式安全检查未通过"],
        candidates: [{
          candidateId: "liquidity-adjusted-momentum",
          title: "流动性调整动量",
          hypothesis: "成交活跃度可以区分可交易和不可交易的动量。",
          expression: "待修订：使用了未登记的流动性算子",
          decision: "需修订",
          decisionExplanation: "候选无法转换为当前白名单表达式，未伪造评价结果。",
        }],
        failure: "表达式需要改写后才能进入机器评价。",
      },
      {
        groupId: "portfolio-increment",
        title: "组合增量研究组",
        purpose: "检验候选是否只是复制已有动量暴露。",
        state: "已阻断",
        budgetUsed: 1,
        budgetLimit: 4,
        progress: ["读取允许公开的因子摘要"],
        candidates: [],
        failure: "已有组合相关性矩阵不完整；该分支已关闭，其他分支结果仍然有效。",
      },
    ],
    trialsUsed: 5,
    trialLimit: 12,
    currentCheckpoint: "机器评价与报告发布已完成",
    sealedReview: "等待独立评审",
    sealedMessage: "封闭数据与指标不可见；这里只显示是否已进入独立评审。",
    formalReportPath: "/reports/run-momentum-50-v2",
    reportHash: "sha256:formal-report-v2-01",
    limitations: ["行业与市值覆盖率随日期变化。", "当前结论不代表晋级，也不构成投资建议。"],
  };
}

export function makeRunningRun(planId = "plan-momentum-50-v2"): ResearchRunView {
  return {
    runId: "run-momentum-50-v2",
    planId,
    state: "排队中",
    provider: {
      mode: "确定性回退",
      providerLabel: "离线可复现研究建议",
      modelLabel: "测试 fixture；不冒充真实模型",
    },
    groups: [
      {
        groupId: "market-behaviour",
        title: "市场行为研究组",
        purpose: "等待受控研究资源。",
        state: "等待中",
        budgetUsed: 0,
        budgetLimit: 4,
        progress: [],
        candidates: [],
      },
    ],
    trialsUsed: 0,
    trialLimit: 12,
    currentCheckpoint: "研究任务已进入队列",
    sealedReview: "未申请",
    sealedMessage: "尚未进入独立封闭评审。",
    limitations: [],
  };
}

export const deterministicResearchOSV2Client: ResearchOSV2Client = {
  async createSuggestion(input) {
    await wait();
    if (!input.question.trim()) throw new Error("请先描述需要解决的 Alpha 研究问题。");
    return makeSuggestion(input);
  },
  async confirmPlan(suggestion, input) {
    await wait();
    return makePlan(suggestion, input);
  },
  async startResearch(planId) {
    await wait();
    return makeRunningRun(planId);
  },
  async getResearchStatus() {
    await wait();
    return makeCompletedRun();
  },
};
