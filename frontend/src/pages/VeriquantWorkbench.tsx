import { useEffect, useMemo, useState } from "react";
import {
  AuthorityBoundaryNotice,
  CandidateLineage,
  CitationTimeline,
  EvidenceAudit,
  FailureDriftAlerts,
  FormalResearchReport,
  PromotionStatus,
  ResearchDAG,
  RunOverview,
  ShadowPortfolio,
  StatusLegend,
} from "@/components/veriquant/WorkbenchPanels";
import type { WorkbenchDetailResponse, WorkbenchRunDetail, WorkbenchRunSummary, WorkbenchRunsResponse } from "@/components/veriquant/WorkbenchTypes";
import { normalizeWorkbenchDetail, normalizeWorkbenchRuns } from "@/components/veriquant/WorkbenchNormalize";
import { OperatorControls } from "@/components/veriquant/OperatorControls";
import { PageHero, SectionHeading } from "@/components/veriquant/NexusUI";

const WORKBENCH_ROOT = "/veriquant/workbench";

async function readJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { headers: { Accept: "application/json" }, signal });
  if (!response.ok) throw new Error(`证据详情接口暂不可用（${response.status}）`);
  return response.json() as Promise<T>;
}

export function VeriquantWorkbench() {
  const [runs, setRuns] = useState<WorkbenchRunSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [detail, setDetail] = useState<WorkbenchRunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const requestedRunId = new URLSearchParams(window.location.search).get("run_id") || "";

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    readJson<WorkbenchRunsResponse>(`${WORKBENCH_ROOT}/runs?limit=50`, controller.signal)
      .then((payload) => {
        const next = normalizeWorkbenchRuns(payload);
        setRuns(next);
        setSelectedRunId((current) => {
          if (requestedRunId && next.some((run) => run.run_id === requestedRunId)) return requestedRunId;
          return current || next[0]?.run_id || "";
        });
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError((reason as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [requestedRunId]);

  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null);
      return undefined;
    }
    const controller = new AbortController();
    readJson<WorkbenchDetailResponse>(`${WORKBENCH_ROOT}/runs/${encodeURIComponent(selectedRunId)}`, controller.signal)
      .then((payload) => setDetail(payload.detail ? normalizeWorkbenchDetail(payload.detail) : null))
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setError((reason as Error).message);
          setDetail(null);
        }
      });
    return () => controller.abort();
  }, [selectedRunId]);

  const selected = useMemo(() => runs.find((run) => run.run_id === selectedRunId), [runs, selectedRunId]);

  return (
    <>
      <PageHero
        eyebrow="证据详情页"
        title="Workbench 保留为只读审计视图。"
        description="主入口已经移动到 Campaign；这里用于查看运行、谱系、Evidence、引用、晋级、Shadow 和失败漂移告警。"
        action={<StatusLegend />}
      />

      <section className="vq-section vq-section-parchment">
        <div className="vq-container">
          <OperatorControls />
          {error ? <div className="vq-empty-state vq-top-gap" role="alert"><strong>{error}</strong></div> : null}
        </div>
      </section>

      <AuthorityBoundaryNotice />

      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <SectionHeading
            eyebrow="运行产物库"
            title="运行、谱系和决定都在一条证据链上。"
            description="界面是只读视图；金融真值仍由确定性服务和只追加 Trial Ledger 拥有。"
          />
          {loading ? (
            <div className="vq-page-loader">正在读取 Campaign 产物…</div>
          ) : (
            <div className="vq-workbench-grid">
              <RunOverview runs={runs} selectedRunId={selectedRunId} onSelect={setSelectedRunId} />
              <div className="vq-detail-stack">
                {selected ? (
                  <section className="vq-glass-panel">
                    <p className="vq-caption">当前运行</p>
                    <h2 className="vq-panel-title">{selected.title || "AlphaForge Campaign"}</h2>
                    <code className="vq-code">{selected.run_id}</code>
                  </section>
                ) : null}
                {detail ? (
                  <>
                    <FormalResearchReport detail={detail} />
                    <ResearchDAG detail={detail} />
                    <CandidateLineage detail={detail} />
                    <EvidenceAudit detail={detail} />
                    <CitationTimeline detail={detail} />
                    <PromotionStatus detail={detail} />
                    <ShadowPortfolio detail={detail} />
                    <FailureDriftAlerts detail={detail} />
                  </>
                ) : (
                  <div className="vq-empty-state">请选择一条运行查看证据详情。</div>
                )}
              </div>
            </div>
          )}
        </div>
      </section>
    </>
  );
}

export default VeriquantWorkbench;
