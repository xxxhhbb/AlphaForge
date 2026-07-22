import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ResearchOSV3LaunchPage } from "@/pages/ResearchOSV3LaunchPage";
import { createResearchOSV3LaunchHttpClient } from "../client";
import type { V3CampaignPlanView, V3LaunchClient, V3ResearchRunControlView } from "../types";

const draft: V3CampaignPlanView = {
  campaignId: "campaign_v3_test",
  campaignHash: "sha256:campaign",
  state: "待确认",
  question: "研究 50 日动量",
  researchMode: "新 Alpha 探索",
  researchSubject: "A 股横截面动量",
  depth: "标准研究",
  interactionStyle: "标准式",
  selectedSpecialists: ["市场行为研究组", "组合增量研究组"],
  plannedResearch: ["形成可证伪假设", "提交受限候选", "逐项机器预检"],
  dataScope: {
    sourceLabel: "已批准代理数据源",
    universe: "历史 A 股股票池",
    dateRange: "2024-01-01 至 2025-01-01",
    snapshotHash: "sha256:snapshot",
    availability: "可用",
    limitations: [],
  },
  budget: { specialistGroups: 2, candidateLimit: 12, trialLimit: 18, modelCallLimit: 24 },
  checkpoints: ["研究计划确认", "机器验证前确认"],
  stopConditions: ["预算耗尽", "用户停止", "验证完成"],
};

const confirmed: V3CampaignPlanView = { ...draft, state: "已确认" };

function runView(overrides: Partial<V3ResearchRunControlView> = {}): V3ResearchRunControlView {
  return {
    runId: "run_v3_test",
    campaignId: draft.campaignId,
    state: "运行中",
    currentStage: "专业研究小组正在形成候选",
    providerLabel: "已配置受限研究模型",
    modelLabel: "deepseek-v4-flash-260425",
    selectedSpecialists: [
      { specialistId: "market_structure", title: "市场行为与交易可行性研究组", state: "运行中", candidateCount: 2, message: "正在检查价格、成交量与交易可行性。" },
      { specialistId: "meta_alpha", title: "组合增量研究组", state: "运行中", candidateCount: 1, message: "正在比较已有因子相关性。" },
    ],
    usedBudget: 0,
    totalBudget: 18,
    resultReady: false,
    typedFailures: [],
    ...overrides,
  };
}

function client(overrides: Partial<V3LaunchClient> = {}): V3LaunchClient {
  return {
    createPlan: async () => draft,
    preparePlanData: async () => draft,
    confirmPlan: async () => confirmed,
    startRun: async () => runView(),
    getRun: async () => runView({
      state: "已完成",
      currentStage: "研究与报告已完成",
      usedBudget: 3,
      resultReady: true,
      selectedSpecialists: [
        { specialistId: "market_structure", title: "市场行为与交易可行性研究组", state: "已完成", candidateCount: 2, message: "已提交并完成机器评价。" },
        { specialistId: "meta_alpha", title: "组合增量研究组", state: "已完成", candidateCount: 1, message: "已完成增量性检查。" },
      ],
    }),
    getReportMarkdown: async () => "# AlphaForge（千因研究院）研究报告\n\n## 一页结论\n\n**结论：本轮没有候选达到研究短名单标准。** 这是完整负向研究结果。\n\n- 候选进度：提出 3 个，机器评价 3 个。",
    cancelRun: async () => runView({ state: "已取消" }),
    resumeRun: async () => runView(),
    ...overrides,
  };
}

function renderPage(mockClient: V3LaunchClient, props: Record<string, unknown> = {}) {
  return render(<MemoryRouter><ResearchOSV3LaunchPage client={mockClient} pollIntervalMs={1} {...props} /></MemoryRouter>);
}

describe("AlphaForge 单入口研究终端", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("用户点击一次即可自动完成计划、确认、启动、轮询并展示报告", async () => {
    const user = userEvent.setup();
    const createPlan = vi.fn(async () => draft);
    const confirmPlan = vi.fn(async () => confirmed);
    const startRun = vi.fn(async () => runView());
    const getRun = vi.fn(async () => runView({ state: "已完成", currentStage: "研究与报告已完成", usedBudget: 3, resultReady: true }));
    const getReportMarkdown = vi.fn(async () => "# AlphaForge（千因研究院）研究报告\n\n## 一页结论\n\n**结论：完整负向研究结果。**");
    const onResultReady = vi.fn();

    renderPage(client({ createPlan, confirmPlan, startRun, getRun, getReportMarkdown }), { onResultReady });
    await user.click(screen.getByRole("button", { name: /开始研究并生成报告/ }));

    await waitFor(() => expect(createPlan).toHaveBeenCalled());
    await waitFor(() => expect(confirmPlan).toHaveBeenCalledWith("campaign_v3_test"));
    await waitFor(() => expect(startRun).toHaveBeenCalledWith("campaign_v3_test"));
    await waitFor(() => expect(getReportMarkdown).toHaveBeenCalledWith("run_v3_test", expect.any(AbortSignal)));
    await screen.findAllByText("完整负向研究结果。", { exact: false });
    expect(onResultReady).toHaveBeenCalledWith("run_v3_test");
  });

  it("后处理未完成时继续轮询，不提前展示旧报告", async () => {
    const user = userEvent.setup();
    const states = [
      runView({ state: "已完成", currentStage: "研究与报告已完成", usedBudget: 0, resultReady: false }),
      runView({ state: "已完成", currentStage: "研究与报告已完成", usedBudget: 3, resultReady: true }),
    ];
    const getRun = vi.fn(async () => states.shift() ?? runView({ state: "已完成", usedBudget: 3, resultReady: true }));
    const getReportMarkdown = vi.fn(async () => "# 报告\n\n## 一页结论\n\n机器评价已经完成。");

    renderPage(client({ getRun, getReportMarkdown }));
    await user.click(screen.getByRole("button", { name: /开始研究并生成报告/ }));

    await waitFor(() => expect(getRun).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("机器评价已经完成。")).toBeInTheDocument();
  });

  it("数据不可用时输出清楚原因，不继续启动模型", async () => {
    const user = userEvent.setup();
    const unavailable = { ...draft, dataScope: { ...draft.dataScope, availability: "不可用" as const, limitations: ["缺少财务公告时间语义"] } };
    const startRun = vi.fn(async () => runView());
    renderPage(client({ createPlan: async () => unavailable, startRun }));

    await user.click(screen.getByRole("button", { name: /开始研究并生成报告/ }));

    expect(await screen.findByText(/缺少财务公告时间语义/)).toBeInTheDocument();
    expect(startRun).not.toHaveBeenCalled();
  });

  it("界面不暴露内部编排术语或密钥输入", () => {
    const { container } = renderPage(client());
    expect(container.textContent).not.toMatch(/\b(Desk|Router|Swarm|pipeline)\b/i);
    expect(screen.queryByLabelText(/密钥|token|API key/i)).not.toBeInTheDocument();
    expect(container.textContent).toContain("页面只展示研究过程和报告");
  });

  it("HTTP 客户端能读取正式 Markdown 报告", async () => {
    const fetchMock = vi.fn(async () => new Response("# 报告", { status: 200, headers: { "content-type": "text/markdown" } }));
    vi.stubGlobal("fetch", fetchMock);
    const text = await createResearchOSV3LaunchHttpClient("/api/test/v3").getReportMarkdown("run_v3_test");
    expect(text).toBe("# 报告");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/test/v3/runs/run_v3_test/report.md",
      expect.objectContaining({ method: "GET", credentials: "same-origin" }),
    );
  });

  it("兼容 canonical snake_case 运行清单且不推测机器证据", async () => {
    const manifest = {
      run_id: "run-canonical",
      campaign_hash: "sha256:campaign",
      selected_group_ids: ["market_structure", "macro_regime"],
      current_stage: "RESEARCHING",
      remaining_candidate_slots: 10,
      candidate_count: 4,
      trial_count: 2,
      typed_failures: ["DATA_GAP", "UNAUTHORIZED_CAPABILITY"],
      specialist_summaries: [
        { specialist_id: "market_structure", status: "COMPLETED_DRAFT", candidate_count: 8, message: "本组已提交 8 个候选，明细已进入正式报告。" },
        { specialist_id: "macro_regime", status: "DATA_GAP", candidate_count: 0, message: "当前批准数据不足，本组未启动模型。" },
      ],
      provider_id: "configured_provider",
      model_id: "research_model",
      official_evidence_hash: null,
      report_hash: null,
    };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(manifest), { status: 200, headers: { "content-type": "application/json" } })));
    const view = await createResearchOSV3LaunchHttpClient("/api/test/v3").getRun("run-canonical");
    expect(view.state).toBe("运行中");
    expect(view.selectedSpecialists.map((item) => item.title)).toEqual(["市场行为与交易可行性研究组", "宏观状态研究组"]);
    expect(view.selectedSpecialists.map((item) => item.candidateCount)).toEqual([8, 0]);
    expect(view.resultReady).toBe(false);
    expect(view.totalBudget).toBe(12);
  });
});
