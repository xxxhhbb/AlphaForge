import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { VeriquantWorkbench } from "../VeriquantWorkbench";

const fixture = {
  runs: [
    {
      run_id: "run-fixture",
      title: "Workbench fixture",
      status: "SHADOW_ACTIVE",
      decision_time: "2026-07-18T00:00:00Z",
      updated_at: "2026-07-18T01:00:00Z",
      alert_count: 1,
    },
  ],
  detail: {
    report: {
      title: "正式中文研究报告",
      markdown: "# 一页结论\n\n机器证据支持继续研究。",
      report_hash: "sha256:report-fixture",
    },
    run: {
      run_id: "run-fixture",
      status: "SHADOW_ACTIVE",
      research_dag: {
        nodes: [
          { id: "desk:fundamental", status: "DRAFT", stage: "hypothesis_propose" },
          { id: "evidence:court", status: "VALIDATED_RESEARCH", stage: "factor_evaluate" },
        ],
      },
    },
    research_dag: {
      nodes: [
        { id: "desk:fundamental", status: "DRAFT", stage: "hypothesis_propose" },
        { id: "evidence:court", status: "VALIDATED_RESEARCH", stage: "factor_evaluate" },
      ],
    },
    lineage: {
      candidates: [
        {
          candidate_id: "cand-1",
          family_id: "fam-1",
          status: "DRAFT",
          parent_id: "root",
          operator: "original",
          hypothesis: {
            statement: "Released book value may predict subsequent returns.",
            economic_mechanism: "Accounting information may diffuse slowly.",
            falsification_tests: ["Reject if cost-aware validation is weak."],
          },
          factor_draft: {
            name: "book_to_market_rank",
            description: "Rank released book value over market capitalization.",
            expression: "rank_cross_section(divide_safe(book_value, market_cap))",
          },
          redacted_feedback: { explanation: "Visible validation summary only." },
        },
      ],
    },
    evidence: {
      items: [{ evidence_id: "ev-1", metric: "net_icir", value: 1.23, summary: "visible validation only" }],
    },
    critics: {
      findings: [{ critic: "CitationCritic", verdict: "VALIDATED_RESEARCH", message: "citation span located" }],
    },
    promotion: {
      status: "CHALLENGER",
      sealed_status: "PASSED_REDACTED",
      sealed_failure_code: "NONE",
    },
    citations: {
      items: [
        {
          title: "Document fixture",
          snippet: "<img src=x onerror=alert(1)> plain text citation",
          source_content_hash: "sha256:abc",
          available_at: "2026-01-02T00:00:00Z",
        },
      ],
    },
    shadow_portfolio: {
      vintages: [{ vintage_id: "paper-1", gross_exposure: 1, net_exposure: 0 }],
    },
    nav_attribution: {
      attribution: [{ label: "alpha", value: 0.012 }],
    },
    drift_alerts: {
      alerts: [{ code: "COST_DRIFT", message: "cost increased" }],
    },
  },
};

function mockFetchWithFixture() {
  return vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    const body = path.includes("/runs/run-fixture")
      ? { status: "ok", detail: fixture.detail }
      : path.includes("/operator/profiles")
        ? { profiles: [] }
        : { status: "ok", runs: fixture.runs };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
}

describe("VeriquantWorkbench 只读证据详情页", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("渲染 fixture API 数据、状态标签、引用链和安全边界", async () => {
    vi.stubGlobal("fetch", mockFetchWithFixture());

    const { container } = render(<VeriquantWorkbench />, { wrapper: MemoryRouter });

    expect(await screen.findByText("Workbench 保留为只读审计视图。")).toBeInTheDocument();
    expect((await screen.findAllByText("run-fixture")).length).toBeGreaterThan(0);
    expect(await screen.findByText("来源与引用")).toBeInTheDocument();
    expect(screen.getByText(/研究席位只提出假设、草案和解释/)).toBeInTheDocument();
    expect(screen.getByText(/证据服务只从冻结输入计算可重放指标/)).toBeInTheDocument();
    expect(screen.getByText(/晋级控制器独占 Promotion 与 sealed test 决策/)).toBeInTheDocument();
    expect(screen.getByText("Released book value may predict subsequent returns.")).toBeInTheDocument();
    expect(screen.getByText("rank_cross_section(divide_safe(book_value, market_cap))")).toBeInTheDocument();
    expect(screen.getByText("Visible validation summary only.")).toBeInTheDocument();
    expect(screen.getByText("一页结论")).toBeInTheDocument();
    expect(screen.getByText("机器证据支持继续研究。")).toBeInTheDocument();
    expect(screen.getByText(/sha256:report-fixture/)).toBeInTheDocument();

    for (const label of ["草案", "研究已验证", "挑战者", "影子组合运行中", "已拒绝", "未完成", "已阻断"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }

    expect(screen.getByText(/<img src=x onerror=alert\(1\)> plain text citation/)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });

  it("没有运行时显示空状态", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
      const body = String(url).includes("/operator/profiles") ? { profiles: [] } : { status: "ok", runs: [] };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }));

    render(<VeriquantWorkbench />, { wrapper: MemoryRouter });

    expect(await screen.findByText(/证据详情页已就绪/)).toBeInTheDocument();
  });

  it("从 fixture payload 渲染失败和未完成状态", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      const body = path.includes("/operator/profiles")
        ? { profiles: [] }
        : path.includes("/runs/run-rejected")
          ? { status: "ok", detail: { run: { run_id: "run-rejected", status: "REJECTED" }, drift_alerts: { alerts: [{ code: "FAILED", message: "mechanical gate rejected" }] } } }
          : { status: "ok", runs: [{ run_id: "run-rejected", status: "REJECTED" }, { run_id: "run-incomplete", status: "INCOMPLETE" }] };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }));

    render(<VeriquantWorkbench />, { wrapper: MemoryRouter });

    expect((await screen.findAllByText("run-rejected")).length).toBeGreaterThan(0);
    const overview = screen.getByText("运行列表").closest("section");
    expect(overview).not.toBeNull();
    expect(within(overview as HTMLElement).getAllByText("已拒绝").length).toBeGreaterThan(0);
    expect(within(overview as HTMLElement).getAllByText("未完成").length).toBeGreaterThan(0);
  });

  it("渲染冻结 Workbench 仓库对象形态", async () => {
    const realArtifactShape = {
      run: {
        run_id: "v1-offline-seed-7",
        status: "SHADOW_ACTIVE",
        decision_time: "2024-01-25T09:25:00+08:00",
        research_dag: {
          alternative_data: "INCOMPLETE",
          expectations_events: "DRAFT",
          fundamental: "INCOMPLETE",
        },
      },
      research_dag: {
        alternative_data: "INCOMPLETE",
        expectations_events: "DRAFT",
        fundamental: "INCOMPLETE",
      },
      lineage: {
        completed_desks: ["expectations_events"],
        failed_or_incomplete_desks: ["fundamental", "alternative_data"],
        selected_desk: "expectations_events",
        factor_dag_hash: "sha256:factor-dag",
      },
      evidence: {
        evidence_class: "OFFICIAL_EVIDENCE",
        sealed_redacted: true,
        validation: {
          partition: "VALIDATION",
          metrics: { net_return: 0.0066, rank_ic: 0.6909 },
        },
      },
      critics: {
        branch_failures: { fundamental: "UNKNOWN_FIELD" },
        statistical_robustness: {
          findings: [{ check_name: "cost_sensitivity", passed: true, message: "net return survives cost stress" }],
        },
      },
      promotion: {
        decision: { verdict: "PROMOTE_CHALLENGER", to_state: "CHALLENGER", failure_code: null },
        audit: { status: "PROMOTED" },
      },
      citations: {
        hypothesis: {
          citations: [{ document_id: "doc-frozen", quote: "Frozen source citation.", chunk_hash: "sha256:citation" }],
        },
      },
      shadow_portfolio: {
        run_id: "v1-offline-seed-7",
        status: "READY",
        report_time: "2024-01-25T06:30:00Z",
        vintage_hash: "sha256:vintage",
      },
      nav_attribution: {
        nav_start: 100000,
        nav_end: 99910,
        net_pnl: -90,
        turnover: 0.6,
        challenger_contribution: { "candidate-visible-momentum@v1": 0 },
      },
      drift_alerts: {
        items: [{ alert_type: "ic_return_proxy_drift", severity: "WARN" }],
      },
    };

    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      const body = path.includes("/operator/profiles")
        ? { profiles: [] }
        : path.includes("/runs/v1-offline-seed-7")
          ? { status: "ok", detail: realArtifactShape }
          : { status: "ok", runs: [{ run_id: "v1-offline-seed-7", status: "SHADOW_ACTIVE" }] };
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));

    render(<VeriquantWorkbench />, { wrapper: MemoryRouter });

    expect((await screen.findAllByText("desk:alternative_data")).length).toBeGreaterThan(0);
    expect(screen.getByText("validation.net_return")).toBeInTheDocument();
    expect(screen.getByText(/StatisticalRobustness:cost_sensitivity/)).toBeInTheDocument();
    expect(screen.getByText(/ResearchDesk:fundamental/)).toBeInTheDocument();
    expect(screen.getByText("PROMOTE_CHALLENGER")).toBeInTheDocument();
    expect(screen.getByText("Frozen source citation.")).toBeInTheDocument();
    expect(screen.getByText("影子组合")).toBeInTheDocument();
  });

  it("API 错误时 fail-closed，不伪造运行内容", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("unavailable", { status: 503 })));

    render(<VeriquantWorkbench />, { wrapper: MemoryRouter });

    expect(await screen.findByRole("alert")).toHaveTextContent("证据详情接口暂不可用（503）");
    expect(screen.queryByText("影子组合运行中", { selector: "button *" })).not.toBeInTheDocument();
  });

  it("独立服务未注册固定流程接口时显示明确提示而不是 JSON 解析错误", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/operator/profiles")) {
        return new Response("<!doctype html>", { status: 200, headers: { "content-type": "text/html" } });
      }
      return new Response(JSON.stringify({ status: "ok", runs: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }));

    render(<VeriquantWorkbench />, { wrapper: MemoryRouter });

    expect(await screen.findByText(/当前 AlphaForge 独立服务未启用固定流程操作接口/)).toBeInTheDocument();
    expect(screen.queryByText(/Unexpected token/)).not.toBeInTheDocument();
  });
});
