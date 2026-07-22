"""One-Desk Real-LLM vertical slice for VeriQuant.

This package is a thin composition layer only.  It wires a real provider
protocol, strict prompt rendering, desk-scoped read-only research tools, the
existing fixed five-stage controller, the proposal adapter, restricted
FactorDAG/PIT calculation, TrialLedger reservation, deterministic Evidence
Service, and the mechanical Evidence Court.

It does not create a new financial truth service, does not expose sealed data,
and does not grant an LLM any authority over metrics, promotion, portfolio,
risk, orders, execution, or ledger state.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..adapters.proposal import DraftProposal, ProposalContext, QuantaAlphaProposalAdapter
from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import require_hash, require_identifier
from ..data import FieldRole, canonical_content_hash
from ..evidence import EvidenceCourt, TrialOutcome
from ..llm_desks.api import (
    BoundedReActExplorer,
    CompilePITArtifact,
    DeskId as LLMDeskId,
    DeskLLMConfig,
    DeskRunResult,
    FIVE_STAGE_SEQUENCE as LLM_FIVE_STAGE_SEQUENCE,
    FactorDraftFill as LLMFactorDraftFill,
    FeedbackExplanation as LLMFeedbackExplanation,
    HypothesisFill as LLMHypothesisFill,
    LLMDeskStage,
    LLMDeskStatus,
    OfficialEvaluationView,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    OpenAICompatibleResponse,
    ProviderBudget,
    ProviderSession,
    ReactLimits,
    ReadOnlyToolRegistry,
    RedactedEvidenceSummary,
    ResearchQuestionView,
    ResearchStageController,
)
from ..llm_desks.safety import assert_no_forbidden_content, opaque_hash
from ..real_llm_promptpack.models import RenderedPromptBundle, StagePromptRenderer
from ..real_llm_research_tools.tools import (
    BoundedLLMReactPlanner,
    FrozenResearchToolFactory,
    ResearchVisibleContext,
)
from ..research.authority import (
    CalculationResult,
    DeterministicOfficialEvidenceAuthority,
    EvaluationAttempt,
    OfficialEvaluation,
)
from ..research.models import DeskSpec as ResearchDeskSpec
from ..research.models import build_default_desks


REAL_LLM_VERTICAL_SLICE_SCHEMA_VERSION = "veriquant.real-llm-vertical-slice/v1"


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


def _exact_payload(
    payload: Mapping[str, Any],
    fields: frozenset[str],
    *,
    stage: LLMDeskStage,
) -> None:
    if not isinstance(payload, Mapping) or set(payload) != fields:
        _fail(
            FailureCode.PROVIDER_SCHEMA_REJECTED,
            "provider returned an inexact vertical-slice schema",
            stage=stage.value,
            expected=tuple(sorted(fields)),
        )
    if payload.get("state") != LLMDeskStatus.DRAFT.value:
        _fail(FailureCode.INVALID_STATE, "real LLM desk may output DRAFT only", stage=stage.value)


def _tuple_of_text(value: Any, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        _fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field} must be an array of non-empty strings")
    result = tuple(item.strip() for item in value)
    if not allow_empty and not result:
        _fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field} cannot be empty")
    return result


def _trial_outcome_for(code: FailureCode, *, stage: str) -> TrialOutcome:
    if code in {
        FailureCode.INVALID_FACTOR_DAG,
        FailureCode.UNKNOWN_FIELD,
        FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
        FailureCode.PROVIDER_SCHEMA_REJECTED,
    }:
        return TrialOutcome.PARSE_REJECTION
    if stage == "compile_pit" or code in {FailureCode.INVALID_TIME_ORDER, FailureCode.UNAUTHORIZED_CAPABILITY}:
        return TrialOutcome.PIT_REJECTION
    if code is FailureCode.PROVIDER_TIMEOUT:
        return TrialOutcome.TIMEOUT
    return TrialOutcome.EVALUATION_REJECTION


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(rendered + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _safe_visible_metrics(summary: RedactedEvidenceSummary) -> Mapping[str, float]:
    metrics = {key: float(value) for key, value in sorted(summary.visible_metrics.items())}
    for key in metrics:
        lowered = key.lower()
        if any(token in lowered for token in ("sealed", "holdout", "promotion", "official")):
            _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "redacted feedback contains privileged metric keys")
    return MappingProxyType(metrics)


@dataclass(frozen=True, kw_only=True)
class RedactedCandidateReceipt(CanonicalContract):
    """Machine-readable public receipt for the program-submitted candidate."""

    SCHEMA_VERSION: ClassVar[str] = REAL_LLM_VERTICAL_SLICE_SCHEMA_VERSION

    run_id: str
    desk_id: str
    status: str
    identity_hash: str | None
    trial_id: str | None
    factor_draft_hash: str | None
    factor_dag_hash: str | None
    pit_snapshot_hash: str | None
    evidence_hash: str | None
    decision_hash: str | None
    court_outcome: str | None
    failure_code: str | None = None
    budget_units_consumed: int = 0
    sealed_visible: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_identifier(self.desk_id, "desk_id")
        if self.status not in {"SUBMITTED", "PIT_VALIDATED", "EVALUATED", "REJECTED", "REVISE", "BLOCKED"}:
            _fail(FailureCode.INVALID_STATE, "unknown candidate receipt status")
        for name in (
            "identity_hash",
            "factor_draft_hash",
            "factor_dag_hash",
            "pit_snapshot_hash",
            "evidence_hash",
            "decision_hash",
        ):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        if self.trial_id is not None:
            require_identifier(self.trial_id, "trial_id")
        if self.court_outcome is not None:
            require_identifier(self.court_outcome, "court_outcome")
            if "PROMOTE" in self.court_outcome:
                _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "sealed promotion is outside the RL-4 slice")
        if self.failure_code is not None:
            require_identifier(self.failure_code, "failure_code")
        if isinstance(self.budget_units_consumed, bool) or self.budget_units_consumed < 0:
            _fail(FailureCode.INVALID_VALUE, "budget_units_consumed must be a non-negative integer")
        if self.sealed_visible is not False:
            _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "candidate receipt must never expose sealed information")


@dataclass(frozen=True, kw_only=True)
class VerticalSliceRunManifest(CanonicalContract):
    """Resume-safe manifest for one RL-4 run."""

    SCHEMA_VERSION: ClassVar[str] = REAL_LLM_VERTICAL_SLICE_SCHEMA_VERSION

    run_id: str
    desk_id: str
    status: str
    stage_sequence: tuple[str, ...]
    controller_result_hash: str
    provider_trace_hash: str
    candidate_receipt_hash: str | None
    hypothesis_hash: str | None
    factor_draft_hash: str | None
    compile_pit_hash: str | None
    evaluation_view_hash: str | None
    feedback_hash: str | None
    prompt_hashes: tuple[str, ...]
    tool_context_hashes: tuple[str, ...]
    artifact_hashes: Mapping[str, str]
    failure_code: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_identifier(self.desk_id, "desk_id")
        if self.status not in {"COMPLETED", "INCOMPLETE", "BLOCKED"}:
            _fail(FailureCode.INVALID_STATE, "unknown vertical slice status")
        if self.stage_sequence:
            expected = tuple(stage.value for stage in LLM_FIVE_STAGE_SEQUENCE)
            if self.stage_sequence != expected and self.status == "COMPLETED":
                _fail(FailureCode.INVALID_STATE, "completed run must follow the fixed five-stage sequence")
        for name in (
            "controller_result_hash",
            "provider_trace_hash",
            "candidate_receipt_hash",
            "hypothesis_hash",
            "factor_draft_hash",
            "compile_pit_hash",
            "evaluation_view_hash",
            "feedback_hash",
        ):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        for value in (*self.prompt_hashes, *self.tool_context_hashes):
            require_hash(value, "run_hash")
        hashes = dict(self.artifact_hashes)
        for key, value in hashes.items():
            require_identifier(key, "artifact_hash_key")
            require_hash(value, key)
        object.__setattr__(self, "artifact_hashes", MappingProxyType(dict(sorted(hashes.items()))))
        if self.failure_code is not None:
            require_identifier(self.failure_code, "failure_code")


@dataclass(frozen=True)
class _CandidatePair:
    hypothesis: LLMHypothesisFill
    factor: LLMFactorDraftFill


@dataclass(frozen=True)
class _CompiledSubmission:
    proposal: DraftProposal
    attempt: EvaluationAttempt
    calculation: CalculationResult
    receipt: RedactedCandidateReceipt


class CandidateSubmissionService:
    """Program-owned candidate boundary for compile/PIT/evidence.

    The LLM provider can only produce a DRAFT factor.  This service is injected
    into the controller and is the only object in the vertical slice that can
    adapt the draft into a restricted FactorDAG, reserve trial budget, run PIT
    calculation, call the official Evidence Service, and ask the mechanical
    Evidence Court for a visible train/validation-only verdict.
    """

    schema_version = REAL_LLM_VERTICAL_SLICE_SCHEMA_VERSION

    def __init__(
        self,
        *,
        run_id: str,
        run_dir: Path,
        desk: ResearchDeskSpec,
        question: ResearchQuestionView,
        authority: DeterministicOfficialEvidenceAuthority,
        adapter: QuantaAlphaProposalAdapter | None = None,
        court: EvidenceCourt | None = None,
        direction_hash: str | None = None,
    ) -> None:
        require_identifier(run_id, "run_id")
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self.desk = desk
        self.question = question
        self.authority = authority
        self.adapter = adapter or QuantaAlphaProposalAdapter(max_candidates=1)
        self.court = court or EvidenceCourt()
        self.direction_hash = direction_hash or canonical_content_hash(
            {"run_id": run_id, "desk": desk.desk_id.value, "question": question.to_dict()}
        )
        self._registered: dict[str, _CandidatePair] = {}
        self._compiled: dict[str, _CompiledSubmission] = {}
        self._latest_receipt: RedactedCandidateReceipt | None = None
        self._latest_official: OfficialEvaluation | None = None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.authority.prepare_run(run_id=run_id, run_dir=self.run_dir, desks=(desk,))

    @property
    def latest_receipt(self) -> RedactedCandidateReceipt | None:
        return self._latest_receipt

    @property
    def latest_official_evaluation(self) -> OfficialEvaluation | None:
        return self._latest_official

    def register_candidate(self, *, hypothesis: LLMHypothesisFill, factor: LLMFactorDraftFill) -> None:
        if hypothesis.state is not LLMDeskStatus.DRAFT or factor.state is not LLMDeskStatus.DRAFT:
            _fail(FailureCode.INVALID_STATE, "LLM candidate registration accepts DRAFT artifacts only")
        if factor.source_hypothesis_hash != hypothesis.artifact_hash:
            _fail(FailureCode.INVALID_HASH, "factor draft is not bound to the registered hypothesis")
        self._registered[factor.artifact_hash] = _CandidatePair(hypothesis=hypothesis, factor=factor)

    def compile_and_validate(
        self,
        *,
        desk_id: LLMDeskId,
        question: ResearchQuestionView,
        factor: LLMFactorDraftFill,
    ) -> CompilePITArtifact:
        self._require_desk_and_question(desk_id=desk_id, question=question)
        pair = self._registered.get(factor.artifact_hash)
        if pair is None:
            _fail(FailureCode.INVALID_STATE, "factor draft was not submitted through the program boundary")
        proposal = self._adapt(pair)
        attempt = self.authority.start_attempt(run_id=self.run_id, desk=self.desk, proposal=proposal)
        receipt = RedactedCandidateReceipt(
            run_id=self.run_id,
            desk_id=self.desk.desk_id.value,
            status="SUBMITTED",
            identity_hash=attempt.identity.identity_hash,
            trial_id=attempt.started.trial_id,
            factor_draft_hash=proposal.factor_draft.artifact_hash,
            factor_dag_hash=proposal.factor_dag.artifact_hash,
            pit_snapshot_hash=None,
            evidence_hash=None,
            decision_hash=None,
            court_outcome=None,
            budget_units_consumed=1,
        )
        self._latest_receipt = receipt
        try:
            calculation = self.authority.calculate(
                run_id=self.run_id,
                desk=self.desk,
                proposal=proposal,
                decision_time=question.decision_time,
            )
        except ContractValidationError as exc:
            self.authority.fail_started_attempt(
                run_id=self.run_id,
                attempt=attempt,
                outcome=_trial_outcome_for(exc.code, stage="compile_pit"),
                failure_code=exc.code,
            )
            self._latest_receipt = RedactedCandidateReceipt(
                run_id=self.run_id,
                desk_id=self.desk.desk_id.value,
                status="BLOCKED",
                identity_hash=attempt.identity.identity_hash,
                trial_id=attempt.started.trial_id,
                factor_draft_hash=proposal.factor_draft.artifact_hash,
                factor_dag_hash=proposal.factor_dag.artifact_hash,
                pit_snapshot_hash=None,
                evidence_hash=None,
                decision_hash=None,
                court_outcome=None,
                failure_code=exc.code.value,
                budget_units_consumed=1,
            )
            raise
        compiled = CompilePITArtifact(
            compiled_factor_hash=proposal.factor_dag.artifact_hash,
            pit_snapshot_hash=calculation.snapshot.artifact_hash,
        )
        receipt = RedactedCandidateReceipt(
            run_id=self.run_id,
            desk_id=self.desk.desk_id.value,
            status="PIT_VALIDATED",
            identity_hash=attempt.identity.identity_hash,
            trial_id=attempt.started.trial_id,
            factor_draft_hash=proposal.factor_draft.artifact_hash,
            factor_dag_hash=proposal.factor_dag.artifact_hash,
            pit_snapshot_hash=calculation.snapshot.artifact_hash,
            evidence_hash=None,
            decision_hash=None,
            court_outcome=None,
            budget_units_consumed=1,
        )
        self._latest_receipt = receipt
        self._compiled[compiled.artifact_hash] = _CompiledSubmission(
            proposal=proposal,
            attempt=attempt,
            calculation=calculation,
            receipt=receipt,
        )
        return compiled

    def evaluate_official(
        self,
        *,
        desk_id: LLMDeskId,
        question: ResearchQuestionView,
        compiled: CompilePITArtifact,
    ) -> OfficialEvaluationView:
        self._require_desk_and_question(desk_id=desk_id, question=question)
        submission = self._compiled.get(compiled.artifact_hash)
        if submission is None:
            _fail(FailureCode.INVALID_STATE, "compiled candidate is unknown to submission service")
        try:
            official = self.authority.evaluate(
                run_id=self.run_id,
                desk=self.desk,
                proposal=submission.proposal,
                attempt=submission.attempt,
                calculation=submission.calculation,
            )
        except ContractValidationError as exc:
            self.authority.fail_started_attempt(
                run_id=self.run_id,
                attempt=submission.attempt,
                outcome=_trial_outcome_for(exc.code, stage="evaluate"),
                failure_code=exc.code,
            )
            self._latest_receipt = RedactedCandidateReceipt(
                run_id=self.run_id,
                desk_id=self.desk.desk_id.value,
                status="BLOCKED",
                identity_hash=submission.attempt.identity.identity_hash,
                trial_id=submission.attempt.started.trial_id,
                factor_draft_hash=submission.proposal.factor_draft.artifact_hash,
                factor_dag_hash=submission.proposal.factor_dag.artifact_hash,
                pit_snapshot_hash=submission.calculation.snapshot.artifact_hash,
                evidence_hash=None,
                decision_hash=None,
                court_outcome=None,
                failure_code=exc.code.value,
                budget_units_consumed=1,
            )
            raise
        decision = self.court.decide(
            evidence=official.evidence,
            identity_hash=official.manifest.identity_hash,
            proposer_id=f"desk:{self.desk.desk_id.value}",
            approver_id="mechanical-verdict-service-rl4",
            decided_at=self.authority.dataset.evaluation_time,
            pit_valid=True,
            sealed_passed=None,
        )
        status = "EVALUATED"
        if decision.outcome.value == "REJECT":
            status = "REJECTED"
        elif decision.outcome.value == "REVISE":
            status = "REVISE"
        receipt = RedactedCandidateReceipt(
            run_id=self.run_id,
            desk_id=self.desk.desk_id.value,
            status=status,
            identity_hash=official.manifest.identity_hash,
            trial_id=submission.attempt.started.trial_id,
            factor_draft_hash=submission.proposal.factor_draft.artifact_hash,
            factor_dag_hash=submission.proposal.factor_dag.artifact_hash,
            pit_snapshot_hash=submission.calculation.snapshot.artifact_hash,
            evidence_hash=official.evidence.artifact_hash,
            decision_hash=decision.artifact_hash,
            court_outcome=decision.outcome.value,
            budget_units_consumed=1,
        )
        metrics = official.feedback_summary.visible_metrics
        view = OfficialEvaluationView(
            evidence_hash=official.evidence.artifact_hash,
            redacted_summary=RedactedEvidenceSummary(
                evidence_hash=official.evidence.artifact_hash,
                visible_metrics={key: float(value) for key, value in sorted(metrics.items())},
                allowed_notes=(f"visible_outcome:{decision.outcome.value}",),
            ),
        )
        self._latest_official = official
        self._latest_receipt = receipt
        return view

    def _adapt(self, pair: _CandidatePair) -> DraftProposal:
        payload = {
            "hypothesis": {
                "statement": pair.hypothesis.statement,
                "economic_mechanism": pair.hypothesis.economic_mechanism,
                "expected_sign": pair.hypothesis.expected_sign.value,
                "falsification_tests": list(pair.hypothesis.falsification_tests),
            },
            "factor_task": {
                "name": pair.factor.name,
                "description": pair.factor.description,
                "expression": pair.factor.expression,
                "variables": list(pair.factor.variables),
                "lookback_bars": pair.factor.lookback_bars,
                "neutralization": list(pair.factor.neutralization),
            },
        }
        context = ProposalContext(
            direction_hash=self.direction_hash,
            universe=self.question.universe,
            horizon=self.question.horizon,
            decision_time=self.question.decision_time,
            availability_assumptions=self.question.availability_assumptions,
            field_roles=self._field_roles(),
        )
        return self.adapter.adapt(payload, context=context)

    def _field_roles(self) -> Mapping[str, FieldRole]:
        dataset = self.authority.dataset.registry.get(self.authority.dataset.dataset_id)
        return dataset.field_roles

    def _require_desk_and_question(self, *, desk_id: LLMDeskId, question: ResearchQuestionView) -> None:
        if desk_id.value != self.desk.desk_id.value:
            _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "submission service received another desk")
        if question.artifact_hash != self.question.artifact_hash:
            _fail(FailureCode.INVALID_HASH, "submission service received another question")


class RealLLMOneDeskProposalProvider:
    """Provider adapter that uses the real LLM protocol with testable fake transport."""

    def __init__(
        self,
        *,
        client: OpenAICompatibleClient,
        config: OpenAICompatibleConfig,
        renderer: StagePromptRenderer | None = None,
        react_planner: BoundedLLMReactPlanner | None = None,
        candidate_service: CandidateSubmissionService | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.renderer = renderer or StagePromptRenderer()
        self.react_planner = react_planner or BoundedLLMReactPlanner(
            limits=ReactLimits(max_iterations=2, max_tool_calls=1, max_tokens=512)
        )
        self.candidate_service = candidate_service
        self.prompt_hashes: list[str] = []
        self.tool_context_hashes: list[str] = []

    def propose_hypothesis(
        self,
        *,
        desk: DeskLLMConfig,
        question: ResearchQuestionView,
        tools: ReadOnlyToolRegistry,
        session: ProviderSession,
        explorer: BoundedReActExplorer | None = None,
    ) -> LLMHypothesisFill:
        stage = LLMDeskStage.HYPOTHESIS
        react = self.react_planner.run(
            stage=stage,
            prompt_material={"desk": desk.desk_id.value, "question_hash": question.artifact_hash},
            tools=tools,
            session=session,
        )
        rendered = self._render(
            desk=desk,
            question=question,
            stage=stage,
            tool_summaries=tuple(item.to_dict() for item in react.tool_summaries),
        )
        payload = self._call(
            stage=stage,
            rendered=rendered,
            session=session,
            expected_fields=frozenset(
                {"state", "statement", "economic_mechanism", "expected_sign", "falsification_tests"}
            ),
        )
        return LLMHypothesisFill(
            state=LLMDeskStatus.DRAFT,
            desk_id=desk.desk_id,
            statement=payload["statement"],
            economic_mechanism=payload["economic_mechanism"],
            expected_sign=payload["expected_sign"],
            universe=question.universe,
            horizon=question.horizon,
            decision_time=question.decision_time,
            availability_assumptions=question.availability_assumptions,
            falsification_tests=_tuple_of_text(payload["falsification_tests"], "falsification_tests"),
            react_context_hashes=(react.context_hash,),
        )

    def draft_factor(
        self,
        *,
        desk: DeskLLMConfig,
        question: ResearchQuestionView,
        hypothesis: LLMHypothesisFill,
        tools: ReadOnlyToolRegistry,
        session: ProviderSession,
        explorer: BoundedReActExplorer | None = None,
    ) -> LLMFactorDraftFill:
        stage = LLMDeskStage.FACTOR_DRAFT
        react = self.react_planner.run(
            stage=stage,
            prompt_material={"desk": desk.desk_id.value, "hypothesis_hash": hypothesis.artifact_hash},
            tools=tools,
            session=session,
        )
        rendered = self._render(
            desk=desk,
            question=question,
            stage=stage,
            tool_summaries=tuple(item.to_dict() for item in react.tool_summaries),
            lineage_summary=f"hypothesis_hash:{hypothesis.artifact_hash}",
        )
        payload = self._call(
            stage=stage,
            rendered=rendered,
            session=session,
            expected_fields=frozenset(
                {"state", "name", "description", "expression", "variables", "lookback_bars", "neutralization"}
            ),
        )
        factor = LLMFactorDraftFill(
            state=LLMDeskStatus.DRAFT,
            desk_id=desk.desk_id,
            name=payload["name"],
            description=payload["description"],
            expression=payload["expression"],
            variables=_tuple_of_text(payload["variables"], "variables"),
            lookback_bars=payload["lookback_bars"],
            neutralization=_tuple_of_text(payload["neutralization"], "neutralization", allow_empty=True),
            source_hypothesis_hash=hypothesis.artifact_hash,
            react_context_hashes=(react.context_hash,),
        )
        if self.candidate_service is not None:
            self.candidate_service.register_candidate(hypothesis=hypothesis, factor=factor)
        return factor

    def explain_feedback(
        self,
        *,
        desk: DeskLLMConfig,
        summary: RedactedEvidenceSummary,
        session: ProviderSession,
    ) -> LLMFeedbackExplanation:
        stage = LLMDeskStage.REDACTED_FEEDBACK
        rendered = self._render_feedback(desk=desk, summary=summary)
        payload = self._call(
            stage=stage,
            rendered=rendered,
            session=session,
            expected_fields=frozenset({"state", "explanation"}),
        )
        return LLMFeedbackExplanation(
            state=LLMDeskStatus.DRAFT,
            desk_id=desk.desk_id,
            summary_hash=summary.artifact_hash,
            explanation=payload["explanation"],
        )

    def _call(
        self,
        *,
        stage: LLMDeskStage,
        rendered: RenderedPromptBundle,
        session: ProviderSession,
        expected_fields: frozenset[str],
    ) -> Mapping[str, Any]:
        session.reserve_call(stage)
        trace_material = {
            "prompt_hash": rendered.prompt_hash,
            "material_hash": rendered.material_hash,
            "schema_hash": rendered.schema_hash,
            "stage": stage.value,
        }
        try:
            response = self.client.complete_json(
                messages=rendered.messages,
                model=self.config.model,
                timeout_seconds=float(session.budget.timeout_seconds),
                max_tokens=session.remaining_tokens,
                temperature=float(self.config.temperature),
                top_p=float(self.config.top_p),
                response_format=self.config.response_format,
                extra=dict(self.config.non_sensitive_params or {}),
            )
            if not isinstance(response, OpenAICompatibleResponse):
                _fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider returned an invalid response envelope")
            assert_no_forbidden_content(response.payload, where=f"vertical_provider.{stage.value}")
            _exact_payload(response.payload, expected_fields, stage=stage)
        except ContractValidationError as exc:
            session.record_failure(
                stage=stage,
                prompt_material=trace_material,
                output_hash=None,
                failure_code=exc.code,
                parameters=self._trace_params(),
            )
            raise
        except Exception as exc:
            session.record_failure(
                stage=stage,
                prompt_material=trace_material,
                output_hash=opaque_hash(type(exc).__name__),
                failure_code=FailureCode.ADAPTER_EXECUTION_FAILED,
                parameters=self._trace_params(),
            )
            raise ContractValidationError(
                FailureCode.ADAPTER_EXECUTION_FAILED,
                "real LLM client failed through the vertical-slice provider boundary",
            ) from exc
        session.record_success(
            stage=stage,
            prompt_material=trace_material,
            output=response.payload,
            tokens_in=response.input_tokens,
            tokens_out=response.output_tokens,
            cost=float(response.cost),
            parameters=self._trace_params(),
        )
        self.prompt_hashes.append(rendered.prompt_hash)
        return MappingProxyType(dict(response.payload))

    def _render(
        self,
        *,
        desk: DeskLLMConfig,
        question: ResearchQuestionView,
        stage: LLMDeskStage,
        tool_summaries: tuple[Mapping[str, Any], ...],
        lineage_summary: str = "new one-desk candidate",
    ) -> RenderedPromptBundle:
        material = {
            "question_text": question.text,
            "horizon": question.horizon,
            "decision_time": question.decision_time.isoformat(),
            "universe_summary": f"{len(question.universe)} symbols in frozen universe",
            "availability_assumptions": question.availability_assumptions,
            "approved_fields": self._approved_fields_for(desk.desk_id),
            "tool_summaries": tool_summaries,
            "citations": (),
            "lineage_summary": lineage_summary,
            "validation_summary": "visible validation unavailable before calculation",
        }
        bundle = self.renderer.render(desk_id=desk.desk_id.value, stage=stage, material=material)
        self.tool_context_hashes.extend(
            item["result_hash"] for item in tool_summaries if isinstance(item.get("result_hash"), str)
        )
        return bundle

    def _render_feedback(self, *, desk: DeskLLMConfig, summary: RedactedEvidenceSummary) -> RenderedPromptBundle:
        metrics = _safe_visible_metrics(summary)
        material = {
            "question_text": "Review the visible validation summary and explain the DRAFT revision rationale.",
            "horizon": "visible_validation",
            "decision_time": "1970-01-01T00:00:00+00:00",
            "universe_summary": "same frozen universe as submitted candidate",
            "availability_assumptions": ("visible validation numbers are redacted to allowlisted fields",),
            "approved_fields": tuple(sorted(metrics)),
            "tool_summaries": (),
            "citations": (),
            "validation_summary": {
                "summary_hash": summary.artifact_hash,
                "metrics": dict(metrics),
                "notes": summary.allowed_notes,
            },
        }
        return self.renderer.render(
            desk_id=desk.desk_id.value,
            stage=LLMDeskStage.REDACTED_FEEDBACK,
            material=material,
        )

    @staticmethod
    def _approved_fields_for(desk_id: LLMDeskId) -> tuple[str, ...]:
        fields_by_desk: dict[LLMDeskId, tuple[str, ...]] = {
            LLMDeskId.FUNDAMENTAL: ("book_value", "market_cap", "close"),
            LLMDeskId.EXPECTATIONS_EVENTS: ("close", "volume"),
            LLMDeskId.MARKET_STRUCTURE: ("close", "volume", "market_cap"),
            LLMDeskId.ALTERNATIVE_DATA: ("close", "volume"),
            LLMDeskId.MACRO_REGIME: ("close", "macro_rate", "market_cap"),
            LLMDeskId.META_ALPHA: ("close", "volume", "market_cap"),
        }
        return fields_by_desk[desk_id]

    def _trace_params(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "provider": "openai_compatible_protocol",
                "model": self.config.model,
                "temperature": float(self.config.temperature),
                "top_p": float(self.config.top_p),
                "response_format": self.config.response_format,
                "extra_keys": tuple(sorted((self.config.non_sensitive_params or {}).keys())),
            }
        )


class RealLLMVerticalSlice:
    """Run one desk end-to-end through the real-provider protocol boundary."""

    schema_version = REAL_LLM_VERTICAL_SLICE_SCHEMA_VERSION

    def __init__(
        self,
        *,
        client: OpenAICompatibleClient,
        config: OpenAICompatibleConfig,
        authority: DeterministicOfficialEvidenceAuthority | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.authority = authority or DeterministicOfficialEvidenceAuthority()

    def run(
        self,
        *,
        run_id: str,
        run_dir: Path,
        desk_id: LLMDeskId | str = LLMDeskId.FUNDAMENTAL,
        question: ResearchQuestionView | None = None,
        resume: bool = True,
    ) -> VerticalSliceRunManifest:
        run_dir = Path(run_dir)
        llm_desk_id = LLMDeskId(desk_id)
        manifest_path = run_dir / "vertical_manifest.json"
        if resume and manifest_path.exists():
            existing = VerticalSliceRunManifest.from_payload(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
            if existing.run_id != run_id or existing.desk_id != llm_desk_id.value:
                _fail(FailureCode.INVALID_STATE, "resume manifest does not match requested run identity")
            return existing
        question = question or self.default_question(run_id=run_id)
        research_desk = _research_desk_for(llm_desk_id)
        visible_context = build_visible_context(authority=self.authority, desk_id=llm_desk_id)
        tool_factory = FrozenResearchToolFactory(contexts={llm_desk_id: visible_context})
        tools = tool_factory.registry_for_desk(llm_desk_id)
        desk_config = DeskLLMConfig(
            desk_id=llm_desk_id,
            role=f"{llm_desk_id.value} real LLM DRAFT proposer",
            context=("Use frozen visible summaries.", "Return only strict DRAFT JSON."),
            model=self.config.model,
            budget=ProviderBudget(model=self.config.model, max_tokens=2048, max_calls=6, timeout_seconds=5.0),
            tool_names=tools.names,
            artifact_namespace=llm_desk_id.value,
        )
        candidate_service = CandidateSubmissionService(
            run_id=run_id,
            run_dir=run_dir,
            desk=research_desk,
            question=question,
            authority=self.authority,
        )
        provider = RealLLMOneDeskProposalProvider(
            client=self.client,
            config=self.config,
            candidate_service=candidate_service,
        )
        controller = ResearchStageController(
            compiler=candidate_service,
            evaluator=candidate_service,
            explorer=BoundedReActExplorer(limits=ReactLimits(max_iterations=2, max_tool_calls=1, max_tokens=512)),
        )
        result = controller.run_desk(desk=desk_config, question=question, provider=provider, tools=tools)
        receipt = candidate_service.latest_receipt
        _write_run_artifacts(run_dir=run_dir, result=result, receipt=receipt)
        manifest = _manifest_from_result(
            run_id=run_id,
            desk_id=llm_desk_id,
            result=result,
            receipt=receipt,
            prompt_hashes=tuple(provider.prompt_hashes),
            tool_context_hashes=tuple(provider.tool_context_hashes),
        )
        _atomic_write_json(manifest_path, manifest.to_dict())
        return manifest

    def default_question(self, *, run_id: str) -> ResearchQuestionView:
        dataset = self.authority.dataset.registry.get(self.authority.dataset.dataset_id)
        return ResearchQuestionView(
            question_id=f"{run_id}:question",
            text="Find a visible valuation signal for the frozen equity universe.",
            universe=dataset.card.universe,
            horizon="5d",
            decision_time=self.authority.dataset.decision_time,
            availability_assumptions=(
                "use only fields with available_at not after decision_time",
                "historical membership is point-in-time",
                "labels are never prompt-visible features",
            ),
        )


def build_visible_context(
    *,
    authority: DeterministicOfficialEvidenceAuthority,
    desk_id: LLMDeskId | str,
) -> ResearchVisibleContext:
    llm_desk = LLMDeskId(desk_id)
    dataset = authority.dataset.registry.get(authority.dataset.dataset_id)
    feature_fields = tuple(
        sorted(field for field, role in dataset.field_roles.items() if role is FieldRole.FEATURE)
    )
    snapshot = authority.dataset.registry.as_of_snapshot(
        authority.dataset.dataset_id,
        decision_time=authority.dataset.decision_time,
        start_time=dataset.calendar[0],
        fields=feature_fields,
    )
    return ResearchVisibleContext(
        desk_id=llm_desk,
        dataset_manifest_hash=dataset.card.snapshot_hash,
        dataset_card_summary=(
            f"offline frozen market fixture {dataset.card.dataset_id} with {dataset.card.record_count} records"
        ),
        pit_field_coverage=dict(snapshot.coverage_by_field),
        availability_summary="records and membership are usable only after available_at cutoff",
        historical_universe_summary=f"{len(dataset.card.universe)} symbols with point-in-time membership changes",
        allowed_fields=feature_fields,
        citations=(),
        lineage_summary="new one-desk vertical slice candidate",
        validation_summary="visible validation unavailable before calculation",
    )


def _research_desk_for(desk_id: LLMDeskId) -> ResearchDeskSpec:
    desks = {desk.desk_id.value: desk for desk in build_default_desks()}
    try:
        return desks[desk_id.value]
    except KeyError as exc:  # pragma: no cover - enum exhaustiveness guard
        raise ContractValidationError(
            FailureCode.INVALID_IDENTIFIER,
            "unknown research desk id",
            details={"desk_id": desk_id.value},
        ) from exc


def _write_run_artifacts(
    *,
    run_dir: Path,
    result: DeskRunResult,
    receipt: RedactedCandidateReceipt | None,
) -> None:
    _atomic_write_json(run_dir / "desk_run_result.json", result.to_dict())
    _atomic_write_json(run_dir / "provider_trace.json", result.provider_trace.to_dict())
    if receipt is not None:
        _atomic_write_json(run_dir / "candidate_receipt.json", receipt.to_dict())


def _manifest_from_result(
    *,
    run_id: str,
    desk_id: LLMDeskId,
    result: DeskRunResult,
    receipt: RedactedCandidateReceipt | None,
    prompt_hashes: tuple[str, ...],
    tool_context_hashes: tuple[str, ...],
) -> VerticalSliceRunManifest:
    artifact_hashes: dict[str, str] = {
        "controller_result": result.artifact_hash,
        "provider_trace": result.provider_trace.artifact_hash,
    }
    if result.hypothesis is not None:
        artifact_hashes["hypothesis"] = result.hypothesis.artifact_hash
    if result.factor_draft is not None:
        artifact_hashes["factor_draft"] = result.factor_draft.artifact_hash
    if result.compile_pit is not None:
        artifact_hashes["compile_pit"] = result.compile_pit.artifact_hash
    if result.official_evaluation is not None:
        artifact_hashes["evaluation_view"] = result.official_evaluation.artifact_hash
    if result.feedback is not None:
        artifact_hashes["feedback"] = result.feedback.artifact_hash
    if receipt is not None:
        artifact_hashes["candidate_receipt"] = receipt.artifact_hash
    status = "COMPLETED" if result.status is LLMDeskStatus.DRAFT else result.status.value
    if status == LLMDeskStatus.INCOMPLETE.value:
        status = "INCOMPLETE"
    elif status == LLMDeskStatus.BLOCKED.value:
        status = "BLOCKED"
    return VerticalSliceRunManifest(
        run_id=run_id,
        desk_id=desk_id.value,
        status=status,
        stage_sequence=tuple(record.stage.value for record in result.stage_trace),
        controller_result_hash=result.artifact_hash,
        provider_trace_hash=result.provider_trace.artifact_hash,
        candidate_receipt_hash=receipt.artifact_hash if receipt is not None else None,
        hypothesis_hash=result.hypothesis.artifact_hash if result.hypothesis is not None else None,
        factor_draft_hash=result.factor_draft.artifact_hash if result.factor_draft is not None else None,
        compile_pit_hash=result.compile_pit.artifact_hash if result.compile_pit is not None else None,
        evaluation_view_hash=result.official_evaluation.artifact_hash if result.official_evaluation is not None else None,
        feedback_hash=result.feedback.artifact_hash if result.feedback is not None else None,
        prompt_hashes=prompt_hashes,
        tool_context_hashes=tool_context_hashes,
        artifact_hashes=artifact_hashes,
        failure_code=result.failure_code,
    )


__all__ = [
    "CandidateSubmissionService",
    "RealLLMOneDeskProposalProvider",
    "RealLLMVerticalSlice",
    "RedactedCandidateReceipt",
    "VerticalSliceRunManifest",
    "build_visible_context",
]
