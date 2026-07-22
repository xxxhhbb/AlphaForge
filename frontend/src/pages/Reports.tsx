import { useEffect, useMemo, useState } from "react";
import { FileCheck2, Filter, Search } from "lucide-react";
import { Link } from "react-router-dom";
import { EmptyState, PageHero, SectionHeading, StatusChip } from "@/components/veriquant/NexusUI";
import { listVeriQuantRuns, type VeriQuantRunSummary } from "@/lib/veriquant";

type ReportListItem = VeriQuantRunSummary & { source?: "v3" };

export function Reports() {
  const [runs, setRuns] = useState<ReportListItem[]>([]);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    Promise.allSettled([
      listVeriQuantRuns(controller.signal),
      fetch("/api/veriquant/research-os/v3/runs", { credentials: "same-origin", signal: controller.signal })
        .then(async (response) => response.ok ? response.json() as Promise<{ runs?: VeriQuantRunSummary[] }> : { runs: [] })
        .then((payload) => (payload.runs ?? []).map((run) => ({ ...run, title: run.title || "多专业研究结果", source: "v3" as const }))),
    ]).then(([legacy, v3]) => {
      const merged = new Map<string, ReportListItem>();
      if (legacy.status === "fulfilled") legacy.value.forEach((run) => merged.set(run.run_id, run));
      if (v3.status === "fulfilled") v3.value.forEach((run) => merged.set(run.run_id, run));
      setRuns([...merged.values()]);
    }).catch(() => setRuns([]));
    return () => controller.abort();
  }, []);

  const filtered = useMemo(
    () => runs.filter((run) => (
      (!status || run.status === status)
      && (!query || `${run.run_id} ${run.title || ""}`.toLowerCase().includes(query.toLowerCase()))
    )),
    [runs, query, status],
  );

  return (
    <>
      <PageHero
        eyebrow="报告中心"
        title="把研究过程沉淀成可追溯报告。"
        description="报告连接研究问题、Campaign、数据版本、FactorDAG、官方 Evidence、审查发现和代码版本；不伪造 sealed 明文或成功状态。"
      />

      <section className="vq-section vq-section-parchment">
        <div className="vq-container">
          <div className="vq-glass-panel vq-filter-panel">
            <div className="vq-operator-grid">
              <label className="vq-grow">
                <span className="vq-caption">搜索报告</span>
                <div className="vq-search-wrap">
                  <Search aria-hidden="true" />
                  <input
                    className="vq-search"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="运行 ID 或研究名称"
                  />
                </div>
              </label>
              <label>
                <span className="vq-caption">状态</span>
                <select className="vq-select" value={status} onChange={(event) => setStatus(event.target.value)}>
                  <option value="">全部状态</option>
                  <option value="INCOMPLETE">未完成</option>
                  <option value="BLOCKED">已阻断</option>
                  <option value="VALIDATED_RESEARCH">研究已验证</option>
                  <option value="CHALLENGER">挑战者</option>
                  <option value="SHADOW_ACTIVE">影子运行中</option>
                </select>
              </label>
            </div>
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <SectionHeading
            eyebrow="研究档案"
            title={`${filtered.length} 份可追溯报告`}
            description="从最初问题到最终结论，每一步都保留身份、时间、数据和证据上下文。"
          />
          {filtered.length ? (
            <div className="vq-grid-3">
              {filtered.map((run) => (
                <article className="vq-utility-card" key={run.run_id}>
                  <div className="vq-panel-bar">
                    <FileCheck2 aria-hidden="true" />
                    <StatusChip status={run.status} />
                  </div>
                  <h3>{run.title || "AlphaForge Campaign 报告"}</h3>
                  <p className="vq-code">{run.run_id}</p>
                  <Link
                    className="vq-inline-link"
                    to={run.source === "v3"
                      ? `/research/results/${encodeURIComponent(run.run_id)}`
                      : `/reports/${encodeURIComponent(run.run_id)}`}
                  >查看正式报告 →</Link>
                </article>
              ))}
            </div>
          ) : (
            <EmptyState title="暂无匹配报告" description="调整筛选条件，或从 Campaign 确认页启动新的研究流程。" />
          )}
        </div>
      </section>

      <section className="vq-section vq-section-dark">
        <div className="vq-container">
          <SectionHeading
            eyebrow="报告边界"
            title="结论清楚，证据完整，复现简单。"
            description="统一报告把数据版本、因子身份、成本假设、审查发现和晋级依据组织成一条可验证研究链。"
          />
          <Filter aria-hidden="true" />
        </div>
      </section>
    </>
  );
}
