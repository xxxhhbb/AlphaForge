import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  CircleDashed,
  Database,
  DownloadCloud,
  FileCheck2,
  FlaskConical,
  LockKeyhole,
  ShieldCheck,
  Sparkles,
  UsersRound,
} from "lucide-react";
import type {
  CandidateDecision,
  DataCoverageItemView,
  DynamicDataPlanView,
  ProviderProvenanceView,
  ResearchGroupView,
  ResearchPlanView,
  ResearchRunView,
  ResearchSuggestionView,
} from "./types";

const readinessLabel: Record<DynamicDataPlanView["readiness"], string> = {
  READY: "研究数据已就绪",
  PARTIAL: "部分数据可用",
  UNAVAILABLE: "所需数据暂不可用",
};

const universeKindLabel: Record<string, string> = {
  HISTORICAL_ALL_A_SHARE: "A 股历史可交易股票池",
  HISTORICAL_INDEX_CONSTITUENTS: "历史指数成分股",
  EXPLICIT_SYMBOLS: "指定股票范围",
  REGISTERED_PORTFOLIO_HISTORY: "已登记组合的历史持仓范围",
};

const decisionLabel: Record<CandidateDecision, string> = {
  未评价: "尚未进入机器评价",
  拒绝: "证据不足或不符合研究政策",
  需修订: "需要修正数据、表达式或研究设计",
  研究验证通过: "通过可见研究验证，尚未独立晋级",
  挑战者: "通过独立评审，可进入后续影子观察",
};

export function ProviderBadge({ provider }: { provider: ProviderProvenanceView }) {
  return (
    <div className={`ros2-provider ros2-provider-${provider.mode}`}>
      <Sparkles aria-hidden="true" />
      <span><strong>{provider.mode}</strong><small>{provider.providerLabel}</small>{provider.modelLabel || provider.reason ? <small>{provider.modelLabel ?? provider.reason}</small> : null}</span>
    </div>
  );
}

function CoverageRow({ item }: { item: DataCoverageItemView }) {
  return (
    <li className={`ros2-coverage ros2-coverage-${item.availability}`}>
      <Database aria-hidden="true" />
      <span><strong>{item.field}</strong><small>{item.coverageLabel} · {item.sourceLabel}</small>{item.limitation ? <em>{item.limitation}</em> : null}</span>
      <b>{item.availability}</b>
    </li>
  );
}

function DynamicDataPlan({ plan }: { plan: DynamicDataPlanView }) {
  const universe = plan.universe.label ?? universeKindLabel[plan.universe.kind] ?? plan.universe.kind;
  const sessionGap = Math.max(plan.minimumSessions - plan.currentSessions, 0);
  return (
    <section className="ros2-data-plan" aria-labelledby="ros2-data-plan-title">
      <header>
        <div>
          <p>按研究问题推导</p>
          <h3 id="ros2-data-plan-title"><Database aria-hidden="true" />动态数据计划</h3>
        </div>
        <span data-readiness={plan.readiness}>{readinessLabel[plan.readiness]}</span>
      </header>
      <dl className="ros2-data-plan-summary">
        <div><dt>历史范围</dt><dd>最低 {plan.minimumSessions} 个交易会话</dd><small>由预热、训练、验证、标签与安全缓冲共同推导</small></div>
        <div><dt>当前冻结数据</dt><dd>{plan.currentSessions} 个交易会话</dd><small>{sessionGap ? `仍缺 ${sessionGap} 个会话` : "已覆盖本次推导需求"}</small></div>
        <div><dt>股票池语义</dt><dd>{universe}</dd><small>{plan.universe.symbolCount ? `当前 ${plan.universe.symbolCount} 只` : "按历史成员关系获取，不预设固定股票数"}</small></div>
        <div><dt>数据获取</dt><dd>{plan.acquisitionRequired ? "需要补充获取并冻结" : "无需新增获取"}</dd><small>获取规模受运行预算约束，不会静默缩小研究范围</small></div>
      </dl>
      <div className="ros2-data-plan-details">
        <div><strong>计划请求的数据接口</strong><p>{plan.requestedApis.length ? plan.requestedApis.join(" · ") : "无"}</p></div>
        <div><strong>计划请求的字段</strong><p>{plan.requestedFields.length ? plan.requestedFields.join(" · ") : "无"}</p></div>
      </div>
      {plan.researchableParts.length ? (
        <div className="ros2-data-plan-note"><CheckCircle2 aria-hidden="true" /><div><strong>现在可以研究</strong><p>{plan.researchableParts.join("；")}</p></div></div>
      ) : null}
      {plan.missingRequirements.length || plan.degradationSuggestions.length ? (
        <div className="ros2-data-plan-note is-gap"><DownloadCloud aria-hidden="true" /><div>
          <strong>数据缺口与可用替代路线</strong>
          {plan.missingRequirements.map((item) => <p key={`missing-${item}`}>缺口：{item}</p>)}
          {plan.degradationSuggestions.map((item) => <p key={`degrade-${item}`}>建议：{item}</p>)}
        </div></div>
      ) : null}
      <code className="ros2-data-plan-hash">计划标识：{plan.planHash}</code>
    </section>
  );
}

export function SuggestionPanel({ suggestion, onConfirm, confirming }: {
  suggestion: ResearchSuggestionView;
  onConfirm: () => void;
  confirming: boolean;
}) {
  return (
    <section className="ros2-panel ros2-suggestion" aria-labelledby="ros2-suggestion-title">
      <header className="ros2-panel-heading">
        <div><p>研究建议</p><h2 id="ros2-suggestion-title">{suggestion.researchMode}</h2></div>
        <div className="ros2-heading-meta"><span className="ros2-state">{suggestion.answerLevel}</span><ProviderBadge provider={suggestion.provider} /></div>
      </header>
      <p className="ros2-lead">{suggestion.rationale}</p>
      <div className="ros2-two-column">
        <article>
          <h3><UsersRound aria-hidden="true" />建议参与的专业研究小组</h3>
          <ol>{suggestion.selectedGroups.map((group) => <li key={group}>{group}</li>)}</ol>
          <h3><FlaskConical aria-hidden="true" />计划执行的实验</h3>
          <ol>{suggestion.plannedExperiments.map((experiment) => <li key={experiment}>{experiment}</li>)}</ol>
        </article>
        <article>
          <h3><Database aria-hidden="true" />数据覆盖</h3>
          <ul className="ros2-coverage-list">{suggestion.dataCoverage.map((item) => <CoverageRow item={item} key={item.field} />)}</ul>
        </article>
      </div>
      {suggestion.dataPlan ? <DynamicDataPlan plan={suggestion.dataPlan} /> : null}
      {suggestion.dataGaps.length ? (
        <div className="ros2-warning" role="status"><AlertCircle aria-hidden="true" /><div><strong>数据不足不会被静默忽略</strong>{suggestion.dataGaps.map((gap) => <p key={gap}>{gap}</p>)}</div></div>
      ) : null}
      <footer className="ros2-panel-actions">
        <p><ShieldCheck aria-hidden="true" />确认前不会启动计算，也不会消耗试验预算。</p>
        <button className="ros2-primary" disabled={confirming} onClick={onConfirm} type="button">{confirming ? "正在确认…" : "确认研究计划"}<ArrowRight aria-hidden="true" /></button>
      </footer>
    </section>
  );
}

export function PlanPanel({ plan, onStart, starting }: { plan: ResearchPlanView; onStart: () => void; starting: boolean }) {
  return (
    <section className="ros2-panel" aria-labelledby="ros2-plan-title">
      <header className="ros2-panel-heading"><div><p>已确认研究计划</p><h2 id="ros2-plan-title">{plan.researchObject}</h2></div><span className="ros2-state">{plan.state}</span></header>
      <dl className="ros2-contract-grid">
        <div><dt>历史股票池</dt><dd>{plan.universe}</dd></div>
        <div><dt>时间范围</dt><dd>{plan.horizon}</dd></div>
        <div><dt>研究深度</dt><dd>{plan.depth}</dd></div>
        <div><dt>协作方式</dt><dd>{plan.collaborationStyle}</dd></div>
      </dl>
      {plan.dataPlan ? <DynamicDataPlan plan={plan.dataPlan} /> : null}
      <div className="ros2-plan-lists">
        <div><strong>检查点</strong><ul>{plan.checkpoints.map((item) => <li key={item}>{item}</li>)}</ul></div>
        <div><strong>停止条件</strong><ul>{plan.stopConditions.map((item) => <li key={item}>{item}</li>)}</ul></div>
        <div><strong>受控预算</strong><p>{plan.budget.groups} 个小组 · {plan.budget.candidates} 个候选 · {plan.budget.trials} 次试验</p><code>{plan.planHash}</code></div>
      </div>
      <footer className="ros2-panel-actions"><p><LockKeyhole aria-hidden="true" />研究模型只能写草案；机器证据和独立评审由后端权威服务完成。</p><button className="ros2-primary" disabled={starting} onClick={onStart} type="button">{starting ? "正在启动…" : "启动受控研究"}<ArrowRight aria-hidden="true" /></button></footer>
    </section>
  );
}

function CandidateCard({ candidate }: { candidate: ResearchGroupView["candidates"][number] }) {
  return (
    <article className="ros2-candidate">
      <header><div><small>{candidate.candidateId}</small><h4>{candidate.title}</h4></div><span data-decision={candidate.decision}>{candidate.decision}</span></header>
      <p>{candidate.hypothesis}</p>
      <code>{candidate.expression}</code>
      <div className="ros2-decision"><FileCheck2 aria-hidden="true" /><span><strong>{decisionLabel[candidate.decision]}</strong><small>{candidate.decisionExplanation}</small></span></div>
      {candidate.metricSummary ? <dl className="ros2-metrics"><div><dt>成本后 ICIR</dt><dd>{candidate.metricSummary.netIcir.toFixed(2)}</dd></div><div><dt>稳定性</dt><dd>{candidate.metricSummary.stability.toFixed(2)}</dd></div><div><dt>换手</dt><dd>{candidate.metricSummary.turnover.toFixed(2)}</dd></div><div><dt>增量分</dt><dd>{candidate.metricSummary.incrementalScore.toFixed(2)}</dd></div></dl> : null}
      {candidate.evidenceHash ? <small className="ros2-hash">证据标识：{candidate.evidenceHash}</small> : null}
    </article>
  );
}

function GroupCard({ group }: { group: ResearchGroupView }) {
  const isFailure = group.state === "失败" || group.state === "已阻断" || group.state === "需修订";
  return (
    <article className={`ros2-group ${isFailure ? "is-problem" : ""}`}>
      <header><span>{isFailure ? <AlertCircle aria-hidden="true" /> : <CheckCircle2 aria-hidden="true" />}</span><div><h3>{group.title}</h3><p>{group.purpose}</p></div><b>{group.state}</b></header>
      <div className="ros2-budget"><span>分支预算</span><strong>{group.budgetUsed} / {group.budgetLimit}</strong></div>
      <ol className="ros2-progress">{group.progress.map((item) => <li key={item}><CircleDashed aria-hidden="true" />{item}</li>)}</ol>
      {group.failure ? <p className="ros2-branch-failure">{group.failure}</p> : null}
      <div className="ros2-candidate-list">{group.candidates.map((candidate) => <CandidateCard candidate={candidate} key={candidate.candidateId} />)}</div>
    </article>
  );
}

export function RunPanel({ run }: { run: ResearchRunView }) {
  const completed = run.groups.filter((group) => group.state === "已完成").length;
  const problems = run.groups.length - completed;
  return (
    <section className="ros2-run" aria-labelledby="ros2-run-title">
      <header className="ros2-run-heading"><div><p>研究运行驾驶舱</p><h2 id="ros2-run-title">专业研究小组与机器验证</h2><code>{run.runId}</code></div><ProviderBadge provider={run.provider} /></header>
      <div className="ros2-summary"><article><span>参与小组</span><strong>{run.groups.length}</strong></article><article><span>完成分支</span><strong>{completed}</strong></article><article><span>异常分支</span><strong>{problems}</strong></article><article><span>试验预算</span><strong>{run.trialsUsed} / {run.trialLimit}</strong></article></div>
      <div className="ros2-checkpoint"><CircleDashed aria-hidden="true" /><div><small>当前检查点</small><strong>{run.currentCheckpoint}</strong></div><span>{run.state}</span></div>
      <div className="ros2-group-list">{run.groups.map((group) => <GroupCard group={group} key={group.groupId} />)}</div>
      <aside className="ros2-sealed"><LockKeyhole aria-hidden="true" /><div><strong>独立封闭评审：{run.sealedReview}</strong><p>{run.sealedMessage}</p></div></aside>
      <section className="ros2-meaning" aria-label="机器结论说明"><h3>如何理解机器结论</h3><dl>{Object.entries(decisionLabel).map(([state, explanation]) => <div key={state}><dt>{state}</dt><dd>{explanation}</dd></div>)}</dl></section>
      <footer className="ros2-report"><div><FileCheck2 aria-hidden="true" /><span><strong>正式中文研究报告</strong><small>{run.state === "已完成" && run.reportHash ? `报告标识：${run.reportHash}` : "研究完成前不开放报告"}</small></span></div>{run.state === "已完成" && run.formalReportPath ? <a href={`/reports/${encodeURIComponent(run.runId)}`}>查看正式报告 <ArrowRight aria-hidden="true" /></a> : <span>等待研究完成</span>}</footer>
    </section>
  );
}
