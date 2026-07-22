import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  ArrowUpRight,
  BookOpenCheck,
  CheckCircle2,
  CircleHelp,
  Gauge,
  Layers3,
  PauseCircle,
  Search,
  ShieldCheck,
} from "lucide-react";
import {
  advancedResearchModes,
  defaultCampaignApi,
  interactionStyles,
  offlineFactorLibrary,
  researchDepths,
  researchModes,
  sanitizePublicPayload,
} from "@/features/alphaforge";
import type {
  CampaignApiAdapter,
  CampaignDraftView,
  CampaignProviderStatusView,
  CampaignRecommendation,
  CampaignRunView,
  FactorLibraryItemView,
  InteractionStyleId,
  ResearchDepthId,
  ResearchModeId,
} from "@/features/alphaforge";
import {
  CampaignDeskBoard,
  CampaignDraftPanel,
  CampaignModePicker,
  FactorLibraryGrid,
  RuntimeModeBadge,
} from "@/components/alphaforge";
import "@/styles/alphaforge/campaign.css";

type WorkspaceView = "campaign" | "dashboard" | "library";

interface CampaignWorkspacePageProps {
  api?: CampaignApiAdapter;
  run?: CampaignRunView;
  factors?: FactorLibraryItemView[];
}

export function CampaignWorkspacePage({
  api = defaultCampaignApi,
  run,
  factors = offlineFactorLibrary,
}: CampaignWorkspacePageProps) {
  const [view, setView] = useState<WorkspaceView>("campaign");
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<ResearchModeId>();
  const [depth, setDepth] = useState<ResearchDepthId>("STANDARD_RESEARCH");
  const [interaction, setInteraction] = useState<InteractionStyleId>("STANDARD");
  const [draft, setDraft] = useState<CampaignDraftView>();
  const [activeRun, setActiveRun] = useState<CampaignRunView>();
  const [recommendation, setRecommendation] = useState<CampaignRecommendation>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [starting, setStarting] = useState(false);
  const [providerStatus, setProviderStatus] = useState<CampaignProviderStatusView>();
  const [realLlmExperiment, setRealLlmExperiment] = useState(false);
  const allModes = useMemo(() => [...researchModes, ...advancedResearchModes], []);
  const safeRun = useMemo(
    () => (activeRun ?? run ? sanitizePublicPayload(activeRun ?? run) : undefined),
    [activeRun, run],
  );
  const safeFactors = useMemo(() => sanitizePublicPayload(factors), [factors]);

  useEffect(() => {
    let mounted = true;
    api.getProviderStatus()
      .then((status) => { if (mounted) setProviderStatus(sanitizePublicPayload(status)); })
      .catch(() => { if (mounted) setProviderStatus({ status: "UNAVAILABLE", reasonCode: "STATUS_UNAVAILABLE" }); });
    return () => { mounted = false; };
  }, [api]);

  const selectMode = (next: ResearchModeId) => {
    setMode(next);
    setDraft(undefined);
    setActiveRun(undefined);
    setRecommendation(undefined);
    const option = allModes.find((item) => item.id === next);
    if (option && !question.trim()) setQuestion(option.exampleQuestion);
  };

  const askForRecommendation = async () => {
    setBusy(true);
    setError("");
    setDraft(undefined);
    try {
      setRecommendation(await api.recommend(question.trim() || "我不确定，帮我选择"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "暂时无法生成推荐");
    } finally {
      setBusy(false);
    }
  };

  const createDraft = async () => {
    if (!mode) {
      setError("请先选择研究模式");
      return;
    }
    setBusy(true);
    setError("");
    setRecommendation(undefined);
    setActiveRun(undefined);
    try {
      setDraft(await api.createDraft({ question, mode, depth, interactionStyle: interaction }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Campaign Draft 创建失败");
    } finally {
      setBusy(false);
    }
  };

  const confirmDraft = async () => {
    if (!draft) return;
    setBusy(true);
    setError("");
    try {
      const confirmed = await api.confirm(draft);
      setDraft(confirmed);
      if (confirmed.campaignId) {
        setActiveRun(await api.getRun(confirmed.campaignId));
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Campaign 确认失败");
    } finally {
      setBusy(false);
    }
  };

  const startRun = async () => {
    const campaignId = activeRun?.campaignId ?? draft?.campaignId;
    if (!campaignId) {
      setError("请先确认 Campaign，再启动研究");
      return;
    }
    setStarting(true);
    setError("");
    try {
      setActiveRun(await api.startRun(campaignId, realLlmExperiment ? "REAL_LLM" : "DETERMINISTIC"));
      setView("dashboard");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "研究启动失败");
    } finally {
      setStarting(false);
    }
  };

  return (
    <main className="af-product-shell">
      <header className="af-product-header">
        <a className="af-brand" href="/" aria-label="AlphaForge 千因研究院首页">
          <span>AF</span><strong>AlphaForge<small>千因研究院</small></strong>
        </a>
        <nav className="af-workspace-nav" aria-label="AlphaForge 研究工作区">
          <button aria-current={view === "campaign" ? "page" : undefined} onClick={() => setView("campaign")} type="button">发起研究</button>
          <button aria-current={view === "dashboard" ? "page" : undefined} onClick={() => setView("dashboard")} type="button">运行驾驶舱</button>
          <button aria-current={view === "library" ? "page" : undefined} onClick={() => setView("library")} type="button">Alpha 因子库</button>
        </nav>
        <div className="af-product-header-meta">
          <RuntimeModeBadge mode={realLlmExperiment ? "REAL_LLM" : "DETERMINISTIC"} />
          <span><ShieldCheck aria-hidden="true" />受控研究环境</span>
        </div>
      </header>

      {view === "campaign" ? (
        <>
          <section className="af-question-stage">
            <div className="af-question-copy">
              <p className="af-kicker">Research Campaign</p>
              <h1>你今天希望解决什么研究问题？</h1>
              <p>描述目标，系统会把它编译成有预算、有检查点、可暂停的研究活动。专业 Agent 负责提出草案，确定性服务负责验证事实。</p>
            </div>
            <label className="af-question-input">
              <span>研究问题</span>
              <textarea
                aria-label="研究问题"
                onChange={(event) => { setQuestion(event.target.value); setDraft(undefined); setActiveRun(undefined); }}
                placeholder="例如：最近动量因子为什么失效？"
                rows={3}
                value={question}
              />
              <small>不要输入密钥、账户信息或未批准的数据路径。</small>
            </label>
          </section>

          <CampaignModePicker onSelect={selectMode} onUnsure={askForRecommendation} selected={mode} />

          {recommendation ? (
            <section className="af-recommendation" role="status">
              <CircleHelp aria-hidden="true" />
              <div>
                <p className="af-kicker">Research Copilot 推荐</p>
                <h2>{allModes.find((item) => item.id === recommendation.recommendedMode)?.title}</h2>
                <p>{recommendation.explanation}</p>
                <ul>{recommendation.questions.map((item) => <li key={item}>{item}</li>)}</ul>
                <button onClick={() => selectMode(recommendation.recommendedMode)} type="button">采用推荐并继续填写</button>
              </div>
              <span className="af-recommendation-boundary">只推荐，不运行</span>
            </section>
          ) : null}

          <section className="af-configuration" aria-labelledby="af-config-title">
            <div className="af-section-heading">
              <div><p className="af-kicker">运行方式</p><h2 id="af-config-title">选择研究深度与协作方式</h2></div>
              <p>这些选项决定研究边界和暂停节点，不改变任何金融权威。</p>
            </div>
            <div className="af-config-grid">
              <fieldset>
                <legend>研究深度</legend>
                {researchDepths.map((item) => (
                  <label className={depth === item.id ? "is-selected" : ""} key={item.id}>
                    <input checked={depth === item.id} name="depth" onChange={() => { setDepth(item.id); setDraft(undefined); }} type="radio" />
                    <span><strong>{item.title}</strong><small>{item.description}</small><em>{item.boundary}</em></span>
                  </label>
                ))}
              </fieldset>
              <fieldset>
                <legend>协作方式</legend>
                {interactionStyles.map((item) => (
                  <label className={interaction === item.id ? "is-selected" : ""} key={item.id}>
                    <input checked={interaction === item.id} name="interaction" onChange={() => { setInteraction(item.id); setDraft(undefined); }} type="radio" />
                    <span><strong>{item.title}</strong><small>{item.description}</small></span>
                  </label>
                ))}
              </fieldset>
            </div>
            <section className="af-provider-status" aria-label="真实 LLM 实验模式">
              <div>
                <p className="af-kicker">LLM Experiment</p>
                <h3>真实 LLM 实验模式</h3>
                <p>LLM 仅生成研究草案与解释；确定性服务仍负责 Evidence，且不授予 Promotion、Risk、Order 或 Ledger 权限。</p>
                <small>
                  Provider：{providerStatus?.status === "AVAILABLE" ? "可用" : "不可用"}
                  {providerStatus?.host ? ` · ${providerStatus.host}` : ""}
                  {providerStatus?.model ? ` · ${providerStatus.model}` : ""}
                </small>
              </div>
              <label className="af-llm-toggle">
                <input
                  aria-label="启用真实 LLM 实验模式"
                  checked={realLlmExperiment}
                  disabled={providerStatus?.status !== "AVAILABLE"}
                  onChange={(event) => setRealLlmExperiment(event.target.checked)}
                  type="checkbox"
                />
                <span>{realLlmExperiment ? "已启用" : "默认确定性"}</span>
              </label>
            </section>
            <div className="af-create-row">
              <div><Layers3 aria-hidden="true" /><span><strong>生成后仍是待确认草案</strong><small>确认前不创建实验，也不消耗 Trial 预算。</small></span></div>
              <button className="af-primary-button" disabled={busy || !mode || !question.trim()} onClick={createDraft} type="button">
                {busy ? "正在准备…" : "生成 Campaign Draft"}<ArrowRight aria-hidden="true" />
              </button>
            </div>
          </section>

          {error ? <p className="af-error" role="alert">{error}</p> : null}
          {draft ? (
            <CampaignDraftPanel
              confirming={busy}
              draft={draft}
              onConfirm={confirmDraft}
              onOpenDashboard={() => setView("dashboard")}
            />
          ) : null}
        </>
      ) : null}

      {view === "dashboard" ? (
        safeRun
          ? <DashboardView onStart={startRun} run={safeRun} starting={starting} />
          : <EmptyDashboardView />
      ) : null}
      {view === "library" ? <LibraryView factors={safeFactors} /> : null}

      <footer className="af-product-footer">
        <CheckCircle2 aria-hidden="true" />
        <p>AlphaForge 只负责受控研究。页面不收集服务商密钥，不提供真实账户、真实下单或账本修改入口。</p>
      </footer>
    </main>
  );
}

function EmptyDashboardView() {
  return (
    <section className="af-dashboard-section" aria-label="尚未启动研究">
      <div className="af-section-heading">
        <div>
          <p className="af-kicker">Campaign 运行驾驶舱</p>
          <h1>尚未选择或启动 Research Campaign</h1>
        </div>
        <p>请先生成并确认 Campaign。这里不会用演示分支或静态结果替代真实运行状态。</p>
      </div>
    </section>
  );
}

function DashboardView({
  onStart,
  run,
  starting,
}: {
  onStart: () => void;
  run: CampaignRunView;
  starting: boolean;
}) {
  const failed = run.selectedDesks.filter((desk) => desk.status === "FAILED" || desk.status === "BLOCKED").length;
  const completed = run.selectedDesks.filter((desk) => desk.status === "COMPLETED").length;
  const hasStarted = run.state === "RUNNING" || run.state === "AWAITING_CHECKPOINT" || run.state === "VALIDATING" || run.state === "COMPLETED";
  return (
    <>
      <header className="af-page-heading">
        <div><p className="af-kicker">Campaign 运行驾驶舱</p><h1>研究分支、预算与检查点</h1><code>{run.campaignId}</code></div>
        <RuntimeModeBadge mode={run.runtimeMode} />
      </header>
      <section className="af-run-summary" aria-label="Campaign 运行摘要">
        <article><span>已选 Desk</span><strong>{run.selectedDesks.length}</strong><small>只展示 Router 选中的分支</small></article>
        <article><span>已完成分支</span><strong>{completed}</strong><small>完成与失败严格区分</small></article>
        <article><span>失败分支</span><strong>{failed}</strong><small>不会伪造结果</small></article>
        <article><span>Trial 预算</span><strong>{run.trialsUsed} / {run.budget.trials}</strong><small>失败也消耗预算</small></article>
      </section>
      <section className="af-checkpoint-banner">
        <PauseCircle aria-hidden="true" />
        <div><p className="af-kicker">当前检查点</p><h2>{run.currentCheckpoint}</h2><p>{run.messages.join(" ")}</p></div>
        <span>{run.checkpointState === "REACHED" ? "等待你的确认" : "尚未到达"}</span>
        <button className="af-primary-button" disabled={starting || hasStarted} onClick={onStart} type="button">
          {hasStarted ? "研究已启动" : starting ? "正在启动研究…" : "启动研究"}
        </button>
      </section>
      <section className="af-dashboard-section">
        <div className="af-section-heading">
          <div><p className="af-kicker">Selected Desks</p><h2>只运行本次路由选中的专业 Agent</h2></div>
          <p>每个分支拥有独立上下文、只读工具、预算和产物目录。一个分支失败不会拖垮无依赖分支。</p>
        </div>
        <CampaignDeskBoard desks={run.selectedDesks} />
      </section>
      {run.researchOutputs?.length ? (
        <section className="af-dashboard-section" aria-label="研究 Desk 草案产物">
          <div className="af-section-heading">
            <div><p className="af-kicker">Research Outputs</p><h2>真实 Desk 产出的研究草案</h2></div>
            <p>这些内容是 DRAFT。FactorDAG 编译与评价若标记为 smoke，不代表生产 PIT、官方 Evidence 或晋级。</p>
          </div>
          <div className="af-research-output-grid">
            {run.researchOutputs.map((output) => (
              <article className="af-research-output-card" key={output.task_id}>
                <header>
                  <strong>{run.selectedDesks.find((desk) => desk.id === output.desk_id)?.title ?? output.desk_id}</strong>
                  <span>{output.status} · {output.authority}</span>
                </header>
                {output.hypothesis ? (
                  <section>
                    <small>可证伪假设</small>
                    <h3>{output.hypothesis.statement}</h3>
                    <p>{output.hypothesis.economic_mechanism}</p>
                    <ul>{output.hypothesis.falsification_tests.map((test) => <li key={test}>{test}</li>)}</ul>
                  </section>
                ) : null}
                {output.factor_draft ? (
                  <section>
                    <small>FactorDAG 草案</small>
                    <h3>{output.factor_draft.name}</h3>
                    <p>{output.factor_draft.description}</p>
                    <code>{output.factor_draft.expression}</code>
                  </section>
                ) : null}
                {output.redacted_feedback ? (
                  <section><small>脱敏反馈</small><p>{output.redacted_feedback.explanation}</p></section>
                ) : null}
                {output.message ? <p className="af-failure-note" role="alert">{output.message}</p> : null}
              </article>
            ))}
          </div>
        </section>
      ) : null}
      <section className="af-dashboard-links">
        <a href={run.reportPath ?? "/reports"}><Gauge aria-hidden="true" /><span><strong>查看研究报告</strong><small>模式化结论、失败原因与可追溯哈希</small></span><ArrowUpRight aria-hidden="true" /></a>
        <a href={run.workbenchPath ?? "/veriquant/workbench"}><ShieldCheck aria-hidden="true" /><span><strong>打开 Workbench</strong><small>查看允许公开的 Evidence 与产物详情</small></span><ArrowUpRight aria-hidden="true" /></a>
      </section>
      <aside className="af-authority-note"><AlertTriangle aria-hidden="true" /><p>Agent 输出始终是草案或解释。官方指标、Promotion、组合、风险、执行与账本状态不由本页面或模型写入。</p></aside>
    </>
  );
}

function LibraryView({ factors }: { factors: FactorLibraryItemView[] }) {
  return (
    <>
      <header className="af-page-heading">
        <div><p className="af-kicker">Alpha 因子库</p><h1>把已登记因子作为研究起点</h1><p>查看版本、谱系、FactorDAG 资格和允许公开的摘要，再发起新的 Research Campaign。</p></div>
        <span className="af-readonly-badge"><ShieldCheck aria-hidden="true" />只读目录</span>
      </header>
      <section className="af-library-toolbar">
        <label><Search aria-hidden="true" /><input aria-label="搜索因子" placeholder="搜索因子、主题或家族" type="search" /></label>
        <p><BookOpenCheck aria-hidden="true" />目录只提供研究入口，不允许在此改写指标或金融状态。</p>
      </section>
      <FactorLibraryGrid factors={factors} />
    </>
  );
}
