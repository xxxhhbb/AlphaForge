"""Fail-closed adapters into the existing ShadowReplay provider ports."""

from __future__ import annotations

from dataclasses import replace
from math import isfinite
from types import MappingProxyType
from typing import Any, Callable, Mapping

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.data import canonical_content_hash
from veriquant.shadow_portfolio.models import DataVintage, ShadowRunRequest
from veriquant.shadow_replay.models import (
    ReplayCandidateState,
    ReplayChallengerCandidate,
    ShadowRunOnceResult,
    ShadowReplayPolicy,
)

from .models import FrozenDailyVintage, FrozenVintageBinding
from .protocols import (
    EligibleChallengerProvider,
    FrozenVintageMetadataProvider,
    FrozenVintageProvider,
    MarketVintageReader,
)


def _fail(code: FailureCode, message: str, **details: object) -> None:
    raise ContractValidationError(code, message, details=details)


class FrozenVintagePortAdapter:
    """Validate exactly 20 source sessions and bind provenance into DataVintage."""

    def __init__(self, provider: FrozenVintageProvider) -> None:
        self._provider = provider
        self._source_by_adapted_id: dict[str, FrozenDailyVintage] = {}

    def list_vintages(self, policy: ShadowReplayPolicy) -> tuple[DataVintage, ...]:
        if policy.trading_days != 20:
            _fail(FailureCode.INVALID_VALUE, "real-vintage replay requires exactly 20 sessions")
        source = self._provider.list_frozen_vintages(policy)
        if not isinstance(source, tuple):
            _fail(FailureCode.INVALID_VALUE, "frozen vintage provider must return a tuple")
        if len(source) != 20:
            _fail(
                FailureCode.INCOMPLETE_RESULT,
                "frozen vintage provider must return exactly 20 sessions",
                available=len(source),
            )
        if any(not isinstance(item, FrozenDailyVintage) for item in source):
            _fail(FailureCode.INVALID_VALUE, "provider returned an invalid frozen vintage")
        ordered = tuple(sorted(source, key=lambda item: (item.vintage.decision_time, item.snapshot_id)))
        sessions = tuple(item.session_date for item in ordered)
        if len(set(sessions)) != 20:
            _fail(FailureCode.INVALID_VALUE, "replay sessions must be 20 distinct trading dates")
        adapted: list[DataVintage] = []
        adapted_sources: dict[str, FrozenDailyVintage] = {}
        previous_decision = None
        for item in ordered:
            _validate_required_shadow_inputs(item.vintage)
            decision_time = item.vintage.decision_time
            if previous_decision is not None and decision_time <= previous_decision:
                _fail(FailureCode.INVALID_TIME_ORDER, "frozen vintages must be strictly time ordered")
            previous_decision = decision_time
            bound_dataset_hash = canonical_content_hash(
                {
                    "source_snapshot_hash": item.snapshot_hash,
                    "source_manifest_hash": item.manifest_hash,
                    "calendar_hash": item.calendar_hash,
                    "universe_hash": item.universe_hash,
                    "revision_available_time": item.revision_available_time,
                    "vintage_hash": item.vintage.artifact_hash,
                }
            )
            suffix = item.artifact_hash.removeprefix("sha256:")[:20]
            adapted_id = f"frozen-{item.session_date}-{suffix}"
            projected = replace(
                item.vintage,
                vintage_id=adapted_id,
                dataset_hash=bound_dataset_hash,
            )
            if adapted_id in adapted_sources:
                _fail(FailureCode.ARTIFACT_COLLISION, "adapted vintage identity collision")
            adapted.append(projected)
            adapted_sources[adapted_id] = item
        self._source_by_adapted_id = adapted_sources
        return tuple(adapted)

    def source_for(self, vintage: DataVintage) -> FrozenDailyVintage:
        source = self._source_by_adapted_id.get(vintage.vintage_id)
        if source is None:
            _fail(FailureCode.INVALID_HASH, "challenger lookup used an unknown adapted vintage")
        return source


class MarketVintageFrozenProvider:
    """Build 20 frozen daily replay vintages from a PIT MarketVintage reader."""

    def __init__(
        self,
        *,
        reader: MarketVintageReader,
        bindings: tuple[FrozenVintageBinding, ...],
        metadata_provider: FrozenVintageMetadataProvider,
    ) -> None:
        if not isinstance(bindings, tuple) or not bindings:
            _fail(FailureCode.MISSING_REQUIRED_FIELD, "frozen vintage bindings are required")
        ordered = tuple(sorted(bindings, key=lambda item: (item.decision_time, item.snapshot_id, item.revision_id)))
        identities = {(item.snapshot_id, item.decision_time, item.revision_id) for item in ordered}
        if len(identities) != len(ordered):
            _fail(FailureCode.ARTIFACT_COLLISION, "duplicate frozen vintage binding")
        self._reader = reader
        self._bindings = ordered
        self._metadata_provider = metadata_provider

    def list_frozen_vintages(self, policy: ShadowReplayPolicy) -> tuple[FrozenDailyVintage, ...]:
        if policy.trading_days != 20:
            _fail(FailureCode.INVALID_VALUE, "real-vintage replay requires exactly 20 sessions")
        if len(self._bindings) != 20:
            _fail(
                FailureCode.INCOMPLETE_RESULT,
                "real-vintage replay requires exactly 20 frozen bindings",
                available=len(self._bindings),
            )
        output: list[FrozenDailyVintage] = []
        for binding in self._bindings:
            handle = self._reader.open(binding.manifest_path)
            vintage = self._reader.market_vintage(handle, decision_time=binding.decision_time)
            rows = tuple(getattr(vintage, "rows", ()))
            if not rows:
                _fail(FailureCode.INCOMPLETE_RESULT, "market vintage produced no PIT-visible rows")
            future_rows = tuple(
                getattr(row, "symbol", "<unknown>")
                for row in rows
                if getattr(row, "available_at", binding.decision_time) > binding.decision_time
            )
            if future_rows:
                _fail(
                    FailureCode.INVALID_TIME_ORDER,
                    "market vintage includes rows unavailable at decision time",
                    symbols=future_rows,
                )
            metadata = self._metadata_provider.metadata_for(binding=binding, vintage=vintage)
            if metadata.corporate_action_available_time > binding.decision_time:
                _fail(FailureCode.INVALID_TIME_ORDER, "corporate-action metadata is unavailable at decision time")
            symbols = tuple(sorted({row.symbol for row in rows}))
            coverage_by_symbol = {
                symbol: _row_coverage(tuple(row for row in rows if row.symbol == symbol))
                for symbol in symbols
            }
            data_available_time = max(row.available_at for row in rows)
            projected = DataVintage(
                vintage_id=_market_vintage_id(binding, vintage),
                revision_id=binding.revision_id,
                dataset_hash=getattr(vintage, "market_state_hash"),
                universe=symbols,
                data_available_time=data_available_time,
                decision_time=binding.decision_time,
                corporate_action_available_time=metadata.corporate_action_available_time,
                coverage_by_symbol=coverage_by_symbol,
                beta_by_symbol={symbol: metadata.beta_by_symbol[symbol] for symbol in symbols if symbol in metadata.beta_by_symbol},
                industry_by_symbol={
                    symbol: metadata.industry_by_symbol[symbol]
                    for symbol in symbols
                    if symbol in metadata.industry_by_symbol
                },
                adv_by_symbol={symbol: metadata.adv_by_symbol[symbol] for symbol in symbols if symbol in metadata.adv_by_symbol},
            )
            frozen = FrozenDailyVintage(
                snapshot_id=binding.snapshot_id,
                session_date=binding.decision_time.date().isoformat(),
                snapshot_hash=getattr(vintage, "snapshot_hash"),
                manifest_hash=getattr(vintage, "manifest_hash"),
                calendar_hash=getattr(vintage, "calendar_hash"),
                universe_hash=getattr(vintage, "universe_hash"),
                revision_available_time=data_available_time,
                captured_at=data_available_time,
                vintage=projected,
            )
            _validate_required_shadow_inputs(frozen.vintage)
            output.append(frozen)
        return tuple(output)


class EligibleChallengerPortAdapter:
    """Project only policy-matched, PIT-visible CHALLENGER records."""

    def __init__(
        self,
        provider: EligibleChallengerProvider,
        vintage_adapter: FrozenVintagePortAdapter,
    ) -> None:
        self._provider = provider
        self._vintage_adapter = vintage_adapter

    def get_candidates(
        self,
        policy: ShadowReplayPolicy,
        vintage: DataVintage,
    ) -> tuple[ReplayChallengerCandidate, ...]:
        source = self._vintage_adapter.source_for(vintage)
        candidates = self._provider.get_eligible_challengers(policy, source)
        if not isinstance(candidates, tuple):
            _fail(FailureCode.INVALID_VALUE, "eligible challenger provider must return a tuple")
        for candidate in candidates:
            if not isinstance(candidate, ReplayChallengerCandidate):
                _fail(FailureCode.INVALID_ALPHA_SOURCE, "provider returned an invalid challenger projection")
            if candidate.state is not ReplayCandidateState.CHALLENGER:
                _fail(FailureCode.INVALID_ALPHA_SOURCE, "eligible provider returned a non-CHALLENGER candidate")
            if candidate.policy_version != policy.policy_version or candidate.policy_hash != policy.policy_hash:
                _fail(FailureCode.INVALID_ALPHA_SOURCE, "challenger does not match replay promotion policy")
            if candidate.available_time > vintage.decision_time:
                _fail(FailureCode.INVALID_TIME_ORDER, "future challenger is not visible at decision time")
        return candidates


PositionExtractor = Callable[
    [ShadowRunRequest, DataVintage, tuple[Any, ...], ShadowRunOnceResult],
    Mapping[str, float],
]


class PositionCarryingRunOncePort:
    """Pass previous shadow holdings into each daily run_once call.

    This wrapper is controller glue only: it does not construct portfolios,
    approve risk, simulate execution, or account for PnL.  It forwards every
    run to the injected shadow port and updates carried holdings only from an
    explicit extractor, a delegate ``positions_after`` hook, or append-only
    FILLS already emitted by the delegate's shadow ledger.
    """

    def __init__(
        self,
        delegate: Any,
        *,
        initial_positions: Mapping[str, float] | None = None,
        position_extractor: PositionExtractor | None = None,
    ) -> None:
        self._delegate = delegate
        self._position_extractor = position_extractor
        self._positions: dict[str, float] = dict(initial_positions or {})

    @property
    def current_positions(self) -> Mapping[str, float]:
        return MappingProxyType(dict(self._positions))

    def run_once(
        self,
        request: ShadowRunRequest,
        vintage: DataVintage,
        challengers: tuple[Any, ...],
    ) -> ShadowRunOnceResult:
        carried_request = replace(request, previous_positions=dict(self._positions))
        outcome = self._delegate.run_once(carried_request, vintage, challengers)
        self._positions = _coerce_positions(
            _extract_positions(self._delegate, carried_request, vintage, challengers, outcome, self._position_extractor)
        )
        return outcome


def _validate_required_shadow_inputs(vintage: DataVintage) -> None:
    symbols = set(vintage.universe)
    required_maps: tuple[tuple[str, Mapping[str, Any]], ...] = (
        ("beta_by_symbol", vintage.beta_by_symbol),
        ("industry_by_symbol", vintage.industry_by_symbol),
        ("adv_by_symbol", vintage.adv_by_symbol),
    )
    for field_name, values in required_maps:
        missing = symbols - set(values)
        if missing:
            _fail(
                FailureCode.MISSING_REQUIRED_FIELD,
                f"{field_name} missing universe members",
                symbols=tuple(sorted(missing)),
            )
    for symbol in symbols:
        beta = vintage.beta_by_symbol[symbol]
        adv = vintage.adv_by_symbol[symbol]
        industry = vintage.industry_by_symbol[symbol]
        if not isinstance(beta, (int, float)) or isinstance(beta, bool) or not isfinite(float(beta)):
            _fail(FailureCode.RISK_INPUT_INVALID, "missing or invalid beta", symbol=symbol)
        if not isinstance(adv, (int, float)) or isinstance(adv, bool) or not isfinite(float(adv)) or float(adv) <= 0:
            _fail(FailureCode.RISK_INPUT_INVALID, "missing or invalid ADV", symbol=symbol)
        if not isinstance(industry, str) or not industry.strip():
            _fail(FailureCode.RISK_INPUT_INVALID, "missing or invalid industry", symbol=symbol)


def _market_vintage_id(binding: FrozenVintageBinding, vintage: Any) -> str:
    digest = canonical_content_hash(
        {
            "snapshot_id": binding.snapshot_id,
            "revision_id": binding.revision_id,
            "decision_time": binding.decision_time,
            "market_state_hash": getattr(vintage, "market_state_hash"),
        }
    ).removeprefix("sha256:")[:20]
    return f"real-frozen-{binding.decision_time.date().isoformat()}-{digest}"


def _row_coverage(rows: tuple[Any, ...]) -> float:
    values = [value for row in rows for value in getattr(row, "values", {}).values()]
    if not values:
        return 0.0
    return sum(value is not None for value in values) / len(values)


def _extract_positions(
    delegate: Any,
    request: ShadowRunRequest,
    vintage: DataVintage,
    challengers: tuple[Any, ...],
    outcome: ShadowRunOnceResult,
    extractor: PositionExtractor | None,
) -> Mapping[str, float]:
    if extractor is not None:
        return extractor(request, vintage, challengers, outcome)
    method = getattr(delegate, "positions_after", None)
    if callable(method):
        return method(request, vintage, challengers, outcome)
    ledger = getattr(delegate, "ledger", None)
    snapshot_method = getattr(ledger, "snapshot", None)
    if callable(snapshot_method):
        positions = dict(request.previous_positions)
        snapshot = snapshot_method()
        for event, payload in zip(getattr(snapshot, "events", ()), getattr(snapshot, "payloads", ())):
            if getattr(event, "run_id", None) != request.run_id or getattr(event, "event_type", None) != "FILLS":
                continue
            for fill in payload:
                side = getattr(getattr(fill, "side", None), "value", getattr(fill, "side", None))
                quantity = float(getattr(fill, "filled_quantity", 0.0))
                signed = quantity if side == "BUY" else -quantity if side == "SELL" else 0.0
                symbol = getattr(fill, "symbol", "")
                if symbol:
                    positions[symbol] = positions.get(symbol, 0.0) + signed
        return positions
    return request.previous_positions


def _coerce_positions(value: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(value, Mapping):
        _fail(FailureCode.RISK_INPUT_INVALID, "position extractor must return a mapping")
    positions: dict[str, float] = {}
    for symbol, quantity in value.items():
        if not isinstance(symbol, str) or not symbol:
            _fail(FailureCode.RISK_INPUT_INVALID, "position symbol must be non-empty")
        if not isinstance(quantity, (int, float)) or isinstance(quantity, bool) or not isfinite(float(quantity)):
            _fail(FailureCode.RISK_INPUT_INVALID, "position quantity must be finite", symbol=symbol)
        if float(quantity) != 0.0:
            positions[symbol] = float(quantity)
    return positions
