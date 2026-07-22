import { useEffect, useState } from "react";
import { CloudOff, Database, KeyRound, Network, Server, ShieldCheck } from "lucide-react";
import { MetricTile, PageHero, SectionHeading, StatusChip } from "@/components/veriquant/NexusUI";
import { getVeriQuantStatus } from "@/lib/veriquant";

export function SystemStatus() {
  const [connected, setConnected] = useState(false);
  const [payload, setPayload] = useState<Record<string, unknown>>({});

  useEffect(() => {
    const controller = new AbortController();
    getVeriQuantStatus(controller.signal)
      .then((value) => {
        setPayload(value);
        setConnected(true);
      })
      .catch(() => setConnected(false));
    return () => controller.abort();
  }, []);

  const capabilities = (
    payload.capabilities && typeof payload.capabilities === "object" ? payload.capabilities : {}
  ) as Record<string, unknown>;

  return (
    <>
      <PageHero
        eyebrow="系统与安全边界"
        title="AlphaForge 默认离线、权限分层、fail-closed。"
        description="页面展示研究状态和安全边界；真实 broker、真实订单、真实资金和密钥输出都不属于 AlphaForge 研究界面。"
        action={<StatusChip status={connected ? "COMPLETED" : "PENDING"} />}
      />

      <section className="vq-section vq-section-parchment">
        <div className="vq-container">
          <div className="vq-grid-4">
            <MetricTile label="研究接口" value={connected ? "运行中" : "连接中"} caption="本地只读优先" icon={Server} />
            <MetricTile label="离线模式" value="默认开启" caption="无 provider key 仍可启动" icon={CloudOff} />
            <MetricTile label="密钥保护" value="不展示" caption="不记录 token/API key/Authorization" icon={KeyRound} />
            <MetricTile label="权限架构" value="独立分层" caption="提案、证据、晋级、风险、执行分权" icon={ShieldCheck} />
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <SectionHeading
            eyebrow="核心能力"
            title="从 Campaign 到证据，一条完整研究链。"
            description="多 Agent 探索与确定性服务协同工作，为每个候选保留可追溯、可复现、可审计的研究上下文。"
          />
          <div className="vq-grid-3">
            <article className="vq-utility-card"><StatusChip status="COMPLETED" /><h3>Campaign 入口</h3><p>研究模式、研究深度、协作方式分离建模。</p></article>
            <article className="vq-utility-card"><StatusChip status="COMPLETED" /><h3>受限 FactorDAG</h3><p>结构化、可审计、可复现；禁止任意 Python 因子执行。</p></article>
            <article className="vq-utility-card"><StatusChip status="COMPLETED" /><h3>权限与内容安全</h3><p>sealed、secret、路径、工具和执行权限接受系统化检查。</p></article>
            <article className="vq-utility-card"><StatusChip status="COMPLETED" /><h3>证据服务</h3><p>官方指标从冻结输入计算，页面不可伪造。</p></article>
            <article className="vq-utility-card"><StatusChip status="COMPLETED" /><h3>因子谱系</h3><p>原创、变异、交叉与父子关系形成候选知识图谱。</p></article>
            <article className="vq-utility-card">
              <StatusChip status={capabilities.real_visible_official_evidence ? "COMPLETED" : "PENDING"} />
              <h3>真实数据 smoke</h3>
              <p>已批准代理数据源只能标记为 APPROVED_PROXY_DATA_VENDOR，不能表述为官方 Tushare。</p>
            </article>
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-dark">
        <div className="vq-container">
          <SectionHeading
            eyebrow="可信底座"
            title="离线可运行，结果可复现，权限可验证。"
            description="数据、研究、证据、晋级、组合与纸面执行各自拥有清晰职责；UI 不注册 live/broker/order 入口。"
          />
          <div className="vq-grid-3">
            <div className="vq-glass-panel"><Database aria-hidden="true" /><h3>冻结数据</h3><p>内容寻址的数据快照、交易日历和历史股票池。</p></div>
            <div className="vq-glass-panel"><Network aria-hidden="true" /><h3>确定性运行</h3><p>固定输入、配置和种子产生稳定一致的研究输出。</p></div>
            <div className="vq-glass-panel"><ShieldCheck aria-hidden="true" /><h3>分层权限</h3><p>研究创造力与证据、晋级、风险职责清晰协作。</p></div>
          </div>
        </div>
      </section>
    </>
  );
}
