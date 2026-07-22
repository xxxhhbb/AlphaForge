"""Adapt bounded LLM provider output into selected DRAFT research proposals.

The adapter is deliberately thin: it repairs only safe string-array shape
errors, preserves the user's requested lookback, rejects privilege escalation,
and returns :class:`GroupProposalPayload` objects that remain DRAFT/CONCEPTUAL.
Official evidence, promotion, portfolio, risk, orders, and ledgers are outside
this package's authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Mapping, Protocol

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import fail, require_identifier, require_text
from ..llm_desks.models import FactorDraftFill, HypothesisFill, LLMDeskStatus
from ..llm_desks.safety import assert_no_forbidden_content, opaque_hash
from ..research_group_runtime import (
    DeterministicGroupProposalProvider,
    GroupCandidateDraft,
    GroupProposalPayload,
)
from ..research_group_runtime.selector import SelectedGroupContext
from ..research_os import AnswerLevel, ResearchIntent


SCHEMA_VERSION = "alphaforge.research-group-llm-adapter/v1"

_FORBIDDEN_RAW_KEYS = frozenset(
    {
        "official_metrics",
        "official_metric",
        "promotion",
        "promotion_verdict",
        "risk_approval",
        "target_weight",
        "target_weights",
        "portfolio",
        "orders",
        "order_plan",
        "fills",
        "ledger",
        "sealed",
        "sealed_metrics",
        "sealed_rank",
        "test_metrics",
        "tool_whitelist",
        "tools",
        "tool_names",
        "api_key",
        "apikey",
        "authorization",
        "token",
        "cookie",
        "secret",
    }
)
_ALLOWED_PROPOSAL_KEYS = frozenset(
    {
        "state",
        "group_id",
        "hypothesis",
        "economic_mechanism",
        "falsification_tests",
        "candidates",
        "runtime_mode",
    }
)
_ALLOWED_CANDIDATE_KEYS = frozenset(
    {"name", "expression", "variables", "lookback_sessions", "lookback_bars", "rationale"}
)
_STRING_ARRAY_SEPARATORS = re.compile(r"[,;；\n]+")


class ProviderMode(str, Enum):
    """Source class for provider output; deterministic fallback is explicit."""

    REAL_LLM = "REAL_LLM"
    DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"


@dataclass(frozen=True)
class ResearchGroupProviderRequest(CanonicalContract):
    """Provider-facing request with selected-only context and bounded budget."""

    context: SelectedGroupContext
    intent: ResearchIntent
    candidate_budget: int
    provider_mode: ProviderMode
    selected_group_ids: tuple[str, ...]
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.context, SelectedGroupContext):
            fail(FailureCode.INVALID_VALUE, "context must be SelectedGroupContext")
        if not isinstance(self.intent, ResearchIntent):
            fail(FailureCode.INVALID_VALUE, "intent must be ResearchIntent")
        if not 1 <= self.candidate_budget <= 3:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "candidate_budget must be 1..3")
        if not isinstance(self.provider_mode, ProviderMode):
            try:
                object.__setattr__(self, "provider_mode", ProviderMode(self.provider_mode))
            except ValueError as exc:
                raise ContractValidationError(
                    FailureCode.INVALID_VALUE,
                    "provider_mode must be REAL_LLM or DETERMINISTIC_FALLBACK",
                    details={"provider_mode": str(self.provider_mode)},
                ) from exc
        if not self.selected_group_ids:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "selected_group_ids are required")
        for group_id in self.selected_group_ids:
            require_identifier(group_id, "selected_group_ids")
        if self.context.group_id not in self.selected_group_ids:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "provider context was not selected")


@dataclass(frozen=True)
class RawProviderCandidate(CanonicalContract):
    """Sanitized, non-official candidate from a provider."""

    name: str
    expression: str
    variables: tuple[str, ...]
    lookback_sessions: int
    rationale: str
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_text(self.name, "name")
        require_text(self.expression, "expression")
        require_text(self.rationale, "rationale")
        if not self.variables:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "variables are required")
        for value in self.variables:
            require_identifier(value, "variables")
        if not isinstance(self.lookback_sessions, int) or self.lookback_sessions <= 0:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "lookback_sessions must be positive")
        assert_no_forbidden_content(
            {
                "name": self.name,
                "expression": self.expression,
                "variables": self.variables,
                "rationale": self.rationale,
            },
            where="raw_provider_candidate",
        )


@dataclass(frozen=True)
class RawProviderProposal(CanonicalContract):
    """Provider output after safe repair, still only DRAFT."""

    state: str
    group_id: str
    hypothesis: str
    economic_mechanism: str
    falsification_tests: tuple[str, ...]
    candidates: tuple[RawProviderCandidate, ...]
    runtime_mode: ProviderMode
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.state != "DRAFT":
            fail(FailureCode.INVALID_STATE, "provider proposals must remain DRAFT")
        require_identifier(self.group_id, "group_id")
        require_text(self.hypothesis, "hypothesis")
        require_text(self.economic_mechanism, "economic_mechanism")
        if not self.falsification_tests:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "falsification_tests are required")
        if not 1 <= len(self.candidates) <= 3:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "provider must emit one to three candidates")
        if not isinstance(self.runtime_mode, ProviderMode):
            try:
                object.__setattr__(self, "runtime_mode", ProviderMode(self.runtime_mode))
            except ValueError as exc:
                raise ContractValidationError(
                    FailureCode.INVALID_VALUE,
                    "runtime_mode must be REAL_LLM or DETERMINISTIC_FALLBACK",
                    details={"runtime_mode": str(self.runtime_mode)},
                ) from exc
        assert_no_forbidden_content(
            {
                "hypothesis": self.hypothesis,
                "economic_mechanism": self.economic_mechanism,
                "falsification_tests": self.falsification_tests,
            },
            where="raw_provider_proposal",
        )


class ResearchGroupLLMProvider(Protocol):
    """Minimal provider protocol used by the adapter."""

    provider_kind: ProviderMode

    def complete_proposal(
        self,
        request: ResearchGroupProviderRequest,
    ) -> Mapping[str, Any] | RawProviderProposal | GroupProposalPayload | tuple[HypothesisFill, tuple[FactorDraftFill, ...]]: ...


class DeterministicResearchGroupFallbackProvider:
    """Explicit offline fallback; never mislabeled as REAL_LLM."""

    provider_kind = ProviderMode.DETERMINISTIC_FALLBACK

    def __init__(self) -> None:
        self._provider = DeterministicGroupProposalProvider()

    def complete_proposal(self, request: ResearchGroupProviderRequest) -> GroupProposalPayload:
        if request.provider_mode is not ProviderMode.DETERMINISTIC_FALLBACK:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "deterministic fallback cannot claim REAL_LLM")
        return self._provider.propose(
            context=request.context,
            intent=request.intent,
            candidate_budget=request.candidate_budget,
        )


class ResearchGroupLLMAdapter:
    """Provider adapter implementing the existing GroupProposalProvider API."""

    def __init__(
        self,
        provider: ResearchGroupLLMProvider,
        *,
        selected_group_ids: tuple[str, ...],
        provider_mode: ProviderMode = ProviderMode.REAL_LLM,
    ) -> None:
        self.provider = provider
        self.selected_group_ids = selected_group_ids
        self.provider_mode = ProviderMode(provider_mode)
        provider_kind = getattr(provider, "provider_kind", self.provider_mode)
        provider_kind = ProviderMode(provider_kind)
        if provider_kind is ProviderMode.DETERMINISTIC_FALLBACK and self.provider_mode is ProviderMode.REAL_LLM:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "deterministic fallback cannot be initialized as REAL_LLM")
        if not selected_group_ids:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "selected_group_ids are required")
        for group_id in selected_group_ids:
            require_identifier(group_id, "selected_group_ids")

    def propose(
        self,
        *,
        context: SelectedGroupContext,
        intent: ResearchIntent,
        candidate_budget: int,
    ) -> GroupProposalPayload:
        request = ResearchGroupProviderRequest(
            context=context,
            intent=intent,
            candidate_budget=candidate_budget,
            provider_mode=self.provider_mode,
            selected_group_ids=self.selected_group_ids,
        )
        try:
            raw = self.provider.complete_proposal(request)
        except TimeoutError as exc:
            raise ContractValidationError(
                FailureCode.PROVIDER_TIMEOUT,
                "provider timed out",
                details={"provider_payload_hash": opaque_hash(type(exc).__name__)},
            ) from exc
        raw_proposal = self._normalize_raw(raw, request)
        return self._to_group_payload(raw_proposal, request)

    def _normalize_raw(
        self,
        raw: Mapping[str, Any] | RawProviderProposal | GroupProposalPayload | tuple[HypothesisFill, tuple[FactorDraftFill, ...]],
        request: ResearchGroupProviderRequest,
    ) -> RawProviderProposal:
        if isinstance(raw, RawProviderProposal):
            return self._validate_raw(raw, request)
        if isinstance(raw, GroupProposalPayload):
            return self._from_group_payload(raw, request)
        if _is_llm_fill_tuple(raw):
            return self._from_llm_fills(raw[0], raw[1], request)
        if isinstance(raw, Mapping):
            return self._from_mapping(raw, request)
        fail(
            FailureCode.PROVIDER_SCHEMA_REJECTED,
            "provider returned an unsupported proposal payload type",
            payload_hash=opaque_hash(raw),
        )

    def _from_mapping(self, raw: Mapping[str, Any], request: ResearchGroupProviderRequest) -> RawProviderProposal:
        _reject_forbidden(raw, where="provider_payload")
        unknown = set(raw) - _ALLOWED_PROPOSAL_KEYS
        if unknown:
            fail(FailureCode.UNKNOWN_FIELD, "provider proposal contains unknown fields", fields=tuple(sorted(unknown)))
        missing = {"state", "group_id", "hypothesis", "economic_mechanism", "falsification_tests", "candidates"} - set(raw)
        if missing:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "provider proposal is missing fields", fields=tuple(sorted(missing)))
        state = raw["state"]
        if state != "DRAFT":
            fail(FailureCode.INVALID_STATE, "provider proposals must remain DRAFT", state=str(state))
        falsification_tests = _coerce_string_array(raw["falsification_tests"], "falsification_tests")
        candidates_raw = raw["candidates"]
        if not isinstance(candidates_raw, (tuple, list)):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "candidates must be an array")
        candidates = tuple(self._candidate_from_mapping(item, request) for item in candidates_raw)
        runtime_mode = ProviderMode(raw.get("runtime_mode", self.provider_mode))
        return self._validate_raw(
            RawProviderProposal(
                state=state,
                group_id=str(raw["group_id"]),
                hypothesis=str(raw["hypothesis"]),
                economic_mechanism=str(raw["economic_mechanism"]),
                falsification_tests=falsification_tests,
                candidates=candidates,
                runtime_mode=runtime_mode,
            ),
            request,
        )

    def _candidate_from_mapping(self, raw: Any, request: ResearchGroupProviderRequest) -> RawProviderCandidate:
        if not isinstance(raw, Mapping):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "candidate must be an object")
        _reject_forbidden(raw, where="provider_candidate")
        unknown = set(raw) - _ALLOWED_CANDIDATE_KEYS
        if unknown:
            fail(FailureCode.UNKNOWN_FIELD, "candidate contains unknown fields", fields=tuple(sorted(unknown)))
        lookback_value = raw.get("lookback_sessions", raw.get("lookback_bars"))
        missing = {"name", "expression", "variables", "rationale"}
        if lookback_value is None:
            missing.add("lookback_sessions")
        missing -= set(raw)
        if missing:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "candidate is missing fields", fields=tuple(sorted(missing)))
        if type(lookback_value) is not int:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "lookback_sessions must be an integer")
        variables = _coerce_string_array(raw["variables"], "variables", identifiers=True)
        return RawProviderCandidate(
            name=str(raw["name"]),
            expression=str(raw["expression"]),
            variables=variables,
            lookback_sessions=lookback_value,
            rationale=str(raw["rationale"]),
        )

    def _from_group_payload(self, raw: GroupProposalPayload, request: ResearchGroupProviderRequest) -> RawProviderProposal:
        return self._validate_raw(
            RawProviderProposal(
                state="DRAFT",
                group_id=raw.group_id,
                hypothesis=raw.hypothesis,
                economic_mechanism=raw.economic_mechanism,
                falsification_tests=raw.falsification_tests,
                candidates=tuple(
                    RawProviderCandidate(
                        name=item.candidate_id,
                        expression=item.expression,
                        variables=item.variables,
                        lookback_sessions=item.lookback_sessions,
                        rationale=item.rationale,
                    )
                    for item in raw.candidates
                ),
                runtime_mode=self.provider_mode,
            ),
            request,
        )

    def _from_llm_fills(
        self,
        hypothesis: HypothesisFill,
        factors: tuple[FactorDraftFill, ...],
        request: ResearchGroupProviderRequest,
    ) -> RawProviderProposal:
        if hypothesis.state is not LLMDeskStatus.DRAFT:
            fail(FailureCode.INVALID_STATE, "hypothesis fill must be DRAFT")
        if hypothesis.desk_id.value != request.context.group_id:
            fail(FailureCode.INVALID_FIELD_OWNER, "hypothesis belongs to another selected group")
        if not factors:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "factor fills are required")
        candidates: list[RawProviderCandidate] = []
        for item in factors:
            if item.state is not LLMDeskStatus.DRAFT:
                fail(FailureCode.INVALID_STATE, "factor fill must be DRAFT")
            if item.desk_id.value != request.context.group_id:
                fail(FailureCode.INVALID_FIELD_OWNER, "factor belongs to another selected group")
            candidates.append(
                RawProviderCandidate(
                    name=item.name,
                    expression=item.expression,
                    variables=item.variables,
                    lookback_sessions=item.lookback_bars,
                    rationale=item.description,
                )
            )
        return self._validate_raw(
            RawProviderProposal(
                state="DRAFT",
                group_id=request.context.group_id,
                hypothesis=hypothesis.statement,
                economic_mechanism=hypothesis.economic_mechanism,
                falsification_tests=hypothesis.falsification_tests,
                candidates=tuple(candidates),
                runtime_mode=self.provider_mode,
            ),
            request,
        )

    def _validate_raw(self, raw: RawProviderProposal, request: ResearchGroupProviderRequest) -> RawProviderProposal:
        if raw.group_id != request.context.group_id:
            fail(FailureCode.INVALID_FIELD_OWNER, "provider returned another selected group's proposal")
        if raw.group_id not in request.selected_group_ids:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "provider returned an unselected group")
        if raw.runtime_mode != request.provider_mode:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "provider runtime mode does not match configured mode")
        if len(raw.candidates) > request.candidate_budget:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "provider exceeded branch candidate budget")
        expected_lookback = request.intent.factor_lookback_sessions or 20
        for item in raw.candidates:
            if item.lookback_sessions != expected_lookback:
                fail(
                    FailureCode.PROVIDER_SCHEMA_REJECTED,
                    "provider did not preserve requested lookback",
                    expected=expected_lookback,
                    actual=item.lookback_sessions,
                )
        return raw

    def _to_group_payload(self, raw: RawProviderProposal, request: ResearchGroupProviderRequest) -> GroupProposalPayload:
        candidates = tuple(
            GroupCandidateDraft(
                candidate_id=f"{request.context.group_id}_candidate_{index + 1}",
                expression=item.expression,
                variables=item.variables,
                lookback_sessions=item.lookback_sessions,
                rationale=item.rationale,
            )
            for index, item in enumerate(raw.candidates)
        )
        return GroupProposalPayload(
            group_id=raw.group_id,
            hypothesis=raw.hypothesis,
            economic_mechanism=raw.economic_mechanism,
            falsification_tests=raw.falsification_tests,
            candidates=candidates,
            answer_level=AnswerLevel.CONCEPTUAL,
        )


def _is_llm_fill_tuple(raw: Any) -> bool:
    return (
        isinstance(raw, tuple)
        and len(raw) == 2
        and isinstance(raw[0], HypothesisFill)
        and isinstance(raw[1], tuple)
        and all(isinstance(item, FactorDraftFill) for item in raw[1])
    )


def _coerce_string_array(value: Any, field_name: str, *, identifiers: bool = False) -> tuple[str, ...]:
    if isinstance(value, tuple):
        parts = value
    elif isinstance(value, list):
        parts = tuple(value)
    elif isinstance(value, str):
        if not _STRING_ARRAY_SEPARATORS.search(value):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field_name} must be an array")
        parts = tuple(part.strip() for part in _STRING_ARRAY_SEPARATORS.split(value) if part.strip())
    else:
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field_name} must be an array of strings")
    if not parts:
        fail(FailureCode.MISSING_REQUIRED_FIELD, f"{field_name} must be non-empty")
    normalized: list[str] = []
    for item in parts:
        if not isinstance(item, str) or not item.strip():
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field_name} entries must be non-empty strings")
        text = item.strip()
        assert_no_forbidden_content(text, where=field_name)
        if identifiers:
            require_identifier(text, field_name)
        normalized.append(text)
    if len(normalized) != len(set(normalized)):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates")
    return tuple(normalized)


def _reject_forbidden(value: Any, *, where: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider payload keys must be strings")
            lowered = key.lower()
            if lowered in _FORBIDDEN_RAW_KEYS or any(token in lowered for token in _FORBIDDEN_RAW_KEYS):
                fail(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    "provider payload requested forbidden authority or secrets",
                    where=where,
                    field=key,
                )
            _reject_forbidden(item, where=f"{where}.{key}")
        return
    if isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _reject_forbidden(item, where=f"{where}[{index}]")
        return
    assert_no_forbidden_content(value, where=where)
