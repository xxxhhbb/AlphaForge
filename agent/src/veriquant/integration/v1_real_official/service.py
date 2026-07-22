"""Thin adapter from approved frozen proxy inputs to visible Official Evidence.

The adapter verifies existing content-addressed artifacts, rebuilds the existing
``ResearchDatasetBundle``, uses the existing restricted FactorDAG executor, and
delegates all metrics to ``EvidenceService``.  It never reads sealed evidence and
has no Promotion, portfolio, risk, order, or accounting authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.contracts.base import fail, require_hash, require_identifier
from veriquant.data import FieldRole, MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.evidence import (
    AttemptController,
    CostModel,
    EvaluationRow,
    EvidenceService,
    ExperimentIdentity,
    TrialLedger,
    TrialOutcome,
)
from veriquant.factors import execute_factor_dag, parse_compile_factor_dag
from veriquant.integration.v1_approved_proxy_policy import ApprovedProxyQualification
from veriquant.integration.v1_real_data_chain import (
    RealAuxiliaryChainResult,
    build_research_envelope,
    build_split,
    load_verified_price_snapshot,
)
from veriquant.proxy_research_smoke.api import default_auxiliary_factors
from veriquant.real_research.api import AShareResearchBridge, build_default_bridge_request
from veriquant.real_research.models import ResearchDatasetBundle
from veriquant.real_research.reader import InMemoryFrozenSnapshotReader
from veriquant.tushare_market_state.store import FrozenMarketStateStore

from .models import (
    FactorAttemptStatus,
    RealOfficialEvidenceFactorResult,
    RealOfficialEvidenceRun,
)


SCHEMA_VERSION = "veriquant.real-official-evidence/v1"
APPROVED_QUALIFICATION_HASH = "sha256:9ba9b4bca0e4592a1141dd3d8bcf41b0672ee698eadd9129b5e9f4af6f68af65"
PINNED_AUXILIARY_RESULT_HASH = "sha256:47c517aeb39ecf212f5cd34819ed1728a4d8abd6404891f8c20bd4a68b0fc88e"
PINNED_SNAPSHOT_HASH = "sha256:dd9b48413fae1c89c52e54b5f52cf7ef46de88fd3d98fc6bae776a1a30cd527a"
PINNED_MARKET_STATE_VINTAGE_HASH = "sha256:8a83adc155089ca38c377ac4e579584763b3cc53af0b2a77c7d443fe1949a992"


@dataclass(frozen=True, kw_only=True)
class FactorCandidate:
    factor_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        if not isinstance(self.payload, Mapping):
            fail(FailureCode.INVALID_FACTOR_DAG, "factor payload must be a mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, kw_only=True)
class PinnedRealOfficialInputPaths:
    repo_root: Path
    qualification: Path
    auxiliary_result: Path
    snapshot_manifest: Path
    snapshot_root: Path
    calendar_root: Path
    market_state_root: Path

    @classmethod
    def default(cls) -> "PinnedRealOfficialInputPaths":
        root = Path(__file__).resolve().parents[5]
        stage = root / ".autobuild/veriquant/integration/v1_tushare_proxy_stage"
        snapshot_hex = PINNED_SNAPSHOT_HASH.removeprefix("sha256:")
        return cls(
            repo_root=root,
            qualification=root / ".autobuild/veriquant/integration/v1_real_data/approved_proxy_qualification.json",
            auxiliary_result=root / ".autobuild/veriquant/integration/v1_real_data/real_auxiliary_smoke.json",
            snapshot_manifest=(
                stage
                / "snapshots/tushare-proxy-operational-smoke"
                / snapshot_hex
                / "manifest.json"
            ),
            snapshot_root=stage / "snapshots",
            calendar_root=root / ".autobuild/veriquant/calendars",
            market_state_root=root / ".autobuild/veriquant/integration/v1_real_data/market_state",
        )


def default_factor_candidates() -> tuple[FactorCandidate, ...]:
    """Reuse the two inert DRAFT payloads already bound by the real auxiliary run."""

    return tuple(
        FactorCandidate(factor_id=item.factor_id, payload=item.payload)
        for item in default_auxiliary_factors()
    )


def _typed_artifact(path: Path, contract_type: type[Any], expected_hash: str) -> Any:
    require_hash(expected_hash, "expected_hash")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(FailureCode.INCOMPLETE_RESULT, "pinned artifact is missing or invalid", path=path.name, error=type(exc).__name__)
    declared_hash = payload.pop("artifact_hash", None)
    typed = contract_type.from_payload(payload)
    if typed.artifact_hash != expected_hash or declared_hash != expected_hash:
        fail(FailureCode.INVALID_HASH, "pinned artifact hash changed", path=path.name)
    return typed


def _sha256_file(path: Path) -> str:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        fail(FailureCode.INCOMPLETE_RESULT, "trusted service source is unavailable", error=type(exc).__name__)


def _snapshot_for_partitions(
    bundle: ResearchDatasetBundle,
    *,
    fields: tuple[str, ...],
    partitions: frozenset[str],
) -> MarketSnapshot:
    rows = tuple(
        SnapshotRow(
            event_time=row.event_time,
            symbol=row.symbol,
            values={field: row.values[field] for field in fields},
        )
        for row in bundle.feature_rows
        if row.partition in partitions
    )
    if not rows:
        fail(FailureCode.INCOMPLETE_RESULT, "PIT bundle has no rows for visible partition")
    coverage = {
        field: sum(row.values[field] is not None for row in rows) / len(rows)
        for field in fields
    }
    visible_times = {row.event_time.isoformat() for row in rows}
    universe = {
        key: value
        for key, value in bundle.universe_manifest.historical_universe.items()
        if key in visible_times
    }
    return MarketSnapshot(
        dataset_id=bundle.dataset_card.dataset_id,
        decision_time=bundle.decision_time,
        start_time=min(row.event_time for row in rows),
        fields=fields,
        rows=rows,
        historical_universe=universe,
        coverage_by_field=coverage,
    )


def _evaluation_rows(
    *,
    bundle: ResearchDatasetBundle,
    factor_result: Any,
    partition: str,
) -> tuple[EvaluationRow, ...]:
    labels = {
        (row.event_time, row.symbol): row.value
        for row in bundle.label_rows
        if row.partition == partition
    }
    volume = {
        (row.event_time, row.symbol): float(row.values["volume"])
        for row in bundle.feature_rows
        if row.partition == partition and row.values.get("volume") is not None
    }
    output: list[EvaluationRow] = []
    for value in factor_result.values:
        key = (value.event_time, value.symbol)
        if key not in labels or value.value is None:
            continue
        row_volume = volume.get(key)
        if row_volume is None or row_volume <= 0.0:
            continue
        output.append(
            EvaluationRow(
                event_time=value.event_time,
                symbol=value.symbol,
                score=float(value.value),
                label=float(labels[key]),
                market_regime="APPROVED_PROXY_A_SHARE",
                volume=row_volume,
            )
        )
    if not output:
        fail(FailureCode.INCOMPLETE_RESULT, "factor produced no evaluable visible rows", partition=partition)
    return tuple(sorted(output, key=lambda row: (row.event_time, row.symbol)))


def _failure_outcome(phase: str) -> TrialOutcome:
    if phase == "PARSE":
        return TrialOutcome.PARSE_REJECTION
    if phase == "PIT":
        return TrialOutcome.PIT_REJECTION
    return TrialOutcome.EVALUATION_REJECTION


class RealOfficialEvidenceAdapter:
    """Verify pinned real inputs and compute TRAIN/VALIDATION official evidence."""

    def __init__(self, *, paths: PinnedRealOfficialInputPaths | None = None) -> None:
        self.paths = paths or PinnedRealOfficialInputPaths.default()

    def _load_inputs(self) -> tuple[ApprovedProxyQualification, RealAuxiliaryChainResult, ResearchDatasetBundle]:
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
        if qualification.auxiliary_result_hash != auxiliary.artifact_hash:
            fail(FailureCode.INVALID_HASH, "qualification does not bind the auxiliary frozen run")
        if not qualification.eligible_for_official_evidence_inputs:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "provider input is not eligible for official Evidence")
        if qualification.official_tushare_source:
            fail(FailureCode.AUXILIARY_MASQUERADE, "proxy qualification claims official Tushare")
        handle, snapshot = load_verified_price_snapshot(
            manifest_path=self.paths.snapshot_manifest,
            snapshot_root=self.paths.snapshot_root,
            calendar_root=self.paths.calendar_root,
        )
        if (
            handle.snapshot_hash != auxiliary.snapshot_hash
            or handle.calendar_hash != auxiliary.calendar_hash
            or handle.universe_hash != auxiliary.universe_hash
        ):
            fail(FailureCode.INVALID_HASH, "verified snapshot identities differ from qualification inputs")
        market_state = FrozenMarketStateStore(self.paths.market_state_root).load(
            dataset_id=snapshot.request.dataset_id,
            vintage_hash=PINNED_MARKET_STATE_VINTAGE_HASH,
        )
        if (
            market_state.manifest.artifact_hash != auxiliary.market_state_manifest_hash
            or market_state.manifest.vintage_hash != auxiliary.market_state_vintage_hash
            or market_state.capture.artifact_hash != auxiliary.market_state_capture_hash
        ):
            fail(FailureCode.INVALID_HASH, "frozen market-state identities changed")
        snapshot_id = "tushare-proxy-operational-smoke"
        envelope = build_research_envelope(snapshot_id=snapshot_id, snapshot=snapshot, market_state=market_state)
        request = build_default_bridge_request(
            snapshot_id=snapshot_id,
            decision_time=snapshot.request.decision_time,
            split_manifest=build_split(snapshot),
            forward_days=5,
            execution_lag_days=1,
            feature_fields=("close", "volume"),
            run_id="approved-proxy-real-official-visible",
        )
        bundle = AShareResearchBridge(
            InMemoryFrozenSnapshotReader({snapshot_id: envelope})
        ).build_bundle(request)
        if bundle.dataset_card.snapshot_hash != bundle.bridge_run_manifest.output_hash:
            fail(FailureCode.INVALID_HASH, "research bundle content identity is inconsistent")
        return qualification, auxiliary, bundle

    def run(
        self,
        *,
        output_root: str | Path,
        run_id: str = "approved-proxy-real-official-visible",
        seed: int = 7,
        factors: Sequence[FactorCandidate] | None = None,
        family_budget: int | None = None,
    ) -> RealOfficialEvidenceRun:
        require_identifier(run_id, "run_id")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        candidates = tuple(factors or default_factor_candidates())
        if not candidates or any(not isinstance(item, FactorCandidate) for item in candidates):
            fail(FailureCode.INVALID_VALUE, "typed factor candidates are required")
        budget = len(candidates) if family_budget is None else family_budget
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < len(candidates):
            fail(FailureCode.INVALID_VALUE, "family budget must cover the bounded candidate list")
        qualification, auxiliary, bundle = self._load_inputs()
        root = Path(output_root)
        root.mkdir(parents=True, exist_ok=True)
        ledger = TrialLedger(root / "trial_ledger.sqlite3", clock=lambda: bundle.decision_time)
        family_id = f"{run_id}:approved-proxy-real"
        ledger.create_family(family_id, budget)
        controller = AttemptController(ledger)
        cost_model = CostModel(spread_bps=1.0, linear_impact_bps=1.5)
        evidence_service = EvidenceService()
        source_root = Path(__file__).resolve().parents[2]
        code_hash = canonical_content_hash(
            {
                "adapter_schema": SCHEMA_VERSION,
                "adapter_source": _sha256_file(Path(__file__)),
                "evidence_service_source": _sha256_file(source_root / "evidence/service.py"),
                "factor_executor_source": _sha256_file(source_root / "factors/dag.py"),
            }
        )
        availability_hash = canonical_content_hash(
            {
                "qualification_hash": qualification.artifact_hash,
                "bridge_manifest_hash": bundle.bridge_run_manifest.artifact_hash,
                "snapshot_hash": auxiliary.snapshot_hash,
                "calendar_hash": auxiliary.calendar_hash,
                "market_state_manifest_hash": auxiliary.market_state_manifest_hash,
            }
        )
        allowed_dag_hashes = set(auxiliary.factor_dag_hashes)
        results: list[RealOfficialEvidenceFactorResult] = []
        for index, candidate in enumerate(candidates):
            payload_hash = canonical_content_hash(candidate.payload)
            identity = ExperimentIdentity(
                experiment_id=f"{run_id}:{candidate.factor_id}",
                dataset_hash=bundle.dataset_card.snapshot_hash,
                universe_hash=bundle.universe_manifest.artifact_hash,
                factor_hash=payload_hash,
                split_hash=bundle.split_manifest.artifact_hash,
                label_hash=bundle.label_specification.artifact_hash,
                cost_hash=cost_model.artifact_hash,
                code_hash=code_hash,
                availability_hash=availability_hash,
                seed=seed,
            )
            attempt = controller.start(
                family_id=family_id,
                trial_id=f"{run_id}:trial-{index:02d}-{candidate.factor_id}",
                identity=identity,
                proposer_id="deterministic-static-real-factor-proposer",
            )
            phase = "PARSE"
            dag_hash: str | None = None
            train_result_hash: str | None = None
            validation_result_hash: str | None = None
            try:
                dag = parse_compile_factor_dag(
                    candidate.payload,
                    field_roles={field: FieldRole.FEATURE for field in bundle.feature_fields},
                )
                dag_hash = dag.artifact_hash
                if dag_hash not in allowed_dag_hashes:
                    fail(FailureCode.INVALID_HASH, "FactorDAG is not bound by the pinned auxiliary run")
                phase = "PIT"
                fields = tuple(sorted(set(dag.required_fields)))
                train_snapshot = _snapshot_for_partitions(
                    bundle,
                    fields=fields,
                    partitions=frozenset({"train"}),
                )
                visible_snapshot = _snapshot_for_partitions(
                    bundle,
                    fields=fields,
                    partitions=frozenset({"train", "validation"}),
                )
                train_result = execute_factor_dag(
                    dag,
                    train_snapshot,
                    seed=seed,
                    config={"authority": "EVIDENCE_SERVICE", "view": "TRAIN"},
                )
                validation_result = execute_factor_dag(
                    dag,
                    visible_snapshot,
                    seed=seed,
                    config={"authority": "EVIDENCE_SERVICE", "view": "TRAIN_VALIDATION"},
                )
                train_result_hash = train_result.artifact_hash
                validation_result_hash = validation_result.artifact_hash
                phase = "EVALUATION"
                train_rows = _evaluation_rows(
                    bundle=bundle,
                    factor_result=train_result,
                    partition="train",
                )
                validation_rows = _evaluation_rows(
                    bundle=bundle,
                    factor_result=validation_result,
                    partition="validation",
                )
                train_evidence = evidence_service.evaluate(
                    identity=identity,
                    rows=train_rows,
                    partition="TRAIN",
                    cost_model=cost_model,
                )
                validation_evidence = evidence_service.evaluate(
                    identity=identity,
                    rows=validation_rows,
                    partition="VALIDATION",
                    cost_model=cost_model,
                )
                terminal = controller.commit_success(attempt)
                results.append(
                    RealOfficialEvidenceFactorResult(
                        factor_id=candidate.factor_id,
                        status=FactorAttemptStatus.OFFICIAL_VISIBLE_EVIDENCE,
                        trial_id=attempt.trial_id,
                        identity_hash=identity.identity_hash,
                        candidate_payload_hash=payload_hash,
                        factor_dag_hash=dag_hash,
                        train_factor_result_hash=train_result_hash,
                        validation_factor_result_hash=validation_result_hash,
                        train_evidence=train_evidence,
                        validation_evidence=validation_evidence,
                        ledger_terminal_event_hash=terminal.event_hash,
                        failure_code=None,
                        failure_message=None,
                    )
                )
            except ContractValidationError as exc:
                terminal = controller.commit_failure(
                    attempt,
                    outcome=_failure_outcome(phase),
                    failure_code=exc.code,
                )
                results.append(
                    RealOfficialEvidenceFactorResult(
                        factor_id=candidate.factor_id,
                        status=FactorAttemptStatus.FAILED_CONSUMED,
                        trial_id=attempt.trial_id,
                        identity_hash=identity.identity_hash,
                        candidate_payload_hash=payload_hash,
                        factor_dag_hash=dag_hash,
                        train_factor_result_hash=train_result_hash,
                        validation_factor_result_hash=validation_result_hash,
                        train_evidence=None,
                        validation_evidence=None,
                        ledger_terminal_event_hash=terminal.event_hash,
                        failure_code=exc.code,
                        failure_message=str(exc),
                    )
                )
        replay = ledger.replay()
        family = replay.families[family_id]
        succeeded = sum(item.status is FactorAttemptStatus.OFFICIAL_VISIBLE_EVIDENCE for item in results)
        status = (
            "COMPLETED_OFFICIAL_VISIBLE"
            if succeeded == len(results)
            else "PARTIAL_OFFICIAL_VISIBLE"
            if succeeded
            else "FAILED_CONSUMED"
        )
        return RealOfficialEvidenceRun(
            run_id=run_id,
            status=status,
            source_class=qualification.source_class.value,
            provider_class=qualification.provider_class.value,
            official_tushare_source=False,
            decision_time=bundle.decision_time,
            qualification_hash=qualification.artifact_hash,
            auxiliary_result_hash=auxiliary.artifact_hash,
            snapshot_hash=auxiliary.snapshot_hash,
            calendar_hash=auxiliary.calendar_hash,
            universe_hash=auxiliary.universe_hash,
            research_bundle_hash=bundle.artifact_hash,
            bridge_manifest_hash=bundle.bridge_run_manifest.artifact_hash,
            trial_ledger_tip_hash=replay.last_hash,
            family_budget=budget,
            budget_consumed=family.consumed,
            factors=tuple(results),
            official_evidence_run=bool(succeeded),
            sealed_evidence_run=False,
            promotion_run=False,
            challenger_created=False,
        )


__all__ = [
    "APPROVED_QUALIFICATION_HASH",
    "PINNED_AUXILIARY_RESULT_HASH",
    "FactorCandidate",
    "PinnedRealOfficialInputPaths",
    "RealOfficialEvidenceAdapter",
    "default_factor_candidates",
]
