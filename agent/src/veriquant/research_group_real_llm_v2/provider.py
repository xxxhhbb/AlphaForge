"""Selected-only, bounded and fail-closed real-LLM proposal adapter."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Mapping, Protocol

from ..candidate_translation import (
    CandidateDraftEnvelope,
    CandidateTranslationStatus,
    SafeCandidateTranslator,
)
from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import fail
from ..llm_desks.safety import opaque_hash
from ..research_group_llm_adapter import (
    DeterministicResearchGroupFallbackProvider,
    ProviderMode,
    ResearchGroupLLMAdapter,
    ResearchGroupProviderRequest,
)
from ..research_group_runtime import GroupProposalPayload
from ..research_group_runtime.selector import SelectedGroupContext
from ..research_os import ResearchIntent
from .models import (
    GroupAttemptStatus,
    GroupExecutionPolicy,
    GroupLLMCallRequest,
    GroupLLMCallResponse,
    GroupProposalAttempt,
    GroupProposalProvenance,
)


class GroupRawProposalTransport(Protocol):
    """Network boundary. Implementations must return inert JSON only."""

    available: bool

    def complete_group_proposal(self, request: GroupLLMCallRequest) -> GroupLLMCallResponse: ...


class _SingleResponseProvider:
    provider_kind = ProviderMode.REAL_LLM

    def __init__(self, response: GroupLLMCallResponse) -> None:
        self.response = response

    def complete_proposal(self, request: ResearchGroupProviderRequest):
        return self.response.payload


class SelectedGroupRealLLMProposalProvider:
    """Implements ``GroupProposalProvider`` without inheriting financial authority.

    Each call receives only the selected group's context and tool names.  A
    schema or FactorDAG expression error may trigger one typed revision.  Only
    provider unavailability uses deterministic fallback; errors and timeouts
    otherwise remain branch-local failures in the existing group runtime.
    """

    def __init__(
        self,
        *,
        transport: GroupRawProposalTransport,
        selected_group_ids: tuple[str, ...],
        policies: tuple[GroupExecutionPolicy, ...],
        fallback_on_unavailable: bool = True,
        translator: SafeCandidateTranslator | None = None,
    ) -> None:
        if not 1 <= len(selected_group_ids) <= 6 or len(selected_group_ids) != len(set(selected_group_ids)):
            fail(FailureCode.INVALID_VALUE, "selected_group_ids must contain one to six unique groups")
        self.transport = transport
        self.selected_group_ids = selected_group_ids
        self.policies = {item.group_id: item for item in policies}
        if set(self.policies) != set(selected_group_ids):
            fail(FailureCode.INVALID_VALUE, "one execution policy is required for every selected group")
        self.fallback_on_unavailable = bool(fallback_on_unavailable)
        self.translator = translator or SafeCandidateTranslator()
        self._attempts: dict[str, GroupProposalAttempt] = {}

    @property
    def attempts(self) -> tuple[GroupProposalAttempt, ...]:
        return tuple(self._attempts[key] for key in sorted(self._attempts))

    def propose(
        self,
        *,
        context: SelectedGroupContext,
        intent: ResearchIntent,
        candidate_budget: int,
    ) -> GroupProposalPayload:
        policy = self._policy(context, candidate_budget)
        if not bool(getattr(self.transport, "available", False)):
            return self._fallback(context=context, intent=intent, candidate_budget=candidate_budget)

        total_tokens = 0
        previous_hash: str | None = None
        repair_code: str | None = None
        max_calls = 1 + policy.max_revisions
        for call_number in range(1, max_calls + 1):
            request = GroupLLMCallRequest(
                context=context,
                intent=intent,
                policy=policy,
                selected_group_ids=self.selected_group_ids,
                attempt_number=call_number,
                repair_code=repair_code,
                prior_payload_hash=previous_hash,
            )
            try:
                response = self._call_with_timeout(request, policy.timeout_seconds)
            except ContractValidationError as exc:
                if exc.code is FailureCode.ADAPTER_UNAVAILABLE and self.fallback_on_unavailable:
                    return self._fallback(context=context, intent=intent, candidate_budget=candidate_budget)
                self._record_failure(context.group_id, call_number, total_tokens, exc.code)
                raise
            total_tokens += response.input_tokens + response.output_tokens
            if total_tokens > policy.max_tokens:
                self._record_failure(context.group_id, call_number, total_tokens, FailureCode.PROVIDER_BUDGET_EXCEEDED)
                fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "research-group token budget exceeded")
            try:
                _reject_authority_payload(response.payload)
            except ContractValidationError as exc:
                self._record_failure(context.group_id, call_number, total_tokens, exc.code)
                raise
            previous_hash = opaque_hash(response.payload)
            try:
                proposal = ResearchGroupLLMAdapter(
                    _SingleResponseProvider(response),
                    selected_group_ids=self.selected_group_ids,
                    provider_mode=ProviderMode.REAL_LLM,
                ).propose(context=context, intent=intent, candidate_budget=candidate_budget)
                self._validate_expressions(proposal, context=context, intent=intent, policy=policy)
            except ContractValidationError as exc:
                if call_number < max_calls and exc.code in {
                    FailureCode.PROVIDER_SCHEMA_REJECTED,
                    FailureCode.UNKNOWN_FIELD,
                    FailureCode.INVALID_FACTOR_DAG,
                    FailureCode.INVALID_VALUE,
                }:
                    repair_code = exc.code.value
                    continue
                self._record_failure(context.group_id, call_number, total_tokens, exc.code)
                raise
            self._attempts[context.group_id] = GroupProposalAttempt(
                group_id=context.group_id,
                status=GroupAttemptStatus.COMPLETED,
                provenance=GroupProposalProvenance.REAL_LLM,
                provider_calls=call_number,
                revisions_used=call_number - 1,
                tokens_used=total_tokens,
                proposal_hash=proposal.artifact_hash,
            )
            return proposal
        fail(FailureCode.ADAPTER_EXECUTION_FAILED, "provider exited without a proposal")

    def _policy(self, context: SelectedGroupContext, candidate_budget: int) -> GroupExecutionPolicy:
        if context.group_id not in self.selected_group_ids:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "unselected group cannot call the real provider")
        policy = self.policies[context.group_id]
        if candidate_budget > policy.candidate_budget:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "runtime candidate budget exceeds group policy")
        if not set(context.tool_whitelist).issubset(policy.tool_whitelist):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "group context requests undeclared tools")
        return policy

    def _call_with_timeout(self, request: GroupLLMCallRequest, timeout: float) -> GroupLLMCallResponse:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.transport.complete_group_proposal, request)
        try:
            response = future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise ContractValidationError(FailureCode.PROVIDER_TIMEOUT, "research-group provider timed out") from exc
        except ContractValidationError:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        except Exception as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            raise ContractValidationError(FailureCode.ADAPTER_EXECUTION_FAILED, "research-group provider failed") from exc
        executor.shutdown(wait=True)
        if not isinstance(response, GroupLLMCallResponse):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "transport returned an invalid response envelope")
        return response

    def _validate_expressions(
        self,
        proposal: GroupProposalPayload,
        *,
        context: SelectedGroupContext,
        intent: ResearchIntent,
        policy: GroupExecutionPolicy,
    ) -> None:
        for candidate in proposal.candidates:
            result = self.translator.translate(
                CandidateDraftEnvelope(
                    campaign_hash=context.campaign_hash,
                    group_id=context.group_id,
                    candidate_id=candidate.candidate_id,
                    expression=candidate.expression,
                    requested_lookback_sessions=intent.factor_lookback_sessions,
                    declared_lookback_sessions=candidate.lookback_sessions,
                    allowed_fields=policy.allowed_fields,
                    source_draft_hash=candidate.artifact_hash,
                )
            )
            if result.status is not CandidateTranslationStatus.ACCEPTED:
                code = FailureCode.UNKNOWN_FIELD if result.status is CandidateTranslationStatus.UNSUPPORTED else FailureCode.PROVIDER_SCHEMA_REJECTED
                raise ContractValidationError(
                    code,
                    "provider expression requires bounded revision",
                    details={"translation_failure_code": result.failure_code or "UNKNOWN"},
                )

    def _fallback(self, *, context: SelectedGroupContext, intent: ResearchIntent, candidate_budget: int) -> GroupProposalPayload:
        if not self.fallback_on_unavailable:
            fail(FailureCode.ADAPTER_UNAVAILABLE, "real provider is unavailable")
        proposal = ResearchGroupLLMAdapter(
            DeterministicResearchGroupFallbackProvider(),
            selected_group_ids=self.selected_group_ids,
            provider_mode=ProviderMode.DETERMINISTIC_FALLBACK,
        ).propose(context=context, intent=intent, candidate_budget=candidate_budget)
        self._attempts[context.group_id] = GroupProposalAttempt(
            group_id=context.group_id,
            status=GroupAttemptStatus.FALLBACK_COMPLETED,
            provenance=GroupProposalProvenance.DETERMINISTIC_FALLBACK,
            provider_calls=0,
            revisions_used=0,
            tokens_used=0,
            proposal_hash=proposal.artifact_hash,
            failure_code=FailureCode.ADAPTER_UNAVAILABLE.value,
        )
        return proposal

    def _record_failure(self, group_id: str, calls: int, tokens: int, code: FailureCode) -> None:
        self._attempts[group_id] = GroupProposalAttempt(
            group_id=group_id,
            status=GroupAttemptStatus.FAILED,
            provenance=GroupProposalProvenance.REAL_LLM,
            provider_calls=calls,
            revisions_used=max(0, calls - 1),
            tokens_used=tokens,
            proposal_hash=None,
            failure_code=code.value,
        )


_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {
        "official_metric",
        "official_metrics",
        "promotion",
        "promotion_verdict",
        "sealed",
        "sealed_metrics",
        "test_metrics",
        "portfolio",
        "position",
        "positions",
        "target_weight",
        "target_weights",
        "risk_approval",
        "order",
        "orders",
        "fills",
        "ledger",
        "api_key",
        "authorization",
        "token",
        "secret",
    }
)


def _reject_authority_payload(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider keys must be strings")
            lowered = key.lower()
            if lowered in _FORBIDDEN_AUTHORITY_KEYS:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "LLM payload contains forbidden authority")
            _reject_authority_payload(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _reject_authority_payload(item)
