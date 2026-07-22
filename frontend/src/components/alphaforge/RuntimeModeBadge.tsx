import { Bot, CircleOff, Cpu } from "lucide-react";
import type { RuntimeMode } from "@/features/alphaforge";

const labels: Record<RuntimeMode, string> = {
  DETERMINISTIC: "确定性离线",
  REAL_LLM: "真实模型",
  BLOCKED: "不可用",
};

export function RuntimeModeBadge({ mode }: { mode: RuntimeMode }) {
  const Icon = mode === "REAL_LLM" ? Bot : mode === "BLOCKED" ? CircleOff : Cpu;
  return (
    <span className={`af-runtime-badge af-runtime-${mode.toLowerCase()}`} data-runtime-mode={mode}>
      <Icon aria-hidden="true" />
      {labels[mode]}
    </span>
  );
}
