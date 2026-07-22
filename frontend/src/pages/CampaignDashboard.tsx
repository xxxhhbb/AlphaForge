import { useEffect, useState } from "react";
import {
  AlertTriangle,
  BookOpenCheck,
  BrainCircuit,
  CheckCircle2,
  Clock3,
  FlaskConical,
  GitBranch,
  PauseCircle,
  ShieldCheck,
} from "lucide-react";
import { Link } from "react-router-dom";
import { PageHero, SectionHeading, StatusChip } from "@/components/veriquant/NexusUI";
import { listVeriQuantRuns, type VeriQuantRunSummary } from "@/lib/veriquant";

const DESKS = [
  { name: "元阿尔法 Desk", status: "RUNNING", task: "分析现有因子相关性与增量缺口" },
  { name: "市场结构 Desk", status: "RUNNING", task: "检查换手、流动性、冲击成本与拥挤风险" },
  { name: "宏观状态 Desk", status: "PENDING", task: "划分交易时点可知的市场状态" },
];

const STAGES = [
  ["scope", "确认 Campaign 边界", "COMPLETED"],
  ["route", "选择 1–3 个专业 Desk", "COMPLETED"],
  ["method", "运行方法专属状态机", "RUNNING"],
  ["factor", "FactorDAG / PIT", "PENDING"],
  ["evidence", "官方 Evidence 与 Critic", "PENDING"],
  ["aggregate", "聚合报告与用户检查点", "PENDING"],
];

const ARTIFACTS = [
  "proposal.json",
  "factor_spec.json",
  "experiment_manifest.json",
  "evidence_pack.json",
  "critic_findings.json",
  "events.jsonl",
];

export function CampaignDashboard() {
  const [runs, setRuns] = useState<VeriQuantRunSummary[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    listVeriQuantRuns(controller.signal).then(setRuns).catch(() => setRuns([]));
    return () => controller.abort();
  }, []);

  return (
    <>
      <PageHero
        eyebrow="Campaign 运行驾驶舱"
        title="专业 Agent 并行提案，确定性服务负责真值。"
        description="驾驶舱展示 Campaign 的路由、分支状态、检查点和产物；它不替代 Evidence Court、Promotion Controller 或 Risk Gate。"
        action={<Link className="vq-button-secondary" to="/veriquant/workbench">查看证据详情</Link>}
      />

      <section className="vq-section vq-section-parchment">
        <div className="vq-container vq-dashboard-grid">
          <article className="vq-glass-panel">
            <div className="vq-panel-bar">
              <div>
                <p className="vq-eyebrow">当前 Campaign</p>
                <h2 className="vq-panel-title">补充组合缺口 · 标准研究</h2>
              </div>
              <StatusChip status="RUNNING" />
            </div>
            <p>
              目标：寻找与现有动量和价值低相关的 20 日 Alpha。系统只给被路由的 Desk 上下文，
              未选中的 Desk 不获得工具和 Campaign 内容。
            </p>
            <div className="vq-campaign-timeline">
              {STAGES.map(([id, label, status]) => (
                <div className="vq-timeline-row" key={id}>
                  <span><Clock3 aria-hidden="true" /></span>
                  <strong>{label}</strong>
                  <StatusChip status={status} />
                </div>
              ))}
            </div>
          </article>

          <aside className="vq-glass-panel">
            <div className="vq-card-icon"><PauseCircle aria-hidden="true" /></div>
            <h3>检查点与恢复</h3>
            <p>每个阶段提交后写入 artifact hash；恢复时跳过已提交阶段，不重复调用 LLM 或重复写 Trial。</p>
            <div className="vq-page-actions">
              <button className="vq-button-secondary" type="button">暂停 Campaign</button>
              <button className="vq-button-secondary" type="button">从检查点恢复</button>
            </div>
          </aside>
        </div>
      </section>

      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <SectionHeading
            eyebrow="已路由 Desk"
            title="不会默认六个 Desk 全开。"
            description="路由器根据研究模式、研究对象、数据可用性和预算选择专业 Agent；失败分支不会伪造成成功。"
          />
          <div className="vq-desk-grid">
            {DESKS.map((desk, index) => (
              <article className="vq-desk-card" key={desk.name}>
                <div className="vq-desk-card-top">
                  <BrainCircuit aria-hidden="true" />
                  <span>{String(index + 1).padStart(2, "0")}</span>
                </div>
                <h3>{desk.name}</h3>
                <p>{desk.task}</p>
                <StatusChip status={desk.status} />
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-parchment">
        <div className="vq-container">
          <SectionHeading
            eyebrow="运行产物"
            title="每个分支有独立命名空间。"
            description="artifact 稳定排序，不能因为并发完成顺序改变 hash；sealed 信息不进入 prompt、trace、feedback 或页面明文。"
          />
          <div className="vq-grid-3">
            {ARTIFACTS.map((artifact) => (
              <article className="vq-utility-card" key={artifact}>
                <FlaskConical aria-hidden="true" />
                <h3 className="vq-code">{artifact}</h3>
                <p>内容寻址、只读展示、可回放审计。</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-dark">
        <div className="vq-container">
          <SectionHeading
            eyebrow="安全边界"
            title="驾驶舱只编排，不裁决。"
            description="LLM 输出始终是 DRAFT 或解释；官方证据、晋级、风险审批、订单和账本都不属于页面权限。"
          />
          <div className="vq-grid-4">
            <article className="vq-glass-panel"><BookOpenCheck aria-hidden="true" /><h3>Evidence</h3><p>从冻结输入计算。</p></article>
            <article className="vq-glass-panel"><ShieldCheck aria-hidden="true" /><h3>Promotion</h3><p>同 proposer 不批准自己。</p></article>
            <article className="vq-glass-panel"><GitBranch aria-hidden="true" /><h3>Lineage</h3><p>失败也消耗预算。</p></article>
            <article className="vq-glass-panel"><AlertTriangle aria-hidden="true" /><h3>Fail Closed</h3><p>越权、缺数据、歧义状态全部阻断。</p></article>
          </div>
          {runs.length ? (
            <div className="vq-run-list vq-run-list-spaced">
              {runs.slice(0, 3).map((run) => (
                <Link className="vq-run-button vq-link-card" key={run.run_id} to="/veriquant/workbench">
                  <div className="vq-run-button-top">
                    <strong>{run.title || "历史 Campaign"}</strong>
                    <StatusChip status={run.status} />
                  </div>
                  <code>{run.run_id}</code>
                </Link>
              ))}
            </div>
          ) : (
            <p className="vq-caption">暂无历史运行；确认 Campaign 后会自动沉淀到证据详情页。</p>
          )}
        </div>
      </section>

      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <div className="vq-advanced-strip">
            <CheckCircle2 aria-hidden="true" />
            <div>
              <strong>下一步</strong>
              <p>如果真实数据与 20 日 Shadow 回放完成，驾驶舱会展示真实数据 smoke、连续回放和恢复 drill 的结果。</p>
            </div>
            <Link to="/system">查看系统边界</Link>
          </div>
        </div>
      </section>
    </>
  );
}
