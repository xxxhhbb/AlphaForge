import { ArrowRight, Check, CircleAlert, Clock3, Database, Gauge, ShieldCheck } from "lucide-react";
import { deskLabels, lifecycleLabels, researchDepths, researchModes } from "@/features/alphaforge";
import type { CampaignDraftView } from "@/features/alphaforge";
import { RuntimeModeBadge } from "./RuntimeModeBadge";

interface CampaignDraftPanelProps {
  draft: CampaignDraftView;
  confirming?: boolean;
  onConfirm: () => void;
  onOpenDashboard?: () => void;
}

export function CampaignDraftPanel({ draft, confirming = false, onConfirm, onOpenDashboard }: CampaignDraftPanelProps) {
  const mode = researchModes.find((item) => item.id === draft.mode);
  const depth = researchDepths.find((item) => item.id === draft.depth);
  const confirmed = draft.state === "CONFIRMED";
  const quick = draft.depth === "QUICK_EXPLORATION";

  return (
    <section className="af-draft-panel" aria-labelledby="af-draft-title" data-campaign-state={draft.state}>
      <header className="af-draft-header">
        <div>
          <p className="af-kicker">Campaign Draft</p>
          <h2 id="af-draft-title">确认研究合同</h2>
        </div>
        <div className="af-draft-statuses">
          <span className={`af-state-chip${confirmed ? " is-confirmed" : ""}`}>
            {confirmed ? <Check aria-hidden="true" /> : <Clock3 aria-hidden="true" />}
            {lifecycleLabels[draft.state]}
          </span>
          <RuntimeModeBadge mode={draft.runtimeMode} />
        </div>
      </header>

      <div className="af-draft-question">
        <span>{mode?.title}</span>
        <strong>{draft.question}</strong>
        <p>{draft.researchObject}</p>
      </div>

      <dl className="af-contract-grid">
        <div><dt><Database aria-hidden="true" />历史股票池</dt><dd>{draft.universe.join("、")}</dd></div>
        <div><dt><Clock3 aria-hidden="true" />预测周期</dt><dd>{draft.horizon}</dd></div>
        <div><dt><Gauge aria-hidden="true" />研究深度</dt><dd>{depth?.title}</dd></div>
        <div><dt><ShieldCheck aria-hidden="true" />运行边界</dt><dd>{quick ? "只生成草案" : "确认后进入确定性研究流程"}</dd></div>
      </dl>

      <div className="af-contract-columns">
        <div>
          <h3>选中的专业 Desk</h3>
          <div className="af-chip-list">
            {draft.selectedDesks.map((desk) => <span key={desk}>{deskLabels[desk] ?? desk}</span>)}
          </div>
        </div>
        <div>
          <h3>研究方法</h3>
          <div className="af-chip-list muted">
            {draft.methodModels.map((method) => <span key={method}>{method}</span>)}
          </div>
        </div>
      </div>

      <div className="af-budget-strip">
        <span><small>因子家族</small><strong>{draft.budget.factorFamilies}</strong></span>
        <span><small>最大尝试</small><strong>{draft.budget.trials}</strong></span>
        <span><small>模型调用</small><strong>{draft.budget.modelCalls}</strong></span>
        <span><small>最长运行</small><strong>{draft.budget.runtimeMinutes} 分钟</strong></span>
      </div>

      <div className="af-contract-columns compact">
        <div><h3>用户检查点</h3><ul>{draft.checkpoints.map((item) => <li key={item}>{item}</li>)}</ul></div>
        <div><h3>停止条件</h3><ul>{draft.stopConditions.map((item) => <li key={item}>{item}</li>)}</ul></div>
      </div>

      {quick ? (
        <div className="af-boundary-note" data-quick-boundary="true">
          <CircleAlert aria-hidden="true" />
          <p><strong>快速探索只生成研究草案。</strong>完成后回到用户，不进入后续确定性验证链。</p>
        </div>
      ) : null}

      <footer className="af-draft-actions">
        {!confirmed ? (
          <button className="af-primary-button" disabled={confirming} onClick={onConfirm} type="button">
            <Check aria-hidden="true" />{confirming ? "正在确认…" : "确认此 Research Campaign"}
          </button>
        ) : (
          <button className="af-primary-button" onClick={onOpenDashboard} type="button">
            进入运行驾驶舱<ArrowRight aria-hidden="true" />
          </button>
        )}
        <p>{confirmed ? "已冻结股票池、周期、预算、检查点与停止条件。" : "确认前不会创建运行意图，也不会启动任何实验。"}</p>
      </footer>
    </section>
  );
}
