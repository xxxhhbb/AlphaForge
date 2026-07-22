import { ArrowUpRight, GitFork, ShieldCheck } from "lucide-react";
import { factorActionLabels } from "@/features/alphaforge";
import type { FactorLibraryItemView } from "@/features/alphaforge";

const eligibilityLabels: Record<FactorLibraryItemView["eligibility"], string> = {
  DISCOVERED: "已发现",
  METADATA_VALIDATED: "元数据已验证",
  DAG_PORTED: "已转为 FactorDAG 草案",
  PIT_TESTED: "已通过 PIT 测试",
  AUXILIARY_ONLY: "仅辅助研究",
  REJECTED: "已拒绝",
};

const qualificationLabels: Record<FactorLibraryItemView["qualificationStatus"], string> = {
  DIRECT_FACTOR_DAG: "Direct FactorDAG 种子",
  NEEDS_DETERMINISTIC_OPERATOR: "需要确定性算子",
  UNSAFE_AUXILIARY_ONLY: "仅辅助研究",
  REJECTED: "已拒绝",
};

export function FactorLibraryGrid({ factors }: { factors: FactorLibraryItemView[] }) {
  return (
    <>
      <div className="af-library-count" role="status">
        静态目录 {factors.length} 项 · 资格状态来自 factor_library_qualification · 默认离线 fixture
      </div>
      <div className="af-factor-grid">
        {factors.map((factor) => (
          <article className="af-factor-card" key={factor.factorId}>
            <header>
              <span>{factor.sourceFamily} · {factor.theme}</span>
              <small><ShieldCheck aria-hidden="true" />{eligibilityLabels[factor.eligibility]}</small>
            </header>
            <h2>{factor.displayName}</h2>
            <code>{factor.factorId}</code>
            <p>{factor.visibleSummary}</p>
            <div className="af-lineage"><GitFork aria-hidden="true" /><span>{factor.lineage}</span></div>
            <p className="af-factor-qualification">资格状态：{qualificationLabels[factor.qualificationStatus]}</p>
            <footer aria-label={`${factor.displayName}可用操作`}>
              {factor.actions.map((action) => (
                <a
                  href={`/campaign/confirm?action=${action}&factor_id=${encodeURIComponent(factor.factorId)}`}
                  key={action}
                >
                  {factorActionLabels[action]}<ArrowUpRight aria-hidden="true" />
                </a>
              ))}
            </footer>
          </article>
        ))}
      </div>
    </>
  );
}
