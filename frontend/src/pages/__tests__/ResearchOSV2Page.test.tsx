import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ResearchOSV2Page } from "../ResearchOSV2Page";
import {
  deterministicResearchOSV2Client,
  createResearchOSV2HttpClient,
  makeCompletedRun,
  makeRunningRun,
  type ResearchOSV2Client,
  type ResearchRunView,
} from "@/features/research-os-v2";

describe("AlphaForge 中文研究闭环页面", () => {
  afterEach(() => vi.unstubAllGlobals());
  const renderPage = () => render(<ResearchOSV2Page client={deterministicResearchOSV2Client} pollIntervalMs={1} />);

  const reachStart = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole("button", { name: /生成研究建议/ }));
    await user.click(await screen.findByRole("button", { name: /确认研究计划/ }));
    return screen.findByRole("button", { name: /启动受控研究/ });
  };

  it("确认研究计划前不允许启动计算", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(screen.queryByRole("button", { name: /启动受控研究/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /生成研究建议/ }));

    expect(await screen.findByText("建议参与的专业研究小组")).toBeInTheDocument();
    expect(screen.getAllByText("已批准代理数据源", { exact: false }).length).toBeGreaterThan(0);
    expect(screen.getByText("动态数据计划")).toBeInTheDocument();
    expect(screen.getByText("最低 295 个交易会话")).toBeInTheDocument();
    expect(screen.getByText("已覆盖本次推导需求")).toBeInTheDocument();
    expect(screen.getByText("按历史成员关系获取，不预设固定股票数")).toBeInTheDocument();
    expect(screen.getByText("daily_basic", { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /启动受控研究/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /确认研究计划/ }));
    expect(await screen.findByRole("button", { name: /启动受控研究/ })).toBeInTheDocument();
  });

  it("清楚区分真实模型来源、完成分支和异常分支", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: /生成研究建议/ }));
    await user.click(await screen.findByRole("button", { name: /确认研究计划/ }));
    await user.click(await screen.findByRole("button", { name: /启动受控研究/ }));

    expect(await screen.findByText("专业研究小组与机器验证")).toBeInTheDocument();
    expect(screen.getAllByText("确定性回退").length).toBeGreaterThan(0);
    expect(screen.getByText("测试 fixture；不冒充真实模型")).toBeInTheDocument();
    expect(screen.getByText("市场行为研究组")).toBeInTheDocument();
    expect(screen.getByText("交易可行性研究组")).toBeInTheDocument();
    expect(screen.getByText("组合增量研究组")).toBeInTheDocument();
    expect(screen.getAllByText("研究验证通过").length).toBeGreaterThan(0);
    expect(screen.getAllByText("需修订").length).toBeGreaterThan(0);
    expect(screen.getByText("该分支已关闭，其他分支结果仍然有效。", { exact: false })).toBeInTheDocument();
  });

  it("封闭评审只显示不透明状态，不泄漏指标或原因", async () => {
    const client: ResearchOSV2Client = {
      ...deterministicResearchOSV2Client,
      async startResearch(planId) {
        return makeCompletedRun(planId);
      },
    };
    const user = userEvent.setup();
    const { container } = render(<ResearchOSV2Page client={client} pollIntervalMs={1} />);

    await user.click(screen.getByRole("button", { name: /生成研究建议/ }));
    await user.click(await screen.findByRole("button", { name: /确认研究计划/ }));
    await user.click(await screen.findByRole("button", { name: /启动受控研究/ }));

    expect(await screen.findByText("独立封闭评审：等待独立评审")).toBeInTheDocument();
    expect(screen.getByText("封闭数据与指标不可见", { exact: false })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/sealed[_ -]?(metric|score|rank|threshold)/i);
  });

  it("用户界面不暴露内部编排术语或金融真值写入入口", () => {
    const { container } = render(<ResearchOSV2Page client={deterministicResearchOSV2Client} />);
    const publicText = container.textContent ?? "";

    expect(publicText).not.toMatch(/\b(Desk|Router|Swarm|pipeline)\b/i);
    expect(screen.queryByRole("button", { name: /修改.*指标|批准.*晋级|设置.*持仓|创建.*订单/ })).not.toBeInTheDocument();
    expect(publicText).toContain("页面不收集服务商密钥");
  });

  it("数据不足时仍返回辅助研究路线而不是伪造机器评价", async () => {
    const user = userEvent.setup();
    renderPage();
    const input = screen.getByLabelText("Alpha 研究问题");

    await user.clear(input);
    await user.type(input, "北向资金是否增强 50 日动量？");
    await user.click(screen.getByRole("button", { name: /生成研究建议/ }));

    expect(await screen.findByText("数据不足不会被静默忽略")).toBeInTheDocument();
    expect(screen.getByText("北向资金逐笔可得时间尚未冻结", { exact: false })).toBeInTheDocument();
    expect(screen.getAllByText("辅助研究", { exact: false }).length).toBeGreaterThan(0);
    expect(screen.getByText("需要补充获取并冻结")).toBeInTheDocument();
    expect(screen.getByText("数据缺口与可用替代路线")).toBeInTheDocument();
    expect(screen.getByText("先以已冻结的日频资金流代理变量进行辅助研究", { exact: false })).toBeInTheDocument();
  });

  it("拒绝格式损坏的动态数据计划", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      suggestionId: "bad-data-plan",
      question: "研究动量",
      researchMode: "新 Alpha 探索",
      researchObject: "A 股动量",
      canRun: true,
      answerLevel: "机器评价",
      rationale: "测试",
      selectedGroups: [],
      plannedExperiments: [],
      dataCoverage: [],
      dataGaps: [],
      provider: {},
      dataPlan: { planHash: "broken", minimumSessions: -1 },
    }), { status: 200, headers: { "content-type": "application/json" } })));
    const client = createResearchOSV2HttpClient("/test/research");

    await expect(client.createSuggestion({
      question: "研究动量",
      depth: "标准研究",
      collaborationStyle: "标准式",
    })).rejects.toThrow("不完整或不受支持");
  });

  it("后端响应缺少 typed 字段时 fail closed", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ suggestionId: "incomplete" }),
      { status: 200, headers: { "content-type": "application/json" } },
    )));
    const client = createResearchOSV2HttpClient("/test/research");

    await expect(client.createSuggestion({
      question: "研究 50 日动量",
      depth: "标准研究",
      collaborationStyle: "标准式",
    })).rejects.toThrow("不完整或不受支持");
  });

  it("启动响应为排队中时持续查询，完成前不开放报告", async () => {
    let resolveStatus!: (run: ResearchRunView) => void;
    const statusPromise = new Promise<ResearchRunView>((resolve) => { resolveStatus = resolve; });
    const client: ResearchOSV2Client = {
      ...deterministicResearchOSV2Client,
      getResearchStatus: () => statusPromise,
    };
    const user = userEvent.setup();
    render(<ResearchOSV2Page client={client} pollIntervalMs={1} />);
    await user.click(await reachStart(user));

    expect(await screen.findByText("排队中")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /查看正式报告/ })).not.toBeInTheDocument();
    expect(screen.getByText("研究完成前不开放报告")).toBeInTheDocument();

    resolveStatus(makeCompletedRun());
    expect(await screen.findByRole("link", { name: /查看正式报告/ })).toHaveAttribute(
      "href",
      "/reports/run-momentum-50-v2",
    );
  });

  it("后台失败保持失败并且不开放报告", async () => {
    const failed = { ...makeRunningRun(), state: "失败" as const, currentCheckpoint: "数据质量检查失败", formalReportPath: "/must-not-open" };
    const client: ResearchOSV2Client = {
      ...deterministicResearchOSV2Client,
      getResearchStatus: async () => failed,
    };
    const user = userEvent.setup();
    render(<ResearchOSV2Page client={client} pollIntervalMs={1} />);
    await user.click(await reachStart(user));

    expect(await screen.findByText("数据质量检查失败")).toBeInTheDocument();
    expect(screen.getAllByText("失败").length).toBeGreaterThan(0);
    expect(screen.queryByRole("link", { name: /查看正式报告/ })).not.toBeInTheDocument();
  });

  it("页面卸载后停止状态查询", async () => {
    let calls = 0;
    const client: ResearchOSV2Client = {
      ...deterministicResearchOSV2Client,
      getResearchStatus: async () => {
        calls += 1;
        return { ...makeRunningRun(), state: "运行中" };
      },
    };
    const user = userEvent.setup();
    const view = render(<ResearchOSV2Page client={client} pollIntervalMs={1} />);
    await user.click(await reachStart(user));
    await waitFor(() => expect(calls).toBeGreaterThan(0));
    view.unmount();
    const callsAtUnmount = calls;
    await new Promise((resolve) => setTimeout(resolve, 15));
    expect(calls).toBe(callsAtUnmount);
  });

  it("有界查询达到上限时明确提示但不伪造失败", async () => {
    const client: ResearchOSV2Client = {
      ...deterministicResearchOSV2Client,
      getResearchStatus: async () => ({ ...makeRunningRun(), state: "运行中" }),
    };
    const user = userEvent.setup();
    render(<ResearchOSV2Page client={client} maxPollAttempts={2} pollIntervalMs={1} />);
    await user.click(await reachStart(user));

    expect(await screen.findByText("状态查询已达到上限", { exact: false })).toBeInTheDocument();
    expect(screen.queryByText("研究服务已确认失败")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /查看正式报告/ })).not.toBeInTheDocument();
  });

  it("连续点击启动只创建一个后台任务", async () => {
    let starts = 0;
    let resolveStart!: (run: ResearchRunView) => void;
    const startPromise = new Promise<ResearchRunView>((resolve) => { resolveStart = resolve; });
    const client: ResearchOSV2Client = {
      ...deterministicResearchOSV2Client,
      startResearch: () => {
        starts += 1;
        return startPromise;
      },
    };
    const user = userEvent.setup();
    const view = render(<ResearchOSV2Page client={client} pollIntervalMs={50} />);
    const startButton = await reachStart(user);
    fireEvent.click(startButton);
    fireEvent.click(startButton);
    expect(starts).toBe(1);
    resolveStart(makeRunningRun());
    expect(await screen.findByText("排队中")).toBeInTheDocument();
    view.unmount();
  });

  it("真实模型 provenance 仅表示草案来源", async () => {
    const realProvider = {
      mode: "真实模型" as const,
      providerLabel: "已配置研究模型服务",
      modelLabel: "仅生成研究草案，不拥有机器评价权限",
    };
    const client: ResearchOSV2Client = {
      ...deterministicResearchOSV2Client,
      startResearch: async (planId) => ({ ...makeRunningRun(planId), provider: realProvider }),
      getResearchStatus: async () => ({ ...makeCompletedRun(), provider: realProvider }),
    };
    const user = userEvent.setup();
    render(<ResearchOSV2Page client={client} pollIntervalMs={1} />);
    await user.click(await reachStart(user));

    expect(await screen.findByText("仅生成研究草案，不拥有机器评价权限")).toBeInTheDocument();
    expect(screen.getAllByText("真实模型").length).toBeGreaterThan(0);
  });
});
