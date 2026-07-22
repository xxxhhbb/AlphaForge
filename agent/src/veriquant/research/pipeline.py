"""Program-controlled five-stage Research Desk branch state machine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..adapters import ProposalContext, QuantaAlphaProposalAdapter
from ..contracts import CanonicalContract, ContractValidationError, FailureCode, ResearchQuestion
from ..evidence import TrialOutcome
from .artifacts import BranchArtifactStore
from .authority import (
    CalculationResult,
    DeterministicOfficialEvidenceAuthority,
    EvaluationAttempt,
    OfficialEvaluation,
)
from .models import (
    FIVE_STAGE_SEQUENCE,
    BranchStage,
    DeskSpec,
    DeskToolRegistry,
    TaskStatus,
)
from .providers import (
    FactorDraftFill,
    FeedbackExplanation,
    HypothesisFill,
    IncompleteProviderResult,
    ProviderSession,
    ResearchProposalProvider,
)


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


@dataclass(frozen=True, kw_only=True)
class BranchRunResult(CanonicalContract):
    branch_id: str
    factor_spec_hash: str
    proposal: Any
    calculation: CalculationResult
    evaluation: OfficialEvaluation
    feedback: FeedbackExplanation
    stage_trace: tuple[BranchStage, ...]
    provider_tokens_used: int
    provider_iterations_used: int

    def __post_init__(self) -> None:
        if self.stage_trace != FIVE_STAGE_SEQUENCE:
            _fail(FailureCode.INVALID_STATE, "branch did not execute the explicit five-stage sequence")
        if self.factor_spec_hash != self.proposal.factor_draft.artifact_hash:
            _fail(FailureCode.INVALID_HASH, "branch factor hash does not bind proposal")
        if self.provider_tokens_used < 0 or self.provider_iterations_used < 3:
            _fail(FailureCode.INVALID_STATE, "provider did not fill all three allowed slots")


@dataclass(frozen=True, kw_only=True)
class BranchExecution:
    status: TaskStatus
    result: BranchRunResult | None
    failure_code: FailureCode | None
    message: str | None

    def __post_init__(self) -> None:
        if self.status is TaskStatus.COMPLETED:
            if self.result is None or self.failure_code is not None or self.message is not None:
                _fail(FailureCode.INVALID_VALUE, "completed branch requires only a real result")
        elif self.result is not None:
            _fail(FailureCode.INVALID_VALUE, "failed/incomplete branch cannot carry a success result")


class ResearchBranchStateMachine:
    """Fixed order is data, not metaclass-discovered public methods."""

    STAGES = FIVE_STAGE_SEQUENCE

    def __init__(
        self,
        *,
        run_id: str,
        desk: DeskSpec,
        question: ResearchQuestion,
        provider: ResearchProposalProvider,
        tools: DeskToolRegistry,
        store: BranchArtifactStore,
        authority: DeterministicOfficialEvidenceAuthority,
    ) -> None:
        self.run_id = run_id
        self.desk = desk
        self.question = question
        self.provider = provider
        self.tools = tools
        self.store = store
        self.authority = authority
        self.session = ProviderSession(desk)

    def _construct_proposal(
        self, hypothesis: HypothesisFill, factor: FactorDraftFill
    ):
        dataset = self.authority.dataset.registry.get(self.authority.dataset.dataset_id)
        adapter = QuantaAlphaProposalAdapter(max_candidates=1)
        return adapter.adapt(
            {
                "hypothesis": {
                    "statement": hypothesis.statement,
                    "economic_mechanism": hypothesis.economic_mechanism,
                    "expected_sign": hypothesis.expected_sign,
                    "falsification_tests": list(hypothesis.falsification_tests),
                },
                "factor_task": {
                    "name": factor.name,
                    "description": factor.description,
                    "expression": factor.expression,
                    "variables": list(factor.variables),
                    "lookback_bars": factor.lookback_bars,
                    "neutralization": list(factor.neutralization),
                },
            },
            context=ProposalContext(
                direction_hash=self.question.artifact_hash,
                universe=self.question.universe,
                horizon=self.question.horizon,
                decision_time=self.question.decision_time,
                availability_assumptions=self.question.availability_assumptions,
                field_roles=dataset.field_roles,
            ),
        )

    def run(self) -> BranchExecution:
        hypothesis: HypothesisFill | None = None
        factor: FactorDraftFill | None = None
        proposal = None
        attempt: EvaluationAttempt | None = None
        calculation: CalculationResult | None = None
        evaluation: OfficialEvaluation | None = None
        feedback: FeedbackExplanation | None = None
        trace: list[BranchStage] = []
        current_stage = BranchStage.HYPOTHESIS_PROPOSE
        attempt_completed = False
        try:
            for stage in self.STAGES:
                current_stage = stage
                self.store.append_event(
                    stage=stage.value,
                    status="STARTED",
                    event_time=self.question.decision_time,
                )
                if stage is BranchStage.HYPOTHESIS_PROPOSE:
                    hypothesis = self.provider.propose_hypothesis(
                        desk=self.desk,
                        question=self.question,
                        tools=self.tools,
                        session=self.session,
                    )
                    if not isinstance(hypothesis, HypothesisFill):
                        raise IncompleteProviderResult("hypothesis slot produced no typed content")
                    self.store.write_artifact("proposal.json", hypothesis)
                elif stage is BranchStage.FACTOR_CONSTRUCT:
                    if hypothesis is None:
                        _fail(FailureCode.INVALID_STATE, "factor construction lacks hypothesis")
                    factor = self.provider.construct_factor(
                        desk=self.desk,
                        question=self.question,
                        hypothesis=hypothesis,
                        tools=self.tools,
                        session=self.session,
                    )
                    if not isinstance(factor, FactorDraftFill):
                        raise IncompleteProviderResult("factor slot produced no typed content")
                    proposal = self._construct_proposal(hypothesis, factor)
                    self.store.write_artifact("factor_spec.json", proposal.factor_draft)
                elif stage is BranchStage.FACTOR_COMPILE_CALCULATE:
                    if proposal is None:
                        _fail(FailureCode.INVALID_STATE, "calculation lacks a factor proposal")
                    attempt = self.authority.start_attempt(
                        run_id=self.run_id,
                        desk=self.desk,
                        proposal=proposal,
                    )
                    calculation = self.authority.calculate(
                        run_id=self.run_id,
                        desk=self.desk,
                        proposal=proposal,
                        decision_time=self.question.decision_time,
                    )
                elif stage is BranchStage.FACTOR_EVALUATE:
                    if proposal is None or attempt is None or calculation is None:
                        _fail(FailureCode.INVALID_STATE, "evaluation lacks compiled PIT artifacts")
                    evaluation = self.authority.evaluate(
                        run_id=self.run_id,
                        desk=self.desk,
                        proposal=proposal,
                        attempt=attempt,
                        calculation=calculation,
                    )
                    attempt_completed = True
                    self.store.write_artifact("experiment_manifest.json", evaluation.manifest)
                    self.store.write_artifact("evidence_pack.json", evaluation.evidence)
                elif stage is BranchStage.FEEDBACK:
                    if evaluation is None:
                        _fail(FailureCode.INVALID_STATE, "feedback lacks official redacted evidence")
                    feedback = self.provider.explain_feedback(
                        desk=self.desk,
                        summary=evaluation.feedback_summary,
                        session=self.session,
                    )
                    if not isinstance(feedback, FeedbackExplanation):
                        raise IncompleteProviderResult("feedback slot produced no typed content")
                else:  # pragma: no cover - fixed exhaustive stage tuple
                    _fail(FailureCode.INVALID_STATE, "unknown branch stage")
                trace.append(stage)
                self.store.append_event(
                    stage=stage.value,
                    status="COMPLETED",
                    event_time=self.question.decision_time,
                )
            assert proposal is not None and calculation is not None and evaluation is not None and feedback is not None
            return BranchExecution(
                status=TaskStatus.COMPLETED,
                result=BranchRunResult(
                    branch_id=self.desk.desk_id.value,
                    factor_spec_hash=proposal.factor_draft.artifact_hash,
                    proposal=proposal,
                    calculation=calculation,
                    evaluation=evaluation,
                    feedback=feedback,
                    stage_trace=tuple(trace),
                    provider_tokens_used=self.session.tokens_used,
                    provider_iterations_used=self.session.iterations_used,
                ),
                failure_code=None,
                message=None,
            )
        except IncompleteProviderResult as exc:
            if attempt is not None and not attempt_completed:
                outcome = (
                    TrialOutcome.PIT_REJECTION
                    if current_stage is BranchStage.FACTOR_COMPILE_CALCULATE
                    else TrialOutcome.EVALUATION_REJECTION
                )
                self.authority.fail_started_attempt(
                    run_id=self.run_id,
                    attempt=attempt,
                    outcome=outcome,
                    failure_code=FailureCode.INCOMPLETE_RESULT,
                )
            self.store.mark_unproduced(
                failure_code=FailureCode.INCOMPLETE_RESULT.value,
                message=str(exc),
            )
            self.store.append_event(
                stage=current_stage.value,
                status="INCOMPLETE",
                event_time=self.question.decision_time,
            )
            return BranchExecution(
                status=TaskStatus.INCOMPLETE,
                result=None,
                failure_code=FailureCode.INCOMPLETE_RESULT,
                message=str(exc),
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, ContractValidationError) else FailureCode.ADAPTER_EXECUTION_FAILED
            if attempt is not None and not attempt_completed:
                outcome = (
                    TrialOutcome.PIT_REJECTION
                    if current_stage is BranchStage.FACTOR_COMPILE_CALCULATE
                    else TrialOutcome.EVALUATION_REJECTION
                )
                self.authority.fail_started_attempt(
                    run_id=self.run_id,
                    attempt=attempt,
                    outcome=outcome,
                    failure_code=code,
                )
            message = str(exc) or type(exc).__name__
            self.store.mark_unproduced(failure_code=code.value, message=message)
            self.store.append_event(
                stage=current_stage.value,
                status="FAILED",
                event_time=self.question.decision_time,
            )
            return BranchExecution(
                status=TaskStatus.FAILED,
                result=None,
                failure_code=code,
                message=message,
            )
