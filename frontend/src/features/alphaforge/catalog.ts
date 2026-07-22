import type {
  FactorLibraryActionId,
  InteractionStyleOption,
  ResearchDepthOption,
  ResearchModeOption,
} from "./types";

export const researchModes: ResearchModeOption[] = [
  {
    id: "NEW_ALPHA_EXPLORATION",
    title: "新 Alpha 探索",
    exampleQuestion: "帮我找新的 20 日股票因子",
    systemWork: "在已批准的数据与预算内提出多类可证伪假设。",
    output: "候选因子清单与研究路线",
    methods: ["假设驱动", "受限异常探索", "对抗证伪"],
    desks: ["市场结构", "基本面", "元阿尔法"],
  },
  {
    id: "VALIDATE_IDEA",
    title: "验证我的想法",
    exampleQuestion: "盈利上调是否能预测未来收益？",
    systemWork: "把用户想法转成可证伪假设，并按预注册规则实验。",
    output: "支持、拒绝或条件成立",
    methods: ["假设驱动", "对抗证伪"],
    desks: ["基本面", "预期事件", "市场结构"],
  },
  {
    id: "IMPROVE_EXISTING_FACTOR",
    title: "改进现有因子",
    exampleQuestion: "这个反转因子能否降低换手？",
    systemWork: "先绑定父因子，再进行有预算的受限变体研究。",
    output: "新旧版本对比报告",
    methods: ["受限演化", "父子比较", "成本容量检查"],
    desks: ["原始领域", "市场结构", "元阿尔法"],
  },
  {
    id: "PORTFOLIO_GAP_SEARCH",
    title: "补充组合缺口",
    exampleQuestion: "找一个与动量、价值低相关的 Alpha",
    systemWork: "从已有因子与组合残差出发寻找增量候选。",
    output: "增量 Alpha 候选与相关性说明",
    methods: ["残差增量", "假设驱动", "增量性检验"],
    desks: ["元阿尔法", "市场结构", "相关领域"],
  },
  {
    id: "DIAGNOSE_FACTOR_PORTFOLIO",
    title: "诊断因子/组合",
    exampleQuestion: "最近动量因子为什么失效？",
    systemWork: "检查归因、市场状态、成本、拥挤与数据问题。",
    output: "根因排序与处理建议",
    methods: ["诊断归因", "市场状态分析", "对抗证伪"],
    desks: ["元阿尔法", "市场结构", "宏观状态"],
  },
  {
    id: "EVENT_MARKET_TOPIC",
    title: "事件与市场专题",
    exampleQuestion: "财报后漂移在高波动期是否更强？",
    systemWork: "区分事件发生、市场可得与实际可交易时间。",
    output: "条件化因子或专题报告",
    methods: ["事件研究", "状态条件", "对抗证伪"],
    desks: ["预期事件", "宏观状态", "市场结构"],
  },
];

export const advancedResearchModes: ResearchModeOption[] = [
  {
    id: "PAPER_STRATEGY_REPLICATION",
    title: "论文/策略复现",
    exampleQuestion: "复现这篇论文中的因子定义",
    systemWork: "记录原始定义、数据映射、时间语义和实现差异。",
    output: "复现结论与差异报告",
    methods: ["受控复现"],
    desks: ["相关领域", "市场结构"],
    advanced: true,
  },
  {
    id: "NEW_DATA_SOURCE_EVALUATION",
    title: "新数据源评估",
    exampleQuestion: "评估这个数据源能否安全用于研究",
    systemWork: "检查来源、可得时间、覆盖率、偏差和增量价值。",
    output: "数据集评估报告",
    methods: ["来源审计", "PIT 评估"],
    desks: ["相关领域", "元阿尔法"],
    advanced: true,
  },
  {
    id: "PRELAUNCH_REVIEW",
    title: "上线前评审",
    exampleQuestion: "评审候选因子的成本、容量与稳定性",
    systemWork: "检查成本、容量、压力情景和独立审批条件。",
    output: "上线前评审报告",
    methods: ["容量与风险评审"],
    desks: ["市场结构", "元阿尔法"],
    advanced: true,
  },
  {
    id: "MONITOR_DECAY_RETIREMENT",
    title: "监控、衰减与退役",
    exampleQuestion: "这个因子是否已经漂移并需要退役？",
    systemWork: "比较研究基线、检测漂移，并形成供人决策的建议。",
    output: "监控与处置建议",
    methods: ["在线监控", "诊断归因"],
    desks: ["元阿尔法", "市场结构", "宏观状态"],
    advanced: true,
  },
];

export const researchDepths: ResearchDepthOption[] = [
  {
    id: "QUICK_EXPLORATION",
    title: "快速探索",
    description: "形成假设、候选草案和下一步研究路线。",
    boundary: "只停留在草案层，不启动确定性评价。",
  },
  {
    id: "STANDARD_RESEARCH",
    title: "标准研究",
    description: "完成 PIT、walk-forward、Critic 与增量性检验。",
    boundary: "只向研究员返回经过脱敏的可见摘要。",
  },
  {
    id: "STRICT_VALIDATION",
    title: "严格验证",
    description: "增加独立审批、容量、成本、压力与封闭验证。",
    boundary: "sealed 内容由独立控制器持有，不进入研究上下文。",
  },
];

export const interactionStyles: InteractionStyleOption[] = [
  { id: "GUIDED", title: "引导式", description: "每个重要节点都解释并等待确认。" },
  { id: "STANDARD", title: "标准式", description: "确认研究简报、候选短名单和验证结论。" },
  { id: "BATCH", title: "批处理式", description: "在预算内运行到约定检查点后停止。" },
];

export const deskLabels: Record<string, string> = {
  fundamental: "基本面研究 Desk",
  expectations_events: "预期与事件 Desk",
  market_structure: "市场结构 Desk",
  alternative_data: "另类数据 Desk",
  macro_regime: "宏观状态 Desk",
  meta_alpha: "元阿尔法 Desk",
};

export const factorActionLabels: Record<FactorLibraryActionId, string> = {
  validate: "验证",
  improve: "改进",
  diagnose: "诊断",
  gap: "补充组合缺口",
};

export const lifecycleLabels: Record<string, string> = {
  DRAFT: "草案",
  NEEDS_CONFIRMATION: "待确认",
  CONFIRMED: "已确认",
  ROUTED: "已路由",
  RUNNING: "运行中",
  AWAITING_CHECKPOINT: "等待检查点确认",
  VALIDATING: "确定性验证中",
  COMPLETED: "已完成",
  STOPPED: "已停止",
  BLOCKED: "已阻断",
  FAILED: "失败",
  WAITING: "等待中",
};
