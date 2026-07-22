import {
  AlertTriangle,
  ArrowRight,
  Check,
  CircleDashed,
  Database,
  FileCheck2,
  FlaskConical,
  LockKeyhole,
  Route,
  ShieldCheck,
  UsersRound,
} from "lucide-react";
import type {
  CandidatePreflightView,
  FormalResearchReportView,
  ResearchGroupResultView,
  ResearchOSV3RunView,
} from "./types";
import "./research-os-v3.css";

function CandidateCard({ candidate }: { candidate: CandidatePreflightView }) {
  return (
    <article className="ros3-candidate" data-outcome={candidate.outcome}>
      <header>
        <div><small>候选</small><h4>{candidate.candidateId}</h4></div>
        <span>{candidate.outcome}</span>
      </header>
      <p>{candidate.explanation}</p>
      {candidate.factorDagHash ? (
        <div className="ros3-dag"><FileCheck2 aria-hidden="true" /><span><strong>Qlib 研究表达式预检通过</strong></span></div>
      ) : null}
      <details className="ros3-evidence-details"><summary>查看证据详情</summary><code>候选标识：{candidate.candidateHash}</code>{candidate.factorDagHash ? <code>表达式标识：{candidate.factorDagHash}</code> : null}</details>
    </article>
  );
}

function GroupCard({ group }: { group: ResearchGroupResultView }) {
  const positive = group.outcome === "草案已提交";
  return (
    <article className="ros3-group" data-outcome={group.outcome}>
      <header>
        <span className="ros3-group-icon">{positive ? <Check aria-hidden="true" /> : <AlertTriangle aria-hidden="true" />}</span>
        <div><h3>{group.title}</h3><p>{group.purpose}</p></div>
        <b>{group.outcome}</b>
      </header>
      <dl className="ros3-group-summary">
        <div><dt>候选</dt><dd>{group.candidates.length}</dd></div>
        <div><dt>预检通过</dt><dd>{group.candidates.filter((item) => item.outcome === "预检通过").length}</dd></div>
        <div><dt>研究步骤调用</dt><dd>{group.providerCalls}</dd></div>
      </dl>
      {group.missingCapabilities.length ? (
        <div className="ros3-notice"><Database aria-hidden="true" /><div><strong>数据或能力缺口</strong><p>{group.missingCapabilities.join("；")}</p></div></div>
      ) : null}
      {group.findings.length ? (
        <ul className="ros3-findings">{group.findings.map((finding) => <li key={finding}>{finding}</li>)}</ul>
      ) : null}
      <div className="ros3-candidates">{group.candidates.map((candidate) => <CandidateCard candidate={candidate} key={`${group.groupId}-${candidate.candidateId}-${candidate.candidateHash}`} />)}</div>
      {group.resultHash ? <details className="ros3-evidence-details"><summary>查看本组证据详情</summary><code>结果标识：{group.resultHash}</code></details> : null}
    </article>
  );
}

export function ResearchRoutePanel({ run }: { run: ResearchOSV3RunView }) {
  return (
    <section className="ros3-panel" aria-labelledby="ros3-route-title">
      <header className="ros3-section-heading"><Route aria-hidden="true" /><div><small>研究路线</small><h2 id="ros3-route-title">从问题到机器结论</h2></div></header>
      <ol className="ros3-route">
        {run.route.map((step) => (
          <li data-state={step.state} key={step.id}>
            <span>{step.state === "已完成" ? <Check aria-hidden="true" /> : <CircleDashed aria-hidden="true" />}</span>
            <div><strong>{step.title}</strong><p>{step.description}</p></div>
            <b>{step.state}</b>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function MachineValidationPanel({ run }: { run: ResearchOSV3RunView }) {
  const validation = run.machineValidation;
  return (
    <section className="ros3-panel" aria-labelledby="ros3-validation-title">
      <header className="ros3-section-heading"><ShieldCheck aria-hidden="true" /><div><small>机器验证</small><h2 id="ros3-validation-title">证据与独立评审</h2></div></header>
      <div className="ros3-validation-grid">
        <article><span>官方机器证据</span><strong>{validation.evidence}</strong><p>{validation.publicMessage}</p>{validation.evidenceHash ? <details className="ros3-evidence-details"><summary>查看证据详情</summary><code>{validation.evidenceHash}</code></details> : null}</article>
        <article><span>独立评审</span><strong>{validation.promotion}</strong><p>研究模型和页面都无权写入晋级结论。</p></article>
      </div>
      <aside className="ros3-sealed"><LockKeyhole aria-hidden="true" /><div><strong>封闭评审保持隔离</strong><p>{validation.sealedMessage}</p></div></aside>
    </section>
  );
}

export function FormalResearchReportPanel({ report, runId }: { report: FormalResearchReportView; runId: string }) {
  const funnel = report.funnel;
  const best = report.candidates.find((candidate) => candidate.isBest);
  return (
    <article className="ros3-report" aria-labelledby="ros3-report-title">
      <header><div><small>正式中文研究报告</small><h2 id="ros3-report-title">{report.title}</h2></div><span>{report.outcome}</span></header>
      <section className="ros3-report-conclusion"><strong>一页结论</strong><p>{report.conclusion}</p></section>
      {funnel ? <section className="ros3-funnel" aria-label="候选漏斗">
        <div><span>提出候选</span><strong>{funnel.proposed}</strong></div>
        <div><span>预检通过</span><strong>{funnel.preflightAccepted}</strong></div>
        <div><span>机器评价</span><strong>{funnel.machineEvaluated}</strong></div>
        <div><span>研究短名单</span><strong>{funnel.shortlisted}</strong></div>
      </section> : null}
      <dl className="ros3-report-grid">
        <div><dt>研究问题</dt><dd>{report.question}</dd></div>
        <div><dt>数据与范围</dt><dd>{report.dataSummary}</dd></div>
        <div><dt>机器结论</dt><dd>{report.machineConclusion}</dd></div>
      </dl>
      {best ? <section className="ros3-best-candidate"><small>当前最佳候选</small><h3>{best.candidateId}</h3><p>{best.hypothesis}</p><strong>{best.verdict}</strong></section> : funnel ? <section className="ros3-negative-result"><strong>本轮没有形成最佳候选</strong><p>这仍是有效研究结果。请根据机器理由修订研究设计、补充数据，或停止当前方向。</p></section> : null}
      {report.candidates.length ? <section className="ros3-decision-list" aria-labelledby="ros3-decisions-title"><h3 id="ros3-decisions-title">候选与机器选择理由</h3>{report.candidates.map((candidate) => (
        <article key={candidate.candidateHash} data-best={candidate.isBest || undefined}>
          <header><div><small>{candidate.groupLabel}</small><h4>{candidate.candidateId}</h4></div><b>{candidate.verdict}</b></header>
          <p>{candidate.hypothesis}</p><code className="ros3-expression">{candidate.expression}</code>
          {candidate.metrics.length ? <dl>{candidate.metrics.map((metric) => <div key={metric.name}><dt>{metric.name}</dt><dd>{metric.value.toFixed(4)}</dd></div>)}</dl> : <p className="ros3-muted">没有可公开的官方机器指标。</p>}
          {candidate.reasons.length ? <ul>{candidate.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}
          <details className="ros3-evidence-details"><summary>查看证据详情</summary><code>候选标识：{candidate.candidateHash}</code>{candidate.factorDagHash ? <code>表达式标识：{candidate.factorDagHash}</code> : null}</details>
        </article>
      ))}</section> : null}
      <div className="ros3-report-lists">
        <section><h3>限制</h3>{report.limitations.length ? <ul>{report.limitations.map((item) => <li key={item}>{item}</li>)}</ul> : <p>未记录额外限制。</p>}</section>
        <section><h3>建议下一步</h3>{report.nextActions.length ? <ol>{report.nextActions.map((item) => <li key={item}>{item}</li>)}</ol> : <p>等待机器验证或用户决策。</p>}</section>
      </div>
      <footer><FileCheck2 aria-hidden="true" /><details className="ros3-evidence-details"><summary>查看报告证据详情</summary><code>{report.reportHash ?? "尚未生成稳定报告标识"}</code></details><a href={`/api/veriquant/research-os/v3/runs/${encodeURIComponent(runId)}/report.md`} rel="noreferrer" target="_blank">打开完整报告</a></footer>
    </article>
  );
}

export function ResearchOSV3Result({ run }: { run: ResearchOSV3RunView }) {
  return (
    <div className="ros3-shell">
      <header className="ros3-hero">
        <div><p>AlphaForge · 千因研究院</p><h1>研究结论与机器验证</h1><span>{run.runId}</span></div>
        <div className="ros3-run-state"><small>当前结果</small><strong>{run.outcome}</strong><span>{run.authorityLabel}</span></div>
      </header>
      {run.report ? <section className="ros3-understanding"><small>系统理解</small><h2>{run.report.question}</h2><p>{run.report.conclusion}</p><div><span>数据能力</span><strong>{run.report.dataSummary}</strong></div></section> : null}
      <section className="ros3-summary" aria-label="研究摘要">
        <article><UsersRound aria-hidden="true" /><span><small>参与研究组</small><strong>{run.selectedGroups.length}</strong></span></article>
        <article><FlaskConical aria-hidden="true" /><span><small>候选总数</small><strong>{run.candidateCount}</strong></span></article>
        <article><FileCheck2 aria-hidden="true" /><span><small>预检通过</small><strong>{run.acceptedPreflightCount}</strong></span></article>
        <article><ShieldCheck aria-hidden="true" /><span><small>机器证据</small><strong>{run.machineValidation.evidence}</strong></span></article>
      </section>
      <ResearchRoutePanel run={run} />
      <section className="ros3-panel" aria-labelledby="ros3-groups-title">
        <header className="ros3-section-heading"><UsersRound aria-hidden="true" /><div><small>专业研究组</small><h2 id="ros3-groups-title">独立研究结果</h2></div></header>
        <p className="ros3-section-lead">只展示本次研究实际选择的小组。负向结果、数据缺口和部分结果会被完整保留，不会伪装成成功。</p>
        <div className="ros3-groups">{run.selectedGroups.map((group) => <GroupCard group={group} key={group.groupId} />)}</div>
      </section>
      <MachineValidationPanel run={run} />
      {run.report ? <FormalResearchReportPanel report={run.report} runId={run.runId} /> : (
        <section className="ros3-report-pending"><FileCheck2 aria-hidden="true" /><div><strong>正式中文研究报告尚未发布</strong><p>研究草案需先完成机器验证；当前不会用模型文字冒充正式结论。</p></div><span>等待验证 <ArrowRight aria-hidden="true" /></span></section>
      )}
      <footer className="ros3-provenance"><div><span>数据来源</span><strong>{run.sourceLabel}</strong></div><div><span>研究模型</span><strong>{run.modelLabel ?? run.providerLabel}</strong></div><details className="ros3-evidence-details"><summary>查看运行证据详情</summary><code>运行清单：{run.manifestHash}</code><code>研究计划：{run.campaignHash}</code><code>数据上下文：{run.contextHash}</code></details></footer>
    </div>
  );
}
