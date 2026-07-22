import { AlertTriangle, CheckCircle2, Circle, CircleOff, LoaderCircle } from "lucide-react";
import { lifecycleLabels } from "@/features/alphaforge";
import type { CampaignDeskView } from "@/features/alphaforge";
import { RuntimeModeBadge } from "./RuntimeModeBadge";

const statusIcon = {
  COMPLETED: CheckCircle2,
  RUNNING: LoaderCircle,
  WAITING: Circle,
  FAILED: AlertTriangle,
  BLOCKED: CircleOff,
};

export function CampaignDeskBoard({ desks }: { desks: CampaignDeskView[] }) {
  return (
    <div className="af-desk-board" aria-label="本次 Campaign 选中的研究 Desk">
      {desks.map((desk) => {
        const DeskIcon = statusIcon[desk.status];
        return (
          <article className={`af-desk-card af-status-${desk.status.toLowerCase()}`} data-desk-status={desk.status} key={desk.id}>
            <header>
              <span className="af-desk-status-icon"><DeskIcon aria-hidden="true" /></span>
              <div><h3>{desk.title}</h3><p>{lifecycleLabels[desk.status]}</p></div>
              <RuntimeModeBadge mode={desk.runtimeMode} />
            </header>
            <div className="af-desk-budget">
              <span>分支预算</span><strong>{desk.budgetUsed} / {desk.budgetLimit}</strong>
              <progress aria-label={`${desk.title}预算`} max={desk.budgetLimit} value={desk.budgetUsed} />
            </div>
            <ol className="af-stage-list">
              {desk.stages.map((item) => {
                const StageIcon = statusIcon[item.status];
                return (
                  <li data-stage-status={item.status} key={item.id}>
                    <StageIcon aria-hidden="true" />
                    <span><strong>{item.title}</strong><small>{item.detail}</small></span>
                    <em>{lifecycleLabels[item.status]}</em>
                  </li>
                );
              })}
            </ol>
            {desk.failure ? <p className="af-failure-note" role="alert"><AlertTriangle aria-hidden="true" />{desk.failure}</p> : null}
          </article>
        );
      })}
    </div>
  );
}
