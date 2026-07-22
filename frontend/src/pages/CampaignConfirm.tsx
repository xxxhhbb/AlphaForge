import { ArrowRight, CheckCircle2, LockKeyhole, Route, ShieldCheck } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHero, SectionHeading, StatusChip } from "@/components/veriquant/NexusUI";

const MODE_HINTS: Record<string, string> = {
  "新 Alpha 探索": "寻找新的 20 日股票 Alpha，并限制候选家族和试验预算。",
  "验证我的想法": "把用户想法预注册为可证伪假设，再进入 PIT 与 Evidence。",
  "改进现有因子": "需要父因子身份，子版本只能做受限变异和父子比较。",
  "补充组合缺口": "从已有因子/组合的残差和相关性出发寻找增量 Alpha。",
  "诊断因子/组合": "默认产出根因排序，不自动生成持仓或直接晋级。",
  "事件与市场专题": "区分事件发生、信息可得与可交易时间，输出专题报告或条件化候选。",
  advanced: "高级研究需要更明确的数据、复现或上线评审边界。",
  unsure: "Copilot 只会给推荐和解释；运行前仍必须由用户确认 Campaign。",
};

const DEFAULT_MODE = "新 Alpha 探索";

const briefRows = [
  ["研究对象", "A 股历史股票池，20 日预测周期"],
  ["研究深度", "标准研究：PIT、walk-forward、Critic、增量性检验"],
  ["协作方式", "标准式：确认 Brief、候选短名单、验证结论"],
  ["允许数据", "价格、成交量、行业、市值、基本面、宏观字段"],
  ["预算", "最多 3 个 Desk、4 个因子家族、24 次 Trial"],
  ["停止条件", "预算耗尽、无增量候选、用户停止、验证完成"],
];

const checkpoints = ["确认 Research Brief", "确认候选短名单", "sealed 验证前确认", "Promotion 前确认"];

export function CampaignConfirm() {
  const [searchParams] = useSearchParams();
  const requestedMode = searchParams.get("mode") || DEFAULT_MODE;
  const mode = MODE_HINTS[requestedMode] ? requestedMode : DEFAULT_MODE;
  const note = MODE_HINTS[requestedMode] || MODE_HINTS[DEFAULT_MODE];

  return (
    <>
      <PageHero
        eyebrow="Campaign 确认页"
        title="先确认研究合同，再启动任何实验。"
        description="未确认的 Campaign Draft 不能启动 FactorDAG、PIT、Evidence 或 Promotion。这里展示的是用户即将批准的研究边界。"
        action={<StatusChip status="NEEDS_CONFIRMATION" />}
      />

      <section className="vq-section vq-section-parchment">
        <div className="vq-container vq-confirm-layout">
          <article className="vq-glass-panel vq-confirm-main">
            <div className="vq-card-icon"><Route aria-hidden="true" /></div>
            <p className="vq-eyebrow">Research Brief</p>
            <h2>{mode}</h2>
            <p>{note}</p>
            <div className="vq-brief-table">
              {briefRows.map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
            <div className="vq-page-actions">
              <Link className="vq-button-primary" to="/campaign/dashboard">
                确认并进入驾驶舱 <ArrowRight aria-hidden="true" />
              </Link>
              <Link className="vq-button-secondary" to="/">返回首页修改问题</Link>
            </div>
          </article>

          <aside className="vq-glass-panel">
            <div className="vq-card-icon"><LockKeyhole aria-hidden="true" /></div>
            <h3>运行前检查点</h3>
            <ul className="vq-check-list">
              {checkpoints.map((item) => <li key={item}><CheckCircle2 aria-hidden="true" />{item}</li>)}
            </ul>
            <div className="vq-command-note">
              <ShieldCheck aria-hidden="true" />
              <p>页面不能代替用户确认，也不能写 Evidence、Promotion、风险审批或持仓。</p>
            </div>
          </aside>
        </div>
      </section>

      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <SectionHeading
            eyebrow="确认规则"
            title="Campaign 是研究的边界，不是一个漂亮表单。"
            description="它冻结 universe、horizon、decision time、allowed data、预算、检查点和停止条件；所有 Trial 与证据必须能追溯到这个 Campaign hash。"
          />
          <div className="vq-grid-3">
            <article className="vq-utility-card">
              <StatusChip status="DRAFT" />
              <h3>Draft 不可执行</h3>
              <p>未确认草案只能用于讨论和澄清，不能触发官方证据或晋级。</p>
            </article>
            <article className="vq-utility-card">
              <StatusChip status="CONFIRMED" />
              <h3>确认后才路由</h3>
              <p>Router 根据模式、数据可用性、预算和 Desk 能力生成可审计 DAG。</p>
            </article>
            <article className="vq-utility-card">
              <StatusChip status="BLOCKED" />
              <h3>缺数据则阻断</h3>
              <p>数据不可用时必须明确 BLOCKED 或降级建议，不能静默换数据。</p>
            </article>
          </div>
        </div>
      </section>
    </>
  );
}
