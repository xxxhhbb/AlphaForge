import { useEffect, useState } from "react";
import { ArrowLeft, ShieldCheck } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { FormalResearchReport } from "@/components/veriquant/WorkbenchPanels";
import type { WorkbenchDetailResponse, WorkbenchRunDetail } from "@/components/veriquant/WorkbenchTypes";
import { normalizeWorkbenchDetail } from "@/components/veriquant/WorkbenchNormalize";
import { PageHero } from "@/components/veriquant/NexusUI";

async function loadReport(runId: string, signal: AbortSignal): Promise<WorkbenchRunDetail> {
  const response = await fetch(`/veriquant/workbench/runs/${encodeURIComponent(runId)}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) throw new Error(`正式报告暂时无法读取（${response.status}）。`);
  const payload = await response.json() as WorkbenchDetailResponse;
  return normalizeWorkbenchDetail(payload.detail);
}

export function FormalReportPage() {
  const { runId = "" } = useParams();
  const [detail, setDetail] = useState<WorkbenchRunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!runId) {
      setError("缺少研究运行标识。");
      setLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    loadReport(runId, controller.signal)
      .then(setDetail)
      .catch((reason) => {
        if (!controller.signal.aborted) setError((reason as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [runId]);

  return (
    <>
      <PageHero
        eyebrow="正式研究报告"
        title="机器证据支持的中文研究结论。"
        description="报告只呈现已发布的公开研究证据，不展示封闭样本内容，也不允许页面改写机器指标或晋级结论。"
        action={(
          <div className="vq-report-actions">
            <Link className="vq-button-secondary" to="/reports"><ArrowLeft aria-hidden="true" />返回报告中心</Link>
            <Link className="vq-button-secondary" to={`/veriquant/workbench?run_id=${encodeURIComponent(runId)}`}>
              <ShieldCheck aria-hidden="true" />查看完整证据链
            </Link>
          </div>
        )}
      />
      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <p className="vq-caption">运行标识</p>
          <code className="vq-code">{runId}</code>
          {loading ? <div className="vq-page-loader">正在读取正式报告…</div> : null}
          {error ? <div className="vq-empty-state vq-top-gap" role="alert"><strong>{error}</strong></div> : null}
          {!loading && !error && detail ? <div className="vq-top-gap"><FormalResearchReport detail={detail} /></div> : null}
        </div>
      </section>
    </>
  );
}

export default FormalReportPage;
