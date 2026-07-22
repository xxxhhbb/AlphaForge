import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ResearchOSV3ResultClient } from "@/features/research-os-v3";
import { researchOSV3Fixture } from "@/features/research-os-v3";
import { ResearchOSV3ResultPage } from "../ResearchOSV3ResultPage";

describe("ResearchOSV3ResultPage", () => {
  it("loads and renders a research result through the injected client", async () => {
    const client: ResearchOSV3ResultClient = { getResearchResult: vi.fn().mockResolvedValue(researchOSV3Fixture) };
    render(<MemoryRouter><ResearchOSV3ResultPage client={client} runId="run-ui" /></MemoryRouter>);
    expect(screen.getByText("正在读取研究结果")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "专业研究小组与机器验证" })).toBeInTheDocument();
    expect(client.getResearchResult).toHaveBeenCalledWith("run-ui", expect.any(AbortSignal));
  });

  it("shows a Chinese fail-closed message when the client rejects", async () => {
    const client: ResearchOSV3ResultClient = { getResearchResult: vi.fn().mockRejectedValue(new Error("运行记录不存在")) };
    render(<MemoryRouter><ResearchOSV3ResultPage client={client} runId="missing" /></MemoryRouter>);
    expect(await screen.findByRole("alert")).toHaveTextContent("研究结果暂不可用");
    expect(screen.getByRole("alert")).toHaveTextContent("运行记录不存在");
  });
});
