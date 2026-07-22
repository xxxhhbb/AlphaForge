import { afterEach, describe, expect, it, vi } from "vitest";
import { createResearchOSV3HttpClient, ResearchOSV3ClientError } from "../client";

afterEach(() => vi.unstubAllGlobals());

describe("Research OS V3 HTTP client", () => {
  it("loads the canonical run endpoint and normalizes the result", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      source_label: "APPROVED_PROXY_DATA_VENDOR",
      result: { run_id: "run/a", status: "COMPLETED", branches: [] },
    }), { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const client = createResearchOSV3HttpClient();
    const result = await client.getResearchResult("run/a");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/veriquant/research-os/v3/runs/run%2Fa",
      expect.objectContaining({ method: "GET", credentials: "same-origin" }),
    );
    expect(result.runId).toBe("run/a");
    expect(result.sourceLabel).toBe("已批准代理数据源");
  });

  it("fails closed on incomplete payloads", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "COMPLETED" }), { status: 200 })));
    const client = createResearchOSV3HttpClient();
    await expect(client.getResearchResult("run-1")).rejects.toEqual(expect.objectContaining<Partial<ResearchOSV3ClientError>>({
      name: "ResearchOSV3ClientError",
    }));
  });

  it("rejects an empty run id without making a request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(createResearchOSV3HttpClient().getResearchResult("  ")).rejects.toThrow("缺少研究运行标识");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fails closed on a corrupted canonical report", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      result: { run_id: "run-corrupt", status: "COMPLETED", branches: [] },
      report: { question: "问题", executive_answer: "伪结论", outcome: "EVALUATED_RESEARCH_RESULT", candidate_count: 2, candidates: [] },
    }), { status: 200 })));
    await expect(createResearchOSV3HttpClient().getResearchResult("run-corrupt")).rejects.toThrow("候选明细不完整");
  });
});
