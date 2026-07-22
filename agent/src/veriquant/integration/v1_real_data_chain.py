"""Thin, authority-preserving integration for the compatible-proxy A-share smoke.

The chain verifies an existing frozen price snapshot, freezes complete row-level
market state, runs restricted FactorDAG/PIT diagnostics, and applies the source
provenance gate.  It intentionally has no Evidence, Promotion, CHALLENGER,
portfolio, execution, or ledger capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from importlib import metadata
from pathlib import Path
from typing import Any, ClassVar, Mapping
from zoneinfo import ZoneInfo

from veriquant.contracts import CanonicalContract, ContractValidationError, FailureCode
from veriquant.contracts.base import fail, require_aware_datetime, require_hash, require_text
from veriquant.data import canonical_content_hash
from veriquant.data.ashare.models import FrozenAShareSnapshot
from veriquant.provider_provenance import (
    ProviderEligibility,
    ProviderProvenanceManifest,
    ProviderProvenancePolicy,
    ProviderSourceClass,
    evaluate_provider_provenance,
)
from veriquant.proxy_research_smoke.api import (
    AUXILIARY_EVIDENCE_CLASS,
    AuxiliaryFactorStatus,
    ProxyResearchSmokeController,
    SourceQualification,
)
from veriquant.real_ashare_data.reader import FrozenAShareSnapshotReader, FrozenSnapshotHandle
from veriquant.real_research.reader import (
    FrozenSnapshotEnvelope,
    InMemoryFrozenSnapshotReader,
    build_reader_manifest,
)
from veriquant.real_research.models import ResearchSplitManifest
from veriquant.tushare_market_state.models import (
    FrozenMarketStateBundle,
    MarketStateCapture,
    MarketStateInputRow,
    MarketStateRequest,
)
from veriquant.tushare_market_state.store import FrozenMarketStateStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = "veriquant.integration-real-data/v1"
PROXY_HOST = "ts.gyzcloud.top"
PROXY_PATH = "/api"


@dataclass(frozen=True, kw_only=True)
class RealAuxiliaryChainResult(CanonicalContract):
    """Machine result whose booleans make the authority boundary unambiguous."""

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    status: str
    source_class: str
    authority_class: str
    provider_eligibility: str
    snapshot_fetched_at: datetime
    decision_time: datetime
    snapshot_hash: str
    snapshot_manifest_hash: str
    calendar_hash: str
    universe_hash: str
    bar_capture_hash: str
    market_state_capture_hash: str
    market_state_vintage_hash: str
    market_state_manifest_hash: str
    provenance_manifest_hash: str
    provenance_decision_hash: str
    research_manifest_hash: str
    factor_dag_hashes: tuple[str, ...]
    factor_output_hashes: tuple[str, ...]
    market_state_row_count: int
    factor_count: int
    network_used: bool
    capture_replay_verified: bool
    official_evidence_run: bool
    promotion_run: bool
    challenger_created: bool
    real_shadow_replay_run: bool
    gate_blocker: str

    def __post_init__(self) -> None:
        if self.status != "COMPLETED_AUXILIARY":
            fail(FailureCode.INVALID_STATE, "real compatible-proxy chain has an invalid status")
        if self.source_class != ProviderSourceClass.TUSHARE_COMPATIBLE_PROXY.value:
            fail(FailureCode.INVALID_VALUE, "source class is not the reviewed compatible proxy")
        if self.authority_class != AUXILIARY_EVIDENCE_CLASS:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "compatible-proxy result must remain auxiliary")
        if self.provider_eligibility != ProviderEligibility.AUXILIARY_ONLY.value:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "unattested compatible proxy cannot be official")
        require_aware_datetime(self.snapshot_fetched_at, "snapshot_fetched_at")
        require_aware_datetime(self.decision_time, "decision_time")
        for name in (
            "snapshot_hash",
            "snapshot_manifest_hash",
            "calendar_hash",
            "universe_hash",
            "bar_capture_hash",
            "market_state_capture_hash",
            "market_state_vintage_hash",
            "market_state_manifest_hash",
            "provenance_manifest_hash",
            "provenance_decision_hash",
            "research_manifest_hash",
        ):
            require_hash(getattr(self, name), name)
        if not self.factor_dag_hashes or not self.factor_output_hashes:
            fail(FailureCode.INCOMPLETE_RESULT, "at least one evaluated FactorDAG is required")
        if len(self.factor_dag_hashes) != len(self.factor_output_hashes):
            fail(FailureCode.INCOMPLETE_RESULT, "FactorDAG and output identities differ in length")
        for value in self.factor_dag_hashes + self.factor_output_hashes:
            require_hash(value, "factor_hash")
        if self.market_state_row_count < 1 or self.factor_count != len(self.factor_dag_hashes):
            fail(FailureCode.INCOMPLETE_RESULT, "row/factor counts are invalid")
        if type(self.network_used) is not bool or type(self.capture_replay_verified) is not bool:
            fail(FailureCode.INVALID_VALUE, "network/replay flags must be boolean")
        if not self.capture_replay_verified:
            fail(FailureCode.INVALID_HASH, "captured market-state replay was not reproduced")
        if any(
            (
                self.official_evidence_run,
                self.promotion_run,
                self.challenger_created,
                self.real_shadow_replay_run,
            )
        ):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "auxiliary source crossed an official authority gate")
        require_text(self.gate_blocker, "gate_blocker")


class SingleFrozenEnvelopeProvider:
    """Read-only provider accepted by ``ProxyResearchSmokeController``."""

    def __init__(self, snapshot_id: str, envelope: FrozenSnapshotEnvelope) -> None:
        self._snapshot_id = snapshot_id
        self._reader = InMemoryFrozenSnapshotReader({snapshot_id: envelope})

    def load_envelope(self, *, snapshot_id: str) -> FrozenSnapshotEnvelope:
        if snapshot_id != self._snapshot_id:
            fail(FailureCode.INVALID_IDENTIFIER, "snapshot id is not registered")
        return self._reader.read_snapshot(snapshot_id)


class CapturedMarketStateSdk:
    """Offline exact replay of a previously captured fixed SDK call sequence."""

    def __init__(self, capture: MarketStateCapture) -> None:
        if not isinstance(capture, MarketStateCapture):
            fail(FailureCode.INVALID_VALUE, "typed market-state capture is required")
        self._calls = list(capture.calls)

    def _next(self, api_name: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not self._calls:
            fail(FailureCode.INCOMPLETE_RESULT, "captured SDK replay exhausted early")
        call = self._calls.pop(0)
        if call.api_name != api_name or dict(call.params) != dict(params):
            fail(FailureCode.INVALID_HASH, "captured SDK call sequence or parameters changed")
        return [dict(row) for row in call.rows]

    def stock_basic(self, **params: Any) -> list[dict[str, Any]]:
        return self._next("stock_basic", params)

    def suspend_d(self, **params: Any) -> list[dict[str, Any]]:
        return self._next("suspend_d", params)

    def stk_limit(self, **params: Any) -> list[dict[str, Any]]:
        return self._next("stk_limit", params)

    def adj_factor(self, **params: Any) -> list[dict[str, Any]]:
        return self._next("adj_factor", params)

    def assert_consumed(self) -> None:
        if self._calls:
            fail(FailureCode.INCOMPLETE_RESULT, "captured SDK replay has unused calls")


def load_verified_price_snapshot(
    *, manifest_path: str | Path, snapshot_root: str | Path, calendar_root: str | Path
) -> tuple[FrozenSnapshotHandle, FrozenAShareSnapshot]:
    """Open and re-read a frozen snapshot so byte/hash changes fail closed."""

    reader = FrozenAShareSnapshotReader(snapshot_root=snapshot_root, calendar_root=calendar_root)
    handle = reader.open(Path(manifest_path).resolve())
    snapshot = reader.read_snapshot(handle)
    return handle, snapshot


def build_market_state_request(
    *, snapshot: FrozenAShareSnapshot, handle: FrozenSnapshotHandle, bar_capture_hash: str
) -> MarketStateRequest:
    require_hash(bar_capture_hash, "bar_capture_hash")
    rows = tuple(
        MarketStateInputRow(
            symbol=row.symbol,
            trade_date=row.event_time.astimezone(SHANGHAI).date().isoformat(),
            event_time=row.event_time,
            close=float(row.values["close"]),
        )
        for row in sorted(snapshot.rows, key=lambda item: (item.symbol, item.event_time))
    )
    if snapshot.trading_calendar is None:
        fail(FailureCode.INCOMPLETE_RESULT, "frozen price snapshot has no trading calendar")
    return MarketStateRequest(
        dataset_id=snapshot.request.dataset_id,
        codes=tuple(sorted(snapshot.request.codes)),
        start_date=min(row.trade_date for row in rows),
        end_date=max(row.trade_date for row in rows),
        decision_time=snapshot.request.decision_time,
        rows=rows,
        source_snapshot_hash=handle.snapshot_hash,
        calendar_hash=handle.calendar_hash,
        universe_hash=handle.universe_hash,
        bar_capture_hash=bar_capture_hash,
    )


def build_research_envelope(
    *, snapshot_id: str, snapshot: FrozenAShareSnapshot, market_state: FrozenMarketStateBundle
) -> FrozenSnapshotEnvelope:
    if market_state.manifest.source_snapshot_hash != snapshot.snapshot_hash:
        fail(FailureCode.INVALID_HASH, "market-state vintage is not bound to the price snapshot")
    states = tuple(row.to_row_market_state() for row in market_state.rows)
    state_keys = {(row.symbol, row.event_time) for row in states}
    price_keys = {(row.symbol, row.event_time) for row in snapshot.rows}
    if state_keys != price_keys:
        fail(FailureCode.INCOMPLETE_RESULT, "price and market-state row keys differ")
    if snapshot.trading_calendar is None:
        fail(FailureCode.INCOMPLETE_RESULT, "trading calendar is missing")
    sessions = tuple(
        datetime.combine(datetime.fromisoformat(value).date(), time(15), SHANGHAI)
        for value in snapshot.trading_calendar.session_dates
    )
    manifest = build_reader_manifest(
        snapshot_id=snapshot_id,
        snapshot=snapshot,
        trading_calendar=sessions,
        row_market_state=states,
        revision=market_state.manifest.revision,
        reader_schema_version="compatible-proxy-market-state/v1",
    )
    return FrozenSnapshotEnvelope(manifest=manifest, snapshot=snapshot, row_market_state=states)


def build_split(snapshot: FrozenAShareSnapshot) -> ResearchSplitManifest:
    sessions = tuple(sorted({row.event_time for row in snapshot.rows}))
    usable = sessions[:-6]
    if len(usable) < 12:
        fail(FailureCode.INCOMPLETE_RESULT, "real auxiliary smoke needs enough sessions for splits and labels")
    train_end = len(usable) // 2
    validation_end = train_end + max(2, (len(usable) - train_end) // 2)
    return ResearchSplitManifest(
        split_id="compatible-proxy-visible-splits",
        train_start=usable[0],
        train_end=usable[train_end - 1],
        validation_start=usable[train_end],
        validation_end=usable[validation_end - 1],
        sealed_start=usable[validation_end],
        sealed_end=usable[-1],
    )


def run_real_auxiliary_chain(
    *,
    sdk: Any,
    manifest_path: str | Path,
    snapshot_root: str | Path,
    calendar_root: str | Path,
    market_state_root: str | Path,
    bar_capture_hash: str,
    endpoint_host: str = PROXY_HOST,
    endpoint_path: str = PROXY_PATH,
    network_used: bool,
) -> RealAuxiliaryChainResult:
    """Run the bounded real-data auxiliary slice and stop at the provenance gate."""

    if endpoint_host != PROXY_HOST or endpoint_path != PROXY_PATH:
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "provider endpoint is outside the fixed allowlist")
    handle, snapshot = load_verified_price_snapshot(
        manifest_path=manifest_path,
        snapshot_root=snapshot_root,
        calendar_root=calendar_root,
    )
    request = build_market_state_request(
        snapshot=snapshot,
        handle=handle,
        bar_capture_hash=bar_capture_hash,
    )
    store = FrozenMarketStateStore(market_state_root)
    market_state = store.freeze(request, sdk=sdk)
    replay_sdk = CapturedMarketStateSdk(market_state.capture)
    replayed = store.freeze(request, sdk=replay_sdk)
    replay_sdk.assert_consumed()
    if replayed.manifest.artifact_hash != market_state.manifest.artifact_hash:
        fail(FailureCode.INVALID_HASH, "captured market-state replay changed its manifest hash")

    combined_capture_hash = canonical_content_hash(
        {"bar_capture_hash": bar_capture_hash, "market_state_capture_hash": market_state.capture.artifact_hash}
    )
    try:
        sdk_version = metadata.version("tushare")
    except metadata.PackageNotFoundError:
        sdk_version = "unavailable-in-caller-runtime"
    provenance = ProviderProvenanceManifest(
        provider_id="tushare-compatible-proxy-sdk",
        source_class=ProviderSourceClass.TUSHARE_COMPATIBLE_PROXY,
        endpoint_host=endpoint_host,
        endpoint_path=endpoint_path,
        sdk_name="tushare",
        sdk_version=sdk_version,
        acquired_at=snapshot.fetched_at,
        api_names=tuple(sorted({"daily", "index_weight", "trade_cal", "adj_factor", "stock_basic", "suspend_d", "stk_limit"})),
        snapshot_hash=handle.snapshot_hash,
        capture_hash=combined_capture_hash,
        calendar_hash=handle.calendar_hash,
        universe_hash=handle.universe_hash,
        upstream_attestation_hash=None,
    )
    decision = evaluate_provider_provenance(provenance, ProviderProvenancePolicy())
    if decision.eligibility is not ProviderEligibility.AUXILIARY_ONLY:
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "default proxy provenance policy did not fail closed")

    snapshot_id = "tushare-proxy-operational-smoke"
    envelope = build_research_envelope(
        snapshot_id=snapshot_id,
        snapshot=snapshot,
        market_state=market_state,
    )
    research = ProxyResearchSmokeController(SingleFrozenEnvelopeProvider(snapshot_id, envelope)).run(
        snapshot_id=snapshot_id,
        decision_time=snapshot.request.decision_time,
        split_manifest=build_split(snapshot),
        feature_fields=("close", "volume"),
        source_qualification=SourceQualification.TUSHARE_COMPATIBLE_PROXY,
        seed=7,
        run_id="compatible-proxy-real-auxiliary",
    )
    evaluated = tuple(item for item in research.factors if item.status is AuxiliaryFactorStatus.EVALUATED)
    if not evaluated:
        fail(FailureCode.INCOMPLETE_RESULT, "no restricted FactorDAG evaluated successfully")
    return RealAuxiliaryChainResult(
        status="COMPLETED_AUXILIARY",
        source_class=ProviderSourceClass.TUSHARE_COMPATIBLE_PROXY.value,
        authority_class=AUXILIARY_EVIDENCE_CLASS,
        provider_eligibility=decision.eligibility.value,
        snapshot_fetched_at=snapshot.fetched_at,
        decision_time=snapshot.request.decision_time,
        snapshot_hash=handle.snapshot_hash,
        snapshot_manifest_hash=handle.manifest_hash,
        calendar_hash=handle.calendar_hash,
        universe_hash=handle.universe_hash,
        bar_capture_hash=bar_capture_hash,
        market_state_capture_hash=market_state.capture.artifact_hash,
        market_state_vintage_hash=market_state.manifest.vintage_hash,
        market_state_manifest_hash=market_state.manifest.artifact_hash,
        provenance_manifest_hash=provenance.artifact_hash,
        provenance_decision_hash=decision.artifact_hash,
        research_manifest_hash=research.artifact_hash,
        factor_dag_hashes=tuple(item.dag_hash for item in evaluated if item.dag_hash is not None),
        factor_output_hashes=tuple(item.output_hash for item in evaluated if item.output_hash is not None),
        market_state_row_count=len(market_state.rows),
        factor_count=len(evaluated),
        network_used=network_used,
        capture_replay_verified=True,
        official_evidence_run=False,
        promotion_run=False,
        challenger_created=False,
        real_shadow_replay_run=False,
        gate_blocker=(
            "Compatible proxy has no independently approved upstream attestation; default provider "
            "policy permits auxiliary FactorDAG/PIT diagnostics only."
        ),
    )


__all__ = [
    "CapturedMarketStateSdk",
    "PROXY_HOST",
    "PROXY_PATH",
    "RealAuxiliaryChainResult",
    "SingleFrozenEnvelopeProvider",
    "build_market_state_request",
    "build_research_envelope",
    "build_split",
    "load_verified_price_snapshot",
    "run_real_auxiliary_chain",
]
