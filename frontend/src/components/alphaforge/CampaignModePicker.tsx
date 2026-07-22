import {
  Activity,
  ArrowUpRight,
  BookOpenCheck,
  Compass,
  FlaskConical,
  GitCompareArrows,
  Lightbulb,
  Puzzle,
} from "lucide-react";
import { advancedResearchModes, researchModes } from "@/features/alphaforge";
import type { ResearchModeId } from "@/features/alphaforge";

const icons = [Lightbulb, FlaskConical, GitCompareArrows, Puzzle, Activity, BookOpenCheck];

interface CampaignModePickerProps {
  selected?: ResearchModeId;
  onSelect: (mode: ResearchModeId) => void;
  onUnsure: () => void;
}

export function CampaignModePicker({ selected, onSelect, onUnsure }: CampaignModePickerProps) {
  return (
    <section className="af-mode-section" aria-labelledby="af-mode-title">
      <div className="af-section-heading">
        <div>
          <p className="af-kicker">研究模式</p>
          <h2 id="af-mode-title">这次研究要解决什么问题？</h2>
        </div>
        <p>研究模式描述目标；动量、价值、反转与事件只是研究对象或分支。</p>
      </div>

      <div className="af-mode-grid">
        {researchModes.map((mode, index) => {
          const Icon = icons[index];
          const active = selected === mode.id;
          return (
            <button
              aria-pressed={active}
              className={`af-mode-card${active ? " is-selected" : ""}`}
              key={mode.id}
              onClick={() => onSelect(mode.id)}
              type="button"
            >
              <span className="af-mode-icon"><Icon aria-hidden="true" /></span>
              <span className="af-mode-number">{String(index + 1).padStart(2, "0")}</span>
              <strong>{mode.title}</strong>
              <span>{mode.exampleQuestion}</span>
              <small>{mode.output}</small>
            </button>
          );
        })}
      </div>

      <div className="af-mode-secondary-row">
        <button className="af-unsure-button" onClick={onUnsure} type="button">
          <Compass aria-hidden="true" />
          <span><strong>我不确定，帮我选择</strong><small>只给出推荐与澄清问题，不会直接运行</small></span>
          <ArrowUpRight aria-hidden="true" />
        </button>
        <details className="af-advanced-modes">
          <summary>高级研究</summary>
          <div>
            {advancedResearchModes.map((mode) => (
              <button key={mode.id} onClick={() => onSelect(mode.id)} type="button">
                <strong>{mode.title}</strong>
                <span>{mode.output}</span>
              </button>
            ))}
          </div>
        </details>
      </div>
    </section>
  );
}
