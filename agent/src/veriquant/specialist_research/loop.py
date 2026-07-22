"""Program-controlled shell around an open-ended specialist LLM research loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import fail
from ..llm_desks.safety import canonical_hash
from .checkpoint import SpecialistCheckpointStore
from .models import (
    CandidateCritique,
    CandidatePreflightResult,
    CritiqueAction,
    InvestigationFinding,
    PreflightStatus,
    ResearchExplorationPlan,
    SpecialistAgentCheckpoint,
    SpecialistAgentOutcome,
    SpecialistDraftIssue,
    SpecialistResultKind,
    SpecialistCandidateDraft,
    RedactedMachineFeedback,
    SpecialistResearchRequest,
    SpecialistStage,
    SpecialistTurn,
    SpecialistUsage,
    TurnKind,
    require_transition,
)
from .preflight import SpecialistFactorPreflight
from .profiles import SpecialistAgentProfile, minimum_investigation_tool_names
from .protocols import get_specialist_protocol
from .provider import SpecialistProviderContext, SpecialistResearchProvider
from .tools import RestrictedResearchToolRegistry


_RECOVERABLE_LLM_STAGE_FAILURES = frozenset(
    {
        FailureCode.PROVIDER_TIMEOUT,
        FailureCode.PROVIDER_SCHEMA_REJECTED,
        FailureCode.PROVIDER_BUDGET_EXCEEDED,
        FailureCode.INCOMPLETE_RESULT,
        FailureCode.INVALID_VALUE,
        FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
    }
)


@dataclass
class _RuntimeState:
    stage: SpecialistStage
    history: list[SpecialistStage]
    plan: ResearchExplorationPlan | None
    findings: list[InvestigationFinding]
    candidates: list[SpecialistCandidateDraft]
    active_candidates: list[SpecialistCandidateDraft]
    preflight_results: list[CandidatePreflightResult]
    capability_requests: list[Any]
    draft_issues: list[SpecialistDraftIssue]
    iterations: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    revisions: int


class SpecialistResearchAgent:
    """One specialist: broad DRAFT research inside hard program boundaries."""

    def __init__(
        self,
        *,
        preflight: SpecialistFactorPreflight | None = None,
        strict_protocol: bool = False,
        broad_candidate_search: bool = False,
    ) -> None:
        self._preflight = preflight or SpecialistFactorPreflight()
        self._strict_protocol = strict_protocol
        self._broad_candidate_search = broad_candidate_search

    def run(
        self,
        *,
        request: SpecialistResearchRequest,
        profile: SpecialistAgentProfile,
        provider: SpecialistResearchProvider,
        tools: RestrictedResearchToolRegistry,
        checkpoint_store: SpecialistCheckpointStore | None = None,
        cancel_requested: bool = False,
    ) -> SpecialistAgentOutcome:
        profile.assert_matches(request)
        if tools.profile.specialist_id != request.specialist_id:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "tool registry belongs to another specialist")
        if checkpoint_store is not None:
            existing = checkpoint_store.load_outcome()
            if existing is not None:
                if existing.request_hash != request.artifact_hash:
                    fail(FailureCode.INVALID_HASH, "terminal specialist outcome belongs to another request")
                return existing
        state = self._restore(request, checkpoint_store)
        started = time.monotonic()
        try:
            while state.stage not in {
                SpecialistStage.COMPLETED,
                SpecialistStage.BLOCKED,
                SpecialistStage.FAILED,
                SpecialistStage.CANCELLED,
                SpecialistStage.BUDGET_EXHAUSTED,
            }:
                if cancel_requested:
                    self._advance(state, SpecialistStage.CANCELLED)
                    return self._finish(request, state, "CANCELLED", "research cancelled", checkpoint_store)
                self._check_time(request, started)
                if state.stage is SpecialistStage.CREATED:
                    self._advance(state, SpecialistStage.SCOPING)
                elif state.stage is SpecialistStage.SCOPING:
                    self._advance(state, SpecialistStage.PLANNING)
                elif state.stage is SpecialistStage.PLANNING:
                    state.plan = self._obtain_valid_plan(request, profile, provider, tools, state)
                    self._advance(state, SpecialistStage.INVESTIGATING)
                elif state.stage is SpecialistStage.INVESTIGATING:
                    if self._strict_protocol:
                        self._run_minimum_investigations(request, profile, tools, state)
                        self._save_checkpoint(request, state, checkpoint_store)
                    turn = self._next_valid_turn(
                        request, profile, provider, tools, state,
                        allowed={TurnKind.TOOL_CALL, TurnKind.CANDIDATES},
                        issue_kind="candidate",
                    )
                    state.capability_requests.extend(turn.capability_requests)
                    state.draft_issues.extend(turn.draft_issues)
                    if turn.kind is TurnKind.TOOL_CALL:
                        if state.tool_calls >= request.budget.max_tool_calls:
                            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "specialist tool budget exhausted")
                        assert turn.tool_call is not None
                        arguments_hash = canonical_hash(dict(turn.tool_call.arguments))
                        if any(
                            item.tool_name == turn.tool_call.tool_name
                            and item.arguments_hash == arguments_hash
                            for item in state.findings
                        ):
                            state.draft_issues.append(
                                SpecialistDraftIssue(
                                    item_kind="tool_call",
                                    item_index=state.iterations - 1,
                                    failure_code="DUPLICATE_TOOL_CALL",
                                    message="duplicate read-only investigation was skipped",
                                )
                            )
                            self._save_checkpoint(request, state, checkpoint_store)
                            continue
                        summary = tools.call(turn.tool_call.tool_name, turn.tool_call.arguments)
                        state.tool_calls += 1
                        state.findings.append(
                            InvestigationFinding(
                                tool_name=summary.tool_name,
                                research_question=turn.tool_call.research_question,
                                arguments_hash=summary.arguments_hash,
                                result_hash=summary.result_hash,
                                result_summary=summary.result_summary,
                            )
                        )
                    else:
                        if not state.findings:
                            fail(FailureCode.INCOMPLETE_RESULT, "specialist must investigate at least one visible source")
                        self._accept_candidate_batch(request, profile, state, turn.candidates, revised=False)
                        self._advance(state, SpecialistStage.ENUMERATING)
                elif state.stage is SpecialistStage.ENUMERATING:
                    if self._should_generate_more(request, state):
                        self._advance(state, SpecialistStage.GENERATING)
                    else:
                        self._advance(state, SpecialistStage.SELF_CRITIQUING)
                elif state.stage is SpecialistStage.GENERATING:
                    try:
                        turn = self._next_valid_turn(
                            request, profile, provider, tools, state,
                            allowed={TurnKind.CANDIDATES},
                            issue_kind="candidate",
                        )
                    except ContractValidationError as exc:
                        if state.candidates and exc.code in _RECOVERABLE_LLM_STAGE_FAILURES:
                            state.draft_issues.append(
                                SpecialistDraftIssue(
                                    item_kind="candidate_expansion",
                                    item_index=state.iterations,
                                    failure_code=exc.code.value,
                                    message="candidate expansion stopped; committed candidate batches remain valid",
                                )
                            )
                            self._advance(state, SpecialistStage.SELF_CRITIQUING)
                            self._save_checkpoint(request, state, checkpoint_store)
                            continue
                        raise
                    state.capability_requests.extend(turn.capability_requests)
                    state.draft_issues.extend(turn.draft_issues)
                    self._accept_candidate_batch(request, profile, state, turn.candidates, revised=False)
                    if not self._should_generate_more(request, state):
                        self._advance(state, SpecialistStage.SELF_CRITIQUING)
                elif state.stage is SpecialistStage.SELF_CRITIQUING:
                    try:
                        turn = self._next_valid_turn(
                            request, profile, provider, tools, state,
                            allowed={TurnKind.SELF_CRITIQUE},
                            issue_kind="candidate",
                        )
                    except ContractValidationError as exc:
                        if state.active_candidates and exc.code in _RECOVERABLE_LLM_STAGE_FAILURES:
                            state.draft_issues.append(
                                SpecialistDraftIssue(
                                    item_kind="self_critique",
                                    item_index=state.iterations,
                                    failure_code=exc.code.value,
                                    message="LLM self-critique unavailable; all committed drafts continue to deterministic preflight",
                                )
                            )
                            self._advance(state, SpecialistStage.PREFLIGHT)
                            self._save_checkpoint(request, state, checkpoint_store)
                            continue
                        raise
                    state.active_candidates = self._apply_critiques(state.active_candidates, turn.critiques)
                    if not state.active_candidates:
                        self._advance(state, SpecialistStage.BLOCKED)
                        return self._finish(request, state, "NO_CANDIDATE_AFTER_SELF_CRITIQUE", "all candidates were dropped by self-critique", checkpoint_store)
                    self._advance(state, SpecialistStage.PREFLIGHT)
                elif state.stage is SpecialistStage.PREFLIGHT:
                    batch_results = self._preflight_batch(request, state.active_candidates, state.preflight_results)
                    state.preflight_results.extend(batch_results)
                    if any(item.status is PreflightStatus.ACCEPTED for item in batch_results):
                        self._advance(state, SpecialistStage.SUBMITTING)
                    elif state.revisions < min(1, request.budget.max_revisions):
                        self._advance(state, SpecialistStage.REVISING)
                    else:
                        self._advance(state, SpecialistStage.BLOCKED)
                        return self._finish(request, state, "NO_FACTOR_DAG_ACCEPTED", "no candidate passed FactorDAG preflight", checkpoint_store)
                elif state.stage is SpecialistStage.REVISING:
                    turn = self._next_valid_turn(
                        request, profile, provider, tools, state,
                        allowed={TurnKind.REVISED_CANDIDATES},
                        issue_kind="candidate",
                    )
                    state.capability_requests.extend(turn.capability_requests)
                    state.draft_issues.extend(turn.draft_issues)
                    state.revisions += 1
                    try:
                        self._accept_candidate_batch(request, profile, state, turn.candidates, revised=True)
                    except ContractValidationError as exc:
                        if exc.code is not FailureCode.INCOMPLETE_RESULT:
                            raise
                        self._advance(state, SpecialistStage.BLOCKED)
                        return self._finish(
                            request,
                            state,
                            "NO_VALID_REVISED_CANDIDATE",
                            "all revised candidates were quarantined; earlier negative research remains available",
                            checkpoint_store,
                        )
                    self._advance(state, SpecialistStage.SELF_CRITIQUING)
                elif state.stage in {SpecialistStage.SUBMITTING, SpecialistStage.READY_TO_SUBMIT}:
                    target = (
                        SpecialistStage.AWAITING_EVIDENCE
                        if state.stage is SpecialistStage.SUBMITTING
                        else SpecialistStage.AWAITING_FEEDBACK
                    )
                    self._advance(state, target)
                elif state.stage in {SpecialistStage.AWAITING_EVIDENCE, SpecialistStage.AWAITING_FEEDBACK}:
                    self._advance(state, SpecialistStage.COMPLETED)
                    return self._finish(request, state, None, "DRAFT candidates are ready for external PIT/Evidence submission", checkpoint_store)
                else:
                    fail(FailureCode.INVALID_STATE, "unsupported specialist runtime stage")
                self._save_checkpoint(request, state, checkpoint_store)
        except ContractValidationError as exc:
            target = (
                SpecialistStage.BUDGET_EXHAUSTED
                if exc.code in {FailureCode.PROVIDER_BUDGET_EXCEEDED, FailureCode.PROVIDER_TIMEOUT}
                else SpecialistStage.FAILED
            )
            if target in _allowed_targets(state.stage):
                self._advance(state, target)
                return self._finish(request, state, exc.code.value, str(exc), checkpoint_store)
            raise
        fail(FailureCode.INVALID_STATE, "specialist runtime exited without a terminal outcome")

    def revise_from_feedback(
        self,
        *,
        request: SpecialistResearchRequest,
        profile: SpecialistAgentProfile,
        provider: SpecialistResearchProvider,
        tools: RestrictedResearchToolRegistry,
        prior: SpecialistAgentOutcome,
        feedback: tuple[RedactedMachineFeedback, ...],
    ) -> SpecialistAgentOutcome:
        """Fork a completed DRAFT at its feedback checkpoint and revise once."""

        profile.assert_matches(request)
        if prior.request_hash != request.artifact_hash or prior.specialist_id != request.specialist_id:
            fail(FailureCode.INVALID_HASH, "feedback revision belongs to another research request")
        if prior.status is not SpecialistStage.COMPLETED:
            fail(FailureCode.INVALID_STATE, "only a completed DRAFT outcome can receive feedback")
        if not feedback or len({item.candidate_id for item in feedback}) != len(feedback):
            fail(FailureCode.INVALID_VALUE, "feedback must be unique and non-empty")
        known = {item.candidate_id for item in prior.candidates}
        if any(item.candidate_id not in known for item in feedback):
            fail(FailureCode.INVALID_FIELD_OWNER, "feedback references another candidate")
        allowed_feedback = tuple(item for item in feedback if item.revision_allowed)
        if not allowed_feedback:
            return prior
        if prior.usage.revisions >= min(1, request.budget.max_revisions):
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "specialist revision budget exhausted")
        if len(prior.stage_history) < 2 or prior.stage_history[-2] not in {
            SpecialistStage.AWAITING_EVIDENCE,
            SpecialistStage.AWAITING_FEEDBACK,
        }:
            fail(FailureCode.INVALID_STATE, "prior outcome has no feedback checkpoint")
        active_ids = {item.candidate_id for item in allowed_feedback}
        state = _RuntimeState(
            stage=prior.stage_history[-2],
            history=list(prior.stage_history[:-1]),
            plan=prior.plan,
            findings=list(prior.findings),
            candidates=list(prior.candidates),
            active_candidates=[item for item in prior.candidates if item.candidate_id in active_ids],
            preflight_results=list(prior.preflight_results),
            capability_requests=list(prior.capability_requests),
            draft_issues=list(prior.draft_issues),
            iterations=prior.usage.iterations,
            tool_calls=prior.usage.tool_calls,
            input_tokens=prior.usage.input_tokens,
            output_tokens=prior.usage.output_tokens,
            revisions=prior.usage.revisions,
        )
        self._advance(state, SpecialistStage.REVISING)
        feedback_view = tuple(
            {
                "candidate_id": item.candidate_id,
                "compile_status": item.compile_status,
                "pit_status": item.pit_status,
                "visible_failure_codes": item.visible_failure_codes,
                "cost_warning": item.cost_warning,
                "stability_warning": item.stability_warning,
                "incrementality_warning": item.incrementality_warning,
                "revision_allowed": item.revision_allowed,
                "feedback_hash": item.artifact_hash,
            }
            for item in allowed_feedback
        )
        turn = self._next_turn(request, profile, provider, tools, state, extra_feedback=feedback_view)
        self._expect(turn, {TurnKind.REVISED_CANDIDATES})
        state.capability_requests.extend(turn.capability_requests)
        state.draft_issues.extend(turn.draft_issues)
        state.revisions += 1
        self._accept_candidate_batch(request, profile, state, turn.candidates, revised=True)
        self._advance(state, SpecialistStage.SELF_CRITIQUING)
        critique = self._next_turn(request, profile, provider, tools, state)
        self._expect(critique, {TurnKind.SELF_CRITIQUE})
        state.active_candidates = self._apply_critiques(state.active_candidates, critique.critiques)
        if not state.active_candidates:
            self._advance(state, SpecialistStage.BLOCKED)
            return self._finish(request, state, "NO_CANDIDATE_AFTER_SELF_CRITIQUE", "feedback revision was dropped", None)
        self._advance(state, SpecialistStage.PREFLIGHT)
        results = self._preflight_batch(request, state.active_candidates, state.preflight_results)
        state.preflight_results.extend(results)
        if not any(item.status is PreflightStatus.ACCEPTED for item in results):
            self._advance(state, SpecialistStage.BLOCKED)
            return self._finish(request, state, "FEEDBACK_REVISION_NOT_ACCEPTED", "feedback revision failed FactorDAG preflight", None)
        self._advance(state, SpecialistStage.SUBMITTING)
        self._advance(state, SpecialistStage.AWAITING_EVIDENCE)
        self._advance(state, SpecialistStage.COMPLETED)
        return self._finish(request, state, None, "revised DRAFT is ready for a new external evaluation attempt", None)

    @staticmethod
    def _restore(request: SpecialistResearchRequest, store: SpecialistCheckpointStore | None) -> _RuntimeState:
        checkpoint = None if store is None else store.load_checkpoint()
        if checkpoint is None:
            return _RuntimeState(
                stage=SpecialistStage.CREATED,
                history=[SpecialistStage.CREATED],
                plan=None,
                findings=[],
                candidates=[],
                active_candidates=[],
                preflight_results=[],
                capability_requests=[],
                draft_issues=[],
                iterations=0,
                tool_calls=0,
                input_tokens=0,
                output_tokens=0,
                revisions=0,
            )
        if checkpoint.request_hash != request.artifact_hash or checkpoint.specialist_id != request.specialist_id:
            fail(FailureCode.INVALID_HASH, "specialist checkpoint belongs to another request")
        usage = checkpoint.usage
        return _RuntimeState(
            stage=checkpoint.current_stage,
            history=list(checkpoint.stage_history),
            plan=checkpoint.plan,
            findings=list(checkpoint.findings),
            candidates=list(checkpoint.candidates),
            active_candidates=list(checkpoint.active_candidates),
            preflight_results=list(checkpoint.preflight_results),
            capability_requests=list(checkpoint.capability_requests),
            draft_issues=list(checkpoint.draft_issues),
            iterations=usage.iterations,
            tool_calls=usage.tool_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            revisions=usage.revisions,
        )

    @staticmethod
    def _advance(state: _RuntimeState, target: SpecialistStage) -> None:
        require_transition(state.stage, target)
        state.stage = target
        state.history.append(target)

    def _next_turn(
        self,
        request: SpecialistResearchRequest,
        profile: SpecialistAgentProfile,
        provider: SpecialistResearchProvider,
        tools: RestrictedResearchToolRegistry,
        state: _RuntimeState,
        *,
        extra_feedback: tuple[Mapping[str, Any], ...] = (),
    ) -> SpecialistTurn:
        if state.iterations >= request.budget.max_iterations:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "specialist iteration budget exhausted")
        feedback = tuple(
            {
                "candidate_id": item.candidate_id,
                "status": item.status.value,
                "failure_code": item.failure_code,
                "message": item.message,
            }
            for item in state.preflight_results
            if item.status is not PreflightStatus.ACCEPTED
        ) + extra_feedback
        state.iterations += 1
        try:
            turn = provider.next_turn(
                SpecialistProviderContext(
                    request=request,
                    profile=profile,
                    stage=state.stage,
                    iteration=state.iterations,
                    plan=state.plan,
                    finding_summaries=tuple(item.result_summary for item in state.findings),
                    candidates=tuple(state.active_candidates),
                    preflight_feedback=feedback,
                    tool_definitions=tools.definitions,
                )
            )
        except ContractValidationError:
            raise
        except Exception as exc:
            fail(
                FailureCode.ADAPTER_EXECUTION_FAILED,
                "specialist provider failed closed",
                error=type(exc).__name__,
            )
        state.input_tokens += turn.input_tokens
        state.output_tokens += turn.output_tokens
        if state.input_tokens > request.budget.max_input_tokens or state.output_tokens > request.budget.max_output_tokens:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "specialist token budget exhausted")
        if state.input_tokens + state.output_tokens > request.budget.max_tokens:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "specialist total token budget exhausted")
        return turn

    def _next_valid_turn(
        self,
        request: SpecialistResearchRequest,
        profile: SpecialistAgentProfile,
        provider: SpecialistResearchProvider,
        tools: RestrictedResearchToolRegistry,
        state: _RuntimeState,
        *,
        allowed: set[TurnKind],
        issue_kind: str,
    ) -> SpecialistTurn:
        retryable = {
            FailureCode.INCOMPLETE_RESULT,
            FailureCode.PROVIDER_SCHEMA_REJECTED,
            FailureCode.UNKNOWN_FIELD,
            FailureCode.INVALID_VALUE,
        }
        feedback: tuple[Mapping[str, Any], ...] = ()
        for attempt in range(2):
            try:
                turn = self._next_turn(
                    request,
                    profile,
                    provider,
                    tools,
                    state,
                    extra_feedback=feedback,
                )
                self._expect(turn, allowed)
                return turn
            except ContractValidationError as exc:
                if exc.code not in retryable or attempt == 1:
                    raise
                state.draft_issues.append(
                    SpecialistDraftIssue(
                        item_kind=issue_kind,
                        item_index=state.iterations - 1,
                        failure_code=exc.code.value,
                        message="stage payload was rejected and one bounded schema repair was requested",
                    )
                )
                feedback = (
                    {
                        "stage_failure_code": exc.code.value,
                        "required_repair": "return only a payload allowed by required_output_schema",
                    },
                )
        fail(FailureCode.INVALID_STATE, "bounded stage repair exited unexpectedly")

    @staticmethod
    def _run_minimum_investigations(
        request: SpecialistResearchRequest,
        profile: SpecialistAgentProfile,
        tools: RestrictedResearchToolRegistry,
        state: _RuntimeState,
    ) -> None:
        completed = {item.tool_name for item in state.findings}
        for tool_name in minimum_investigation_tool_names(profile):
            if tool_name in completed:
                continue
            if state.tool_calls >= request.budget.max_tool_calls:
                fail(
                    FailureCode.PROVIDER_BUDGET_EXCEEDED,
                    "tool budget cannot satisfy program-required minimum investigation",
                    tool=tool_name,
                )
            query = (
                f"program-required visible preparation for {request.specialist_id}; "
                f"question_hash={canonical_hash({'question': request.question})}"
            )
            summary = tools.call(tool_name, {"query": query})
            state.tool_calls += 1
            state.findings.append(
                InvestigationFinding(
                    tool_name=summary.tool_name,
                    research_question=f"minimum visible investigation: {tool_name.rsplit('.', 1)[-1]}",
                    arguments_hash=summary.arguments_hash,
                    result_hash=summary.result_hash,
                    result_summary=summary.result_summary,
                )
            )
            completed.add(tool_name)

    def _obtain_valid_plan(
        self,
        request: SpecialistResearchRequest,
        profile: SpecialistAgentProfile,
        provider: SpecialistResearchProvider,
        tools: RestrictedResearchToolRegistry,
        state: _RuntimeState,
    ) -> ResearchExplorationPlan:
        retryable = {
            FailureCode.INCOMPLETE_RESULT,
            FailureCode.PROVIDER_SCHEMA_REJECTED,
            FailureCode.UNKNOWN_FIELD,
            FailureCode.INVALID_VALUE,
        }
        feedback: tuple[Mapping[str, Any], ...] = ()
        last_error: ContractValidationError | None = None
        for attempt in range(request.budget.max_planning_rounds):
            try:
                turn = self._next_turn(
                    request,
                    profile,
                    provider,
                    tools,
                    state,
                    extra_feedback=feedback,
                )
                self._expect(turn, {TurnKind.PLAN})
                assert turn.plan is not None
                plan = turn.plan
                if plan.specialist_id != request.specialist_id:
                    fail(FailureCode.INVALID_FIELD_OWNER, "research plan belongs to another specialist")
                get_specialist_protocol(request.specialist_id).validate_plan(plan)
                if self._strict_protocol and (not plan.mechanism_ideas or not plan.candidate_family_plans):
                    fail(
                        FailureCode.INCOMPLETE_RESULT,
                        "strict specialist plan requires mechanism ideas and candidate family plans",
                    )
                if len(plan.directions) > request.budget.max_candidate_families:
                    fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "research plan exceeds family budget")
                idea_count = len(plan.mechanism_ideas) or len(plan.directions)
                if idea_count > request.budget.max_ideas:
                    fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "research plan exceeds idea budget")
                if len(plan.candidate_family_plans) > request.budget.max_candidate_families:
                    fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "research plan exceeds candidate family budget")
                if sum(item.max_combinations for item in plan.candidate_family_plans) > request.budget.max_combinations:
                    fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "research plan exceeds combination budget")
                return plan
            except ContractValidationError as exc:
                last_error = exc
                if exc.code not in retryable or attempt == request.budget.max_planning_rounds - 1:
                    raise
                state.draft_issues.append(
                    SpecialistDraftIssue(
                        item_kind="plan",
                        item_index=attempt,
                        failure_code=exc.code.value,
                        message="planning draft was rejected and a bounded repair was requested",
                    )
                )
                feedback = (
                    {
                        "planning_failure_code": exc.code.value,
                        "required_repair": (
                            "return PLAN with at least two distinct directions, two mechanism families, "
                            "and two candidate family plans using only the declared schema"
                        ),
                    },
                )
        assert last_error is not None
        raise last_error

    @staticmethod
    def _expect(turn: SpecialistTurn, allowed: set[TurnKind]) -> None:
        if turn.kind not in allowed:
            fail(FailureCode.INVALID_STATE, "provider turn is invalid for the current stage")

    @staticmethod
    def _accept_candidate_batch(
        request: SpecialistResearchRequest,
        profile: SpecialistAgentProfile,
        state: _RuntimeState,
        candidates: tuple[SpecialistCandidateDraft, ...],
        *,
        revised: bool,
    ) -> None:
        if not candidates:
            fail(FailureCode.INCOMPLETE_RESULT, "provider produced no candidates")
        existing_ids = {item.candidate_id for item in state.candidates}
        accepted: list[SpecialistCandidateDraft] = []
        seen_ids = set(existing_ids)
        families = {item.family_id for item in state.candidates}
        for index, item in enumerate(candidates):
            try:
                if len(state.candidates) + len(accepted) >= request.budget.max_submitted_candidates:
                    fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "candidate submission budget exhausted")
                if len(state.candidates) + len(accepted) >= request.budget.family_trial_budget:
                    fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "candidate family trial budget exhausted")
                if item.candidate_id in seen_ids:
                    fail(FailureCode.INVALID_VALUE, "candidate id already exists in this lineage")
                if item.family_id not in families and len(families) >= request.budget.max_candidate_families:
                    fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "candidate family budget exhausted")
                if profile.specialist_id != request.specialist_id:
                    fail(FailureCode.INVALID_FIELD_OWNER, "candidate profile belongs to another specialist")
                if state.plan is not None and (state.plan.mechanism_ideas or state.plan.candidate_family_plans):
                    get_specialist_protocol(request.specialist_id).validate_candidate(item)
                if item.forecast_horizon_sessions != request.label_horizon_sessions:
                    fail(FailureCode.INVALID_VALUE, "candidate forecast horizon differs from the research request")
                if revised:
                    if item.revision_number != state.revisions or item.parent_candidate_id not in existing_ids:
                        fail(FailureCode.INVALID_STATE, "revised candidate lineage is invalid")
                elif item.revision_number != 0 or item.parent_candidate_id is not None:
                    fail(FailureCode.INVALID_STATE, "initial candidate cannot carry revision lineage")
            except ContractValidationError as exc:
                state.draft_issues.append(
                    SpecialistDraftIssue(
                        item_kind="candidate",
                        item_index=index,
                        failure_code=exc.code.value,
                        message="candidate was independently rejected by runtime validation",
                    )
                )
                continue
            accepted.append(item)
            seen_ids.add(item.candidate_id)
            families.add(item.family_id)
        if not accepted:
            fail(FailureCode.INCOMPLETE_RESULT, "no independently valid candidates remained in the batch")
        state.candidates.extend(accepted)
        if revised:
            state.active_candidates = accepted
        else:
            state.active_candidates.extend(accepted)

    def _should_generate_more(
        self,
        request: SpecialistResearchRequest,
        state: _RuntimeState,
    ) -> bool:
        if not self._broad_candidate_search:
            return False
        target = min(request.budget.max_submitted_candidates, request.budget.family_trial_budget)
        if len(state.candidates) >= target:
            return False
        # Keep one provider turn for mandatory self-critique. If the remaining
        # budget is too small, preserve the valid partial batch instead of
        # converting it into a provider-budget failure.
        return state.iterations < request.budget.max_iterations - 1

    @staticmethod
    def _apply_critiques(
        candidates: list[SpecialistCandidateDraft],
        critiques: tuple[CandidateCritique, ...],
    ) -> list[SpecialistCandidateDraft]:
        expected = {item.candidate_id for item in candidates}
        received = {item.candidate_id for item in critiques}
        if expected != received or len(received) != len(critiques):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "self-critique must cover every active candidate exactly once")
        actions = {item.candidate_id: item.action for item in critiques}
        return [item for item in candidates if actions[item.candidate_id] is not CritiqueAction.DROP]

    def _preflight_batch(
        self,
        request: SpecialistResearchRequest,
        candidates: list[SpecialistCandidateDraft],
        prior_results: list[CandidatePreflightResult],
    ) -> tuple[CandidatePreflightResult, ...]:
        known_hashes = {
            item.effective_program_hash
            for item in prior_results
            if item.status is PreflightStatus.ACCEPTED and item.effective_program_hash is not None
        }
        results: list[CandidatePreflightResult] = []
        for candidate in candidates:
            result = self._preflight.check(request, candidate)
            if result.status is PreflightStatus.ACCEPTED and result.effective_program_hash in known_hashes:
                result = CandidatePreflightResult(
                    candidate_id=candidate.candidate_id,
                    draft_hash=candidate.artifact_hash,
                    status=PreflightStatus.REVISE,
                    normalized_expression=result.normalized_expression,
                    failure_code="DUPLICATE_FACTOR_DAG",
                    message="candidate canonical formula program duplicates an earlier submitted draft",
                )
            if result.effective_program_hash is not None:
                known_hashes.add(result.effective_program_hash)
            results.append(result)
        return tuple(results)

    @staticmethod
    def _usage(state: _RuntimeState) -> SpecialistUsage:
        return SpecialistUsage(
            iterations=state.iterations,
            tool_calls=state.tool_calls,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            revisions=state.revisions,
        )

    def _save_checkpoint(
        self,
        request: SpecialistResearchRequest,
        state: _RuntimeState,
        store: SpecialistCheckpointStore | None,
    ) -> None:
        if store is None or state.stage in {
            SpecialistStage.COMPLETED,
            SpecialistStage.BLOCKED,
            SpecialistStage.FAILED,
            SpecialistStage.CANCELLED,
            SpecialistStage.BUDGET_EXHAUSTED,
        }:
            return
        store.save_checkpoint(
            SpecialistAgentCheckpoint(
                request_hash=request.artifact_hash,
                specialist_id=request.specialist_id,
                current_stage=state.stage,
                plan=state.plan,
                findings=tuple(state.findings),
                candidates=tuple(state.candidates),
                active_candidates=tuple(state.active_candidates),
                preflight_results=tuple(state.preflight_results),
                capability_requests=tuple(state.capability_requests),
                draft_issues=tuple(state.draft_issues),
                usage=self._usage(state),
                stage_history=tuple(state.history),
            )
        )

    def _finish(
        self,
        request: SpecialistResearchRequest,
        state: _RuntimeState,
        failure_code: str | None,
        message: str,
        store: SpecialistCheckpointStore | None,
    ) -> SpecialistAgentOutcome:
        infrastructure_failures = {
            FailureCode.ADAPTER_EXECUTION_FAILED.value,
            FailureCode.ADAPTER_UNAVAILABLE.value,
            FailureCode.PROVIDER_TIMEOUT.value,
        }
        accepted = any(item.status is PreflightStatus.ACCEPTED for item in state.preflight_results)
        has_capability_gap = bool(state.capability_requests) or any(
            item.status is PreflightStatus.CAPABILITY_REQUEST for item in state.preflight_results
        )
        if state.stage is SpecialistStage.COMPLETED:
            result_kind = SpecialistResultKind.EVALUATED_RESEARCH_RESULT
        elif failure_code in infrastructure_failures:
            result_kind = SpecialistResultKind.SYSTEM_FAILED
        elif has_capability_gap and not accepted:
            result_kind = SpecialistResultKind.DATA_OR_CAPABILITY_GAP_REPORT
        elif state.preflight_results and all(
            item.status is not PreflightStatus.ACCEPTED for item in state.preflight_results
        ):
            result_kind = SpecialistResultKind.NEGATIVE_RESEARCH_RESULT
        elif state.plan is not None or state.findings or state.candidates or state.draft_issues:
            result_kind = SpecialistResultKind.PARTIAL_RESEARCH_RESULT
        else:
            result_kind = SpecialistResultKind.SYSTEM_FAILED
        outcome = SpecialistAgentOutcome(
            request_hash=request.artifact_hash,
            specialist_id=request.specialist_id,
            status=state.stage,
            plan=state.plan,
            findings=tuple(state.findings),
            candidates=tuple(state.candidates),
            preflight_results=tuple(state.preflight_results),
            capability_requests=tuple(state.capability_requests),
            result_kind=result_kind,
            draft_issues=tuple(state.draft_issues),
            usage=self._usage(state),
            stage_history=tuple(state.history),
            failure_code=failure_code,
            message=message,
        )
        if store is not None:
            store.save_outcome(outcome)
        return outcome

    @staticmethod
    def _check_time(request: SpecialistResearchRequest, started: float) -> None:
        if time.monotonic() - started > request.budget.timeout_seconds:
            fail(FailureCode.PROVIDER_TIMEOUT, "specialist wall-time budget exhausted")


class BoundedResearchAgentLoop(SpecialistResearchAgent):
    """Public milestone name for the program-controlled specialist loop."""

    def __init__(
        self,
        *,
        preflight: SpecialistFactorPreflight | None = None,
        broad_candidate_search: bool = False,
    ) -> None:
        super().__init__(
            preflight=preflight,
            strict_protocol=True,
            broad_candidate_search=broad_candidate_search,
        )


def _allowed_targets(stage: SpecialistStage) -> frozenset[SpecialistStage]:
    from .models import TRANSITIONS

    return TRANSITIONS[stage]
