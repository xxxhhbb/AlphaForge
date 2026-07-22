"""Auxiliary-only research smoke for frozen Tushare-compatible snapshots.

This package is deliberately a thin adapter.  It delegates PIT filtering and
label construction to :class:`AShareResearchBridge`, and delegates factor
compilation/execution to the restricted FactorDAG implementation.  It has no
Evidence, Promotion, portfolio, order, or ledger authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Protocol, Sequence, runtime_checkable

from veriquant.contracts import CanonicalContract, ContractValidationError, FailureCode
from veriquant.contracts.base import (
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)
from veriquant.data import FieldRole, MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.factors import FactorValue, execute_factor_dag, parse_compile_factor_dag
from veriquant.real_research.api import (
    AShareResearchBridge,
    FrozenSnapshotEnvelope,
    ResearchDatasetBundle,
    ResearchSplitManifest,
    build_default_bridge_request,
)


SCHEMA_VERSION = "veriquant.proxy-research-smoke/v1"
AUXILIARY_EVIDENCE_CLASS = "AUXILIARY_RESEARCH_EVIDENCE"


class SourceQualification(str, Enum):
    """Caller-supplied provenance classification; never a promotion grant."""

    UNKNOWN = "UNKNOWN"
    TUSHARE_COMPATIBLE_PROXY = "TUSHARE_COMPATIBLE_PROXY"
    OFFICIAL_TUSHARE = "OFFICIAL_TUSHARE"


class AuxiliaryFactorStatus(str, Enum):
    EVALUATED = "EVALUATED"
    REJECTED = "REJECTED"


@runtime_checkable
class ProxySnapshotProvider(Protocol):
    """Minimal read-only contract expected from Agent A.

    Implementations return an already-frozen envelope.  They must not fetch
    network data as a side effect of this method and must not return credentials.
    """

    def load_envelope(self, *, snapshot_id: str) -> FrozenSnapshotEnvelope: ...


@dataclass(frozen=True, kw_only=True)
class AuxiliaryFactorDefinition:
    """An inert FactorDAG payload; executable Python is not representable here."""

    factor_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        if not isinstance(self.payload, Mapping) or not self.payload:
            fail(FailureCode.INVALID_FACTOR_DAG, "factor payload must be a non-empty mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, kw_only=True)
class AuxiliaryFactorResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    factor_id: str
    status: AuxiliaryFactorStatus
    evidence_class: str
    promotion_eligible: bool
    dag_hash: str | None
    output_hash: str | None
    values: tuple[FactorValue, ...]
    failure_code: str | None
    message: str

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        if not isinstance(self.status, AuxiliaryFactorStatus):
            fail(FailureCode.INVALID_VALUE, "invalid auxiliary factor status")
        if self.evidence_class != AUXILIARY_EVIDENCE_CLASS:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "proxy smoke may emit auxiliary evidence only")
        if self.promotion_eligible is not False:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "proxy smoke cannot be promotion eligible")
        if self.status is AuxiliaryFactorStatus.EVALUATED:
            if self.dag_hash is None or self.output_hash is None:
                fail(FailureCode.INCOMPLETE_RESULT, "evaluated factor requires DAG and output hashes")
            require_hash(self.dag_hash, "dag_hash")
            require_hash(self.output_hash, "output_hash")
            if self.failure_code is not None or not self.values:
                fail(FailureCode.INVALID_STATE, "evaluated factor cannot carry failure state")
        else:
            if self.dag_hash is not None or self.output_hash is not None or self.values:
                fail(FailureCode.INVALID_STATE, "rejected factor cannot carry executable output")
            require_text(self.failure_code, "failure_code")
        require_text(self.message, "message")


@dataclass(frozen=True, kw_only=True)
class ProxyResearchSmokeManifest(CanonicalContract):
    """Deterministic auxiliary manifest with an immutable authority boundary."""

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    run_id: str
    snapshot_id: str
    source_qualification: SourceQualification
    evidence_class: str
    promotion_eligible: bool
    decision_time: datetime
    feature_fields: tuple[str, ...]
    label_name: str
    label_forward_days: int
    label_hash: str
    snapshot_hash: str
    bridge_bundle_hash: str
    bridge_output_hash: str
    factor_set_hash: str
    factors: tuple[AuxiliaryFactorResult, ...]
    authority_note: str

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_identifier(self.snapshot_id, "snapshot_id")
        if not isinstance(self.source_qualification, SourceQualification):
            fail(FailureCode.INVALID_VALUE, "source_qualification is invalid")
        if self.evidence_class != AUXILIARY_EVIDENCE_CLASS:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "proxy research manifest must remain auxiliary")
        if self.promotion_eligible is not False:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "proxy research manifest cannot promote")
        require_aware_datetime(self.decision_time, "decision_time")
        require_nonempty_tuple(self.feature_fields, "feature_fields")
        for field in self.feature_fields:
            require_identifier(field, "feature_fields")
        require_text(self.label_name, "label_name")
        if self.label_forward_days != 5:
            fail(FailureCode.INVALID_VALUE, "proxy research smoke requires a five-session label")
        for name in (
            "label_hash",
            "snapshot_hash",
            "bridge_bundle_hash",
            "bridge_output_hash",
            "factor_set_hash",
        ):
            require_hash(getattr(self, name), name)
        require_nonempty_tuple(self.factors, "factors")
        if any(not isinstance(item, AuxiliaryFactorResult) for item in self.factors):
            fail(FailureCode.INVALID_VALUE, "factors must contain AuxiliaryFactorResult values")
        if any(item.promotion_eligible for item in self.factors):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "factor result attempted promotion eligibility")
        require_text(self.authority_note, "authority_note")


def default_auxiliary_factors() -> tuple[AuxiliaryFactorDefinition, ...]:
    """Return two small inert DAGs for the proxy integration smoke."""

    return (
        AuxiliaryFactorDefinition(
            factor_id="proxy-reversal-3d",
            payload={
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "proxy-reversal-3d",
                "nodes": [
                    {"id": "close", "op": "field", "field": "close"},
                    {"id": "return_3d", "op": "pct_change", "inputs": ["close"], "periods": 3},
                    {"id": "rank", "op": "rank_cross_section", "inputs": ["return_3d"]},
                ],
                "output_node": "rank",
            },
        ),
        AuxiliaryFactorDefinition(
            factor_id="proxy-volume-change-3d",
            payload={
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "proxy-volume-change-3d",
                "nodes": [
                    {"id": "volume", "op": "field", "field": "volume"},
                    {"id": "change", "op": "pct_change", "inputs": ["volume"], "periods": 3},
                    {"id": "zscore", "op": "zscore_cross_section", "inputs": ["change"]},
                ],
                "output_node": "zscore",
            },
        ),
    )


def _require_complete_market_state(envelope: FrozenSnapshotEnvelope) -> None:
    """Fail the whole run when any frozen row lacks a required market state."""

    if not isinstance(envelope, FrozenSnapshotEnvelope):
        fail(FailureCode.INVALID_VALUE, "provider returned a non-FrozenSnapshotEnvelope")
    states: dict[tuple[str, datetime], Any] = {}
    for state in envelope.row_market_state:
        key = (state.symbol, state.event_time)
        if key in states:
            fail(FailureCode.INVALID_VALUE, "duplicate row market state", symbol=state.symbol)
        states[key] = state
    for row in envelope.snapshot.rows:
        state = states.get((row.symbol, row.event_time))
        if state is None:
            fail(
                FailureCode.INCOMPLETE_RESULT,
                "snapshot row is missing market state",
                symbol=row.symbol,
                event_time=row.event_time.isoformat(),
            )
        missing = tuple(
            name
            for name in ("suspended", "delisted", "limit_up", "limit_down")
            if getattr(state, name) is None
        )
        if missing:
            fail(
                FailureCode.INCOMPLETE_RESULT,
                "snapshot row has incomplete market state",
                symbol=row.symbol,
                event_time=row.event_time.isoformat(),
                fields=missing,
            )


def _factor_snapshot(bundle: ResearchDatasetBundle) -> MarketSnapshot:
    rows = tuple(
        SnapshotRow(
            event_time=row.event_time,
            symbol=row.symbol,
            values={field: row.values[field] for field in bundle.feature_fields},
        )
        for row in bundle.feature_rows
    )
    if not rows:
        fail(FailureCode.INCOMPLETE_RESULT, "PIT bundle has no factor rows")
    return MarketSnapshot(
        dataset_id=bundle.dataset_card.dataset_id,
        decision_time=bundle.decision_time,
        start_time=min(row.event_time for row in rows),
        fields=bundle.feature_fields,
        rows=rows,
        historical_universe=bundle.universe_manifest.historical_universe,
        coverage_by_field=bundle.data_quality_manifest.coverage_by_field,
    )


class ProxyResearchSmokeController:
    """Build an auxiliary five-day-label bundle and execute restricted DAGs."""

    def __init__(self, provider: ProxySnapshotProvider) -> None:
        if not isinstance(provider, ProxySnapshotProvider):
            fail(FailureCode.INVALID_VALUE, "provider does not implement ProxySnapshotProvider")
        self.provider = provider

    def run(
        self,
        *,
        snapshot_id: str,
        decision_time: datetime,
        split_manifest: ResearchSplitManifest,
        feature_fields: tuple[str, ...],
        factors: Sequence[AuxiliaryFactorDefinition] | None = None,
        source_qualification: SourceQualification = SourceQualification.UNKNOWN,
        seed: int = 7,
        run_id: str = "proxy-research-smoke",
    ) -> ProxyResearchSmokeManifest:
        require_identifier(snapshot_id, "snapshot_id")
        require_aware_datetime(decision_time, "decision_time")
        require_identifier(run_id, "run_id")
        if not isinstance(source_qualification, SourceQualification):
            fail(FailureCode.INVALID_VALUE, "source_qualification is invalid")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        definitions = tuple(factors or default_auxiliary_factors())
        if not definitions or any(not isinstance(item, AuxiliaryFactorDefinition) for item in definitions):
            fail(FailureCode.INVALID_VALUE, "one or more auxiliary FactorDAG definitions are required")

        envelope = self.provider.load_envelope(snapshot_id=snapshot_id)
        _require_complete_market_state(envelope)
        request = build_default_bridge_request(
            snapshot_id=snapshot_id,
            decision_time=decision_time,
            split_manifest=split_manifest,
            forward_days=5,
            execution_lag_days=1,
            feature_fields=feature_fields,
            run_id=run_id,
        )
        bundle = AShareResearchBridge(_SingleEnvelopeReader(snapshot_id, envelope)).build_bundle(request)
        if bundle.label_specification.forward_days != 5:
            fail(FailureCode.INVALID_STATE, "bridge returned a non-five-day label")
        if any(set(row.values) != set(bundle.feature_fields) for row in bundle.feature_rows):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "label or undeclared field entered feature rows")

        snapshot = _factor_snapshot(bundle)
        field_roles = {field: FieldRole.FEATURE for field in bundle.feature_fields}
        results = tuple(
            self._run_factor(
                definition=definition,
                field_roles=field_roles,
                snapshot=snapshot,
                seed=seed,
            )
            for definition in definitions
        )
        return ProxyResearchSmokeManifest(
            run_id=run_id,
            snapshot_id=snapshot_id,
            source_qualification=source_qualification,
            evidence_class=AUXILIARY_EVIDENCE_CLASS,
            promotion_eligible=False,
            decision_time=decision_time,
            feature_fields=bundle.feature_fields,
            label_name=bundle.label_specification.name,
            label_forward_days=bundle.label_specification.forward_days,
            label_hash=canonical_content_hash(bundle.label_rows),
            snapshot_hash=envelope.snapshot.snapshot_hash,
            bridge_bundle_hash=bundle.artifact_hash,
            bridge_output_hash=bundle.bridge_run_manifest.output_hash,
            factor_set_hash=canonical_content_hash(tuple(item.payload for item in definitions)),
            factors=results,
            authority_note=(
                "Auxiliary diagnostics only; source qualification cannot grant official Evidence, "
                "Promotion, CHALLENGER, portfolio, risk, execution, or ledger authority."
            ),
        )

    @staticmethod
    def _run_factor(
        *,
        definition: AuxiliaryFactorDefinition,
        field_roles: Mapping[str, FieldRole],
        snapshot: MarketSnapshot,
        seed: int,
    ) -> AuxiliaryFactorResult:
        try:
            dag = parse_compile_factor_dag(definition.payload, field_roles=field_roles)
            output = execute_factor_dag(
                dag,
                snapshot,
                seed=seed,
                config={"purpose": "proxy_auxiliary_smoke", "evidence_class": AUXILIARY_EVIDENCE_CLASS},
            )
            return AuxiliaryFactorResult(
                factor_id=definition.factor_id,
                status=AuxiliaryFactorStatus.EVALUATED,
                evidence_class=AUXILIARY_EVIDENCE_CLASS,
                promotion_eligible=False,
                dag_hash=dag.artifact_hash,
                output_hash=output.artifact_hash,
                values=output.values,
                failure_code=None,
                message="Restricted FactorDAG evaluated as auxiliary diagnostics.",
            )
        except ContractValidationError as exc:
            return AuxiliaryFactorResult(
                factor_id=definition.factor_id,
                status=AuxiliaryFactorStatus.REJECTED,
                evidence_class=AUXILIARY_EVIDENCE_CLASS,
                promotion_eligible=False,
                dag_hash=None,
                output_hash=None,
                values=(),
                failure_code=exc.code.value,
                message="FactorDAG rejected by the existing restricted compiler.",
            )


class _SingleEnvelopeReader:
    """Private adapter to the existing AShareResearchBridge reader protocol."""

    def __init__(self, snapshot_id: str, envelope: FrozenSnapshotEnvelope) -> None:
        self.snapshot_id = snapshot_id
        self.envelope = envelope

    def read_snapshot(self, snapshot_id: str) -> FrozenSnapshotEnvelope:
        if snapshot_id != self.snapshot_id:
            fail(FailureCode.INVALID_IDENTIFIER, "snapshot id does not match frozen envelope")
        return self.envelope


__all__ = [
    "AUXILIARY_EVIDENCE_CLASS",
    "AuxiliaryFactorDefinition",
    "AuxiliaryFactorResult",
    "AuxiliaryFactorStatus",
    "ProxyResearchSmokeController",
    "ProxyResearchSmokeManifest",
    "ProxySnapshotProvider",
    "SourceQualification",
    "default_auxiliary_factors",
]
