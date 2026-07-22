import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { FormalReportPage } from "../FormalReportPage";

describe("AlphaForge 正式报告详情页", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      status: "ok",
      detail: {
        run: { run_id: "run-report-test", title: "50 日动量研究" },
        report: {
          markdown: "# 50 日动量研究\n\n机器结论：需要修订。",
          report_hash: `sha256:${"a".repeat(64)}`,
        },
      },
    }), { status: 200, headers: { "content-type": "application/json" } })));
  });

  afterEach(() => vi.unstubAllGlobals());

  it("按运行标识打开正式报告，并保留完整证据链入口", async () => {
    render(
      <MemoryRouter initialEntries={["/reports/run-report-test"]}>
        <Routes>
          <Route path="/reports/:runId" element={<FormalReportPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "50 日动量研究" })).toBeInTheDocument();
    expect(screen.getByText("机器结论：需要修订。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /查看完整证据链/ })).toHaveAttribute(
      "href",
      "/veriquant/workbench?run_id=run-report-test",
    );
  });
});
