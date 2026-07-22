import { describe, expect, it } from "vitest";
import { normalizeResearchOSV3Run } from "../normalize";

describe("normalizeResearchOSV3Run", () => {
  it("maps U4 DRAFT results without inventing official evidence", () => {
    const view = normalizeResearchOSV3Run({
      source_label: "APPROVED_PROXY_DATA_VENDOR",
      model: "deepseek-v4-flash-260425",
      result: {
        run_id: "run-1",
        status: "COMPLETED",
        candidate_count: 2,
        accepted_preflight_count: 1,
        manifest_hash: "sha256:manifest",
        campaign_hash: "sha256:campaign",
        context_hash: "sha256:context",
        official_evidence_hash: null,
        promotion_hash: null,
        branches: [{
          specialist_id: "market_structure",
          status: "COMPLETED_DRAFT",
          provider_calls: 4,
          candidate_refs: [
            { candidate_id: "safe", candidate_hash: "sha256:c1", preflight_status: "ACCEPTED", factor_dag_hash: "sha256:dag" },
            { candidate_id: "revise", candidate_hash: "sha256:c2", preflight_status: "REVISE", factor_dag_hash: "sha256:must-not-render", failure_code: "REQUESTED_LOOKBACK_NOT_REPRESENTED" },
          ],
        }],
      },
    });

    expect(view.authorityLabel).toBe("研究草案");
    expect(view.machineValidation.evidence).toBe("尚未执行");
    expect(view.machineValidation.promotion).toBe("未申请");
    expect(view.selectedGroups[0].title).toBe("市场行为与交易可行性研究组");
    expect(view.selectedGroups[0].candidates[0].factorDagHash).toBe("sha256:dag");
    expect(view.selectedGroups[0].candidates[1].factorDagHash).toBeUndefined();
  });

  it("allowlists formal report fields and drops sealed content", () => {
    const view = normalizeResearchOSV3Run({
      result: { run_id: "run-2", status: "COMPLETED", branches: [] },
      report: {
        title: "报告",
        question: "问题",
        conclusion: "结论",
        outcome: "部分结果",
        data_summary: "数据",
        machine_conclusion: "机器结论",
        report_hash: "sha256:report",
        sealed_metrics: { secret_rank: "LEAK-ME-NOT" },
        sealed_reason: "LEAK-ME-NOT",
      },
    });
    expect(JSON.stringify(view)).not.toContain("LEAK-ME-NOT");
    expect(view.report?.reportHash).toBe("sha256:report");
  });

  it("fails visibly for unknown or malformed status instead of claiming success", () => {
    const view = normalizeResearchOSV3Run({ result: { status: "SOMETHING_NEW", branches: [{ status: "UNKNOWN" }] } });
    expect(view.outcome).toBe("失败");
    expect(view.selectedGroups[0].outcome).toBe("系统异常");
    expect(view.machineValidation.evidence).toBe("尚未执行");
  });

  it("normalizes canonical machine selection while dropping sealed fields", () => {
    const view = normalizeResearchOSV3Run({
      result: { run_id: "run-3", status: "COMPLETED", branches: [], candidate_count: 1, accepted_preflight_count: 1 },
      report: {
        question: "问题", executive_answer: "机器结论", outcome: "EVALUATED_RESEARCH_RESULT",
        candidate_count: 1, accepted_preflight_count: 1, evaluated_candidate_count: 1, rejected_candidate_count: 0,
        selected_candidate_hashes: ["sha256:c1"], best_candidate_hash: "sha256:c1",
        candidates: [{ candidate_id: "c1", candidate_hash: "sha256:c1", group_label: "研究组", hypothesis: "假设", economic_mechanism: "机制", expression: "field(close)", preflight_status: "ACCEPTED", machine_evidence_status: "OFFICIAL_VISIBLE_EVALUATED", official_metrics: [{ name: "icir", value: 0.5, evidence_hash: "sha256:e" }], selection_verdict: "SELECTED_FOR_PROMOTION_REVIEW", selection_reason_codes: ["PIT_EVIDENCE_CRITIC_VISIBLE_SELECTION_PASS"] }],
        sealed_metrics: "LEAK-ME-NOT",
      },
    });
    expect(view.report?.funnel?.machineEvaluated).toBe(1);
    expect(view.report?.candidates[0].isBest).toBe(true);
    expect(view.report?.candidates[0].verdict).toBe("进入独立评审短名单");
    expect(JSON.stringify(view)).not.toContain("LEAK-ME-NOT");
  });

  it("fails closed when canonical candidate detail is incomplete", () => {
    expect(() => normalizeResearchOSV3Run({
      result: { run_id: "run-4", status: "COMPLETED", branches: [] },
      report: { question: "问题", executive_answer: "结论", outcome: "EVALUATED_RESEARCH_RESULT", candidate_count: 1, candidates: [] },
    })).toThrow("候选明细不完整");
  });

  it.each([
    ["SYSTEM_FAILURE_REPORT", "系统故障"],
    ["CANCELLED_RESEARCH_REPORT", "已取消"],
  ])("renders terminal report outcome %s without claiming success", (canonicalOutcome, label) => {
    const view = normalizeResearchOSV3Run({
      result: { run_id: "run-terminal", status: "FAILED", branches: [] },
      report: {
        question: "问题",
        executive_answer: "终态说明",
        outcome: canonicalOutcome,
        candidate_count: 0,
        candidates: [],
      },
    });
    expect(view.report?.outcome).toBe(label);
    expect(view.report?.conclusion).toBe("终态说明");
  });
});
