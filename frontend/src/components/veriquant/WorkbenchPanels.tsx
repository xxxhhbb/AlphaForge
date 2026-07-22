import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import type { WorkbenchRunDetail, WorkbenchRunSummary, WorkbenchStatus } from "./WorkbenchTypes";
import { STATUS_LABELS, WORKBENCH_STATUSES } from "./WorkbenchTypes";
import { asArray, asRecord, valueText } from "@/lib/veriquant";
import { EmptyState } from "./NexusUI";

// Stable, non-rendered identifiers retained for older Workbench automation.
// User-facing titles and authority notices below remain Chinese-only.
export const WORKBENCH_PANEL_CONTRACTS = [
  "Run Overview",
  "Research DAG",
  "Candidate Lineage",
  "Evidence Audit",
  "Source/Citation Timeline",
  "Promotion Status",
  "Shadow Portfolio",
  "Failure/Drift Alerts",
] as const;

export const SEMANTIC_BOUNDARY_CONTRACTS = [
  "Source-backed hypothesis is not alpha evidence.",
  "DRAFT factor is not CHALLENGER.",
  "Shadow Portfolio is not a real account.",
  "Sealed data shows only redacted decision or failure code.",
  "News citation is not a tradable signal.",
] as const;

export function WorkbenchStatusBadge({ status }: { status: string }) {
  const normalized = WORKBENCH_STATUSES.includes(status as WorkbenchStatus)
    ? (status as WorkbenchStatus)
    : "INCOMPLETE";
  return <span className={`vq-status vq-status-${normalized.toLowerCase()}`}>{STATUS_LABELS[normalized]}</span>;
}

export function AuthorityBoundaryNotice() {
  const rules = [
    "研究席位只提出假设、草案和解释，不写官方指标。",
    "时点服务只还原每个决策时刻可得的信息世界。",
    "证据服务只从冻结输入计算可重放指标。",
    "晋级控制器独占 Promotion 与 sealed test 决策。",
    "组合与风险服务独立检查挑战者，不被页面覆盖。",
    "纸面执行账本只追加保存运行与归因事实。",
  ];
  return (
    <section className="vq-section vq-section-dark vq-authority">
      <div className="vq-container">
        <p className="vq-eyebrow">可信协作边界</p>
        <h2 className="vq-panel-title vq-panel-title-large">每个角色只拥有自己的权限。</h2>
        <ul>{rules.map((item) => <li key={item}>{item}</li>)}</ul>
      </div>
    </section>
  );
}

export function StatusLegend() {
  return (
    <div className="vq-status-legend">
      {WORKBENCH_STATUSES.map((status) => <WorkbenchStatusBadge key={status} status={status} />)}
    </div>
  );
}

export function RunOverview({
  runs,
  selectedRunId,
  onSelect,
}: {
  runs: WorkbenchRunSummary[];
  selectedRunId?: string;
  onSelect: (runId: string) => void;
}) {
  if (!runs.length) {
    return <EmptyState title="证据详情页已就绪" description="确认 Campaign 并完成运行后，这里会显示只读证据链。" />;
  }
  return (
    <section className="vq-glass-panel">
      <div className="vq-panel-bar">
        <h2 className="vq-panel-title">运行列表</h2>
        <span className="vq-caption">{runs.length} 条</span>
      </div>
      <div className="vq-run-list">
        {runs.map((run) => (
          <button
            key={run.run_id}
            className={`vq-run-button ${selectedRunId === run.run_id ? "active" : ""}`}
            onClick={() => onSelect(run.run_id)}
            type="button"
          >
            <div className="vq-run-button-top">
              <strong>{run.title || "AlphaForge Campaign 运行"}</strong>
              <WorkbenchStatusBadge status={run.status} />
            </div>
            <code>{run.run_id}</code>
            <p className="vq-caption">
              决策时间：{valueText(run.decision_time)} · 告警：{run.alert_count ?? 0}
            </p>
          </button>
        ))}
      </div>
    </section>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="vq-utility-card vq-detail-card">
      <h2 className="vq-panel-title">{title}</h2>
      {children}
    </section>
  );
}

function Missing({ title, description }: { title: string; description: string }) {
  return <EmptyState title={title} description={description} />;
}

export function FormalResearchReport({ detail }: { detail: WorkbenchRunDetail }) {
  const report = asRecord(detail.report);
  const markdown = typeof report.markdown === "string" ? report.markdown : "";
  return (
    <Panel title="正式中文研究报告">
      {markdown ? (
        <>
          <div className="vq-formal-report">
            <ReactMarkdown>{markdown}</ReactMarkdown>
          </div>
          <p className="vq-caption vq-top-gap">
            报告标识：<code>{valueText(report.report_hash)}</code>
          </p>
        </>
      ) : (
        <Missing title="正式报告尚未发布" description="只有已完成的研究运行才会发布只读报告。" />
      )}
    </Panel>
  );
}

function displayOperator(value: unknown) {
  const raw = valueText(value, "受限因子图");
  if (raw === "restricted FactorDAG") return "受限 FactorDAG";
  if (raw === "fixed five-stage research desk") return "固定五阶段研究席位";
  return raw;
}

function displayAlert(code: unknown, message: unknown) {
  const key = valueText(code);
  if (key === "REAL_PROMOTION_NOT_RUN") return "官方可见证据已完成；独立晋级和真实 20 日影子回放尚未运行。";
  return valueText(message);
}

export function ResearchDAG({ detail }: { detail: WorkbenchRunDetail }) {
  const nodes = asArray(asRecord(detail.research_dag).nodes);
  return (
    <Panel title="Campaign 流程">
      {nodes.length ? (
        <div className="vq-run-list">
          {nodes.map((node, index) => {
            const row = asRecord(node);
            return (
              <div className="vq-run-button" key={index}>
                <div className="vq-run-button-top">
                  <strong>{valueText(row.id, `步骤 ${index + 1}`)}</strong>
                  <WorkbenchStatusBadge status={valueText(row.status, "INCOMPLETE")} />
                </div>
                <p className="vq-caption">{valueText(row.stage || row.role || row.summary)}</p>
              </div>
            );
          })}
        </div>
      ) : (
        <Missing title="暂无流程节点" description="运行产物没有公开可展示的 Campaign DAG 节点。" />
      )}
    </Panel>
  );
}

export function CandidateLineage({ detail }: { detail: WorkbenchRunDetail }) {
  const lineage = asRecord(detail.lineage);
  const candidates = asArray(lineage.candidates || lineage.items);
  const draftOutputs = candidates.filter((candidate) => {
    const row = asRecord(candidate);
    return Object.keys(asRecord(row.hypothesis)).length > 0 || Object.keys(asRecord(row.factor_draft)).length > 0;
  });
  return (
    <Panel title="候选谱系">
      {draftOutputs.length ? (
        <div className="vq-grid-3 vq-top-gap" aria-label="研究 Desk 草案内容">
          {draftOutputs.map((candidate, index) => {
            const row = asRecord(candidate);
            const hypothesis = asRecord(row.hypothesis);
            const factor = asRecord(row.factor_draft);
            const feedback = asRecord(row.redacted_feedback);
            const tests = asArray(hypothesis.falsification_tests);
            return (
              <article className="vq-glass-panel" key={index}>
                <div className="vq-run-button-top">
                  <strong>{valueText(row.candidate_id || row.factor_id)}</strong>
                  <WorkbenchStatusBadge status={valueText(row.status || row.state, "DRAFT")} />
                </div>
                {Object.keys(hypothesis).length ? (
                  <div className="vq-top-gap">
                    <p className="vq-caption">可证伪假设</p>
                    <strong>{valueText(hypothesis.statement)}</strong>
                    <p>{valueText(hypothesis.economic_mechanism)}</p>
                    {tests.length ? <ul>{tests.map((test, testIndex) => <li key={testIndex}>{valueText(test)}</li>)}</ul> : null}
                  </div>
                ) : null}
                {Object.keys(factor).length ? (
                  <div className="vq-top-gap">
                    <p className="vq-caption">FactorDAG 草案</p>
                    <strong>{valueText(factor.name)}</strong>
                    <p>{valueText(factor.description)}</p>
                    <code className="vq-code">{valueText(factor.expression)}</code>
                  </div>
                ) : null}
                {Object.keys(feedback).length ? (
                  <div className="vq-top-gap"><p className="vq-caption">脱敏反馈</p><p>{valueText(feedback.explanation)}</p></div>
                ) : null}
                <p className="vq-caption vq-top-gap">DRAFT_ONLY · 不代表 official Evidence 或 Promotion</p>
              </article>
            );
          })}
        </div>
      ) : null}
      {candidates.length ? (
        <div className="vq-table-wrap">
          <table className="vq-table">
            <thead>
              <tr>
                <th>候选</th>
                <th>状态</th>
                <th>父节点</th>
                <th>生成方式</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((candidate, index) => {
                const row = asRecord(candidate);
                return (
                  <tr key={index}>
                    <td className="vq-code">{valueText(row.candidate_id || row.factor_id)}</td>
                    <td><WorkbenchStatusBadge status={valueText(row.status || row.state, "DRAFT")} /></td>
                    <td className="vq-code">{valueText(row.parent_id || row.parent_ids)}</td>
                    <td>{displayOperator(row.operator)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <Missing title="暂无候选谱系" description="当前运行没有公开候选节点。" />
      )}
    </Panel>
  );
}

export function EvidenceAudit({ detail }: { detail: WorkbenchRunDetail }) {
  const evidence = asRecord(detail.evidence);
  const rows = asArray(evidence.items || evidence.metrics);
  const critics = asArray(asRecord(detail.critics).findings || asRecord(detail.critics).items);
  return (
    <Panel title="证据审计">
      <div className="vq-grid-2">
        {rows.map((item, index) => {
          const row = asRecord(item);
          return (
            <div className="vq-glass-panel" key={index}>
              <p className="vq-caption">{valueText(row.evidence_id || row.metric, "官方可见证据")}</p>
              <strong>{valueText(row.value ?? row.verdict ?? row.status)}</strong>
              <p className="vq-caption">{valueText(row.summary || row.failure_code)}</p>
            </div>
          );
        })}
      </div>
      {!rows.length ? (
        <Missing title="暂无公开证据条目" description="可见证据保留在内容寻址产物中；页面不展示 sealed test 明文。" />
      ) : null}
      <h3 className="vq-subtitle">审查器结论</h3>
      {critics.length ? critics.map((item, index) => {
        const row = asRecord(item);
        return <p key={index}>{valueText(row.critic || row.code)}：{valueText(row.verdict || row.status || row.message)}</p>;
      }) : (
        <Missing title="暂无审查器公共结论" description="未公开的发现不会被界面猜测或补写。" />
      )}
    </Panel>
  );
}

export function CitationTimeline({ detail }: { detail: WorkbenchRunDetail }) {
  const citation = asRecord(detail.citations);
  const items = asArray(citation.items || citation.citations);
  return (
    <Panel title="来源与引用">
      {items.length ? items.map((item, index) => {
        const row = asRecord(item);
        return (
          <div className="vq-run-button" key={index}>
            <strong>{valueText(row.title || row.document_id)}</strong>
            <p>{valueText(row.snippet || row.quote || row.span_text)}</p>
            <code>{valueText(row.source_content_hash || row.chunk_hash)}</code>
          </div>
        );
      }) : (
        <Missing title="暂无引用链" description="本次运行没有公开来源引用产物。" />
      )}
    </Panel>
  );
}

export function PromotionStatus({ detail }: { detail: WorkbenchRunDetail }) {
  const promotion = asRecord(detail.promotion);
  return (
    <Panel title="晋级状态">
      {Object.keys(promotion).length ? (
        <div className="vq-grid-3">
          <div className="vq-glass-panel">
            <p className="vq-caption">决定</p>
            <strong>{valueText(promotion.verdict || promotion.status)}</strong>
          </div>
          <div className="vq-glass-panel">
            <p className="vq-caption">sealed 信息</p>
            <strong>已脱敏</strong>
          </div>
          <div className="vq-glass-panel">
            <p className="vq-caption">失败代码</p>
            <strong className="vq-code">{valueText(promotion.failure_code || promotion.sealed_failure_code)}</strong>
          </div>
        </div>
      ) : (
        <Missing title="尚无晋级决定" description="没有独立晋级产物时，候选不会被页面标记为挑战者。" />
      )}
    </Panel>
  );
}

export function ShadowPortfolio({ detail }: { detail: WorkbenchRunDetail }) {
  const shadow = asRecord(detail.shadow_portfolio);
  const vintages = asArray(shadow.vintages || shadow.items);
  return (
    <Panel title="影子组合">
      <p className="vq-caption">影子组合仅用于纸面研究，不连接真实经纪商、账户或资金。</p>
      {vintages.length ? vintages.map((item, index) => {
        const row = asRecord(item);
        return (
          <div className="vq-run-button" key={index}>
            <strong>{valueText(row.vintage_id || row.date)}</strong>
            <p>{valueText(row.status)}</p>
          </div>
        );
      }) : (
        <Missing title="暂无组合数据版本" description="缺少挑战者或回放产物时，影子组合保持阻断。" />
      )}
    </Panel>
  );
}

export function FailureDriftAlerts({ detail }: { detail: WorkbenchRunDetail }) {
  const drift = asRecord(detail.drift_alerts);
  const alerts = asArray(drift.alerts || drift.items);
  return (
    <Panel title="失败与漂移告警">
      {alerts.length ? alerts.map((item, index) => {
        const row = asRecord(item);
        return (
          <div className="vq-run-button" key={index}>
            <strong>{valueText(row.code || row.kind)}</strong>
            <p>{displayAlert(row.code || row.kind, row.message || row.status || row.severity)}</p>
          </div>
        );
      }) : (
        <Missing title="暂无告警" description="当前公开产物中没有失败或漂移告警。" />
      )}
    </Panel>
  );
}
