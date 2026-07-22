import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import {
  type CampaignApiAdapter,
  HttpCampaignApiAdapter,
  OfflineCampaignApiAdapter,
  offlineRunFixture,
  sanitizePublicPayload,
} from "@/features/alphaforge";
import { CampaignWorkspacePage } from "../alphaforge";
import { CampaignConfirm } from "../CampaignConfirm";

describe("AlphaForge 中文 Campaign 产品体验", () => {
  it("首页式工作区以研究问题和研究模式为入口，而不是因子类别入口", async () => {
    const user = userEvent.setup();
    render(<CampaignWorkspacePage api={new OfflineCampaignApiAdapter()} />, { wrapper: MemoryRouter });

    expect(screen.getByRole("heading", { name: "你今天希望解决什么研究问题？" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /新 Alpha 探索/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /诊断因子\/组合/ })).toBeInTheDocument();
    expect(screen.queryByText("动量模式")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /我不确定，帮我选择/ }));
    expect(await screen.findByText("只推荐，不运行")).toBeInTheDocument();
    expect(screen.queryByText("已确认")).not.toBeInTheDocument();
  });

  it("Campaign Draft 需要用户确认后才进入驾驶舱", async () => {
    const user = userEvent.setup();
    render(<CampaignWorkspacePage api={new OfflineCampaignApiAdapter()} />, { wrapper: MemoryRouter });

    await user.click(screen.getByRole("button", { name: /诊断因子\/组合/ }));
    await user.click(screen.getByRole("button", { name: /生成 Campaign Draft/ }));

    expect(await screen.findByRole("heading", { name: "确认研究合同" })).toBeInTheDocument();
    expect(screen.getByText("待确认")).toBeInTheDocument();
    expect(screen.getByText("历史股票池")).toBeInTheDocument();
    expect(screen.getByText("预测周期")).toBeInTheDocument();
    expect(screen.getByText("用户检查点")).toBeInTheDocument();
    expect(screen.getByText("停止条件")).toBeInTheDocument();
    expect(screen.getByText("确认前不会创建运行意图，也不会启动任何实验。")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /确认此 Research Campaign/ }));
    expect(await screen.findByText("已确认")).toBeInTheDocument();
    expect(screen.getByText("已冻结股票池、周期、预算、检查点与停止条件。")).toBeInTheDocument();
  });

  it("确认后的 Campaign 可以显式启动研究并返回 Workbench 报告入口", async () => {
    const user = userEvent.setup();
    const offline = new OfflineCampaignApiAdapter();
    const startRun = vi.fn(async (campaignId: string) => ({
      ...offlineRunFixture,
      campaignId,
      state: "COMPLETED" as const,
      checkpointState: "REACHED" as const,
      currentCheckpoint: "离线验收链已生成 Workbench 报告",
      messages: ["已执行 deterministic offline acceptance。"],
      artifactHash: "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      factorDagHash: "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      workbenchDetailHash: "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      shadowStatus: "READY",
    }));
    const adapter: CampaignApiAdapter = {
      createDraft: (input) => offline.createDraft(input),
      recommend: (question) => offline.recommend(question),
      confirm: async (draft) => ({ ...(await offline.confirm(draft)), campaignId: "campaign_ui_start" }),
      getRun: async (campaignId) => ({
        ...offlineRunFixture,
        campaignId,
        state: "ROUTED",
        checkpointState: "PENDING",
        currentCheckpoint: "等待用户确认启动",
        selectedDesks: offlineRunFixture.selectedDesks.slice(0, 2).map((desk) => ({
          ...desk,
          status: "WAITING" as const,
          stages: desk.stages.map((stage) => ({ ...stage, status: "WAITING" as const })),
        })),
      }),
      getProviderStatus: async () => ({ status: "AVAILABLE" as const, host: "llm.example.test", model: "draft-model" }),
      startRun,
      listFactors: () => offline.listFactors(),
    };

    render(<CampaignWorkspacePage api={adapter} />, { wrapper: MemoryRouter });

    await user.click(screen.getByRole("button", { name: /新 Alpha 探索/ }));
    await user.click(screen.getByRole("button", { name: /生成 Campaign Draft/ }));
    await user.click(await screen.findByRole("button", { name: /确认此 Research Campaign/ }));
    await user.click(await screen.findByRole("button", { name: /进入运行驾驶舱/ }));
    await user.click(await screen.findByRole("button", { name: "启动研究" }));

    expect(startRun).toHaveBeenCalledWith("campaign_ui_start", "DETERMINISTIC");
    expect(await screen.findByText("离线验收链已生成 Workbench 报告")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /打开 Workbench/ })).toHaveAttribute("href", "/veriquant/workbench");
  });

  it("驾驶舱没有真实运行时不展示演示 Desk", async () => {
    const user = userEvent.setup();
    render(<CampaignWorkspacePage />, { wrapper: MemoryRouter });

    await user.click(screen.getByRole("button", { name: "运行驾驶舱" }));
    expect(screen.getByRole("heading", { name: "尚未选择或启动 Research Campaign" })).toBeInTheDocument();
    expect(screen.queryByLabelText("本次 Campaign 选中的研究 Desk")).not.toBeInTheDocument();
  });

  it("显式传入的运行只展示 Router 选中的 Desk，并区分失败分支", async () => {
    const user = userEvent.setup();
    render(<CampaignWorkspacePage run={offlineRunFixture} />, { wrapper: MemoryRouter });

    await user.click(screen.getByRole("button", { name: "运行驾驶舱" }));
    const board = screen.getByLabelText("本次 Campaign 选中的研究 Desk");

    expect(within(board).getByText("基本面研究 Desk")).toBeInTheDocument();
    expect(within(board).getByText("市场结构 Desk")).toBeInTheDocument();
    expect(within(board).getByText("元阿尔法 Desk")).toBeInTheDocument();
    expect(within(board).queryByText("另类数据 Desk")).not.toBeInTheDocument();
    expect(screen.getByText(/不会伪造成完成/)).toBeInTheDocument();
    expect(screen.getByText("失败分支")).toBeInTheDocument();
  });

  it("因子库展示 461 项静态目录，只提供 Campaign 操作入口", async () => {
    const user = userEvent.setup();
    render(<CampaignWorkspacePage />, { wrapper: MemoryRouter });

    await user.click(screen.getByRole("button", { name: "Alpha 因子库" }));
    expect(screen.getByRole("status")).toHaveTextContent("静态目录 461 项");
    expect(screen.getAllByText("验证")[0]).toBeInTheDocument();
    expect(screen.getAllByText("改进")[0]).toBeInTheDocument();
    expect(screen.getAllByText("诊断")[0]).toBeInTheDocument();
    expect(screen.getAllByText("补充组合缺口")[0]).toBeInTheDocument();
    expect(screen.queryByText("编辑官方指标")).not.toBeInTheDocument();
    expect(screen.queryByText("直接晋级")).not.toBeInTheDocument();
  });

  it("前端适配器固定端点并脱敏 sealed、secret、promotion 和订单字段", async () => {
    const sanitized = sanitizePublicPayload({
      visible: "公开摘要",
      sealed_metric: 0.99,
      api_key: "should-not-render",
      nested: { promotionDecision: "PROMOTE", text: "Authorization: Bearer abc.def" },
      orders: [{ code: "000001.SZ" }],
    });
    expect(sanitized).toEqual({ visible: "公开摘要", nested: { text: "[敏感内容已移除]" } });

    const fetchMock = vi.fn(async (url: RequestInfo | URL) => new Response(
      JSON.stringify({
        items: [{
          factorId: "safe_factor",
          displayName: "安全因子",
          theme: "动量",
          eligibility: "DAG_PORTED",
          qualificationStatus: "DIRECT_FACTOR_DAG",
          sourceFamily: "academic",
          compatibilityClass: "directly_representable",
          lineage: "static_catalog",
          visibleSummary: "公开摘要",
          official_metrics: { rankic: 1 },
          actions: ["validate"],
        }],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
    const adapter = new HttpCampaignApiAdapter(fetchMock as unknown as typeof fetch);
    const factors = await adapter.listFactors();
    await adapter.startRun("campaign_safe", "REAL_LLM");

    expect(fetchMock).toHaveBeenCalledWith("/veriquant/factor-library", expect.objectContaining({
      headers: expect.objectContaining({ Accept: "application/json" }),
    }));
    expect(fetchMock).toHaveBeenCalledWith("/veriquant/campaign/runs/campaign_safe/start", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ Accept: "application/json" }),
      body: JSON.stringify({ runtimeMode: "REAL_LLM" }),
    }));
    expect(factors[0]).toMatchObject({ factorId: "safe_factor", displayName: "安全因子" });
    expect(factors[0]).not.toHaveProperty("official_metrics");
  });

  it("只在 provider 可用时允许启用真实 LLM 实验模式", async () => {
    const user = userEvent.setup();
    const offline = new OfflineCampaignApiAdapter();
    const startRun = vi.fn(async (campaignId: string, runtimeMode: "DETERMINISTIC" | "REAL_LLM" = "DETERMINISTIC") => ({
      ...offlineRunFixture,
      campaignId,
      runtimeMode,
    }));
    const adapter: CampaignApiAdapter = {
      createDraft: (input) => offline.createDraft(input),
      recommend: (question) => offline.recommend(question),
      confirm: async (draft) => ({ ...(await offline.confirm(draft)), campaignId: "campaign_real_llm" }),
      getRun: async (campaignId) => ({
        ...offlineRunFixture,
        campaignId,
        state: "ROUTED" as const,
        checkpointState: "PENDING" as const,
      }),
      getProviderStatus: async () => ({ status: "AVAILABLE", host: "llm.example.test", model: "draft-model" }),
      startRun,
      listFactors: () => offline.listFactors(),
    };
    render(<CampaignWorkspacePage api={adapter} />, { wrapper: MemoryRouter });

    const toggle = await screen.findByRole("checkbox", { name: "启用真实 LLM 实验模式" });
    expect(toggle).toBeEnabled();
    expect(screen.getByText(/llm\.example\.test/)).toBeInTheDocument();
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: /新?Alpha 探索/ }));
    await user.click(screen.getByRole("button", { name: /生成 Campaign Draft/ }));
    await user.click(await screen.findByRole("button", { name: /确认此 Research Campaign/ }));
    await user.click(await screen.findByRole("button", { name: /进入运行驾驶舱/ }));
    await user.click(await screen.findByRole("button", { name: "启动研究" }));
    expect(startRun).toHaveBeenCalledWith("campaign_real_llm", "REAL_LLM");
  });

  it("独立确认页强调 Draft 不可执行", () => {
    render(<CampaignConfirm />, { wrapper: MemoryRouter });

    expect(screen.getByText("先确认研究合同，再启动任何实验。")).toBeInTheDocument();
    expect(screen.getByText("Draft 不可执行")).toBeInTheDocument();
    expect(screen.getByText(/未确认草案只能用于讨论和澄清/)).toBeInTheDocument();
  });

  it("离线 adapter 中“我不确定”只能推荐，不能运行", async () => {
    const adapter = new OfflineCampaignApiAdapter();
    const recommendation = await adapter.recommend("我不确定");

    expect(recommendation.canRun).toBe(false);
    expect(recommendation.questions.length).toBeGreaterThan(0);
  });
});
