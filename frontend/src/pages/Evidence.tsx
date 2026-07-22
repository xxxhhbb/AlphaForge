import { useEffect, useState } from "react";
import { BookOpenCheck, FileCheck2, LockKeyhole, Scale, ShieldCheck } from "lucide-react";
import { Link } from "react-router-dom";
import { EmptyState, MetricTile, PageHero, SectionHeading, StatusChip } from "@/components/veriquant/NexusUI";
import {
  asRecord,
  getVeriQuantRun,
  listVeriQuantRuns,
  valueText,
  type VeriQuantRunDetail,
  type VeriQuantRunSummary,
} from "@/lib/veriquant";

export function Evidence() {
  const [runs, setRuns] = useState<VeriQuantRunSummary[]>([]);
  const [details, setDetails] = useState<Record<string, VeriQuantRunDetail>>({});

  useEffect(() => {
    const controller = new AbortController();
    listVeriQuantRuns(controller.signal)
      .then(async (items) => {
        setRuns(items);
        const resolved = await Promise.all(items.map(async (run) => {
          try {
            return [run.run_id, await getVeriQuantRun(run.run_id, controller.signal)] as const;
          } catch {
            return [run.run_id, null] as const;
          }
        }));
        setDetails(Object.fromEntries(resolved.filter((item): item is readonly [string, VeriQuantRunDetail] => Boolean(item[1]))));
      })
      .catch(() => setRuns([]));
    return () => controller.abort();
  }, []);

  const challengerCount = runs.filter((run) => run.status === "CHALLENGER").length;
  const validatedCount = runs.filter((run) => run.status === "VALIDATED_RESEARCH").length;

  return (
    <>
      <PageHero
        eyebrow="Evidence 与报告"
        title="让每个结论都能追到冻结输入、代码版本和审计链。"
        description="官方证据只由确定性 Evidence Service 计算；报告负责解释结果，不替代 Promotion、Risk Gate 或账本事实。"
      />

      <section className="vq-section vq-section-parchment">
        <div className="vq-container">
          <div className="vq-grid-4">
            <MetricTile label="证据运行" value={runs.length} caption="冻结输入上的确定性计算" icon={BookOpenCheck} />
            <MetricTile label="研究已验证" value={validatedCount} caption="通过可见验证与审查" icon={ShieldCheck} />
            <MetricTile label="挑战者" value={challengerCount} caption="独立晋级后的候选" icon={Scale} />
            <MetricTile label="sealed 展示" value="脱敏" caption="不泄漏测试集明文" icon={LockKeyhole} />
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <SectionHeading
            eyebrow="报告档案"
            title="同一个地方看 Evidence、Critic、Promotion 与研究报告。"
            description="Workbench 现在是 Campaign 的只读证据详情页；主流程从首页和 Campaign 驾驶舱进入。"
          />
          <div className="vq-grid-2">
            {runs.map((run) => {
              const promotion = asRecord(details[run.run_id]?.promotion);
              return (
                <article className="vq-utility-card" key={run.run_id}>
                  <div className="vq-panel-bar">
                    <span className="vq-code">{run.run_id}</span>
                    <StatusChip status={run.status} />
                  </div>
                  <h3>{run.title || valueText(promotion.verdict || promotion.status, "Campaign 报告")}</h3>
                  <p>失败代码：{valueText(promotion.failure_code || promotion.sealed_failure_code, "暂无")}</p>
                  <Link className="vq-inline-link" to="/veriquant/workbench">
                    查看只读证据详情
                  </Link>
                </article>
              );
            })}
            {runs.length === 0 ? (
              <EmptyState
                title="暂无证据运行"
                description="确认 Campaign 并完成标准研究后，官方证据、审查器结论和报告摘要会出现在这里。"
              />
            ) : null}
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-dark">
        <div className="vq-container">
          <SectionHeading
            eyebrow="sealed test 防火墙"
            title="研究可以开放，测试必须独立。"
            description="研究 Desk、反馈、父代选择、mutation/crossover 和下一轮 prompt 都不能读取 sealed 内容。页面只展示脱敏决定和可验证 hash。"
          />
          <div className="vq-grid-3">
            <article className="vq-glass-panel">
              <FileCheck2 aria-hidden="true" />
              <h3>可见证据</h3>
              <p>用于 train/validation 反馈与候选选择。</p>
            </article>
            <article className="vq-glass-panel">
              <LockKeyhole aria-hidden="true" />
              <h3>密封证据</h3>
              <p>Promotion Controller 独占读取，不进入 LLM context。</p>
            </article>
            <article className="vq-glass-panel">
              <ShieldCheck aria-hidden="true" />
              <h3>审计链</h3>
              <p>hash、identity、Trial Ledger 和报告互相可追溯。</p>
            </article>
          </div>
        </div>
      </section>
    </>
  );
}
