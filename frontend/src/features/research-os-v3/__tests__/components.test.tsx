import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResearchOSV3Result } from "../components";
import { researchOSV3DataGapFixture, researchOSV3EvaluatedFixture, researchOSV3Fixture, researchOSV3PartialFixture, researchOSV3RejectedFixture, researchOSV3ReportFixture } from "../fixtures";

describe("ResearchOSV3Result", () => {
  it("shows selected research groups, candidate preflight and honest evidence state", () => {
    const { container } = render(<ResearchOSV3Result run={researchOSV3Fixture} />);
    expect(screen.getByRole("heading", { name: "研究结论与机器验证" })).toBeInTheDocument();
    expect(screen.getByText("市场行为与交易可行性研究组")).toBeInTheDocument();
    expect(screen.getByText("另类数据研究组")).toBeInTheDocument();
    expect(screen.getByText("组合增量研究组")).toBeInTheDocument();
    expect(screen.getByText("负向结果")).toBeInTheDocument();
    expect(screen.getByText("数据缺口")).toBeInTheDocument();
    expect(container.querySelectorAll('.ros3-candidate[data-outcome="预检通过"]')).toHaveLength(2);
    expect(screen.getAllByText("尚未执行").length).toBeGreaterThan(0);
    expect(screen.getByText(/封闭评审内容不可见/)).toBeInTheDocument();
    expect(screen.getByText(/正式中文研究报告尚未发布/)).toBeInTheDocument();
    const visibleText = container.textContent ?? "";
    expect(visibleText).not.toMatch(/\bDesk\b|\bRouter\b|\bSwarm\b/);
  });

  it("shows the evaluated candidate funnel, best candidate and deterministic reasons", () => {
    const { container } = render(<ResearchOSV3Result run={researchOSV3EvaluatedFixture} />);
    expect(screen.getByText("系统理解")).toBeInTheDocument();
    expect(screen.getByText("当前最佳候选")).toBeInTheDocument();
    expect(screen.getAllByText("momentum_cost_aware").length).toBeGreaterThan(0);
    expect(screen.getByText("成本后信息比率低于研究政策")).toBeInTheDocument();
    expect(screen.getByText("0.7200")).toBeInTheDocument();
    expect(container.textContent).not.toContain("NEVER_RENDER");
  });

  it.each([
    [researchOSV3RejectedFixture, "负向结果", "本轮没有形成最佳候选"],
    [researchOSV3DataGapFixture, "数据或能力缺口", "批准数据缺少可确认发布时间"],
    [researchOSV3PartialFixture, "部分结果", "部分候选完成预检"],
  ])("renders honest non-positive outcomes", (fixture, outcome, message) => {
    render(<ResearchOSV3Result run={fixture} />);
    expect(screen.getAllByText(outcome).length).toBeGreaterThan(0);
    expect(screen.getAllByText(new RegExp(message)).length).toBeGreaterThan(0);
  });

  it("renders a formal Chinese report and never renders sealed payload fields", () => {
    const { container } = render(<ResearchOSV3Result run={researchOSV3ReportFixture} />);
    expect(screen.getByRole("heading", { name: "A 股 50 日动量自动研究报告" })).toBeInTheDocument();
    expect(screen.getByText(/2 个候选通过可见研究标准/)).toBeInTheDocument();
    expect(screen.getByText("sha256:formal-report-u5")).toBeInTheDocument();
    expect(container.textContent).not.toContain("NEVER_RENDER");
    expect(container.textContent).not.toMatch(/sealed_metrics|secret/);
    expect(screen.getByRole("link", { name: "打开完整报告" })).toHaveAttribute("href", "/api/veriquant/research-os/v3/runs/u5-fixture-report/report.md");
  });
});
