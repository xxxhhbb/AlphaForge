import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Reports } from "../Reports";

const runs = [
  { run_id: "real-visible-evidence", title: "真实代理因子验证", status: "INCOMPLETE" },
  { run_id: "blocked-promotion", title: "等待独立晋级", status: "BLOCKED" },
];

describe("AlphaForge 报告中心", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(
      JSON.stringify(String(input).includes("research-os/v3") ? { runs: [] } : { status: "ok", runs }),
      { status: 200, headers: { "content-type": "application/json" } },
    )));
  });

  it("只展示可追溯 Campaign 报告", async () => {
    render(<Reports />, { wrapper: MemoryRouter });

    expect(await screen.findByText("真实代理因子验证")).toBeInTheDocument();
    expect(screen.getByText("等待独立晋级")).toBeInTheDocument();
    expect(screen.getByText("把研究过程沉淀成可追溯报告。")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "查看正式报告 →" })[0]).toHaveAttribute(
      "href",
      "/reports/real-visible-evidence",
    );
    expect(screen.queryByText("Backtest Report Library")).not.toBeInTheDocument();
  });

  it("按研究名称过滤", async () => {
    render(<Reports />, { wrapper: MemoryRouter });

    await screen.findByText("真实代理因子验证");
    fireEvent.change(screen.getByPlaceholderText("运行 ID 或研究名称"), { target: { value: "等待" } });
    expect(screen.queryByText("真实代理因子验证")).not.toBeInTheDocument();
    expect(screen.getByText("等待独立晋级")).toBeInTheDocument();
  });

  it("把 V3 多专业研究结果接到统一结果页", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(
      JSON.stringify(String(input).includes("research-os/v3")
        ? { runs: [{ run_id: "u4-real-multi", status: "COMPLETED", candidate_count: 20 }] }
        : { status: "ok", runs: [] }),
      { status: 200, headers: { "content-type": "application/json" } },
    )));
    render(<Reports />, { wrapper: MemoryRouter });
    expect(await screen.findByText("多专业研究结果")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看正式报告 →" })).toHaveAttribute(
      "href",
      "/research/results/u4-real-multi",
    );
  });
});
