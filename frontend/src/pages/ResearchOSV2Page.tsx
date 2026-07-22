import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, ShieldCheck } from "lucide-react";
import {
  defaultResearchOSV2Client,
  PlanPanel,
  RunPanel,
  SuggestionPanel,
  type CollaborationStyle,
  type ResearchDepth,
  type ResearchOSV2Client,
  type ResearchPlanView,
  type ResearchRunView,
  type ResearchSuggestionView,
} from "@/features/research-os-v2";
import "@/features/research-os-v2/research-os-v2.css";

export interface ResearchOSV2PageProps {
  client?: ResearchOSV2Client;
  pollIntervalMs?: number;
  maxPollAttempts?: number;
}

const terminalRunStates = new Set(["已完成", "已阻断", "失败"]);

export function ResearchOSV2Page({
  client = defaultResearchOSV2Client,
  pollIntervalMs = 2_000,
  maxPollAttempts = 90,
}: ResearchOSV2PageProps) {
  const [question, setQuestion] = useState("挖一下最近 50 天的大动量因子");
  const [depth, setDepth] = useState<ResearchDepth>("标准研究");
  const [collaborationStyle, setCollaborationStyle] = useState<CollaborationStyle>("标准式");
  const [suggestion, setSuggestion] = useState<ResearchSuggestionView>();
  const [plan, setPlan] = useState<ResearchPlanView>();
  const [run, setRun] = useState<ResearchRunView>();
  const [busy, setBusy] = useState<"suggest" | "confirm" | "start">();
  const [error, setError] = useState("");
  const [pollingNotice, setPollingNotice] = useState("");
  const startPromiseRef = useRef<Promise<ResearchRunView> | null>(null);

  const phase = useMemo(() => run ? (run.state === "已完成" ? "研究结果" : run.state) : plan ? "已确认计划" : suggestion ? "待确认建议" : "描述问题", [plan, run, suggestion]);

  useEffect(() => {
    if (!run?.runId || terminalRunStates.has(run.state)) return;
    let cancelled = false;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;

    const poll = async () => {
      if (cancelled) return;
      if (attempts >= maxPollAttempts) {
        setPollingNotice("状态查询已达到上限；后台任务可能仍在运行，请稍后重新打开该研究记录。");
        return;
      }
      attempts += 1;
      controller = new AbortController();
      try {
        const next = await client.getResearchStatus(run.runId, controller.signal);
        if (cancelled) return;
        setRun(next);
        setPollingNotice(terminalRunStates.has(next.state) ? "" : `正在获取后台进度（${attempts}/${maxPollAttempts}）`);
        if (!terminalRunStates.has(next.state)) timer = setTimeout(poll, pollIntervalMs);
      } catch (reason) {
        if (cancelled || (reason instanceof DOMException && reason.name === "AbortError")) return;
        setPollingNotice(reason instanceof Error ? `状态查询停止：${reason.message}` : "状态查询停止：研究服务不可用。");
      }
    };

    timer = setTimeout(poll, pollIntervalMs);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      controller?.abort();
    };
  }, [client, maxPollAttempts, pollIntervalMs, run?.runId]);

  const createSuggestion = async () => {
    setBusy("suggest");
    setError("");
    setSuggestion(undefined);
    setPlan(undefined);
    setRun(undefined);
    setPollingNotice("");
    try {
      setSuggestion(await client.createSuggestion({ question, depth, collaborationStyle }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "暂时无法生成研究建议。");
    } finally {
      setBusy(undefined);
    }
  };

  const confirmPlan = async () => {
    if (!suggestion) return;
    setBusy("confirm");
    setError("");
    try {
      setPlan(await client.confirmPlan(suggestion, { depth, collaborationStyle }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "研究计划确认失败。");
    } finally {
      setBusy(undefined);
    }
  };

  const startResearch = async () => {
    if (!plan || startPromiseRef.current) return;
    setBusy("start");
    setError("");
    setPollingNotice("");
    try {
      const request = client.startResearch(plan.planId);
      startPromiseRef.current = request;
      setRun(await request);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "研究任务启动失败。");
    } finally {
      startPromiseRef.current = null;
      setBusy(undefined);
    }
  };

  return (
    <main className="ros2-shell">
      <div className="ros2-wrap">
        <header className="ros2-hero">
          <div>
            <p className="ros2-kicker">AlphaForge · 千因研究院</p>
            <h1>从一个研究问题，到可追溯的机器结论。</h1>
            <p>系统先判断问题能研究到什么程度，再组织互补的专业研究小组。研究模型负责提出草案，冻结数据与机器服务负责计算证据，独立评审负责决定是否进入下一阶段。</p>
          </div>
          <aside className="ros2-boundary"><ShieldCheck aria-hidden="true" /><strong>当前阶段：{phase}</strong><p>页面不收集服务商密钥，也不允许修改官方指标、晋级结论、持仓或订单。</p></aside>
        </header>

        <section className="ros2-question" aria-labelledby="ros2-question-title">
          <label id="ros2-question-title">你今天希望解决什么 Alpha 研究问题？
            <textarea aria-label="Alpha 研究问题" onChange={(event) => setQuestion(event.target.value)} placeholder="例如：挖一下最近 50 天的大动量因子" value={question} />
          </label>
          <div className="ros2-options">
            <label>研究深度<select aria-label="研究深度" onChange={(event) => setDepth(event.target.value as ResearchDepth)} value={depth}><option>快速探索</option><option>标准研究</option><option>严格验证</option></select></label>
            <label>协作方式<select aria-label="协作方式" onChange={(event) => setCollaborationStyle(event.target.value as CollaborationStyle)} value={collaborationStyle}><option>引导式</option><option>标准式</option><option>批处理式</option></select></label>
            <button className="ros2-primary" disabled={busy === "suggest" || !question.trim()} onClick={createSuggestion} type="button">{busy === "suggest" ? "正在分析…" : "生成研究建议"}<ArrowRight aria-hidden="true" /></button>
          </div>
          {error ? <p className="ros2-error" role="alert">{error}</p> : null}
        </section>

        <div className="ros2-content">
          {suggestion && !plan ? <SuggestionPanel confirming={busy === "confirm"} onConfirm={confirmPlan} suggestion={suggestion} /> : null}
          {plan && !run ? <PlanPanel onStart={startResearch} plan={plan} starting={busy === "start"} /> : null}
          {pollingNotice ? <p className="ros2-polling" role="status">{pollingNotice}</p> : null}
          {run ? <RunPanel run={run} /> : null}
        </div>
      </div>
    </main>
  );
}

export default ResearchOSV2Page;
