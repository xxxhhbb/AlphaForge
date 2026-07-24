import { useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, Circle, FileText, Loader2, Search, ShieldCheck, Sparkles } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  defaultResearchOSV3LaunchClient,
  type V3CampaignPlanView,
  type V3InteractionStyle,
  type V3LaunchClient,
  type V3ResearchDepth,
  type V3ResearchRunControlView,
} from "@/features/research-os-v3-launch";
import "@/features/research-os-v3-launch/research-os-v3-launch.css";

const TERMINAL_STATES = new Set(["已完成", "部分完成", "已阻断", "失败", "已取消"]);
const REPORTABLE_TERMINAL_STATES = new Set(["已完成", "部分完成", "已阻断", "失败"]);

type UiStatus = "idle" | "planning" | "reviewing" | "confirming" | "running" | "publishing" | "done" | "failed";

interface StepView {
  id: string;
  title: string;
  description: string;
  state: "等待中" | "运行中" | "已完成" | "需要注意";
}

export interface ResearchOSV3LaunchPageProps {
  client?: V3LaunchClient;
  pollIntervalMs?: number;
  maxPollAttempts?: number;
  onResultReady?: (runId: string) => void;
}

export function ResearchOSV3LaunchPage({
  client = defaultResearchOSV3LaunchClient,
  pollIntervalMs = 2_000,
  maxPollAttempts = 220,
  onResultReady,
}: ResearchOSV3LaunchPageProps) {
  const [question, setQuestion] = useState("挖一下最近 50 天的大动量因子");
  const [depth, setDepth] = useState<V3ResearchDepth>("标准研究");
  const [interactionStyle, setInteractionStyle] = useState<V3InteractionStyle>("标准式");
  const [plan, setPlan] = useState<V3CampaignPlanView>();
  const [run, setRun] = useState<V3ResearchRunControlView>();
  const [report, setReport] = useState("");
  const [status, setStatus] = useState<UiStatus>("idle");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [attempts, setAttempts] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const startedRef = useRef(false);

  const busy = status === "planning" || status === "confirming" || status === "running" || status === "publishing";
  const phase = useMemo(() => {
    if (status === "idle") return "等待研究问题";
    if (status === "planning") return "正在理解问题并制定研究路线";
    if (status === "reviewing") return "等待用户审阅并显式确认研究计划";
    if (status === "confirming") return "正在确认计划并启动受控研究";
    if (status === "running") return run?.currentStage ?? "专业研究小组正在工作";
    if (status === "publishing") return "正在汇总机器证据并生成报告";
    if (status === "done") return "研究报告已完成";
    return "研究已受控停止";
  }, [run?.currentStage, status]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const steps = useMemo(() => buildSteps(status, plan, run, report), [plan, report, run, status]);

  const startResearch = async () => {
    const normalizedQuestion = question.trim();
    if (!normalizedQuestion || busy || startedRef.current) return;
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    startedRef.current = true;
    setPlan(undefined);
    setRun(undefined);
    setReport("");
    setError("");
    setNotice("");
    setAttempts(0);
    setStatus("planning");
    try {
      const draft = await client.createPlan({ question: normalizedQuestion, depth, interactionStyle });
      setPlan(draft);
      if (draft.dataScope.availability === "不可用") {
        setStatus("failed");
        setError(`当前数据不足以自动研究：${draft.dataScope.limitations.join("；") || "缺少可评价数据"}`);
        return;
      }
      setStatus("reviewing");
      setNotice("研究计划已生成。请先审阅数据范围、研究组、预算、检查点和停止条件，再显式确认启动。");
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      setStatus("failed");
      setError(reason instanceof Error ? reason.message : "研究服务暂时不可用。");
    } finally {
      startedRef.current = false;
    }
  };

  const confirmAndStartResearch = async () => {
    if (!plan || busy || startedRef.current) return;
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    startedRef.current = true;
    setRun(undefined);
    setReport("");
    setError("");
    setAttempts(0);
    setStatus("confirming");
    setNotice("正在提交用户确认，并启动受控研究。");
    try {
      const confirmed = await client.confirmPlan(plan.campaignId);
      setPlan(confirmed);
      const started = await client.startRun(confirmed.campaignId);
      setRun(started);
      setStatus("running");
      await pollUntilReport(started.runId);
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      setStatus("failed");
      setError(reason instanceof Error ? reason.message : "研究服务暂时不可用。");
    } finally {
      startedRef.current = false;
    }
  };

  const pollUntilReport = async (runId: string) => {
    let latest: V3ResearchRunControlView | undefined;
    for (let index = 1; index <= maxPollAttempts; index += 1) {
      await delay(pollIntervalMs);
      const next = await client.getRun(runId, abortRef.current?.signal);
      latest = next;
      setRun(next);
      setAttempts(index);
      if (!TERMINAL_STATES.has(next.state)) {
        setStatus("running");
        setNotice(`研究小组正在提交候选与证据，已核对 ${index} 次。`);
        continue;
      }
      if (REPORTABLE_TERMINAL_STATES.has(next.state) && (next.resultReady || next.state === "失败" || next.state === "已阻断")) {
        setStatus("publishing");
        setNotice("研究已结束，正在读取正式中文报告。");
        try {
          const markdown = await client.getReportMarkdown(runId, abortRef.current?.signal);
          setReport(markdown);
          setStatus(next.state === "失败" ? "failed" : "done");
          if (onResultReady) onResultReady(runId);
          return;
        } catch {
          if (next.state === "失败" || next.state === "已阻断") {
            setStatus("failed");
            setError("研究已受控停止，但正式报告暂不可读。请稍后刷新或查看运行记录。");
            return;
          }
        }
      }
      setStatus("publishing");
      setNotice(`研究已结束，正在等待报告发布完成（${index}/${maxPollAttempts}）。`);
    }
    setStatus(latest?.state === "失败" ? "failed" : "publishing");
    setError("状态查询达到上限。研究不会被伪造成失败，请稍后刷新查看报告。");
  };

  const reset = () => {
    abortRef.current?.abort();
    startedRef.current = false;
    setStatus("idle");
    setPlan(undefined);
    setRun(undefined);
    setReport("");
    setError("");
    setNotice("");
    setAttempts(0);
  };

  return (
    <main className="research-terminal-shell">
      <section className="research-terminal-hero">
        <div>
          <p className="eyebrow">AlphaForge · 千因研究院</p>
          <h1>输入一个 Alpha 问题，系统给你一份研究报告。</h1>
          <p className="hero-copy">专业研究小组会自动分工，候选会逐项接受安全表达式检查和机器证据评价。页面只展示研究过程和报告，不让模型修改指标、晋级、持仓或订单。</p>
        </div>
        <aside>
          <ShieldCheck aria-hidden="true" />
          <strong>{phase}</strong>
          <span>{run?.runId ? `运行标识：${run.runId}` : "等待启动"}</span>
        </aside>
      </section>

      <section className="research-terminal-card question-card">
        <label>
          <span>研究问题</span>
          <textarea
            aria-label="研究问题"
            disabled={busy}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="例如：挖一下最近 50 天的大动量因子；诊断过去一个月反转因子为什么失效；找一个和动量低相关的 Alpha。"
            value={question}
          />
        </label>
        <div className="question-actions">
          <select aria-label="研究深度" disabled={busy} onChange={(event) => setDepth(event.target.value as V3ResearchDepth)} value={depth}>
            <option>快速探索</option>
            <option>标准研究</option>
            <option>严格验证</option>
          </select>
          <select aria-label="协作方式" disabled={busy} onChange={(event) => setInteractionStyle(event.target.value as V3InteractionStyle)} value={interactionStyle}>
            <option>引导式</option>
            <option>标准式</option>
            <option>批处理式</option>
          </select>
          <button className="primary-action" disabled={busy || !question.trim()} onClick={startResearch} type="button">
            {busy ? <Loader2 className="spin" aria-hidden="true" /> : <Sparkles aria-hidden="true" />}
            {busy ? "处理中" : "生成研究计划"}
          </button>
          {(plan || run || report || error) ? <button className="ghost-action" disabled={busy} onClick={reset} type="button">返回修改</button> : null}
        </div>
        {notice ? <p className="terminal-notice" role="status">{notice}</p> : null}
        {error ? <p className="terminal-error" role="alert">{error}</p> : null}
      </section>

      {plan && !run && !report ? (
        <PlanReview
          busy={busy}
          onConfirm={confirmAndStartResearch}
          plan={plan}
          showConfirm={status === "reviewing"}
        />
      ) : null}

      <section className="terminal-grid">
        <ProcessTimeline steps={steps} />
        <RunSummary attempts={attempts} plan={plan} run={run} />
      </section>

      {run ? <SpecialistBoard run={run} /> : null}
      {report ? <ReportPanel markdown={report} /> : null}
    </main>
  );
}

function PlanReview({ busy, onConfirm, plan, showConfirm }: { busy: boolean; onConfirm: () => void; plan: V3CampaignPlanView; showConfirm: boolean }) {
  return (
    <section className="research-terminal-card plan-review-card" aria-labelledby="plan-review-title">
      <header className="section-title">
        <ShieldCheck aria-hidden="true" />
        <div>
          <h2 id="plan-review-title">研究计划审阅</h2>
          <p>确认前请检查研究目标、数据范围、预算、检查点和停止条件。系统不会自动替你确认。</p>
        </div>
      </header>
      <div className="plan-review-grid">
        <article>
          <span>原始问题</span>
          <p>{plan.question}</p>
        </article>
        <article>
          <span>研究目标</span>
          <p>{plan.researchSubject}</p>
        </article>
        <article>
          <span>研究模式</span>
          <p>{plan.researchMode} · {plan.depth} · {plan.interactionStyle}</p>
        </article>
        <article>
          <span>数据范围</span>
          <p>{plan.dataScope.sourceLabel}，{plan.dataScope.universe}，{plan.dataScope.dateRange}</p>
          {plan.dataScope.limitations.length ? <small>{plan.dataScope.limitations.join("；")}</small> : <small>当前未报告数据限制。</small>}
        </article>
        <article>
          <span>预算</span>
          <p>{plan.budget.specialistGroups} 个研究组，最多 {plan.budget.candidateLimit} 个候选，{plan.budget.trialLimit} 次机器试验，{plan.budget.modelCallLimit} 次模型调用。</p>
        </article>
      </div>
      <div className="plan-review-lists">
        <div>
          <h3>选中的专业研究组</h3>
          <ul>{plan.selectedSpecialists.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
        <div>
          <h3>研究步骤</h3>
          <ul>{plan.plannedResearch.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
        <div>
          <h3>检查点</h3>
          <ul>{plan.checkpoints.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
        <div>
          <h3>停止条件</h3>
          <ul>{plan.stopConditions.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
      </div>
      {showConfirm ? (
        <div className="plan-review-actions">
          <button className="primary-action" disabled={busy} onClick={onConfirm} type="button">
            {busy ? <Loader2 className="spin" aria-hidden="true" /> : <ShieldCheck aria-hidden="true" />}
            确认并开始研究
          </button>
        </div>
      ) : null}
    </section>
  );
}

function ProcessTimeline({ steps }: { steps: StepView[] }) {
  return (
    <section className="research-terminal-card">
      <header className="section-title"><Search aria-hidden="true" /><div><h2>研究进度</h2><p>从问题理解到报告生成，每一步都能看到。</p></div></header>
      <ol className="timeline">
        {steps.map((step) => (
          <li className={`timeline-item state-${step.state}`} key={step.id}>
            {step.state === "已完成" ? <CheckCircle2 aria-hidden="true" /> : step.state === "运行中" ? <Loader2 className="spin" aria-hidden="true" /> : <Circle aria-hidden="true" />}
            <div><strong>{step.title}</strong><span>{step.description}</span></div>
            <em>{step.state}</em>
          </li>
        ))}
      </ol>
    </section>
  );
}

function RunSummary({ attempts, plan, run }: { attempts: number; plan?: V3CampaignPlanView; run?: V3ResearchRunControlView }) {
  return (
    <section className="research-terminal-card">
      <header className="section-title"><FileText aria-hidden="true" /><div><h2>本轮运行</h2><p>展示系统实际用了什么数据、哪些小组参与、消耗了多少试验。</p></div></header>
      <dl className="summary-list">
        <div><dt>数据范围</dt><dd>{plan?.dataScope.dateRange ?? "等待计划生成"}</dd></div>
        <div><dt>数据状态</dt><dd>{plan?.dataScope.availability ?? "等待检查"}</dd></div>
        <div><dt>参与小组</dt><dd>{run?.selectedSpecialists.length ?? plan?.selectedSpecialists.length ?? 0}</dd></div>
        <div><dt>候选数量</dt><dd>{run?.selectedSpecialists.reduce((sum, item) => sum + item.candidateCount, 0) ?? 0}</dd></div>
        <div><dt>机器试验</dt><dd>{run ? `${run.usedBudget} / ${run.totalBudget}` : "0 / 0"}</dd></div>
        <div><dt>状态核对</dt><dd>{attempts} 次</dd></div>
      </dl>
      {run?.typedFailures.length ? <div className="soft-warning"><strong>受控说明</strong>{run.typedFailures.map((item) => <p key={item}>{item}</p>)}</div> : null}
    </section>
  );
}

function SpecialistBoard({ run }: { run: V3ResearchRunControlView }) {
  return (
    <section className="research-terminal-card">
      <header className="section-title"><Sparkles aria-hidden="true" /><div><h2>专业研究小组</h2><p>每组独立形成候选，失败不会伪造成成功。</p></div></header>
      <div className="specialist-board">
        {run.selectedSpecialists.map((item) => (
          <article key={item.specialistId}>
            <span>{item.state}</span>
            <h3>{item.title}</h3>
            <p>{item.message}</p>
            <b>候选 {item.candidateCount}</b>
          </article>
        ))}
      </div>
    </section>
  );
}

function ReportPanel({ markdown }: { markdown: string }) {
  const summary = parseReport(markdown);
  return (
    <section className="research-terminal-card report-card">
      <header className="section-title"><FileText aria-hidden="true" /><div><h2>正式中文研究报告</h2><p>结论、候选、机器验证和限制都在这里。</p></div></header>
      <div className="customer-report">
        <article className="report-verdict">
          <span>最终结论</span>
          <h1>{summary.conclusion || "研究已完成，详见报告正文。"}</h1>
          {summary.advice ? <p>{summary.advice}</p> : null}
        </article>
        <div className="report-fact-grid">
          {summary.facts.map((fact) => (
            <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>
          ))}
        </div>
        {summary.candidates.length ? (
          <section className="candidate-summary-list">
            <h2>候选结果摘要</h2>
            <div>
              {summary.candidates.map((candidate) => (
                <article key={candidate.id}>
                  <header>
                    <span>{candidate.group}</span>
                    <h3>{candidate.id}</h3>
                  </header>
                  <p>{candidate.hypothesis || "本候选已进入机器验证，详见技术附录。"}</p>
                  <code>{candidate.expression || "表达式未公开展示"}</code>
                  <dl>
                    <div><dt>机器判断</dt><dd>{candidate.judgement || "未评价"}</dd></div>
                    <div><dt>主要原因</dt><dd>{candidate.reason || "无"}</dd></div>
                    {candidate.metrics.map((metric) => (
                      <div key={metric.label}><dt>{metric.label}</dt><dd>{metric.value}</dd></div>
                    ))}
                  </dl>
                </article>
              ))}
            </div>
          </section>
        ) : null}
        <details className="technical-appendix">
          <summary>展开完整机器报告与审计附录</summary>
          <div className="report-reader"><ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown></div>
        </details>
      </div>
    </section>
  );
}

function buildSteps(status: UiStatus, plan?: V3CampaignPlanView, run?: V3ResearchRunControlView, report = ""): StepView[] {
  const hasPlan = Boolean(plan);
  const hasRun = Boolean(run);
  const hasCandidates = Boolean(run?.selectedSpecialists.some((item) => item.candidateCount > 0));
  const hasTrials = Boolean(run && run.usedBudget > 0);
  const hasReport = Boolean(report);
  const dataDescription = plan ? `${plan.dataScope.availability} · ${plan.dataScope.dateRange}` : "确认股票池、时间范围和字段";
  const dataState = plan ? (plan.dataScope.availability === "不可用" ? "需要注意" : "已完成") : "等待中";
  return [
    { id: "question", title: "理解研究问题", description: plan?.researchSubject ?? "等待输入问题", state: hasPlan ? "已完成" : status === "planning" ? "运行中" : "等待中" },
    { id: "data", title: "检查研究数据", description: dataDescription, state: dataState },
    { id: "groups", title: "专业研究小组工作", description: run?.currentStage ?? "等待小组启动", state: hasCandidates ? "已完成" : hasRun ? "运行中" : "等待中" },
    { id: "preflight", title: "候选安全检查", description: "表达式检查、字段白名单、禁止任意 Python 执行", state: hasCandidates ? "已完成" : hasRun ? "运行中" : "等待中" },
    { id: "evidence", title: "机器证据评价", description: "PIT、成本后表现、稳定性、增量性", state: hasTrials ? "已完成" : hasCandidates ? "运行中" : "等待中" },
    { id: "report", title: "生成正式报告", description: "把机器事实翻译成中文研究结论", state: hasReport ? "已完成" : run && TERMINAL_STATES.has(run.state) ? "运行中" : "等待中" },
  ];
}

interface ParsedReport {
  conclusion: string;
  advice: string;
  facts: { label: string; value: string }[];
  candidates: ParsedCandidate[];
}

interface ParsedCandidate {
  group: string;
  id: string;
  hypothesis: string;
  expression: string;
  judgement: string;
  reason: string;
  metrics: { label: string; value: string }[];
}

function parseReport(markdown: string): ParsedReport {
  const lines = markdown.split(/\r?\n/);
  const plain = (value: string) => value.replace(/^[-•]\s*/, "").replace(/\*\*/g, "").replace(/`/g, "").trim();
  const conclusionLine = lines.find((line) => line.includes("结论：") || line.includes("当前结论："));
  const conclusion = conclusionLine ? plain(conclusionLine).replace(/^当前结论：/, "").replace(/^结论：/, "") : "";
  const adviceLine = lines.find((line) => line.includes("本轮建议："));
  const advice = adviceLine ? plain(adviceLine).replace(/^本轮建议：/, "") : "";
  const facts = [
    pickFact(lines, "用户问题") ?? pickFact(lines, "研究问题"),
    pickFact(lines, "研究范围") ?? combineFact("数据区间", pickFact(lines, "数据区间"), pickFact(lines, "数据来源")),
    pickFact(lines, "候选进度"),
    pickFact(lines, "研究结果类型"),
    pickFact(lines, "报告哈希"),
  ].filter((item): item is { label: string; value: string } => Boolean(item));
  const candidates: ParsedCandidate[] = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.startsWith("### ") || !line.includes(" / ")) continue;
    const title = line.slice(4).trim();
    const [group, id] = title.split(" / ", 2);
    const block: string[] = [];
    for (let cursor = index + 1; cursor < lines.length && !lines[cursor].startsWith("### ") && !lines[cursor].startsWith("## "); cursor += 1) {
      block.push(lines[cursor]);
    }
    candidates.push({
      group,
      id,
      hypothesis: pickBlockValue(block, "假设"),
      expression: pickBlockValue(block, "Qlib 研究表达式") || pickBlockValue(block, "受限表达式"),
      judgement: pickBlockValue(block, "当前判断") || pickBlockValue(block, "机器验证") || pickBlockValue(block, "机器证据"),
      reason: pickBlockValue(block, "判断依据") || pickBlockValue(block, "独立晋级"),
      metrics: [
        pickMetric(block, "训练期 ICIR"),
        pickMetric(block, "验证期 ICIR"),
        pickMetric(block, "validation_icir", "验证期 ICIR"),
        pickMetric(block, "validation_rank_ic", "验证 RankIC"),
        pickMetric(block, "validation_net_return", "验证成本后收益"),
        pickMetric(block, "validation_turnover", "验证换手"),
      ].filter((item): item is { label: string; value: string } => Boolean(item)),
    });
  }
  return { conclusion, advice, facts, candidates: candidates.slice(0, 8) };
}

function combineFact(label: string, first?: { label: string; value: string }, second?: { label: string; value: string }): { label: string; value: string } | undefined {
  const parts = [first?.value, second?.value].filter(Boolean);
  return parts.length ? { label, value: parts.join("；") } : undefined;
}

function pickFact(lines: string[], label: string): { label: string; value: string } | undefined {
  const line = lines.find((item) => item.includes(`${label}：`));
  if (!line) return undefined;
  return { label, value: line.replace(/^[-•]\s*/, "").replace(/\*\*/g, "").replace(/`/g, "").split(`${label}：`).slice(1).join(`${label}：`).trim() };
}

function pickBlockValue(lines: string[], label: string): string {
  const found = pickFact(lines, label);
  return found?.value ?? "";
}

function pickMetric(lines: string[], key: string, label = key): { label: string; value: string } | undefined {
  const found = pickFact(lines, key);
  if (!found) return undefined;
  return { label, value: found.value };
}

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export default ResearchOSV3LaunchPage;
