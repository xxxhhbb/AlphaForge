"""Bounded proposal providers for the three LLM-fill slots in each Desk."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from ..contracts import CanonicalContract, ContractValidationError, FailureCode, ResearchQuestion
from ..contracts.base import (
    freeze_mapping,
    require_hash,
    require_nonempty_tuple,
    require_text,
)
from ..evidence.models import VISIBLE_SUMMARY_METRICS
from .models import DeskSpec, DeskToolRegistry


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


class IncompleteProviderResult(RuntimeError):
    """Typed signal that a provider stopped without a substantive artifact."""


@dataclass(frozen=True, kw_only=True)
class HypothesisFill(CanonicalContract):
    statement: str
    economic_mechanism: str
    expected_sign: str
    falsification_tests: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.statement, "statement")
        require_text(self.economic_mechanism, "economic_mechanism")
        if self.expected_sign not in {"POSITIVE", "NEGATIVE", "NON_MONOTONIC"}:
            _fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "expected_sign has an invalid value")
        require_nonempty_tuple(self.falsification_tests, "falsification_tests")
        if any(not isinstance(item, str) or not item for item in self.falsification_tests):
            _fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "falsification tests must be strings")


@dataclass(frozen=True, kw_only=True)
class FactorDraftFill(CanonicalContract):
    name: str
    description: str
    expression: str
    variables: tuple[str, ...]
    lookback_bars: int
    neutralization: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.name, "name")
        require_text(self.description, "description")
        require_text(self.expression, "expression")
        require_nonempty_tuple(self.variables, "variables")
        if any(not isinstance(item, str) or not item for item in (*self.variables, *self.neutralization)):
            _fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "factor variable fields must be strings")
        if isinstance(self.lookback_bars, bool) or not isinstance(self.lookback_bars, int) or self.lookback_bars < 1:
            _fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "lookback_bars must be positive")


@dataclass(frozen=True, kw_only=True)
class FeedbackEvidenceSummary(CanonicalContract):
    identity_hash: str
    visible_metrics: Mapping[str, float]

    def __post_init__(self) -> None:
        require_hash(self.identity_hash, "identity_hash")
        unknown = set(self.visible_metrics) - VISIBLE_SUMMARY_METRICS
        if unknown:
            _fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "feedback summary contains non-allowlisted evidence",
                fields=tuple(sorted(unknown)),
            )
        for key in self.visible_metrics:
            lowered = key.lower()
            if any(token in lowered for token in ("sealed", "test", "promotion")):
                _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "feedback summary exposes sealed evidence")
        object.__setattr__(
            self,
            "visible_metrics",
            freeze_mapping(self.visible_metrics, "visible_metrics", numeric_values=True),
        )


@dataclass(frozen=True, kw_only=True)
class FeedbackExplanation(CanonicalContract):
    summary_hash: str
    explanation: str

    def __post_init__(self) -> None:
        require_hash(self.summary_hash, "summary_hash")
        require_text(self.explanation, "explanation")


class ProviderSession:
    """Per-branch mutable budget counter; sessions are never shared across desks."""

    def __init__(self, desk: DeskSpec) -> None:
        self.desk_id = desk.desk_id
        self.budget = desk.budget
        self.tokens_used = 0
        self.iterations_used = 0
        self.calls: list[str] = []

    @property
    def remaining_tokens(self) -> int:
        return self.budget.max_tokens - self.tokens_used

    def begin(self, stage: str) -> None:
        if self.iterations_used >= self.budget.max_iterations:
            _fail(
                FailureCode.PROVIDER_BUDGET_EXCEEDED,
                "provider iteration budget exhausted",
                desk=self.desk_id.value,
            )
        self.iterations_used += 1
        self.calls.append(stage)

    def consume_tokens(self, tokens: int) -> None:
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            _fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider token count is invalid")
        if tokens > self.remaining_tokens:
            _fail(
                FailureCode.PROVIDER_BUDGET_EXCEEDED,
                "provider token budget exhausted",
                desk=self.desk_id.value,
            )
        self.tokens_used += tokens


class ResearchProposalProvider(Protocol):
    def propose_hypothesis(
        self,
        *,
        desk: DeskSpec,
        question: ResearchQuestion,
        tools: DeskToolRegistry,
        session: ProviderSession,
    ) -> HypothesisFill: ...

    def construct_factor(
        self,
        *,
        desk: DeskSpec,
        question: ResearchQuestion,
        hypothesis: HypothesisFill,
        tools: DeskToolRegistry,
        session: ProviderSession,
    ) -> FactorDraftFill: ...

    def explain_feedback(
        self,
        *,
        desk: DeskSpec,
        summary: FeedbackEvidenceSummary,
        session: ProviderSession,
    ) -> FeedbackExplanation: ...


_HYPOTHESES: Mapping[str, HypothesisFill] = MappingProxyType(
    {
        "fundamental": HypothesisFill(
            statement="Point-in-time book value relative to market value predicts medium-horizon returns.",
            economic_mechanism="Slow diffusion of accounting information can create valuation spreads.",
            expected_sign="POSITIVE",
            falsification_tests=("Reject when visible net evidence is non-positive.",),
        ),
        "expectations_events": HypothesisFill(
            statement="Short visible price reactions proxy changes in market expectations.",
            economic_mechanism="Underreaction to public events can persist for several sessions.",
            expected_sign="POSITIVE",
            falsification_tests=("Reject unstable visible event-proxy evidence.",),
        ),
        "market_structure": HypothesisFill(
            statement="Cross-sectional trading intensity contains a liquidity-demand signal.",
            economic_mechanism="Persistent demand imbalance can appear in volume scaled by market value.",
            expected_sign="POSITIVE",
            falsification_tests=("Reject when cost-aware evidence fails.",),
        ),
        "alternative_data": HypothesisFill(
            statement="A sanitized rolling activity proxy contains incremental attention information.",
            economic_mechanism="Sustained activity can proxy information arrival without external data.",
            expected_sign="POSITIVE",
            falsification_tests=("Reject when the offline proxy lacks stable evidence.",),
        ),
        "macro_regime": HypothesisFill(
            statement="Released macro-rate levels condition the strength of recent momentum.",
            economic_mechanism="Discount-rate regimes can amplify or attenuate continuation.",
            expected_sign="NON_MONOTONIC",
            falsification_tests=("Reject when regime stability is insufficient.",),
        ),
        "meta_alpha": HypothesisFill(
            statement="Combining distinct visible momentum horizons improves research robustness.",
            economic_mechanism="Multiple horizons can reduce dependence on one transient pattern.",
            expected_sign="POSITIVE",
            falsification_tests=("Reject when registered-factor incrementality fails.",),
        ),
    }
)


_FACTORS: Mapping[str, FactorDraftFill] = MappingProxyType(
    {
        "fundamental": FactorDraftFill(
            name="pit_book_to_market",
            description="Cross-sectional rank of point-in-time book value divided by market value.",
            expression="rank_cross_section(divide_safe(book_value, market_cap))",
            variables=("book_value", "market_cap"),
            lookback_bars=1,
        ),
        "expectations_events": FactorDraftFill(
            name="visible_event_reaction",
            description="Cross-sectional rank of visible three-session price reaction.",
            expression="rank_cross_section(pct_change(close, 3))",
            variables=("close",),
            lookback_bars=3,
        ),
        "market_structure": FactorDraftFill(
            name="trading_intensity",
            description="Cross-sectional rank of volume divided by market capitalization.",
            expression="rank_cross_section(divide_safe(volume, market_cap))",
            variables=("volume", "market_cap"),
            lookback_bars=1,
        ),
        "alternative_data": FactorDraftFill(
            name="sanitized_activity_proxy",
            description="Cross-sectional rank of a five-session rolling activity proxy.",
            expression="rank_cross_section(rolling_mean(volume, 5))",
            variables=("volume",),
            lookback_bars=5,
        ),
        "macro_regime": FactorDraftFill(
            name="macro_conditioned_momentum",
            description="Cross-sectional rank of recent return scaled by released macro rate.",
            expression="rank_cross_section(multiply(pct_change(close, 5), macro_rate))",
            variables=("close", "macro_rate"),
            lookback_bars=5,
        ),
        "meta_alpha": FactorDraftFill(
            name="multi_horizon_momentum",
            description="Cross-sectional rank of the sum of five- and twenty-session returns.",
            expression="rank_cross_section(add(pct_change(close, 20), pct_change(close, 5)))",
            variables=("close",),
            lookback_bars=20,
        ),
    }
)


class DeterministicProposalProvider:
    """Default dependency-free provider with fixed static candidate templates."""

    def propose_hypothesis(
        self,
        *,
        desk: DeskSpec,
        question: ResearchQuestion,
        tools: DeskToolRegistry,
        session: ProviderSession,
    ) -> HypothesisFill:
        session.begin("hypothesis_propose")
        tools.call(desk.tool_whitelist[0])
        session.consume_tokens(1)
        return _HYPOTHESES[desk.desk_id.value]

    def construct_factor(
        self,
        *,
        desk: DeskSpec,
        question: ResearchQuestion,
        hypothesis: HypothesisFill,
        tools: DeskToolRegistry,
        session: ProviderSession,
    ) -> FactorDraftFill:
        session.begin("factor_construct")
        tools.call(desk.tool_whitelist[-1])
        session.consume_tokens(1)
        return _FACTORS[desk.desk_id.value]

    def explain_feedback(
        self,
        *,
        desk: DeskSpec,
        summary: FeedbackEvidenceSummary,
        session: ProviderSession,
    ) -> FeedbackExplanation:
        session.begin("feedback")
        session.consume_tokens(1)
        return FeedbackExplanation(
            summary_hash=summary.artifact_hash,
            explanation=(
                f"{desk.desk_id.value} reviewed only allowlisted visible evidence; "
                "the mechanical Evidence Court retains decision authority."
            ),
        )


@dataclass(frozen=True, kw_only=True)
class LLMJsonResponse:
    payload: Mapping[str, Any]
    tokens_used: int


class LLMJsonClient(Protocol):
    def complete_json(
        self, *, stage: str, prompt: str, timeout_seconds: float, max_tokens: int
    ) -> LLMJsonResponse: ...


def _exact_mapping(payload: Mapping[str, Any], fields: frozenset[str], stage: str) -> None:
    if not isinstance(payload, Mapping) or set(payload) != fields:
        _fail(
            FailureCode.PROVIDER_SCHEMA_REJECTED,
            "LLM provider returned an inexact schema",
            stage=stage,
            expected=tuple(sorted(fields)),
        )


def _tuple_of_text(value: Any, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        _fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field} must be an array of strings")
    if not allow_empty and not value:
        _fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{field} cannot be empty")
    return tuple(value)


class OptionalLLMProposalProvider:
    """Opt-in bounded JSON provider; it has no evidence, PIT, tool, or promotion authority."""

    def __init__(self, *, client: LLMJsonClient | None = None) -> None:
        self._client = client

    def _call(
        self,
        *,
        stage: str,
        prompt: str,
        session: ProviderSession,
    ) -> Mapping[str, Any]:
        if os.environ.get("ENABLE_LLM_RESEARCH") != "1":
            _fail(FailureCode.ADAPTER_UNAVAILABLE, "optional LLM research provider is disabled")
        if self._client is None:
            _fail(FailureCode.ADAPTER_UNAVAILABLE, "no LLM JSON client is configured")
        last_error: ContractValidationError | None = None
        for _ in range(session.budget.max_retries + 1):
            session.begin(stage)
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                self._client.complete_json,
                stage=stage,
                prompt=prompt,
                timeout_seconds=float(session.budget.timeout_seconds),
                max_tokens=session.remaining_tokens,
            )
            try:
                response = future.result(timeout=float(session.budget.timeout_seconds))
            except FutureTimeout:
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                last_error = ContractValidationError(
                    FailureCode.PROVIDER_TIMEOUT,
                    "LLM proposal provider timed out",
                    details={"stage": stage},
                )
                continue
            except Exception as exc:
                executor.shutdown(wait=False, cancel_futures=True)
                last_error = ContractValidationError(
                    FailureCode.ADAPTER_EXECUTION_FAILED,
                    f"LLM JSON client failed: {type(exc).__name__}",
                    details={"stage": stage},
                )
                continue
            executor.shutdown(wait=True)
            if not isinstance(response, LLMJsonResponse):
                last_error = ContractValidationError(
                    FailureCode.PROVIDER_SCHEMA_REJECTED,
                    "LLM client returned an invalid response envelope",
                )
                continue
            session.consume_tokens(response.tokens_used)
            return response.payload
        assert last_error is not None
        raise last_error

    def propose_hypothesis(
        self,
        *,
        desk: DeskSpec,
        question: ResearchQuestion,
        tools: DeskToolRegistry,
        session: ProviderSession,
    ) -> HypothesisFill:
        prompt = json.dumps(
            {
                "slot": "hypothesis_only",
                "desk": desk.desk_id.value,
                "role": desk.role,
                "context": desk.context,
                "question": question.text,
                "universe": question.universe,
                "horizon": question.horizon,
                "allowed_tools": tools.names,
            },
            sort_keys=True,
        )
        payload = self._call(stage="hypothesis_propose", prompt=prompt, session=session)
        fields = frozenset(
            {"statement", "economic_mechanism", "expected_sign", "falsification_tests"}
        )
        _exact_mapping(payload, fields, "hypothesis_propose")
        return HypothesisFill(
            statement=payload["statement"],
            economic_mechanism=payload["economic_mechanism"],
            expected_sign=payload["expected_sign"],
            falsification_tests=_tuple_of_text(payload["falsification_tests"], "falsification_tests"),
        )

    def construct_factor(
        self,
        *,
        desk: DeskSpec,
        question: ResearchQuestion,
        hypothesis: HypothesisFill,
        tools: DeskToolRegistry,
        session: ProviderSession,
    ) -> FactorDraftFill:
        prompt = json.dumps(
            {
                "slot": "factor_draft_only",
                "desk": desk.desk_id.value,
                "hypothesis": hypothesis.to_dict(),
                "allowed_tools": tools.names,
            },
            sort_keys=True,
        )
        payload = self._call(stage="factor_construct", prompt=prompt, session=session)
        fields = frozenset(
            {"name", "description", "expression", "variables", "lookback_bars", "neutralization"}
        )
        _exact_mapping(payload, fields, "factor_construct")
        return FactorDraftFill(
            name=payload["name"],
            description=payload["description"],
            expression=payload["expression"],
            variables=_tuple_of_text(payload["variables"], "variables"),
            lookback_bars=payload["lookback_bars"],
            neutralization=_tuple_of_text(payload["neutralization"], "neutralization", allow_empty=True),
        )

    def explain_feedback(
        self,
        *,
        desk: DeskSpec,
        summary: FeedbackEvidenceSummary,
        session: ProviderSession,
    ) -> FeedbackExplanation:
        prompt = json.dumps(
            {
                "slot": "feedback_explanation_only",
                "desk": desk.desk_id.value,
                "visible_summary": summary.to_dict(),
            },
            sort_keys=True,
        )
        payload = self._call(stage="feedback", prompt=prompt, session=session)
        _exact_mapping(payload, frozenset({"explanation"}), "feedback")
        return FeedbackExplanation(
            summary_hash=summary.artifact_hash,
            explanation=payload["explanation"],
        )
