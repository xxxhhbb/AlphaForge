"""Integration-owned adapters for the Agent C bounded LLM Desks package.

These adapters keep LLM desks in the DRAFT-only lane.  They expose the existing
FactorDAG parser through `CompilePITService` and expose only redacted official
validation summaries through `OfficialEvaluateService`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ..adapters.proposal import compile_proposal_expression
from ..contracts import ContractValidationError, FailureCode
from ..data import FieldRole
from ..llm_desks.api import (
    CompilePITArtifact,
    DeskId,
    DeskLLMConfig,
    DeterministicProposalProvider,
    FactorDraftFill,
    MultiDeskRunResult,
    OfficialEvaluationView,
    ProposalProvider,
    ReadOnlyToolRegistry,
    RedactedEvidenceSummary,
    ResearchQuestionView,
    ResearchStageController,
    build_default_desk_configs,
    build_default_tools_for_desk,
)
from ..llm_desks.safety import canonical_hash


@dataclass(frozen=True, kw_only=True)
class FactorDAGCompilePITAdapter:
    """Compile inert Desk factor expressions and bind them to a frozen PIT hash."""

    field_roles: Mapping[str, FieldRole]
    pit_snapshot_hash: str

    def compile_and_validate(
        self,
        *,
        desk_id: DeskId,
        question: ResearchQuestionView,
        factor: FactorDraftFill,
    ) -> CompilePITArtifact:
        compiled = compile_proposal_expression(factor.expression, field_roles=self.field_roles)
        if set(compiled.required_fields) != set(factor.variables):
            raise ContractValidationError(
                FailureCode.INVALID_FACTOR_DAG,
                "LLM Desk declared variables do not match compiled FactorDAG fields",
                details={
                    "desk_id": desk_id.value,
                    "declared": tuple(sorted(factor.variables)),
                    "compiled": compiled.required_fields,
                },
            )
        return CompilePITArtifact(
            compiled_factor_hash=compiled.artifact_hash,
            pit_snapshot_hash=self.pit_snapshot_hash,
        )


@dataclass(frozen=True, kw_only=True)
class InMemoryOfficialEvaluationAdapter:
    """Return a sealed-redacted validation view for controller feedback.

    The adapter is for integration tests and offline wiring smoke.  Production
    wiring should replace `visible_metrics` with a ResearchSummary/OfficialEvidence
    view from the deterministic Evidence Service.
    """

    visible_metrics: Mapping[str, float] = field(
        default_factory=lambda: {
            "rank_ic": 0.02,
            "icir": 0.25,
            "net_return": 0.001,
            "observation_count": 24.0,
        }
    )
    allowed_notes: tuple[str, ...] = ("train/validation redacted summary only",)

    def evaluate_official(
        self,
        *,
        desk_id: DeskId,
        question: ResearchQuestionView,
        compiled: CompilePITArtifact,
    ) -> OfficialEvaluationView:
        evidence_hash = canonical_hash(
            {
                "desk_id": desk_id.value,
                "question_hash": question.artifact_hash,
                "compiled_factor_hash": compiled.compiled_factor_hash,
                "pit_snapshot_hash": compiled.pit_snapshot_hash,
                "visible_metrics": dict(self.visible_metrics),
            }
        )
        return OfficialEvaluationView(
            evidence_hash=evidence_hash,
            redacted_summary=RedactedEvidenceSummary(
                evidence_hash=evidence_hash,
                visible_metrics=dict(self.visible_metrics),
                allowed_notes=self.allowed_notes,
            ),
        )


def run_bounded_llm_desks_offline(
    *,
    question: ResearchQuestionView,
    field_roles: Mapping[str, FieldRole],
    pit_snapshot_hash: str,
    desks: tuple[DeskLLMConfig, ...] | None = None,
    providers: Mapping[DeskId, ProposalProvider] | None = None,
    tools: Mapping[DeskId, ReadOnlyToolRegistry] | None = None,
    evaluator: InMemoryOfficialEvaluationAdapter | None = None,
) -> MultiDeskRunResult:
    """Run the fixed five-stage Desk controller with integration adapters.

    This is an offline smoke hook: deterministic provider by default, no LLM key,
    no network, no route registration, and no promotion authority.
    """

    active_desks = desks or build_default_desk_configs()
    active_providers = providers or {desk.desk_id: DeterministicProposalProvider() for desk in active_desks}
    active_tools = tools or {desk.desk_id: build_default_tools_for_desk(desk.desk_id) for desk in active_desks}
    controller = ResearchStageController(
        compiler=FactorDAGCompilePITAdapter(field_roles=field_roles, pit_snapshot_hash=pit_snapshot_hash),
        evaluator=evaluator or InMemoryOfficialEvaluationAdapter(),
    )
    return controller.run_all(
        desks=active_desks,
        question=question,
        providers=active_providers,
        tools=active_tools,
    )


__all__ = [
    "FactorDAGCompilePITAdapter",
    "InMemoryOfficialEvaluationAdapter",
    "run_bounded_llm_desks_offline",
]
