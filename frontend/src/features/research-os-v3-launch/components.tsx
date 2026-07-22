import { Check, Database, FileCheck2, FlaskConical, ShieldCheck, UsersRound } from "lucide-react";
import type { V3CampaignPlanView, V3ResearchRunControlView } from "./types";

export function CampaignPlanCard({ plan, confirming, preparing, preparationDate, onPreparationDateChange, onPrepare, onConfirm }: {
  plan: V3CampaignPlanView;
  confirming: boolean;
  preparing: boolean;
  preparationDate: string;
  onPreparationDateChange: (value: string) => void;
  onPrepare: () => void;
  onConfirm: () => void;
}) {
  return (
    <section className="ros3l-card" aria-labelledby="ros3l-plan-title">
      <header><FileCheck2 aria-hidden="true" /><div><small>待确认研究计划</small><h2 id="ros3l-plan-title">{plan.researchMode}</h2></div><b>{plan.state}</b></header>
      <p className="ros3l-lead">{plan.researchSubject}</p>
      <dl className="ros3l-facts">
        <div><dt>研究问题</dt><dd>{plan.question}</dd></div>
        <div><dt>研究深度</dt><dd>{plan.depth}</dd></div>
        <div><dt>协作方式</dt><dd>{plan.interactionStyle}</dd></div>
        <div><dt>研究计划标识</dt><dd><code>{plan.campaignHash}</code></dd></div>
      </dl>
      <div className="ros3l-columns">
        <section><h3><UsersRound aria-hidden="true" />专业研究组</h3><ul>{plan.selectedSpecialists.map((item) => <li key={item}>{item}</li>)}</ul></section>
        <section><h3><FlaskConical aria-hidden="true" />研究路线</h3><ol>{plan.plannedResearch.map((item) => <li key={item}>{item}</li>)}</ol></section>
      </div>
      <div className="ros3l-data">
        <Database aria-hidden="true" />
        <div><strong>数据范围：{plan.dataScope.availability}</strong><p>{plan.dataScope.sourceLabel} · {plan.dataScope.universe} · {plan.dataScope.dateRange}</p>{plan.dataScope.snapshotHash ? <code>{plan.dataScope.snapshotHash}</code> : null}</div>
      </div>
      {plan.dataScope.limitations.length ? <ul className="ros3l-warning">{plan.dataScope.limitations.map((item) => <li key={item}>{item}</li>)}</ul> : null}
      {plan.dataScope.availability === "不可用" ? (
        <div className="ros3l-actions">
          <label>真实数据截止日期<input aria-label="真实数据截止日期" type="date" value={preparationDate} onChange={(event) => onPreparationDateChange(event.target.value)} /></label>
          <button className="ros3l-secondary" disabled={preparing || !preparationDate} onClick={onPrepare} type="button">{preparing ? "正在冻结真实研究数据…" : "准备真实研究数据"}</button>
        </div>
      ) : null}
      <dl className="ros3l-budget" aria-label="研究预算">
        <div><dt>专业组</dt><dd>{plan.budget.specialistGroups}</dd></div>
        <div><dt>候选上限</dt><dd>{plan.budget.candidateLimit}</dd></div>
        <div><dt>试验上限</dt><dd>{plan.budget.trialLimit}</dd></div>
        <div><dt>模型调用上限</dt><dd>{plan.budget.modelCallLimit}</dd></div>
      </dl>
      <aside className="ros3l-boundary"><ShieldCheck aria-hidden="true" /><p>确认只授权在上述数据和预算内开展受控研究，不授予模型机器评价、晋级、持仓或订单权限。</p></aside>
      <button className="ros3l-primary" disabled={confirming || plan.state !== "待确认" || plan.dataScope.availability === "不可用"} onClick={onConfirm} type="button">{confirming ? "正在确认…" : plan.dataScope.availability === "不可用" ? "数据不可用，先准备数据" : "确认研究计划"}</button>
    </section>
  );
}

export function ConfirmedPlanCard({ plan, starting, onStart }: {
  plan: V3CampaignPlanView;
  starting: boolean;
  onStart: () => void;
}) {
  return (
    <section className="ros3l-card ros3l-confirmed">
      <header><Check aria-hidden="true" /><div><small>研究边界已冻结</small><h2>计划已由你确认</h2></div><b>{plan.state}</b></header>
      <p>将启动 {plan.selectedSpecialists.length} 个专业研究组，最多形成 {plan.budget.candidateLimit} 个候选。失败分支会保留原因，不会伪造成成功。</p>
      <div className="ros3l-chip-row">{plan.selectedSpecialists.map((item) => <span key={item}>{item}</span>)}</div>
      <button className="ros3l-primary" disabled={starting} onClick={onStart} type="button">{starting ? "正在启动…" : "启动受控研究"}</button>
    </section>
  );
}

export function ResearchRunCard({ run, controlling, onCancel, onResume }: {
  run: V3ResearchRunControlView;
  controlling: boolean;
  onCancel: () => void;
  onResume: () => void;
}) {
  const active = run.state === "排队中" || run.state === "运行中" || run.state === "等待确认";
  const resumable = run.state === "已阻断" || run.state === "失败" || run.state === "已取消";
  return (
    <section className="ros3l-card" aria-labelledby="ros3l-run-title">
      <header><FlaskConical aria-hidden="true" /><div><small>研究运行</small><h2 id="ros3l-run-title">专业研究组与机器验证</h2></div><b>{run.state}</b></header>
      <dl className="ros3l-facts"><div><dt>运行标识</dt><dd><code>{run.runId}</code></dd></div><div><dt>当前阶段</dt><dd>{run.currentStage}</dd></div><div><dt>研究模型</dt><dd>{run.modelLabel ?? run.providerLabel}</dd></div><div><dt>试验预算</dt><dd>{run.usedBudget} / {run.totalBudget}</dd></div></dl>
      <div className="ros3l-progress">
        {run.selectedSpecialists.map((item) => <article key={item.specialistId}><header><strong>{item.title}</strong><span>{item.state}</span></header><p>{item.message}</p><small>候选 {item.candidateCount}</small></article>)}
      </div>
      {run.typedFailures.length ? <div className="ros3l-failures" role="alert"><strong>受控停止原因</strong><ul>{run.typedFailures.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
      <div className="ros3l-actions">
        {active ? <button className="ros3l-secondary" disabled={controlling} onClick={onCancel} type="button">取消本次研究</button> : null}
        {resumable ? <button className="ros3l-primary" disabled={controlling} onClick={onResume} type="button">从已提交阶段恢复</button> : null}
      </div>
    </section>
  );
}
