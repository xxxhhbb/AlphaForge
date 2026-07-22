"""Provider abstractions for bounded LLM Research Desks."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import require_finite_number
from .models import (
    DeskId,
    DeskLLMConfig,
    ExpectedSign,
    FactorDraftFill,
    FeedbackExplanation,
    HypothesisFill,
    LLMDeskStage,
    LLMDeskStatus,
    ProviderBudget,
    ProviderSession,
    RedactedEvidenceSummary,
    ResearchQuestionView,
    coerce_desk_id,
)
from .react import BoundedReActExplorer
from .safety import assert_no_forbidden_content, fail, opaque_hash
from .tools import ReadOnlyToolRegistry


class ProposalProvider(Protocol):
    def propose_hypothesis(
        self,
        *,
        desk: DeskLLMConfig,
        question: ResearchQuestionView,
        tools: ReadOnlyToolRegistry,
        session: ProviderSession,
        explorer: BoundedReActExplorer | None = None,
    ) -> HypothesisFill: ...

    def draft_factor(
        self,
        *,
        desk: DeskLLMConfig,
        question: ResearchQuestionView,
        hypothesis: HypothesisFill,
        tools: ReadOnlyToolRegistry,
        session: ProviderSession,
        explorer: BoundedReActExplorer | None = None,
    ) -> FactorDraftFill: ...

    def explain_feedback(
        self,
        *,
        desk: DeskLLMConfig,
        summary: RedactedEvidenceSummary,
        session: ProviderSession,
    ) -> FeedbackExplanation: ...


_HYPOTHESES: Mapping[DeskId, tuple[str, str, ExpectedSign, tuple[str, ...]]] = MappingProxyType(
    {
        DeskId.FUNDAMENTAL: (
            "Visible valuation spreads may forecast next-period cross-sectional returns.",
            "Slow adjustment to released accounting information can leave temporary valuation gaps.",
            ExpectedSign.POSITIVE,
            ("Reject if net validation evidence is weak after costs.",),
        ),
        DeskId.EXPECTATIONS_EVENTS: (
            "Visible event reactions may underreact across a short horizon.",
            "Public expectation shocks can diffuse gradually after timestamped events.",
            ExpectedSign.POSITIVE,
            ("Reject if event-proxy stability is not visible in validation.",),
        ),
        DeskId.MARKET_STRUCTURE: (
            "Trading intensity may contain a visible demand-pressure proxy.",
            "High activity scaled by size can proxy persistent demand imbalance.",
            ExpectedSign.POSITIVE,
            ("Reject if cost-aware validation turns negative.",),
        ),
        DeskId.ALTERNATIVE_DATA: (
            "A sanitized activity proxy may add incremental attention information.",
            "Coverage-adjusted activity can proxy public information arrival.",
            ExpectedSign.POSITIVE,
            ("Reject if coverage drift dominates validation.",),
        ),
        DeskId.MACRO_REGIME: (
            "Released macro regimes may condition recent return continuation.",
            "Discount-rate regimes can change the payoff to recent momentum.",
            ExpectedSign.NON_MONOTONIC,
            ("Reject if regime slices are unstable.",),
        ),
        DeskId.META_ALPHA: (
            "Combining visible horizons may reduce dependence on one noisy pattern.",
            "A balanced visible composite can improve robustness over one horizon.",
            ExpectedSign.POSITIVE,
            ("Reject if registered-factor incrementality is low.",),
        ),
    }
)

_FACTORS: Mapping[DeskId, tuple[str, str, str, tuple[str, ...], int, tuple[str, ...]]] = MappingProxyType(
    {
        DeskId.FUNDAMENTAL: (
            "llm_book_to_market_draft",
            "Rank released book value over market capitalization.",
            "rank_cross_section(divide_safe(book_value, market_cap))",
            ("book_value", "market_cap"),
            1,
            (),
        ),
        DeskId.EXPECTATIONS_EVENTS: (
            "llm_event_reaction_draft",
            "Rank a visible three-session event reaction proxy.",
            "rank_cross_section(pct_change(close, 3))",
            ("close",),
            3,
            (),
        ),
        DeskId.MARKET_STRUCTURE: (
            "llm_trading_intensity_draft",
            "Rank visible volume scaled by market capitalization.",
            "rank_cross_section(divide_safe(volume, market_cap))",
            ("volume", "market_cap"),
            1,
            (),
        ),
        DeskId.ALTERNATIVE_DATA: (
            "llm_activity_proxy_draft",
            "Rank a five-period activity proxy from offline visible fields.",
            "rank_cross_section(rolling_mean(activity_proxy, 5))",
            ("activity_proxy",),
            5,
            (),
        ),
        DeskId.MACRO_REGIME: (
            "llm_macro_conditioned_momentum_draft",
            "Rank recent return scaled by a released macro field.",
            "rank_cross_section(multiply(pct_change(close, 5), macro_rate))",
            ("close", "macro_rate"),
            5,
            (),
        ),
        DeskId.META_ALPHA: (
            "llm_multi_horizon_momentum_draft",
            "Rank the sum of two visible return horizons.",
            "rank_cross_section(add(pct_change(close, 20), pct_change(close, 5)))",
            ("close",),
            20,
            (),
        ),
    }
)


class DeterministicProposalProvider:
    """Default offline provider; it never calls a network client or reads secrets."""

    def propose_hypothesis(
        self,
        *,
        desk: DeskLLMConfig,
        question: ResearchQuestionView,
        tools: ReadOnlyToolRegistry,
        session: ProviderSession,
        explorer: BoundedReActExplorer | None = None,
    ) -> HypothesisFill:
        stage = LLMDeskStage.HYPOTHESIS
        session.reserve_call(stage)
        react = (explorer or BoundedReActExplorer()).explore(
            stage=stage,
            prompt_material={"desk": desk.desk_id.value, "question_hash": question.artifact_hash},
            tools=tools,
            session=session,
        )
        statement, mechanism, sign, tests = _HYPOTHESES[desk.desk_id]
        output = HypothesisFill(
            state=LLMDeskStatus.DRAFT,
            desk_id=desk.desk_id,
            statement=statement,
            economic_mechanism=mechanism,
            expected_sign=sign,
            universe=question.universe,
            horizon=question.horizon,
            decision_time=question.decision_time,
            availability_assumptions=question.availability_assumptions,
            falsification_tests=tests,
            react_context_hashes=(react.context_hash,),
        )
        session.record_success(
            stage=stage,
            prompt_material={"desk": desk.desk_id.value, "slot": stage.value, "question_hash": question.artifact_hash},
            output=output,
            tokens_in=react.tokens_used,
            tokens_out=1,
            cost=0.0,
            parameters={"provider": "deterministic", "model": desk.model},
        )
        return output

    def draft_factor(
        self,
        *,
        desk: DeskLLMConfig,
        question: ResearchQuestionView,
        hypothesis: HypothesisFill,
        tools: ReadOnlyToolRegistry,
        session: ProviderSession,
        explorer: BoundedReActExplorer | None = None,
    ) -> FactorDraftFill:
        stage = LLMDeskStage.FACTOR_DRAFT
        session.reserve_call(stage)
        react = (explorer or BoundedReActExplorer()).explore(
            stage=stage,
            prompt_material={"desk": desk.desk_id.value, "hypothesis_hash": hypothesis.artifact_hash},
            tools=tools,
            session=session,
        )
        name, description, expression, variables, lookback, neutralization = _FACTORS[desk.desk_id]
        output = FactorDraftFill(
            state=LLMDeskStatus.DRAFT,
            desk_id=desk.desk_id,
            name=name,
            description=description,
            expression=expression,
            variables=variables,
            lookback_bars=lookback,
            neutralization=neutralization,
            source_hypothesis_hash=hypothesis.artifact_hash,
            react_context_hashes=(react.context_hash,),
        )
        session.record_success(
            stage=stage,
            prompt_material={"desk": desk.desk_id.value, "slot": stage.value, "hypothesis_hash": hypothesis.artifact_hash},
            output=output,
            tokens_in=react.tokens_used,
            tokens_out=1,
            cost=0.0,
            parameters={"provider": "deterministic", "model": desk.model},
        )
        return output

    def explain_feedback(
        self,
        *,
        desk: DeskLLMConfig,
        summary: RedactedEvidenceSummary,
        session: ProviderSession,
    ) -> FeedbackExplanation:
        stage = LLMDeskStage.REDACTED_FEEDBACK
        session.reserve_call(stage)
        output = FeedbackExplanation(
            state=LLMDeskStatus.DRAFT,
            desk_id=desk.desk_id,
            summary_hash=summary.artifact_hash,
            explanation="Reviewed redacted visible evidence; deterministic services retain all metric authority.",
        )
        session.record_success(
            stage=stage,
            prompt_material={"desk": desk.desk_id.value, "slot": stage.value, "summary_hash": summary.artifact_hash},
            output=output,
            tokens_in=1,
            tokens_out=1,
            cost=0.0,
            parameters={"provider": "deterministic", "model": desk.model},
        )
        return output


@dataclass(frozen=True, kw_only=True)
class OpenAICompatibleConfig:
    model: str
    temperature: float = 0.0
    top_p: float = 1.0
    response_format: str = "json_object"
    non_sensitive_params: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            fail(FailureCode.INVALID_VALUE, "model must be a non-empty string")
        require_finite_number(self.temperature, "temperature")
        require_finite_number(self.top_p, "top_p")
        params = dict(self.non_sensitive_params or {})
        assert_no_forbidden_content(params, where="provider_config")
        object.__setattr__(self, "non_sensitive_params", MappingProxyType(params))


@dataclass(frozen=True, kw_only=True)
class OpenAICompatibleResponse:
    payload: Mapping[str, Any]
    input_tokens: int
    output_tokens: int
    cost: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider response payload must be a mapping")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{name} must be a non-negative integer")
        require_finite_number(self.cost, "cost")
        if self.cost < 0:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "cost must be non-negative")


class OpenAICompatibleClient(Protocol):
    def complete_json(
        self,
        *,
        messages: tuple[Mapping[str, str], ...],
        model: str,
        timeout_seconds: float,
        max_tokens: int,
        temperature: float,
        top_p: float,
        response_format: str,
        extra: Mapping[str, Any],
    ) -> OpenAICompatibleResponse: ...


def _exact_payload(payload: Mapping[str, Any], fields: frozenset[str], stage: LLMDeskStage) -> None:
    if not isinstance(payload, Mapping) or set(payload) != fields:
        fail(
            FailureCode.PROVIDER_SCHEMA_REJECTED,
            "provider returned an inexact schema",
            stage=stage.value,
            expected=tuple(sorted(fields)),
        )
    assert_no_forbidden_content(payload, where=f"provider.{stage.value}")


def _tuple_of_text(value: Any, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field} must be an array of strings")
    if not allow_empty and not value:
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field} must be non-empty")
    return tuple(value)


def _normalize_payload_shape(payload: Mapping[str, Any], stage: LLMDeskStage) -> Mapping[str, Any]:
    """Normalize harmless JSON shape drift while preserving fail-closed semantics.

    Real OpenAI-compatible models occasionally return a single string for
    fields whose contract is ``array<string>``.  Only these inert text-list
    fields are normalized here; authority-bearing fields, executable content,
    extra keys, official metrics, promotion, orders, and sealed references are
    still rejected by the surrounding validators.
    """

    normalized = dict(payload)
    if stage is LLMDeskStage.HYPOTHESIS:
        normalized["expected_sign"] = _normalize_expected_sign(normalized.get("expected_sign"))
        normalized["falsification_tests"] = _normalize_text_array(
            normalized.get("falsification_tests"),
            "falsification_tests",
            allow_empty=False,
            split_delimited=False,
        )
    elif stage is LLMDeskStage.FACTOR_DRAFT:
        normalized["variables"] = _normalize_text_array(
            normalized.get("variables"),
            "variables",
            allow_empty=False,
            split_delimited=True,
        )
        normalized["neutralization"] = _normalize_text_array(
            normalized.get("neutralization"),
            "neutralization",
            allow_empty=True,
            split_delimited=True,
        )
        normalized["lookback_bars"] = _normalize_positive_int_shape(normalized.get("lookback_bars"))
    return MappingProxyType(normalized)


def _validate_normalized_payload(payload: Mapping[str, Any], stage: LLMDeskStage) -> None:
    """Validate retryable response semantics before recording provider success."""

    if stage is LLMDeskStage.HYPOTHESIS:
        for field in ("statement", "economic_mechanism"):
            if not isinstance(payload.get(field), str) or not str(payload[field]).strip():
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field} must be a non-empty string")
        if payload.get("expected_sign") not in {item.value for item in ExpectedSign}:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "expected_sign has an invalid value")
        _tuple_of_text(payload.get("falsification_tests"), "falsification_tests")
        return
    if stage is LLMDeskStage.FACTOR_DRAFT:
        for field in ("name", "description", "expression"):
            if not isinstance(payload.get(field), str) or not str(payload[field]).strip():
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field} must be a non-empty string")
        _tuple_of_text(payload.get("variables"), "variables")
        _tuple_of_text(payload.get("neutralization"), "neutralization", allow_empty=True)
        lookback = payload.get("lookback_bars")
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "lookback_bars must be a positive integer")
        return
    if stage is LLMDeskStage.REDACTED_FEEDBACK:
        if not isinstance(payload.get("explanation"), str) or not str(payload["explanation"]).strip():
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "explanation must be a non-empty string")


def _normalize_expected_sign(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return value.strip().upper().replace("-", "_").replace(" ", "_")


def _normalize_positive_int_shape(value: Any) -> Any:
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


def _normalize_text_array(value: Any, field: str, *, allow_empty: bool, split_delimited: bool) -> Any:
    if isinstance(value, list):
        return value
    if value is None and allow_empty:
        return []
    if not isinstance(value, str):
        return value
    text = value.strip()
    if allow_empty and text.lower() in {"", "none", "null", "n/a", "na", "[]"}:
        return []
    if split_delimited:
        delimited = text
        for separator in ("，", "；", ";", "\n"):
            delimited = delimited.replace(separator, ",")
        parts = [part.strip() for part in delimited.split(",") if part.strip()]
        if parts:
            return parts
    if text:
        return [text]
    return [] if allow_empty else value


class OpenAICompatibleProvider:
    """Injected OpenAI-compatible JSON provider with no environment discovery."""

    def __init__(self, *, client: OpenAICompatibleClient, config: OpenAICompatibleConfig) -> None:
        self.client = client
        self.config = config

    def _call_json(
        self,
        *,
        stage: LLMDeskStage,
        messages: tuple[Mapping[str, str], ...],
        session: ProviderSession,
        expected_fields: frozenset[str],
    ) -> Mapping[str, Any]:
        last_error: ContractValidationError | None = None
        prompt_material = {"stage": stage.value, "messages": tuple(dict(item) for item in messages)}
        for _ in range(session.budget.max_retries + 1):
            try:
                session.reserve_call(stage)
            except ContractValidationError:
                raise
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                self.client.complete_json,
                messages=messages,
                model=self.config.model,
                timeout_seconds=float(session.budget.timeout_seconds),
                max_tokens=session.remaining_tokens,
                temperature=float(self.config.temperature),
                top_p=float(self.config.top_p),
                response_format=self.config.response_format,
                extra=dict(self.config.non_sensitive_params or {}),
            )
            try:
                response = future.result(timeout=float(session.budget.timeout_seconds))
            except FutureTimeout:
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                last_error = ContractValidationError(
                    FailureCode.PROVIDER_TIMEOUT,
                    "provider timed out",
                    details={"stage": stage.value},
                )
                session.record_failure(
                    stage=stage,
                    prompt_material=prompt_material,
                    output_hash=None,
                    failure_code=FailureCode.PROVIDER_TIMEOUT,
                    parameters=self._trace_params(),
                )
            except ContractValidationError as exc:
                executor.shutdown(wait=False, cancel_futures=True)
                last_error = exc
                session.record_failure(
                    stage=stage,
                    prompt_material=prompt_material,
                    output_hash=None,
                    failure_code=exc.code,
                    parameters=self._trace_params(),
                )
            except Exception:
                executor.shutdown(wait=False, cancel_futures=True)
                last_error = ContractValidationError(
                    FailureCode.ADAPTER_EXECUTION_FAILED,
                    "provider client failed",
                    details={"stage": stage.value},
                )
                session.record_failure(
                    stage=stage,
                    prompt_material=prompt_material,
                    output_hash=None,
                    failure_code=FailureCode.ADAPTER_EXECUTION_FAILED,
                    parameters=self._trace_params(),
                )
            else:
                executor.shutdown(wait=True)
                if not isinstance(response, OpenAICompatibleResponse):
                    last_error = ContractValidationError(
                        FailureCode.PROVIDER_SCHEMA_REJECTED,
                        "client returned an invalid response envelope",
                    )
                    session.record_failure(
                        stage=stage,
                        prompt_material=prompt_material,
                        output_hash=opaque_hash(response),
                        failure_code=FailureCode.PROVIDER_SCHEMA_REJECTED,
                        parameters=self._trace_params(),
                    )
                    continue
                try:
                    assert_no_forbidden_content(response.payload, where=f"provider.{stage.value}")
                    _exact_payload(response.payload, expected_fields, stage)
                    payload = _normalize_payload_shape(response.payload, stage)
                    assert_no_forbidden_content(payload, where=f"provider.{stage.value}.normalized")
                    _validate_normalized_payload(payload, stage)
                except ContractValidationError as exc:
                    last_error = exc
                    session.record_failure(
                        stage=stage,
                        prompt_material=prompt_material,
                        output_hash=opaque_hash(response.payload),
                        failure_code=exc.code,
                        parameters=self._trace_params(),
                    )
                    continue
                session.record_success(
                    stage=stage,
                    prompt_material=prompt_material,
                    output=payload,
                    tokens_in=response.input_tokens,
                    tokens_out=response.output_tokens,
                    cost=float(response.cost),
                    parameters=self._trace_params(),
                )
                return payload
        if last_error is not None:
            raise last_error
        fail(FailureCode.ADAPTER_EXECUTION_FAILED, "provider call failed without a typed error")

    def propose_hypothesis(
        self,
        *,
        desk: DeskLLMConfig,
        question: ResearchQuestionView,
        tools: ReadOnlyToolRegistry,
        session: ProviderSession,
        explorer: BoundedReActExplorer | None = None,
    ) -> HypothesisFill:
        stage = LLMDeskStage.HYPOTHESIS
        react = (explorer or BoundedReActExplorer()).explore(
            stage=stage,
            prompt_material={"desk": desk.desk_id.value, "question_hash": question.artifact_hash},
            tools=tools,
            session=session,
        )
        payload = self._call_json(
            stage=stage,
            session=session,
            expected_fields=frozenset({"statement", "economic_mechanism", "expected_sign", "falsification_tests"}),
            messages=self._messages(
                slot=stage.value,
                desk=desk,
                material={"question_hash": question.artifact_hash, "react_hash": react.context_hash},
            ),
        )
        return HypothesisFill(
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
        hypothesis: HypothesisFill,
        tools: ReadOnlyToolRegistry,
        session: ProviderSession,
        explorer: BoundedReActExplorer | None = None,
    ) -> FactorDraftFill:
        stage = LLMDeskStage.FACTOR_DRAFT
        react = (explorer or BoundedReActExplorer()).explore(
            stage=stage,
            prompt_material={"desk": desk.desk_id.value, "hypothesis_hash": hypothesis.artifact_hash},
            tools=tools,
            session=session,
        )
        payload = self._call_json(
            stage=stage,
            session=session,
            expected_fields=frozenset({"name", "description", "expression", "variables", "lookback_bars", "neutralization"}),
            messages=self._messages(
                slot=stage.value,
                desk=desk,
                material={"hypothesis_hash": hypothesis.artifact_hash, "react_hash": react.context_hash},
            ),
        )
        return FactorDraftFill(
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

    def explain_feedback(
        self,
        *,
        desk: DeskLLMConfig,
        summary: RedactedEvidenceSummary,
        session: ProviderSession,
    ) -> FeedbackExplanation:
        stage = LLMDeskStage.REDACTED_FEEDBACK
        payload = self._call_json(
            stage=stage,
            session=session,
            expected_fields=frozenset({"explanation"}),
            messages=self._messages(
                slot=stage.value,
                desk=desk,
                material={"summary_hash": summary.artifact_hash},
            ),
        )
        return FeedbackExplanation(
            state=LLMDeskStatus.DRAFT,
            desk_id=desk.desk_id,
            summary_hash=summary.artifact_hash,
            explanation=payload["explanation"],
        )

    def _trace_params(self) -> Mapping[str, Any]:
        return {
            "provider": "openai_compatible",
            "model": self.config.model,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "response_format": self.config.response_format,
            "extra_keys": tuple(sorted((self.config.non_sensitive_params or {}).keys())),
        }

    def _messages(
        self,
        *,
        slot: str,
        desk: DeskLLMConfig,
        material: Mapping[str, Any],
    ) -> tuple[Mapping[str, str], ...]:
        system = {
            "role": "system",
            "content": json.dumps(
                {
                    "authority": "draft_only",
                    "slot": slot,
                    "desk": desk.desk_id.value,
                    "schema": "strict_json",
                    "fixed_stage_machine": True,
                },
                sort_keys=True,
            ),
        }
        user = {
            "role": "user",
            "content": json.dumps(material, sort_keys=True),
        }
        return (system, user)


def budget_for_provider(config: OpenAICompatibleConfig, *, max_tokens: int = 2048) -> ProviderBudget:
    return ProviderBudget(model=config.model, max_tokens=max_tokens)
