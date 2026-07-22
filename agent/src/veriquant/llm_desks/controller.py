"""Fixed five-stage controller for bounded LLM Research Desks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import require_hash, require_identifier, require_text
from .models import (
    CompilePITArtifact,
    DeskId,
    DeskLLMConfig,
    FIVE_STAGE_SEQUENCE,
    FactorDraftFill,
    FeedbackExplanation,
    HypothesisFill,
    LLMDeskStage,
    LLMDeskStatus,
    OfficialEvaluationView,
    ProviderSession,
    ProviderTrace,
    RedactedEvidenceSummary,
    ResearchQuestionView,
    coerce_desk_id,
)
from .providers import ProposalProvider
from .react import BoundedReActExplorer
from .safety import fail
from .tools import ReadOnlyToolRegistry


class CompilePITService(Protocol):
    def compile_and_validate(
        self,
        *,
        desk_id: DeskId,
        question: ResearchQuestionView,
        factor: FactorDraftFill,
    ) -> CompilePITArtifact: ...


class OfficialEvaluateService(Protocol):
    def evaluate_official(
        self,
        *,
        desk_id: DeskId,
        question: ResearchQuestionView,
        compiled: CompilePITArtifact,
    ) -> OfficialEvaluationView: ...


@dataclass(frozen=True, kw_only=True)
class StageRecord(CanonicalContract):
    stage: LLMDeskStage
    status: LLMDeskStatus
    artifact_hash: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, LLMDeskStage):
            object.__setattr__(self, "stage", LLMDeskStage(self.stage))
        if not isinstance(self.status, LLMDeskStatus):
            object.__setattr__(self, "status", LLMDeskStatus(self.status))
        if self.artifact_hash is not None:
            require_hash(self.artifact_hash, "artifact_hash")
        if self.failure_code is not None:
            require_identifier(self.failure_code, "failure_code")
        if self.status is LLMDeskStatus.DRAFT and self.failure_code is not None:
            fail(FailureCode.INVALID_VALUE, "successful stage cannot carry a failure code")


@dataclass(frozen=True, kw_only=True)
class DeskRunResult(CanonicalContract):
    desk_id: DeskId
    status: LLMDeskStatus
    stage_trace: tuple[StageRecord, ...]
    provider_trace: ProviderTrace
    hypothesis: HypothesisFill | None = None
    factor_draft: FactorDraftFill | None = None
    compile_pit: CompilePITArtifact | None = None
    official_evaluation: OfficialEvaluationView | None = None
    feedback: FeedbackExplanation | None = None
    failure_code: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "desk_id", coerce_desk_id(self.desk_id))
        if not isinstance(self.status, LLMDeskStatus):
            object.__setattr__(self, "status", LLMDeskStatus(self.status))
        if self.provider_trace.desk_id is not self.desk_id:
            fail(FailureCode.INVALID_VALUE, "provider trace belongs to another desk")
        if self.status is LLMDeskStatus.DRAFT:
            if tuple(record.stage for record in self.stage_trace) != FIVE_STAGE_SEQUENCE:
                fail(FailureCode.INVALID_STATE, "completed desk must follow the fixed five-stage sequence")
            if not all(record.status is LLMDeskStatus.DRAFT for record in self.stage_trace):
                fail(FailureCode.INVALID_STATE, "completed desk has a non-DRAFT stage")
            if any(item is None for item in (self.hypothesis, self.factor_draft, self.compile_pit, self.official_evaluation, self.feedback)):
                fail(FailureCode.INCOMPLETE_RESULT, "completed desk is missing an artifact")
            if self.failure_code is not None or self.message is not None:
                fail(FailureCode.INVALID_VALUE, "completed desk cannot carry failure metadata")
        else:
            if self.failure_code is None:
                fail(FailureCode.INVALID_VALUE, "non-completed desk needs a failure code")


@dataclass(frozen=True, kw_only=True)
class MultiDeskRunResult(CanonicalContract):
    results: tuple[DeskRunResult, ...]

    def __post_init__(self) -> None:
        seen: set[DeskId] = set()
        for result in self.results:
            if result.desk_id in seen:
                fail(FailureCode.INVALID_VALUE, "duplicate desk result")
            seen.add(result.desk_id)


class ResearchStageController:
    """Program-controlled sequence; LLMs fill only the allowed proposal slots."""

    STAGES = FIVE_STAGE_SEQUENCE

    def __init__(
        self,
        *,
        compiler: CompilePITService,
        evaluator: OfficialEvaluateService,
        explorer: BoundedReActExplorer | None = None,
    ) -> None:
        self.compiler = compiler
        self.evaluator = evaluator
        self.explorer = explorer or BoundedReActExplorer()

    def run_desk(
        self,
        *,
        desk: DeskLLMConfig,
        question: ResearchQuestionView,
        provider: ProposalProvider,
        tools: ReadOnlyToolRegistry,
    ) -> DeskRunResult:
        if tools.desk_id is not desk.desk_id:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "tool registry belongs to another desk")
        session = ProviderSession(desk_id=desk.desk_id, budget=desk.budget)
        trace: list[StageRecord] = []
        hypothesis: HypothesisFill | None = None
        factor: FactorDraftFill | None = None
        compiled: CompilePITArtifact | None = None
        evaluation: OfficialEvaluationView | None = None
        feedback: FeedbackExplanation | None = None
        current_stage = LLMDeskStage.HYPOTHESIS
        try:
            current_stage = LLMDeskStage.HYPOTHESIS
            hypothesis = provider.propose_hypothesis(
                desk=desk,
                question=question,
                tools=tools,
                session=session,
                explorer=self.explorer,
            )
            if hypothesis.state is not LLMDeskStatus.DRAFT:
                fail(FailureCode.INCOMPLETE_RESULT, "hypothesis did not produce DRAFT")
            trace.append(StageRecord(stage=current_stage, status=LLMDeskStatus.DRAFT, artifact_hash=hypothesis.artifact_hash))

            current_stage = LLMDeskStage.FACTOR_DRAFT
            factor = provider.draft_factor(
                desk=desk,
                question=question,
                hypothesis=hypothesis,
                tools=tools,
                session=session,
                explorer=self.explorer,
            )
            if factor.state is not LLMDeskStatus.DRAFT:
                fail(FailureCode.INCOMPLETE_RESULT, "factor slot did not produce DRAFT")
            trace.append(StageRecord(stage=current_stage, status=LLMDeskStatus.DRAFT, artifact_hash=factor.artifact_hash))

            current_stage = LLMDeskStage.DETERMINISTIC_COMPILE_PIT
            compiled = self.compiler.compile_and_validate(
                desk_id=desk.desk_id,
                question=question,
                factor=factor,
            )
            if not isinstance(compiled, CompilePITArtifact):
                fail(FailureCode.INCOMPLETE_RESULT, "compile/PIT service returned no typed artifact")
            trace.append(StageRecord(stage=current_stage, status=LLMDeskStatus.DRAFT, artifact_hash=compiled.artifact_hash))

            current_stage = LLMDeskStage.OFFICIAL_EVALUATE
            evaluation = self.evaluator.evaluate_official(
                desk_id=desk.desk_id,
                question=question,
                compiled=compiled,
            )
            if not isinstance(evaluation, OfficialEvaluationView):
                fail(FailureCode.INCOMPLETE_RESULT, "official evaluator returned no typed view")
            trace.append(StageRecord(stage=current_stage, status=LLMDeskStatus.DRAFT, artifact_hash=evaluation.artifact_hash))

            current_stage = LLMDeskStage.REDACTED_FEEDBACK
            feedback = provider.explain_feedback(
                desk=desk,
                summary=evaluation.redacted_summary,
                session=session,
            )
            if feedback.state is not LLMDeskStatus.DRAFT:
                fail(FailureCode.INCOMPLETE_RESULT, "feedback slot did not produce DRAFT")
            trace.append(StageRecord(stage=current_stage, status=LLMDeskStatus.DRAFT, artifact_hash=feedback.artifact_hash))

            return DeskRunResult(
                desk_id=desk.desk_id,
                status=LLMDeskStatus.DRAFT,
                stage_trace=tuple(trace),
                provider_trace=session.snapshot(),
                hypothesis=hypothesis,
                factor_draft=factor,
                compile_pit=compiled,
                official_evaluation=evaluation,
                feedback=feedback,
            )
        except ContractValidationError as exc:
            status = _status_for_failure(exc.code)
            trace.append(
                StageRecord(
                    stage=current_stage,
                    status=status,
                    artifact_hash=None,
                    failure_code=exc.code.value,
                )
            )
            return DeskRunResult(
                desk_id=desk.desk_id,
                status=status,
                stage_trace=tuple(trace),
                provider_trace=session.snapshot(),
                hypothesis=hypothesis,
                factor_draft=factor,
                compile_pit=compiled,
                official_evaluation=evaluation,
                feedback=feedback,
                failure_code=exc.code.value,
                message=str(exc),
            )

    def run_all(
        self,
        *,
        desks: tuple[DeskLLMConfig, ...],
        question: ResearchQuestionView,
        providers: Mapping[DeskId, ProposalProvider],
        tools: Mapping[DeskId, ReadOnlyToolRegistry],
    ) -> MultiDeskRunResult:
        results: list[DeskRunResult] = []
        for desk in desks:
            provider = providers.get(desk.desk_id)
            registry = tools.get(desk.desk_id)
            if provider is None or registry is None:
                session = ProviderSession(desk_id=desk.desk_id, budget=desk.budget)
                results.append(
                    DeskRunResult(
                        desk_id=desk.desk_id,
                        status=LLMDeskStatus.BLOCKED,
                        stage_trace=(
                            StageRecord(
                                stage=LLMDeskStage.HYPOTHESIS,
                                status=LLMDeskStatus.BLOCKED,
                                failure_code=FailureCode.ADAPTER_UNAVAILABLE.value,
                            ),
                        ),
                        provider_trace=session.snapshot(),
                        failure_code=FailureCode.ADAPTER_UNAVAILABLE.value,
                        message="provider or tool registry unavailable",
                    )
                )
                continue
            results.append(
                self.run_desk(
                    desk=desk,
                    question=question,
                    provider=provider,
                    tools=registry,
                )
            )
        return MultiDeskRunResult(results=tuple(results))


def _status_for_failure(code: FailureCode) -> LLMDeskStatus:
    if code in {
        FailureCode.PROVIDER_TIMEOUT,
        FailureCode.PROVIDER_BUDGET_EXCEEDED,
        FailureCode.UNAUTHORIZED_CAPABILITY,
        FailureCode.ADAPTER_UNAVAILABLE,
        FailureCode.ADAPTER_EXECUTION_FAILED,
    }:
        return LLMDeskStatus.BLOCKED
    return LLMDeskStatus.INCOMPLETE
