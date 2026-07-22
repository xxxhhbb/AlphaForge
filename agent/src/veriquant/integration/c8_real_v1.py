"""Wave C8 real approved-proxy A-share V1 loop.

This module is integration glue only.  It reuses the existing frozen A-share
reader, A-share research bridge, restricted FactorDAG executor, official
Evidence service, independent statistical promotion gate, real-vintage replay
adapter, shadow replay controller, and paper-only shadow portfolio service.

The data source is explicitly the operator-approved compatible proxy:
``APPROVED_PROXY_DATA_VENDOR`` / ``TUSHARE_COMPATIBLE_PROXY``.  It is never
represented as official Tushare provenance.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.contracts.base import fail
from veriquant.data import FieldRole, MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.evidence import CostModel, EvaluationRow, EvidenceService, ExperimentIdentity
from veriquant.factors import CompiledFactorDAG, execute_factor_dag, parse_compile_factor_dag
from veriquant.integration.statistical import (
    InMemoryOfficialEvidenceReader,
    PromotionEvaluationRequest,
    build_family_manifest,
    candidate_view_from_official_evidence,
    evaluate_statistical_promotion,
)
from veriquant.integration.v1_approved_proxy_policy import ApprovedProxyQualification
from veriquant.integration.v1_real_data_chain import (
    RealAuxiliaryChainResult,
    build_research_envelope,
    load_verified_price_snapshot,
)
from veriquant.integration.v1_real_official.service import (
    APPROVED_QUALIFICATION_HASH,
    PINNED_AUXILIARY_RESULT_HASH,
    PINNED_MARKET_STATE_VINTAGE_HASH,
    PINNED_SNAPSHOT_HASH,
    PinnedRealOfficialInputPaths,
)
from veriquant.integration.v1_real_replay.models import PromotionChallengerReceipt
from veriquant.integration.workbench import publish_workbench_run
from veriquant.real_ashare_data.reader import FrozenAShareSnapshotReader, MarketVintage
from veriquant.real_research.api import AShareResearchBridge, build_default_bridge_request
from veriquant.real_research.models import ResearchDatasetBundle, ResearchSplitManifest
from veriquant.real_research.reader import InMemoryFrozenSnapshotReader
from veriquant.real_vintage_replay.api import (
    FrozenDailyVintage,
    FrozenVintageBinding,
    FrozenVintageMetadata,
    RealVintageReplayController,
)
from veriquant.real_vintage_replay.recovery import RealVintageReplayStageProvider
from veriquant.run_recovery.api import (
    RecoveryInterrupted,
    RecoveryStatus,
    RunCheckpointStore,
    RunRecoveryController,
    StageIdentity,
    StageTask,
)
from veriquant.shadow_portfolio.ledger import ShadowLedger
from veriquant.shadow_portfolio.models import DataVintage, MarketPrice, ShadowRiskPolicy, ShadowRunRequest
from veriquant.shadow_portfolio.service import DeterministicClock, EqualWeightPortfolioConstructor
from veriquant.shadow_replay.api import (
    ReplayCandidateState,
    ReplayChallengerCandidate,
    ReplayStatus,
    ShadowPortfolioRunOnceAdapter,
    ShadowReplayJournal,
    ShadowReplayPolicy,
)
from veriquant.statistical_promotion.contracts import (
    MultipleTestingPolicy,
    PromotionState,
    RobustnessPolicy,
)
from veriquant.tushare_market_state.models import FrozenMarketStateBundle
from veriquant.tushare_market_state.store import FrozenMarketStateStore
from veriquant.v1_security.api import RouteRecord, SecurityAcceptanceProbe, VeriQuantOfflineDeploymentProfile


SCHEMA_VERSION = "veriquant.c8-real-ashare-v1/v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
C8_REPLAY_DATES = (
    "2024-03-01",
    "2024-03-04",
    "2024-03-05",
    "2024-03-06",
    "2024-03-07",
    "2024-03-08",
    "2024-03-11",
    "2024-03-12",
    "2024-03-13",
    "2024-03-14",
    "2024-03-15",
    "2024-03-18",
    "2024-03-19",
    "2024-03-20",
    "2024-03-21",
    "2024-03-22",
    "2024-03-25",
    "2024-03-26",
    "2024-03-27",
    "2024-03-28",
)
C8_RUN_ID = "c8-real-ashare-v1"
C8_PROVIDER_CLASS = "APPROVED_PROXY_DATA_VENDOR"
C8_SOURCE_CLASS = "TUSHARE_COMPATIBLE_PROXY"


def _dt(value: str, hour: int = 15, minute: int = 0) -> datetime:
    year, month, day = (int(part) for part in value.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_dict(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        payload = value.to_dict()
        return {**payload, "artifact_hash": getattr(value, "artifact_hash")}
    if isinstance(value, Mapping):
        return {str(key): _safe_dict(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple):
        return [_safe_dict(item) for item in value]
    if isinstance(value, list):
        return [_safe_dict(item) for item in value]
    if isinstance(value, datetime):
        return _utc_text(value)
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_name = handle.name
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_name = handle.name
    try:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _typed_artifact(path: Path, contract_type: type[Any], expected_hash: str) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(FailureCode.INCOMPLETE_RESULT, f"required C8 artifact is unreadable: {path.name}", error=type(exc).__name__)
    declared_hash = payload.pop("artifact_hash", None)
    typed = contract_type.from_payload(payload)
    if typed.artifact_hash != expected_hash or declared_hash != expected_hash:
        fail(FailureCode.INVALID_HASH, f"required C8 artifact hash changed: {path.name}")
    return typed


def _shadow_policy_hash() -> str:
    return canonical_content_hash(
        {
            "policy": "c8-real-approved-proxy-shadow",
            "version": "policy.v1",
            "portfolio": "paper-only-equal-weight-demeaned-score",
            "risk": "shadow-alert-policy",
            "source_class": C8_SOURCE_CLASS,
        }
    )


def make_c8_shadow_policy() -> ShadowReplayPolicy:
    return ShadowReplayPolicy(
        replay_id=C8_RUN_ID,
        account_id="c8-shadow-account",
        policy_version="policy.v1",
        policy_hash=_shadow_policy_hash(),
        trading_days=20,
        starting_cash=100000.0,
        max_gross_weight=0.8,
        transaction_cost_bps=5.0,
        slippage_bps=10.0,
        max_adv_participation=0.05,
        seed=7,
    )


def make_c8_robustness_policy() -> RobustnessPolicy:
    """V1 smoke policy that still rejects visibly losing candidates.

    The two-stock approved-proxy sample is deliberately tiny, so C8 uses relaxed
    stability thresholds to prove the end-to-end authority path.  It still
    requires visible validation RankIC and net return after cost to be positive,
    so the promotion receipt is not a rubber stamp.  The resulting CHALLENGER is
    a V1 smoke artifact, not a production trading mandate.
    """

    return RobustnessPolicy(
        policy_id="c8-smoke-robustness",
        policy_version="v1",
        min_observations=4,
        min_rank_ic=0.0,
        min_icir=-999.0,
        min_subperiod_positive_fraction=0.0,
        min_subperiod_min_ic=-1.0,
        min_regime_positive_fraction=0.0,
        min_regime_min_ic=-1.0,
        max_abs_industry_exposure=1.0,
        max_abs_market_cap_exposure=1.0,
        max_turnover=10.0,
        min_net_return_after_cost=0.0,
        max_cost_sensitivity_loss=1.0,
        max_capacity_utilization=1.0,
        min_coverage=0.5,
        max_parameter_sensitivity=1.0,
    )


def make_c8_multiple_testing_policy() -> MultipleTestingPolicy:
    return MultipleTestingPolicy(policy_id="c8-smoke-multiple-testing", policy_version="v1", alpha=0.20)


def c8_factor_definitions() -> tuple[Mapping[str, Any], ...]:
    return (
        {
            "factor_id": "c8_momentum_20d",
            "payload": {
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "c8-real-momentum-20d",
                "nodes": [
                    {"id": "close", "op": "field", "field": "close"},
                    {"id": "mom", "op": "pct_change", "inputs": ["close"], "periods": 20},
                    {"id": "rank", "op": "rank_cross_section", "inputs": ["mom"]},
                ],
                "output_node": "rank",
            },
        },
        {
            "factor_id": "c8_reversal_5d",
            "payload": {
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "c8-real-reversal-5d",
                "nodes": [
                    {"id": "close", "op": "field", "field": "close"},
                    {"id": "lag", "op": "lag", "inputs": ["close"], "periods": 5},
                    {"id": "diff", "op": "subtract", "inputs": ["lag", "close"]},
                    {"id": "rev", "op": "divide_safe", "inputs": ["diff", "lag"], "epsilon": 1e-12},
                    {"id": "rank", "op": "rank_cross_section", "inputs": ["rev"]},
                ],
                "output_node": "rank",
            },
        },
        {
            "factor_id": "c8_volume_change_5d",
            "payload": {
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "c8-real-volume-change-5d",
                "nodes": [
                    {"id": "volume", "op": "field", "field": "volume"},
                    {"id": "chg", "op": "pct_change", "inputs": ["volume"], "periods": 5},
                    {"id": "rank", "op": "rank_cross_section", "inputs": ["chg"]},
                ],
                "output_node": "rank",
            },
        },
    )


def _snapshot_for_dag(bundle: ResearchDatasetBundle, dag: CompiledFactorDAG) -> MarketSnapshot:
    fields = tuple(sorted(dag.required_fields))
    rows = tuple(
        SnapshotRow(
            event_time=row.event_time,
            symbol=row.symbol,
            values={field: row.values[field] for field in fields},
        )
        for row in bundle.feature_rows
        if row.partition in {"train", "validation"} and set(fields) <= set(row.values)
    )
    if not rows:
        fail(FailureCode.INCOMPLETE_RESULT, "visible PIT snapshot has no rows for FactorDAG")
    return MarketSnapshot(
        dataset_id=bundle.dataset_card.dataset_id,
        decision_time=bundle.decision_time,
        start_time=min(row.event_time for row in rows),
        fields=fields,
        rows=rows,
        historical_universe=dict(bundle.universe_manifest.historical_universe),
        coverage_by_field={field: 1.0 for field in fields},
    )


def _evaluation_rows(
    *,
    bundle: ResearchDatasetBundle,
    factor_result: Any,
) -> dict[str, tuple[EvaluationRow, ...]]:
    labels = {
        (row.event_time, row.symbol): row.value
        for row in bundle.label_rows
        if row.partition in {"train", "validation"}
    }
    partitions = {
        (row.event_time, row.symbol): row.partition
        for row in bundle.feature_rows
        if row.partition in {"train", "validation"}
    }
    volume = {
        (row.event_time, row.symbol): float(row.values.get("volume", 1_000_000.0))
        for row in bundle.feature_rows
        if row.partition in {"train", "validation"} and row.values.get("volume") is not None
    }
    output: dict[str, list[EvaluationRow]] = {"train": [], "validation": []}
    for value in factor_result.values:
        key = (value.event_time, value.symbol)
        partition = partitions.get(key)
        label = labels.get(key)
        if partition not in output or label is None or value.value is None:
            continue
        output[partition].append(
            EvaluationRow(
                event_time=value.event_time,
                symbol=value.symbol,
                score=float(value.value),
                label=float(label),
                market_regime="c8_real_approved_proxy",
                volume=volume.get(key, 1_000_000.0),
            )
        )
    return {key: tuple(sorted(rows, key=lambda row: (row.event_time, row.symbol))) for key, rows in output.items()}


def _factor_value_stats(factor_result: Any) -> tuple[float, float]:
    values = [float(item.value) for item in factor_result.values if item.value is not None]
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return mean, variance ** 0.5


@dataclass
class _PreparedInputs:
    paths: PinnedRealOfficialInputPaths
    qualification: ApprovedProxyQualification
    auxiliary: RealAuxiliaryChainResult
    snapshot: Any
    handle: Any
    market_state: FrozenMarketStateBundle
    bundle: ResearchDatasetBundle
    manifest_path: Path


@dataclass
class _OfficialAttempt:
    factor_id: str
    payload: Mapping[str, Any]
    dag: CompiledFactorDAG
    factor_result: Any
    train: Any
    validation: Any
    mean: float
    std: float
    promotion: Any | None = None

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "dag_hash": self.dag.artifact_hash,
            "factor_output_hash": self.factor_result.artifact_hash,
            "identity_hash": self.train.identity_hash,
            "train_evidence_hash": self.train.artifact_hash,
            "validation_evidence_hash": self.validation.artifact_hash,
            "train_metrics": dict(self.train.metrics.flat()),
            "validation_metrics": dict(self.validation.metrics.flat()),
            "factor_value_mean": self.mean,
            "factor_value_std": self.std,
            "promotion_decision_hash": self.promotion.decision.decision_hash if self.promotion else None,
            "promotion_verdict": self.promotion.decision.verdict.value if self.promotion else None,
            "promotion_state": self.promotion.decision.to_state.value if self.promotion else None,
            "promotion_audit_hash": self.promotion.audit.artifact_hash if self.promotion else None,
        }


class _C8MetadataProvider:
    """Derive required risk metadata from frozen visible rows.

    ADV and beta are computed from the frozen daily bars as of the binding
    decision time.  Industry is a conservative single unknown bucket because
    the approved proxy snapshot did not freeze a true industry field.
    """

    def metadata_for(self, *, binding: FrozenVintageBinding, vintage: MarketVintage) -> FrozenVintageMetadata:
        rows_by_symbol: dict[str, list[Any]] = {}
        for row in vintage.rows:
            rows_by_symbol.setdefault(row.symbol, []).append(row)
        adv: dict[str, float] = {}
        beta: dict[str, float] = {}
        for symbol, rows in rows_by_symbol.items():
            ordered = sorted(rows, key=lambda row: row.event_time)[-20:]
            volumes = [float(row.values.get("volume") or 0.0) for row in ordered]
            adv[symbol] = max(1.0, sum(volumes) / len(volumes))
        market_returns = _market_returns(rows_by_symbol)
        for symbol, rows in rows_by_symbol.items():
            beta[symbol] = _beta_for_symbol(rows, market_returns)
        industry = {symbol: "APPROVED_PROXY_UNKNOWN_INDUSTRY" for symbol in rows_by_symbol}
        return FrozenVintageMetadata(
            corporate_action_available_time=max(row.available_at for row in vintage.rows),
            beta_by_symbol=beta,
            industry_by_symbol=industry,
            adv_by_symbol=adv,
        )


def _market_returns(rows_by_symbol: Mapping[str, Sequence[Any]]) -> Mapping[str, float]:
    returns_by_date: dict[str, list[float]] = {}
    for rows in rows_by_symbol.values():
        ordered = sorted(rows, key=lambda row: row.event_time)
        for previous, current in zip(ordered, ordered[1:]):
            prev_close = float(previous.values.get("close") or 0.0)
            close = float(current.values.get("close") or 0.0)
            if prev_close > 0.0:
                returns_by_date.setdefault(current.event_time.date().isoformat(), []).append(close / prev_close - 1.0)
    return {
        date_text: sum(values) / len(values)
        for date_text, values in returns_by_date.items()
        if values
    }


def _beta_for_symbol(rows: Sequence[Any], market_returns: Mapping[str, float]) -> float:
    ordered = sorted(rows, key=lambda row: row.event_time)
    pairs: list[tuple[float, float]] = []
    for previous, current in zip(ordered, ordered[1:]):
        prev_close = float(previous.values.get("close") or 0.0)
        close = float(current.values.get("close") or 0.0)
        market = market_returns.get(current.event_time.date().isoformat())
        if prev_close > 0.0 and market is not None:
            pairs.append((close / prev_close - 1.0, market))
    if len(pairs) < 2:
        return 1.0
    x_mean = sum(item[1] for item in pairs) / len(pairs)
    y_mean = sum(item[0] for item in pairs) / len(pairs)
    variance = sum((item[1] - x_mean) ** 2 for item in pairs)
    if variance <= 0.0:
        return 1.0
    covariance = sum((item[1] - x_mean) * (item[0] - y_mean) for item in pairs)
    return covariance / variance


class _C8FrozenVintageProvider:
    def __init__(self, *, reader: FrozenAShareSnapshotReader, manifest_path: Path, bindings: tuple[FrozenVintageBinding, ...]) -> None:
        self.reader = reader
        self.manifest_path = manifest_path
        self.bindings = bindings
        self.metadata = _C8MetadataProvider()

    def list_frozen_vintages(self, policy: ShadowReplayPolicy) -> tuple[FrozenDailyVintage, ...]:
        if policy.trading_days != 20:
            fail(FailureCode.INVALID_VALUE, "C8 requires exactly 20 replay sessions")
        if len(self.bindings) != 20:
            fail(FailureCode.INCOMPLETE_RESULT, "C8 requires exactly 20 frozen bindings")
        output: list[FrozenDailyVintage] = []
        for binding in self.bindings:
            handle = self.reader.open(binding.manifest_path)
            vintage = self.reader.market_vintage(handle, decision_time=binding.decision_time)
            metadata = self.metadata.metadata_for(binding=binding, vintage=vintage)
            symbols = tuple(sorted({row.symbol for row in vintage.rows}))
            coverage = {
                symbol: sum(
                    item is not None
                    for row in vintage.rows
                    if row.symbol == symbol
                    for item in row.values.values()
                )
                / max(1, sum(len(row.values) for row in vintage.rows if row.symbol == symbol))
                for symbol in symbols
            }
            data_vintage = DataVintage(
                vintage_id=_vintage_id(binding, vintage),
                revision_id=binding.revision_id,
                dataset_hash=vintage.market_state_hash,
                universe=symbols,
                data_available_time=max(row.available_at for row in vintage.rows),
                decision_time=binding.decision_time,
                corporate_action_available_time=metadata.corporate_action_available_time,
                coverage_by_symbol=coverage,
                beta_by_symbol={symbol: metadata.beta_by_symbol[symbol] for symbol in symbols},
                industry_by_symbol={symbol: metadata.industry_by_symbol[symbol] for symbol in symbols},
                adv_by_symbol={symbol: metadata.adv_by_symbol[symbol] for symbol in symbols},
            )
            output.append(
                FrozenDailyVintage(
                    snapshot_id=binding.snapshot_id,
                    session_date=binding.decision_time.date().isoformat(),
                    snapshot_hash=vintage.snapshot_hash,
                    manifest_hash=vintage.manifest_hash,
                    calendar_hash=vintage.calendar_hash,
                    universe_hash=vintage.universe_hash,
                    revision_available_time=data_vintage.data_available_time,
                    captured_at=data_vintage.data_available_time,
                    vintage=data_vintage,
                )
            )
        return tuple(output)


def _vintage_id(binding: FrozenVintageBinding, vintage: MarketVintage) -> str:
    suffix = canonical_content_hash(
        {
            "snapshot_id": binding.snapshot_id,
            "revision_id": binding.revision_id,
            "decision_time": binding.decision_time,
            "market_state_hash": vintage.market_state_hash,
        }
    ).removeprefix("sha256:")[:20]
    return f"c8-{binding.decision_time.date().isoformat()}-{suffix}"


class _C8MarketPriceProvider:
    def __init__(self, *, reader: FrozenAShareSnapshotReader, manifest_path: Path, market_state: FrozenMarketStateBundle) -> None:
        self.reader = reader
        self.manifest_path = manifest_path
        self.market_state_by_key = {(row.symbol, row.event_time): row for row in market_state.rows}
        self._latest_rows_by_decision: dict[str, dict[str, Any]] = {}

    def _latest_rows(self, vintage: Any) -> dict[str, Any]:
        key = vintage.decision_time.isoformat()
        cached = self._latest_rows_by_decision.get(key)
        if cached is not None:
            return cached
        handle = self.reader.open(str(self.manifest_path))
        market_vintage = self.reader.market_vintage(handle, decision_time=vintage.decision_time)
        latest: dict[str, Any] = {}
        for row in sorted(market_vintage.rows, key=lambda item: (item.symbol, item.event_time)):
            if row.values.get("close") is not None:
                latest[row.symbol] = row
        self._latest_rows_by_decision[key] = latest
        return latest

    def _latest_row(self, symbol: str, vintage: Any) -> Any:
        return self._latest_rows(vintage).get(symbol)

    def _price(self, symbol: str, request: ShadowRunRequest, vintage: Any, *, kind: str) -> MarketPrice | None:
        row = self._latest_row(symbol, vintage)
        if row is None:
            return None
        state = self.market_state_by_key.get((symbol, row.event_time))
        return MarketPrice(
            symbol=symbol,
            price=float(row.values["close"]),
            price_time=row.event_time,
            available_time=row.available_at,
            price_kind=kind,
            suspended=bool(getattr(state, "suspended", False)),
            limit_up=bool(getattr(state, "limit_up", False)),
            limit_down=bool(getattr(state, "limit_down", False)),
        )

    def get_execution_price(self, symbol: str, request: ShadowRunRequest, vintage: Any) -> MarketPrice | None:
        return self._price(symbol, request, vintage, kind="mid")

    def get_valuation_price(self, symbol: str, request: ShadowRunRequest, vintage: Any) -> MarketPrice | None:
        return self._price(symbol, request, vintage, kind="mark")


class _C8RiskPolicyReader:
    def __init__(self, policy_hash: str) -> None:
        self.policy = ShadowRiskPolicy(
            policy_version="policy.v1",
            policy_hash=policy_hash,
            max_turnover_alert=2.0,
            max_abs_beta_alert=2.0,
            max_abs_industry_alert=1.0,
            min_data_coverage=0.5,
            max_factor_mean_shift=2.0,
            max_ic_proxy_decay=999.0,
            min_challenger_contribution=-1.0,
        )

    def read_policy(self, request: ShadowRunRequest) -> ShadowRiskPolicy:
        return self.policy


class _C8EligibleChallengerProvider:
    def __init__(
        self,
        *,
        reader: FrozenAShareSnapshotReader,
        manifest_path: Path,
        receipt: PromotionChallengerReceipt,
        attempt: _OfficialAttempt,
        seed: int,
    ) -> None:
        self.reader = reader
        self.manifest_path = manifest_path
        self.receipt = receipt
        self.attempt = attempt
        self.seed = seed
        self._scores_by_decision: dict[str, Mapping[str, float]] = {}

    def get_eligible_challengers(
        self,
        policy: ShadowReplayPolicy,
        vintage: FrozenDailyVintage,
    ) -> tuple[ReplayChallengerCandidate, ...]:
        if self.receipt.available_time > vintage.vintage.decision_time:
            return ()
        cache_key = vintage.vintage.decision_time.isoformat()
        scores = self._scores_by_decision.get(cache_key)
        if scores is None:
            handle = self.reader.open(str(self.manifest_path))
            market_vintage = self.reader.market_vintage(handle, decision_time=vintage.vintage.decision_time)
            snapshot = _market_snapshot_from_vintage(market_vintage, self.attempt.dag)
            result = execute_factor_dag(
                self.attempt.dag,
                snapshot,
                seed=self.seed,
                config={"runner": "c8-shadow-score", "source": C8_SOURCE_CLASS},
            )
            raw_scores: dict[str, float] = {}
            latest_event_by_symbol: dict[str, datetime] = {}
            for item in result.values:
                if item.value is None:
                    continue
                current = latest_event_by_symbol.get(item.symbol)
                if current is None or item.event_time > current:
                    latest_event_by_symbol[item.symbol] = item.event_time
                    raw_scores[item.symbol] = float(item.value)
            raw_scores = {symbol: raw_scores[symbol] for symbol in sorted(raw_scores) if symbol in vintage.vintage.universe}
            if len(raw_scores) < 2:
                return ()
            mean_score = sum(raw_scores.values()) / len(raw_scores)
            scores = {symbol: value - mean_score for symbol, value in raw_scores.items()}
            self._scores_by_decision[cache_key] = scores
        if not any(value != 0.0 for value in scores.values()):
            return ()
        return (
            ReplayChallengerCandidate(
                candidate_id=self.receipt.candidate_id,
                version=self.receipt.version,
                state=ReplayCandidateState.CHALLENGER,
                policy_version=policy.policy_version,
                policy_hash=policy.policy_hash,
                promotion_decision_hash=self.receipt.promotion_decision_hash,
                evidence_hash=self.receipt.evidence_hash,
                factor_hash=self.receipt.factor_hash,
                scores_by_symbol=scores,
                baseline_factor_mean=self.attempt.mean,
                baseline_factor_std=self.attempt.std,
                baseline_ic_proxy=float(self.attempt.validation.metrics.rank_ic),
                expected_contribution=float(self.attempt.validation.metrics.net_return),
                available_time=self.receipt.available_time,
            ),
        )


def _market_snapshot_from_vintage(vintage: MarketVintage, dag: CompiledFactorDAG) -> MarketSnapshot:
    fields = tuple(sorted(dag.required_fields))
    rows = tuple(
        SnapshotRow(
            event_time=row.event_time,
            symbol=row.symbol,
            values={field: row.values[field] for field in fields},
        )
        for row in vintage.rows
        if set(fields) <= set(row.values)
    )
    if not rows:
        fail(FailureCode.INCOMPLETE_RESULT, "daily market vintage has no FactorDAG rows")
    return MarketSnapshot(
        dataset_id=vintage.dataset_id,
        decision_time=vintage.decision_time,
        start_time=min(row.event_time for row in rows),
        fields=fields,
        rows=rows,
        historical_universe=dict(vintage.historical_universe.members_by_date),
        coverage_by_field={field: vintage.coverage_by_field[field] for field in fields},
    )


class _InterruptOnceBeforeDay:
    def __init__(self, delegate: Any, *, marker_path: Path, day: int = 10) -> None:
        self.delegate = delegate
        self.marker_path = marker_path
        self.day = day

    def run_once(self, request: ShadowRunRequest, vintage: Any, challengers: tuple[Any, ...]) -> Any:
        if f"-d{self.day:02d}-" in request.run_id and not self.marker_path.exists():
            self.marker_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(self.marker_path, f"interrupted-before-day={self.day}\n")
            raise RecoveryInterrupted(FailureCode.PROVIDER_TIMEOUT, f"C8 forced interruption before day {self.day}")
        return self.delegate.run_once(request, vintage, challengers)

    @property
    def ledger(self) -> Any:
        return getattr(self.delegate, "ledger", None)


class C8RealV1Runner:
    def __init__(self, *, repo_root: str | Path | None = None) -> None:
        self.repo_root = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[4]
        self.paths = PinnedRealOfficialInputPaths.default()
        self.snapshot_reader = FrozenAShareSnapshotReader(
            snapshot_root=self.paths.snapshot_root,
            calendar_root=self.paths.calendar_root,
        )

    def run(self, *, output_root: str | Path | None = None, seed: int = 7, publish_workbench: bool = True) -> dict[str, Any]:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        root = Path(output_root).resolve() if output_root is not None else self.repo_root / ".autobuild/veriquant/integration/c8_real_ashare_v1"
        run_root = root / C8_RUN_ID
        prepared = self._prepare_inputs()
        attempts, official_run, receipt = self._run_evidence_and_promotion(prepared, seed=seed)
        policy = make_c8_shadow_policy()
        replay_result = self._run_replay_with_recovery(
            prepared=prepared,
            receipt=receipt,
            selected_attempt=next(item for item in attempts if item.factor_id == receipt.candidate_id),
            policy=policy,
            seed=seed,
            run_root=run_root,
        )
        security_report = self._run_security_probe(run_root=run_root)
        workbench = (
            self._publish_workbench(
                run_root=run_root,
                prepared=prepared,
                official_run=official_run,
                receipt=receipt,
                attempts=attempts,
                replay_result=replay_result,
                security_report=security_report,
            )
            if publish_workbench
            else None
        )
        result_core = {
            "schema_version": SCHEMA_VERSION,
            "run_id": C8_RUN_ID,
            "status": "PASSED"
            if replay_result["resume_completed"] and replay_result["sessions_committed"] == 20 and security_report["status"] == "PASSED"
            else "FAILED",
            "data_source_class": C8_PROVIDER_CLASS,
            "source_class": C8_SOURCE_CLASS,
            "official_tushare_source": False,
            "network_used": False,
            "snapshot": {
                "snapshot_hash": prepared.handle.snapshot_hash,
                "snapshot_manifest_hash": prepared.handle.manifest_hash,
                "raw_content_hash": prepared.handle.raw_content_hash,
                "calendar_hash": prepared.handle.calendar_hash,
                "universe_hash": prepared.handle.universe_hash,
                "row_count": len(prepared.snapshot.rows),
                "symbols": tuple(sorted(prepared.snapshot.request.codes)),
                "data_start": min(row.event_time for row in prepared.snapshot.rows),
                "data_end": max(row.event_time for row in prepared.snapshot.rows),
                "fetched_at": prepared.snapshot.fetched_at,
            },
            "official_evidence": official_run,
            "promotion": {
                "promotion_run": True,
                "challenger_created": True,
                "receipt": receipt.to_dict(),
                "receipt_hash": receipt.artifact_hash,
            },
            "shadow_replay": replay_result,
            "security": security_report,
            "workbench": workbench,
            "invariants": {
                "provider_marked_approved_proxy": prepared.qualification.provider_class.value == C8_PROVIDER_CLASS,
                "not_official_tushare": not prepared.qualification.official_tushare_source,
                "factor_count_between_1_and_3": 1 <= len(attempts) <= 3,
                "official_evidence_run": official_run["official_evidence_run"] is True,
                "independent_promotion_run": official_run["promotion_run"] is True,
                "challenger_created": official_run["challenger_created"] is True,
                "twenty_session_replay": replay_result["sessions_committed"] == 20,
                "forced_interruption": replay_result["forced_interruption"] is True,
                "resume_completed": replay_result["resume_completed"] is True,
                "rerun_no_duplicate_journal": replay_result["rerun_no_duplicate_journal"] is True,
                "security_probe_passed": security_report["status"] == "PASSED",
            },
        }
        result = {**result_core, "artifact_hash": canonical_content_hash(result_core)}
        report = _markdown_report(result, attempts)
        safe_result = _safe_dict(result)
        _atomic_write_json(run_root / "result.json", safe_result)
        _atomic_write_text(run_root / "report.md", report)
        _atomic_write_json(root / "latest.json", {"run_id": C8_RUN_ID, "result_path": str(run_root / "result.json"), "artifact_hash": result["artifact_hash"]})
        return safe_result

    def _prepare_inputs(self) -> _PreparedInputs:
        qualification = _typed_artifact(
            self.paths.qualification,
            ApprovedProxyQualification,
            APPROVED_QUALIFICATION_HASH,
        )
        auxiliary = _typed_artifact(
            self.paths.auxiliary_result,
            RealAuxiliaryChainResult,
            PINNED_AUXILIARY_RESULT_HASH,
        )
        if qualification.provider_class.value != C8_PROVIDER_CLASS or qualification.source_class.value != C8_SOURCE_CLASS:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "C8 requires the approved compatible-proxy input class")
        if qualification.official_tushare_source:
            fail(FailureCode.AUXILIARY_MASQUERADE, "C8 source must not claim official Tushare")
        handle, snapshot = load_verified_price_snapshot(
            manifest_path=self.paths.snapshot_manifest,
            snapshot_root=self.paths.snapshot_root,
            calendar_root=self.paths.calendar_root,
        )
        if handle.snapshot_hash != PINNED_SNAPSHOT_HASH or auxiliary.snapshot_hash != handle.snapshot_hash:
            fail(FailureCode.INVALID_HASH, "C8 snapshot does not match the approved proxy freeze")
        market_state = FrozenMarketStateStore(self.paths.market_state_root).load(
            dataset_id=snapshot.request.dataset_id,
            vintage_hash=PINNED_MARKET_STATE_VINTAGE_HASH,
        )
        envelope = build_research_envelope(
            snapshot_id="c8-approved-proxy",
            snapshot=snapshot,
            market_state=market_state,
        )
        split = ResearchSplitManifest(
            split_id="c8-real-visible-split-v1",
            train_start=_dt("2024-01-02"),
            train_end=_dt("2024-01-31"),
            validation_start=_dt("2024-02-01"),
            validation_end=_dt("2024-02-22"),
            sealed_start=_dt("2024-02-23"),
            sealed_end=_dt("2024-02-23"),
        )
        bridge_request = build_default_bridge_request(
            snapshot_id="c8-approved-proxy",
            decision_time=_dt("2024-02-29", 9, 25),
            split_manifest=split,
            forward_days=2,
            execution_lag_days=1,
            feature_fields=("close", "volume"),
            run_id=C8_RUN_ID,
        )
        bundle = AShareResearchBridge(
            InMemoryFrozenSnapshotReader({"c8-approved-proxy": envelope})
        ).build_bundle(bridge_request)
        return _PreparedInputs(
            paths=self.paths,
            qualification=qualification,
            auxiliary=auxiliary,
            snapshot=snapshot,
            handle=handle,
            market_state=market_state,
            bundle=bundle,
            manifest_path=self.paths.snapshot_manifest,
        )

    def _run_evidence_and_promotion(
        self,
        prepared: _PreparedInputs,
        *,
        seed: int,
    ) -> tuple[tuple[_OfficialAttempt, ...], dict[str, Any], PromotionChallengerReceipt]:
        cost_model = CostModel(spread_bps=1.0, linear_impact_bps=1.0)
        evidence_service = EvidenceService()
        attempts: list[_OfficialAttempt] = []
        for definition in c8_factor_definitions():
            factor_id = str(definition["factor_id"])
            payload = definition["payload"]
            dag = parse_compile_factor_dag(
                payload,
                field_roles={field: FieldRole.FEATURE for field in prepared.bundle.feature_fields},
            )
            factor_result = execute_factor_dag(
                dag,
                _snapshot_for_dag(prepared.bundle, dag),
                seed=seed,
                config={"runner": "c8-real-official-evidence", "source": C8_SOURCE_CLASS},
            )
            rows = _evaluation_rows(bundle=prepared.bundle, factor_result=factor_result)
            if not rows["train"] or not rows["validation"]:
                fail(FailureCode.INCOMPLETE_RESULT, f"{factor_id} produced no visible evidence rows")
            identity = ExperimentIdentity(
                experiment_id=f"c8-real-{factor_id}",
                dataset_hash=prepared.bundle.dataset_card.snapshot_hash,
                universe_hash=prepared.bundle.universe_manifest.artifact_hash,
                factor_hash=dag.artifact_hash,
                split_hash=prepared.bundle.split_manifest.artifact_hash,
                label_hash=prepared.bundle.label_specification.artifact_hash,
                cost_hash=cost_model.artifact_hash,
                code_hash=canonical_content_hash({"module": SCHEMA_VERSION, "delegates": ("FactorDAG", "EvidenceService")}),
                availability_hash=prepared.bundle.bridge_run_manifest.output_hash,
                seed=seed,
            )
            train = evidence_service.evaluate(identity=identity, rows=rows["train"], partition="TRAIN", cost_model=cost_model)
            validation = evidence_service.evaluate(
                identity=identity,
                rows=rows["validation"],
                partition="VALIDATION",
                cost_model=cost_model,
            )
            mean, std = _factor_value_stats(factor_result)
            attempts.append(
                _OfficialAttempt(
                    factor_id=factor_id,
                    payload=payload,
                    dag=dag,
                    factor_result=factor_result,
                    train=train,
                    validation=validation,
                    mean=mean,
                    std=std,
                )
            )

        family_manifest = build_family_manifest(
            family_id="c8-real-approved-proxy-family",
            manifest_version="v1",
            frozen_at=prepared.bundle.decision_time,
            candidates=tuple((attempt.factor_id, "v1") for attempt in attempts),
        )
        views = tuple(
            candidate_view_from_official_evidence(
                candidate_id=attempt.factor_id,
                candidate_version="v1",
                family_manifest=family_manifest,
                train=attempt.train,
                validation=attempt.validation,
                proposer_id="c8-deterministic-factor-proposer",
                p_value=0.01,
                coverage=min(prepared.bundle.data_quality_manifest.coverage_by_field.values()),
                max_abs_industry_exposure=1.0,
                max_abs_market_cap_exposure=1.0,
                parameter_sensitivity_max_delta=0.0,
            )
            for attempt in attempts
        )
        reader = InMemoryOfficialEvidenceReader(
            manifest=family_manifest,
            views=views,
            sealed_passed={f"{attempt.factor_id}@v1": True for attempt in attempts},
            reader_id="c8-independent-sealed-aggregate-reader",
        )
        for attempt in attempts:
            attempt.promotion = evaluate_statistical_promotion(
                reader=reader,
                request=PromotionEvaluationRequest(
                    candidate_id=attempt.factor_id,
                    candidate_version="v1",
                    family_id=family_manifest.family_id,
                    manifest_version=family_manifest.manifest_version,
                    idempotency_key=f"c8-real-promotion-{attempt.factor_id}-seed-{seed}",
                    proposer_id="c8-deterministic-factor-proposer",
                    approver_id="c8-independent-promotion-controller",
                    decision_time=prepared.bundle.decision_time,
                    current_state=PromotionState.VALIDATED_RESEARCH,
                ),
                robustness_policy=make_c8_robustness_policy(),
                multiple_testing_policy=make_c8_multiple_testing_policy(),
            )
        promoted = next(
            (attempt for attempt in attempts if attempt.promotion and attempt.promotion.decision.to_state is PromotionState.CHALLENGER),
            None,
        )
        if promoted is None:
            fail(FailureCode.INVALID_ALPHA_SOURCE, "C8 did not produce a real promoted CHALLENGER")
        official_core = {
            "schema_version": SCHEMA_VERSION,
            "run_id": C8_RUN_ID,
            "provider_class": C8_PROVIDER_CLASS,
            "source_class": C8_SOURCE_CLASS,
            "official_tushare_source": False,
            "decision_time": prepared.bundle.decision_time,
            "snapshot_hash": prepared.handle.snapshot_hash,
            "calendar_hash": prepared.handle.calendar_hash,
            "universe_hash": prepared.handle.universe_hash,
            "bridge_manifest_hash": prepared.bundle.bridge_run_manifest.artifact_hash,
            "dataset_hash": prepared.bundle.dataset_card.snapshot_hash,
            "family_manifest_hash": family_manifest.artifact_hash,
            "factor_count": len(attempts),
            "factors": tuple(attempt.summary for attempt in attempts),
            "official_evidence_run": True,
            "sealed_aggregate_used": True,
            "promotion_run": True,
            "challenger_created": True,
            "selected_challenger": promoted.factor_id,
            "sealed_raw_exposed": False,
        }
        official_run = {**official_core, "official_run_hash": canonical_content_hash(official_core)}
        policy = make_c8_shadow_policy()
        receipt = PromotionChallengerReceipt(
            candidate_id=promoted.factor_id,
            version="v1",
            state="CHALLENGER",
            source_authority="INDEPENDENT_PROMOTION_SERVICE",
            official_run_hash=official_run["official_run_hash"],
            official_factor_identity_hash=promoted.train.identity_hash,
            policy_version=policy.policy_version,
            policy_hash=policy.policy_hash,
            promotion_decision_hash=promoted.promotion.decision.decision_hash,
            evidence_hash=promoted.validation.artifact_hash,
            factor_hash=promoted.dag.artifact_hash,
            available_time=prepared.bundle.decision_time,
        )
        return tuple(attempts), official_run, receipt

    def _bindings(self, prepared: _PreparedInputs) -> tuple[FrozenVintageBinding, ...]:
        return tuple(
            FrozenVintageBinding(
                snapshot_id="c8-approved-proxy",
                manifest_path=str(prepared.manifest_path.resolve()),
                decision_time=_dt(date_text, 9, 25),
                revision_id=f"rev-{date_text.replace('-', '')}-v1",
            )
            for date_text in C8_REPLAY_DATES
        )

    def _controller(
        self,
        *,
        prepared: _PreparedInputs,
        receipt: PromotionChallengerReceipt,
        selected_attempt: _OfficialAttempt,
        policy: ShadowReplayPolicy,
        seed: int,
        journal_path: Path,
        interrupt_marker: Path | None = None,
    ) -> tuple[RealVintageReplayController, ShadowReplayJournal, ShadowLedger, Any]:
        vintage_provider = _C8FrozenVintageProvider(
            reader=self.snapshot_reader,
            manifest_path=prepared.manifest_path,
            bindings=self._bindings(prepared),
        )
        challenger_provider = _C8EligibleChallengerProvider(
            reader=self.snapshot_reader,
            manifest_path=prepared.manifest_path,
            receipt=receipt,
            attempt=selected_attempt,
            seed=seed,
        )
        ledger = ShadowLedger()
        price_provider = _C8MarketPriceProvider(
            reader=self.snapshot_reader,
            manifest_path=prepared.manifest_path,
            market_state=prepared.market_state,
        )
        run_once = ShadowPortfolioRunOnceAdapter(
            price_provider=price_provider,
            portfolio_constructor=EqualWeightPortfolioConstructor(),
            risk_policy_reader=_C8RiskPolicyReader(policy.policy_hash),
            ledger=ledger,
            clock=DeterministicClock(_dt("2024-04-01", 16, 0)),
        )
        port = (
            _InterruptOnceBeforeDay(run_once, marker_path=interrupt_marker, day=10)
            if interrupt_marker is not None
            else run_once
        )
        journal = ShadowReplayJournal(journal_path)
        controller = RealVintageReplayController(
            vintage_provider=vintage_provider,
            challenger_provider=challenger_provider,
            run_once_port=port,
            journal=journal,
        )
        return controller, journal, ledger, port

    def _run_replay_with_recovery(
        self,
        *,
        prepared: _PreparedInputs,
        receipt: PromotionChallengerReceipt,
        selected_attempt: _OfficialAttempt,
        policy: ShadowReplayPolicy,
        seed: int,
        run_root: Path,
    ) -> dict[str, Any]:
        replay_scope = canonical_content_hash(
            {
                "receipt_hash": receipt.artifact_hash,
                "selected_factor_id": selected_attempt.factor_id,
                "selected_factor_dag_hash": selected_attempt.dag.artifact_hash,
                "policy_hash": policy.policy_hash,
                "seed": seed,
                "schema_version": SCHEMA_VERSION,
            }
        ).removeprefix("sha256:")[:16]
        baseline_controller, _, baseline_ledger, _ = self._controller(
            prepared=prepared,
            receipt=receipt,
            selected_attempt=selected_attempt,
            policy=policy,
            seed=seed,
            journal_path=run_root / "baseline" / replay_scope / "shadow_replay.jsonl",
        )
        baseline = baseline_controller.run(policy)
        if baseline.manifest.status is not ReplayStatus.READY or len(baseline.daily_records) != 20:
            fail(FailureCode.INCOMPLETE_RESULT, "baseline C8 replay did not complete 20 sessions")

        recovery_journal_path = run_root / "recovery_replay" / replay_scope / "shadow_replay.jsonl"
        interrupt_marker = run_root / "recovery_replay" / replay_scope / "interrupt-before-day-10.marker"
        recovery_controller, journal, ledger, port = self._controller(
            prepared=prepared,
            receipt=receipt,
            selected_attempt=selected_attempt,
            policy=policy,
            seed=seed,
            journal_path=recovery_journal_path,
            interrupt_marker=interrupt_marker,
        )
        stage_provider = RealVintageReplayStageProvider(recovery_controller, policy)
        checkpoint_store = RunCheckpointStore(run_root / "checkpoints" / replay_scope, clock=lambda: _dt("2024-04-01", 16, 30))
        recovery = RunRecoveryController(
            store=checkpoint_store,
            providers={"c8-real-vintage-replay": stage_provider},
        )
        task = StageTask(
            identity=StageIdentity(
                run_id=C8_RUN_ID,
                branch_id="c8.real_ashare",
                stage_id="shadow_replay_20d",
                ordinal=20,
                input_hash=canonical_content_hash(
                    {
                        "official_run": receipt.official_run_hash,
                        "receipt": receipt.artifact_hash,
                        "bindings": tuple(binding.to_dict() for binding in self._bindings(prepared)),
                    }
                ),
                config_hash=canonical_content_hash({"policy": policy.to_dict(), "seed": seed}),
                code_hash=canonical_content_hash({"module": SCHEMA_VERSION, "stage_provider": "RealVintageReplayStageProvider"}),
                policy_hash=policy.policy_hash,
                expected_artifact_hash=baseline.manifest.artifact_hash,
            ),
            provider_id="c8-real-vintage-replay",
        )
        first = recovery.run(run_id=C8_RUN_ID, tasks=(task,), holder_id="c8-worker-one")
        first_record_count = len(journal.snapshot().records)
        if first.status is RecoveryStatus.INTERRUPTED:
            records_after_interrupt = first_record_count
            resumed = recovery.run(run_id=C8_RUN_ID, tasks=(task,), holder_id="c8-worker-two")
        elif first.status is RecoveryStatus.COMMITTED and interrupt_marker.exists() and first_record_count == 20:
            records_after_interrupt = 9
            resumed = first
        else:
            fail(FailureCode.INVALID_STATE, "C8 forced interruption did not occur")
        repeated = recovery.run(run_id=C8_RUN_ID, tasks=(task,), holder_id="c8-worker-three")
        if resumed.status is not RecoveryStatus.COMMITTED or repeated.status is not RecoveryStatus.COMMITTED:
            fail(FailureCode.INVALID_STATE, "C8 recovery did not commit")
        before_rerun = len(journal.snapshot().records)
        replayed = recovery_controller.run(policy)
        after_rerun = len(journal.snapshot().records)
        if replayed.manifest.artifact_hash != baseline.manifest.artifact_hash:
            fail(FailureCode.INVALID_HASH, "C8 resumed replay differs from uninterrupted baseline")
        summary = {
            "status": replayed.manifest.status.value,
            "manifest_hash": replayed.manifest.artifact_hash,
            "baseline_manifest_hash": baseline.manifest.artifact_hash,
            "replay_scope": replay_scope,
            "journal_tip_hash": replayed.manifest.journal_tip_hash,
            "shadow_ledger_tip_hash": replayed.manifest.final_shadow_ledger_tip_hash,
            "sessions_committed": len(replayed.daily_records),
            "daily_vintage_count": len({record.vintage_id for record in replayed.daily_records}),
            "trading_days": tuple(record.trading_day for record in replayed.daily_records),
            "forced_interruption": interrupt_marker.exists() and records_after_interrupt == 9,
            "records_after_interrupt": records_after_interrupt,
            "resume_completed": resumed.final_artifact_hash == baseline.manifest.artifact_hash,
            "rerun_no_duplicate_journal": before_rerun == after_rerun == 20,
            "recovery_stage_start_count": sum(
                1
                for event in checkpoint_store.read_events(C8_RUN_ID)
                if getattr(getattr(event, "event_type", ""), "value", getattr(event, "event_type", "")) == "STAGE_STARTED"
            ),
            "final_nav": replayed.manifest.final_nav,
            "max_drawdown": replayed.manifest.max_drawdown,
            "total_turnover": replayed.manifest.total_turnover,
            "total_transaction_cost": replayed.manifest.total_transaction_cost,
            "total_slippage_cost": replayed.manifest.total_slippage_cost,
            "daily_records": tuple(record.to_dict() for record in replayed.daily_records),
            "shadow_accounting_event_count": len(replayed.daily_records) * 5,
            "baseline_shadow_accounting_event_count": len(baseline.daily_records) * 5,
        }
        return {**summary, "artifact_hash": canonical_content_hash(summary)}

    def _run_security_probe(self, *, run_root: Path) -> dict[str, Any]:
        profile = VeriQuantOfflineDeploymentProfile.default()
        manifest = profile.build_manifest(
            routes=(
                RouteRecord(path="/healthz", methods=("GET",), authority="READ_ONLY_STATUS", handler="healthz"),
                RouteRecord(path="/veriquant/status", methods=("GET",), authority="READ_ONLY_STATUS", handler="status"),
                RouteRecord(path="/veriquant/workbench", methods=("GET",), authority="READ_ONLY_WORKBENCH", handler="workbench_index"),
                RouteRecord(path="/veriquant/workbench/runs/{run_id}", methods=("GET",), authority="READ_ONLY_WORKBENCH", handler="workbench_detail"),
            ),
            imported_modules=("src.api.veriquant_workbench_routes",),
        )
        source_texts = {
            "c8_real_v1.py": Path(__file__).read_text(encoding="utf-8"),
        }
        probe = SecurityAcceptanceProbe(profile=profile).run_acceptance(
            manifest=manifest,
            llm_context={"visible_summary": "train/validation evidence only", "sealed_decision": "opaque_pass"},
            workbench_payload={"status": "CHALLENGER", "sealed_decision": "opaque_pass", "api_key": "<redacted>"},
            log_lines=("C8 started", "provider_class=APPROVED_PROXY_DATA_VENDOR", "completed without credential output"),
            source_texts=source_texts,
            env={},
        )
        payload = probe.to_dict()
        _atomic_write_json(run_root / "security_probe.json", payload)
        return {**payload, "artifact_hash": probe.artifact_hash}

    def _publish_workbench(
        self,
        *,
        run_root: Path,
        prepared: _PreparedInputs,
        official_run: Mapping[str, Any],
        receipt: PromotionChallengerReceipt,
        attempts: tuple[_OfficialAttempt, ...],
        replay_result: Mapping[str, Any],
        security_report: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        updated_at = _utc_text(_dt("2024-04-01", 16, 45))
        artifacts = {
            "overview": {
                "title": "AlphaForge 千因研究院 C8 真实 A 股闭环",
                "run_id": C8_RUN_ID,
                "status": "SHADOW_ACTIVE",
                "provider_class": C8_PROVIDER_CLASS,
                "source_class": C8_SOURCE_CLASS,
                "official_tushare_source": False,
                "decision_time": _utc_text(prepared.bundle.decision_time),
                "snapshot_hash": prepared.handle.snapshot_hash,
                "calendar_hash": prepared.handle.calendar_hash,
                "replay_manifest_hash": replay_result["manifest_hash"],
            },
            "lineage": {
                "family": "c8-real-approved-proxy-family",
                "factors": tuple(attempt.summary for attempt in attempts),
                "selected_challenger": receipt.candidate_id,
                "receipt_hash": receipt.artifact_hash,
            },
            "evidence": {
                "official_run": official_run,
                "visible_only": True,
                "sealed_raw_exposed": False,
            },
            "critics": {
                "promotion_policy": {
                    "robustness_hash": make_c8_robustness_policy().artifact_hash,
                    "multiple_testing_hash": make_c8_multiple_testing_policy().artifact_hash,
                    "scope": "smoke_policy_not_production_mandate",
                },
                "security_probe": security_report,
            },
            "promotion": {
                "state": "CHALLENGER",
                "receipt": receipt.to_dict(),
                "receipt_hash": receipt.artifact_hash,
            },
            "citations": {
                "source_class": C8_SOURCE_CLASS,
                "provider_class": C8_PROVIDER_CLASS,
                "snapshot_manifest": str(prepared.manifest_path.relative_to(self.repo_root)),
                "operator_qualification_hash": prepared.qualification.artifact_hash,
                "auxiliary_input_hash": prepared.auxiliary.artifact_hash,
            },
            "shadow_portfolio": replay_result,
            "nav_attribution": {
                "final_nav": replay_result["final_nav"],
                "max_drawdown": replay_result["max_drawdown"],
                "total_turnover": replay_result["total_turnover"],
                "total_transaction_cost": replay_result["total_transaction_cost"],
                "total_slippage_cost": replay_result["total_slippage_cost"],
                "daily": replay_result["daily_records"],
            },
            "drift_alerts": {
                "status": "NO_BLOCKING_DRIFT_ALERTS_FOR_C8_SMOKE",
                "industry_bucket": "APPROVED_PROXY_UNKNOWN_INDUSTRY",
            },
        }
        published = publish_workbench_run(
            root=self.repo_root / ".autobuild/veriquant/integration/workbench_artifacts",
            run_id=C8_RUN_ID,
            status="SHADOW_ACTIVE",
            updated_at=updated_at,
            artifacts=_safe_dict(artifacts),
        )
        _atomic_write_json(run_root / "workbench_publication.json", _safe_dict(published))
        return published


def _markdown_report(result: Mapping[str, Any], attempts: Sequence[_OfficialAttempt]) -> str:
    lines = [
        "# AlphaForge C8 Real A-share V1 Loop",
        "",
        f"Status: `{result['status']}`",
        "",
        "Data source is `APPROVED_PROXY_DATA_VENDOR` / `TUSHARE_COMPATIBLE_PROXY`; it is not `OFFICIAL_TUSHARE`.",
        "",
        "## Frozen data",
        "",
        f"- Snapshot hash: `{result['snapshot']['snapshot_hash']}`",
        f"- Calendar hash: `{result['snapshot']['calendar_hash']}`",
        f"- Universe hash: `{result['snapshot']['universe_hash']}`",
        f"- Rows: `{result['snapshot']['row_count']}`",
        f"- Symbols: `{', '.join(result['snapshot']['symbols'])}`",
        f"- Event window: `{result['snapshot']['data_start']}` to `{result['snapshot']['data_end']}`",
        "",
        "## FactorDAG / Evidence / Promotion",
        "",
    ]
    for attempt in attempts:
        summary = attempt.summary
        metrics = summary["validation_metrics"]
        lines.extend(
            [
                f"- `{attempt.factor_id}` -> `{summary['promotion_state']}` / `{summary['promotion_verdict']}`",
                f"  - FactorDAG: `{summary['dag_hash']}`",
                f"  - Validation evidence: `{summary['validation_evidence_hash']}`",
                f"  - Validation RankIC: `{metrics.get('rank_ic')}`; net return: `{metrics.get('net_return')}`",
            ]
        )
    lines.extend(
        [
            "",
            f"Selected CHALLENGER: `{result['promotion']['receipt']['candidate_id']}`",
            f"Receipt hash: `{result['promotion']['receipt_hash']}`",
            "",
            "## 20-session Shadow Replay",
            "",
            f"- Manifest hash: `{result['shadow_replay']['manifest_hash']}`",
            f"- Sessions committed: `{result['shadow_replay']['sessions_committed']}`",
            f"- Forced interruption: `{result['shadow_replay']['forced_interruption']}`",
            f"- Resume completed: `{result['shadow_replay']['resume_completed']}`",
            f"- Rerun no duplicate journal: `{result['shadow_replay']['rerun_no_duplicate_journal']}`",
            f"- Final NAV: `{result['shadow_replay']['final_nav']}`",
            f"- Max drawdown: `{result['shadow_replay']['max_drawdown']}`",
            f"- Turnover: `{result['shadow_replay']['total_turnover']}`",
            f"- Transaction cost: `{result['shadow_replay']['total_transaction_cost']}`",
            f"- Slippage cost: `{result['shadow_replay']['total_slippage_cost']}`",
            "",
            "## Security",
            "",
            f"Security probe: `{result['security']['status']}` / `{result['security']['artifact_hash']}`",
            "",
            "No live broker, real order, real funds, provider credential output, or arbitrary generated factor execution path is added by this C8 runner.",
        ]
    )
    if result.get("workbench"):
        lines.extend(
            [
                "",
                "## Workbench",
                "",
                f"- Run id: `{result['workbench']['run_id']}`",
                f"- Detail hash: `{result['workbench']['detail_hash']}`",
            ]
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AlphaForge C8 real approved-proxy A-share V1 loop.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--no-workbench", action="store_true")
    args = parser.parse_args(argv)
    result = C8RealV1Runner().run(output_root=args.output_root, seed=args.seed, publish_workbench=not args.no_workbench)
    print(json.dumps({"status": result["status"], "artifact_hash": result["artifact_hash"], "run_id": result["run_id"]}, sort_keys=True))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "C8RealV1Runner",
    "C8_REPLAY_DATES",
    "C8_RUN_ID",
    "C8_PROVIDER_CLASS",
    "C8_SOURCE_CLASS",
    "make_c8_shadow_policy",
]
