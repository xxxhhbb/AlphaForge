import { useEffect, useState } from "react";
import { BarChart3, Layers3, LineChart, RotateCcw } from "lucide-react";
import { EmptyState, MetricTile, PageHero, SectionHeading, StatusChip } from "@/components/veriquant/NexusUI";
import { listVeriQuantRuns, type VeriQuantRunSummary } from "@/lib/veriquant";

export function Shadow() {
  const [runs, setRuns] = useState<VeriQuantRunSummary[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    listVeriQuantRuns(controller.signal).then(setRuns).catch(() => setRuns([]));
    return () => controller.abort();
  }, []);

  const shadow = runs.filter((run) => run.status === "SHADOW_ACTIVE");

  return (
    <>
      <PageHero
        eyebrow="Shadow Portfolio"
        title="让挑战者进入连续纸面观察，而不连接真实账户。"
        description="只有经过 Promotion、Portfolio Constructor 和独立 Risk Gate 的候选，才能进入纸面执行与只追加账本。"
      />

      <section className="vq-section vq-section-parchment">
        <div className="vq-container">
          <div className="vq-grid-4">
            <MetricTile label="组合运行" value={shadow.length} caption="研究资产的连续纸面观察" icon={LineChart} />
            <MetricTile label="每日数据版本" value="独立" caption="每天只使用当日可得信息" icon={Layers3} />
            <MetricTile label="执行成本" value="建模" caption="价差、冲击与参与率约束" icon={BarChart3} />
            <MetricTile label="历史重放" value="确定性" caption="事件驱动、幂等、可恢复" icon={RotateCcw} />
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <SectionHeading
            eyebrow="组合运行"
            title="从挑战者信号，到每日研究净值。"
            description="组合状态、NAV、回撤、换手、成本和因子归因统一汇总到可重放研究账本。"
          />
          {shadow.length === 0 ? (
            <EmptyState
              title="影子组合空间已就绪"
              description="首个真实挑战者晋级并通过风险门后，20 日回放、每日归因和账本对账会在这里展开。"
            />
          ) : (
            <div className="vq-grid-3">
              {shadow.map((run) => (
                <article className="vq-utility-card" key={run.run_id}>
                  <StatusChip status={run.status} />
                  <h3>{run.title || "影子组合"}</h3>
                  <p className="vq-code">{run.run_id}</p>
                </article>
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="vq-section vq-section-dark">
        <div className="vq-container">
          <SectionHeading
            eyebrow="研究级执行"
            title="每天可重放，每笔可归因。"
            description="确定性时钟、参与率约束、部分成交、只追加账本和每日对账，为 Alpha 的组合表现提供完整研究视角。"
          />
        </div>
      </section>
    </>
  );
}
