import { useEffect, useState } from "react";
import {
  ArrowRight,
  BookOpenCheck,
  BrainCircuit,
  CircleDot,
  Compass,
  FlaskConical,
  GitBranch,
  LineChart,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { Link } from "react-router-dom";
import { SectionHeading, StatusChip } from "@/components/veriquant/NexusUI";
import { getVeriQuantStatus, listVeriQuantRuns, type VeriQuantRunSummary } from "@/lib/veriquant";

const RESEARCH_MODES = [
  {
    title: "新 Alpha 探索",
    prompt: "帮我找新的 20 日股票因子",
    output: "候选因子清单与研究路线",
  },
  {
    title: "验证我的想法",
    prompt: "盈利上调是否能预测未来收益？",
    output: "支持、拒绝或条件成立",
  },
  {
    title: "改进现有因子",
    prompt: "这个反转因子能否降低换手？",
    output: "新版本与父版本对比",
  },
  {
    title: "补充组合缺口",
    prompt: "找一个和动量、价值低相关的 Alpha",
    output: "增量 Alpha 候选",
  },
  {
    title: "诊断因子/组合",
    prompt: "最近动量因子为什么失效？",
    output: "根因排序与处理建议",
  },
  {
    title: "事件与市场专题",
    prompt: "财报后漂移在高波动期是否更强？",
    output: "条件化因子或专题报告",
  },
];

const ADVANCED_MODES = ["论文/策略复现", "新数据源评估", "上线前评审", "监控、衰减与退役"];
const DEPTHS = ["快速探索", "标准研究", "严格验证"];
const INTERACTIONS = ["引导式", "标准式", "批处理式"];

const FLOW = [
  "确认 Campaign",
  "路由专业 Desk",
  "固定方法状态机",
  "FactorDAG / PIT",
  "官方 Evidence",
  "候选聚合与报告",
];

export function Home() {
  const [runs, setRuns] = useState<VeriQuantRunSummary[]>([]);
  const [online, setOnline] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    Promise.allSettled([
      listVeriQuantRuns(controller.signal),
      getVeriQuantStatus(controller.signal),
    ]).then(([runResult, statusResult]) => {
      if (runResult.status === "fulfilled") setRuns(runResult.value);
      setOnline(statusResult.status === "fulfilled");
    });
    return () => controller.abort();
  }, []);

  return (
    <>
      <section className="vq-home-command">
        <div className="vq-container vq-home-command-grid">
          <div className="vq-home-intro">
            <p className="vq-eyebrow">AlphaForge（千因研究院）</p>
            <h1>你今天希望解决什么研究问题？</h1>
            <p>
              AlphaForge 不是让你选择“动量、反转、基本面”这些因子类别，而是先理解你这次研究的目标：
              找新 Alpha、验证想法、改进因子、补组合缺口、诊断失效，或研究事件专题。
            </p>
            <div className="vq-question-box" role="group" aria-label="研究问题输入示例">
              <span>示例</span>
              <strong>最近动量因子为什么失效？</strong>
              <p>系统会把它编译为“诊断模式”，再路由到元阿尔法、市场结构和宏观状态 Desk。</p>
            </div>
            <div className="vq-home-actions">
              <Link className="vq-button-primary" to="/campaign/confirm">
                生成 Campaign 草案 <ArrowRight aria-hidden="true" />
              </Link>
              <Link className="vq-button-secondary" to="/campaign/confirm?mode=unsure">
                我不确定，帮我选择
              </Link>
            </div>
          </div>

          <aside className="vq-command-board" aria-label="Campaign 运行概览">
            <div className="vq-command-board-top">
              <span><CircleDot aria-hidden="true" />研究内核</span>
              <StatusChip status={online ? "COMPLETED" : "PENDING"} />
            </div>
            <div className="vq-pipeline-strip">
              {FLOW.map((step, index) => (
                <div className="vq-pipeline-step" key={step}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <strong>{step}</strong>
                </div>
              ))}
            </div>
            <div className="vq-command-note">
              <ShieldCheck aria-hidden="true" />
              <p>
                LLM 和研究 Desk 只能生成草案与解释；官方指标、晋级、组合、风险和账本由确定性服务负责。
              </p>
            </div>
          </aside>
        </div>
      </section>

      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <SectionHeading
            eyebrow="六个高频研究模式"
            title="先回答“为什么研究”，再决定调用哪些专业 Agent。"
            description="动量、价值、事件、基本面是研究对象或分支，不是研究模式。每次运行都必须来自用户确认过的 Campaign。"
          />
          <div className="vq-mode-grid">
            {RESEARCH_MODES.map((mode, index) => (
              <Link
                className="vq-mode-card"
                key={mode.title}
                to={`/campaign/confirm?mode=${encodeURIComponent(mode.title)}`}
              >
                <span>{String(index + 1).padStart(2, "0")}</span>
                <h3>{mode.title}</h3>
                <p>{mode.prompt}</p>
                <small>{mode.output}</small>
              </Link>
            ))}
          </div>
          <div className="vq-advanced-strip">
            <Compass aria-hidden="true" />
            <div>
              <strong>高级研究</strong>
              <p>{ADVANCED_MODES.join(" · ")}</p>
            </div>
            <Link to="/campaign/confirm?mode=advanced">进入高级研究</Link>
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-parchment">
        <div className="vq-container">
          <SectionHeading
            eyebrow="研究深度 × 协作方式"
            title="每个 Campaign 都有预算、检查点和停止条件。"
            description="快速探索只产出草案；标准研究进入 PIT、Evidence 和 Critic；严格验证增加 sealed、容量、成本、压力和独立审批。"
          />
          <div className="vq-grid-2">
            <article className="vq-glass-panel">
              <div className="vq-card-icon"><FlaskConical aria-hidden="true" /></div>
              <h3>研究深度</h3>
              <div className="vq-pill-list">
                {DEPTHS.map((item) => <span key={item}>{item}</span>)}
              </div>
              <p>系统不会把“快速探索”的草案包装成官方证据，也不会绕过 sealed test。</p>
            </article>
            <article className="vq-glass-panel">
              <div className="vq-card-icon"><BrainCircuit aria-hidden="true" /></div>
              <h3>协作方式</h3>
              <div className="vq-pill-list">
                {INTERACTIONS.map((item) => <span key={item}>{item}</span>)}
              </div>
              <p>用户确认 Research Brief、候选短名单和关键验证节点；批处理也必须有明确终点。</p>
            </article>
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-white">
        <div className="vq-container vq-home-proof-grid">
          <article className="vq-proof-panel">
            <Sparkles aria-hidden="true" />
            <h2>多 Desk 不是全员上阵。</h2>
            <p>Router 只给被选中的 1–3 个专业 Desk 上下文和工具，降低噪声，也减少权限暴露面。</p>
          </article>
          <article className="vq-proof-panel">
            <BookOpenCheck aria-hidden="true" />
            <h2>每个结论都回到证据。</h2>
            <p>CandidateSet、诊断报告、事件报告和组合缺口报告都有不同出口，不能混成一个“自动晋级”。</p>
          </article>
          <article className="vq-proof-panel">
            <GitBranch aria-hidden="true" />
            <h2>因子库是研究入口。</h2>
            <p>你可以从某个因子发起验证、改进、诊断、补缺口或监控，但不能直接改官方指标和持仓。</p>
          </article>
        </div>
      </section>

      <section className="vq-section vq-section-parchment">
        <div className="vq-container">
          <SectionHeading
            eyebrow="最近运行"
            title="已沉淀的 Campaign 会出现在这里。"
            description="运行详情、证据链、候选谱系、晋级状态和影子组合都可以从只读 Workbench 追溯。"
          />
          <div className="vq-grid-3">
            {runs.slice(0, 3).map((run) => (
              <Link key={run.run_id} to="/veriquant/workbench" className="vq-utility-card vq-link-card">
                <StatusChip status={run.status} />
                <h3>{run.title || "AlphaForge Campaign"}</h3>
                <p className="vq-code">{run.run_id}</p>
              </Link>
            ))}
            {runs.length === 0 ? (
              <Link to="/campaign/dashboard" className="vq-utility-card vq-link-card">
                <StatusChip status={online ? "PENDING" : "INCOMPLETE"} />
                <h3>运行驾驶舱已就绪</h3>
                <p>确认 Campaign 后，分支状态、检查点、Evidence 和报告会在这里展开。</p>
              </Link>
            ) : null}
            <Link to="/shadow" className="vq-utility-card vq-link-card">
              <LineChart aria-hidden="true" />
              <h3>Shadow Portfolio</h3>
              <p>只接收通过风险门的挑战者，纸面回放，不连接真实 broker。</p>
            </Link>
          </div>
        </div>
      </section>
    </>
  );
}
