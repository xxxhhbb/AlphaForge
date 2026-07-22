from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.veriquant.contracts import Component, ContractValidationError, DatasetCard
from src.veriquant.data import FieldRole, MarketSnapshot, SnapshotRow, canonical_content_hash
from src.veriquant.factors import parse_compile_factor_dag
from src.veriquant.formula_execution import (
    LEGACY_POLICY_HASH,
    LEGACY_RUNTIME_HASH,
    FactorScoreArtifact,
    FactorScorePoint,
    FormulaExecutionRequest,
    FormulaExecutionService,
    FormulaProgramIdentity,
    FormulaProgramKind,
    LegacyFactorDAGBackend,
    CertifiedQlibExpressionBackend,
    CertifiedQlibProgramBundle,
    evaluation_rows_from_scores,
    score_key,
)
from src.veriquant.frozen_qlib_provider import FrozenQlibProvider, QlibDataRequirement
from src.veriquant.qlib_candidate_translation import (
    QlibCandidateDraftEnvelope,
    SafeQlibCandidateTranslator,
)
from src.veriquant.qlib_operator_worker.api import QlibRuntimeIdentity
from src.veriquant.research_campaign import CampaignState


def _snapshot() -> MarketSnapshot:
    start = datetime(2025, 1, 2, tzinfo=timezone.utc)
    rows = []
    for offset in range(3):
        event_time = start + timedelta(days=offset)
        for symbol, base in (("SH600000", 10.0), ("SZ000001", 20.0)):
            rows.append(
                SnapshotRow(
                    event_time=event_time,
                    symbol=symbol,
                    values={"close": base + offset, "volume": 1_000_000.0 + offset},
                )
            )
    return MarketSnapshot(
        dataset_id="formula-boundary-fixture",
        decision_time=start + timedelta(days=2),
        start_time=start,
        fields=("close", "volume"),
        rows=tuple(rows),
        historical_universe={
            (start + timedelta(days=offset)).isoformat(): ("SH600000", "SZ000001")
            for offset in range(3)
        },
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )


def _program_and_request(snapshot: MarketSnapshot):
    dag = parse_compile_factor_dag(
        {
            "schema_version": "veriquant.factor-dag/v1",
            "dag_id": "legacy.momentum",
            "nodes": [
                {"id": "close", "op": "field", "field": "close"},
                {"id": "change", "op": "pct_change", "inputs": ["close"], "periods": 1},
            ],
            "output_node": "change",
        },
        field_roles={"close": FieldRole.FEATURE},
    )
    identity = FormulaProgramIdentity(
        program_id="legacy.momentum",
        kind=FormulaProgramKind.LEGACY_FACTOR_DAG,
        program_hash=dag.artifact_hash,
        required_fields=dag.required_fields,
        runtime_hash=LEGACY_RUNTIME_HASH,
        policy_hash=LEGACY_POLICY_HASH,
    )
    request = FormulaExecutionRequest(
        program=identity,
        snapshot_hash=snapshot.artifact_hash,
        universe_hash=canonical_content_hash(snapshot.historical_universe),
        seed=7,
    )
    return dag, request


def test_legacy_backend_produces_stable_backend_neutral_scores() -> None:
    snapshot = _snapshot()
    dag, request = _program_and_request(snapshot)
    service = FormulaExecutionService(LegacyFactorDAGBackend())

    first = service.execute(request=request, program=dag, snapshot=snapshot)
    second = service.execute(request=request, program=dag, snapshot=snapshot)

    assert first.artifact_hash == second.artifact_hash
    assert first.program_hash == dag.artifact_hash
    assert first.snapshot_hash == snapshot.artifact_hash
    assert len(first.values) == 6


def test_service_rejects_snapshot_identity_mismatch() -> None:
    snapshot = _snapshot()
    dag, request = _program_and_request(snapshot)
    bad_request = FormulaExecutionRequest(
        program=request.program,
        snapshot_hash=canonical_content_hash({"wrong": True}),
        universe_hash=request.universe_hash,
        seed=request.seed,
    )

    with pytest.raises(ContractValidationError):
        FormulaExecutionService(LegacyFactorDAGBackend()).execute(
            request=bad_request,
            program=dag,
            snapshot=snapshot,
        )


def test_score_artifact_rejects_duplicate_or_unstable_keys() -> None:
    timestamp = datetime(2025, 1, 2, tzinfo=timezone.utc)
    point = FactorScorePoint(event_time=timestamp, symbol="SH600000", score=1.0)
    hashes = {name: canonical_content_hash({name: True}) for name in ("program", "snapshot", "universe", "runtime", "policy")}

    with pytest.raises(ContractValidationError):
        FactorScoreArtifact(
            program_hash=hashes["program"],
            snapshot_hash=hashes["snapshot"],
            universe_hash=hashes["universe"],
            runtime_hash=hashes["runtime"],
            policy_hash=hashes["policy"],
            seed=0,
            values=(point, point),
        )


def test_backend_neutral_scores_bind_to_evidence_rows() -> None:
    snapshot = _snapshot()
    dag, request = _program_and_request(snapshot)
    scores = FormulaExecutionService(LegacyFactorDAGBackend()).execute(
        request=request,
        program=dag,
        snapshot=snapshot,
    )
    labels = {
        score_key(point.event_time, point.symbol): 0.01
        for point in scores.values
        if point.score is not None
    }

    rows = evaluation_rows_from_scores(scores=scores, snapshot=snapshot, labels=labels)

    assert rows
    assert all(row.volume > 0 for row in rows)
    assert tuple((row.event_time, row.symbol) for row in rows) == tuple(
        sorted((row.event_time, row.symbol) for row in rows)
    )


def test_certified_qlib_backend_binds_pit_manifest_and_common_scores(monkeypatch) -> None:
    snapshot = _snapshot()
    translation = SafeQlibCandidateTranslator().translate(
        QlibCandidateDraftEnvelope(
            campaign_hash=canonical_content_hash({"campaign": "confirmed"}),
            campaign_state=CampaignState.CONFIRMED,
            group_id="market_structure",
            candidate_id="qlib.momentum",
            expression="Ref($close,1)",
            confirmed_field_whitelist=("close",),
            requested_lookback_sessions=1,
            source_draft_hash=canonical_content_hash({"draft": "qlib.momentum"}),
        )
    )
    card = DatasetCard(
        dataset_id=snapshot.dataset_id,
        source="offline-fixture",
        universe=("SH600000", "SZ000001"),
        horizon="daily",
        decision_time=snapshot.decision_time,
        availability_assumptions=(
            "available_at <= decision_time",
            "historical universe membership is resolved per session",
        ),
        fields=snapshot.fields,
        event_time=snapshot.start_time,
        published_at=snapshot.decision_time,
        available_at=snapshot.decision_time,
        snapshot_hash=snapshot.artifact_hash,
        record_count=len(snapshot.rows),
        produced_by=Component.DATA_SERVICE,
    )
    manifest = FrozenQlibProvider().from_market_snapshot(
        snapshot,
        dataset_card=card,
        requirement=QlibDataRequirement(
            required_fields=translation.required_fields,
            lookback_bars=translation.maximum_lookback or 0,
            warmup_bars=translation.maximum_lookback or 0,
        ),
    )
    identity = FormulaProgramIdentity(
        program_id="qlib.momentum",
        kind=FormulaProgramKind.CERTIFIED_QLIB_EXPRESSION,
        program_hash=translation.program_hash,
        required_fields=translation.required_fields,
        runtime_hash=QlibRuntimeIdentity.expected().runtime_hash,
        policy_hash=translation.policy_hash,
    )
    request = FormulaExecutionRequest(
        program=identity,
        snapshot_hash=snapshot.artifact_hash,
        universe_hash=canonical_content_hash(snapshot.historical_universe),
        seed=7,
    )

    def fake_worker(worker_request):
        from types import SimpleNamespace

        return SimpleNamespace(
            program_hash=translation.expression_hash,
            runtime_hash=identity.runtime_hash,
            rows=tuple(
                SimpleNamespace(event_time=row.event_time, symbol=row.symbol, score=1.0)
                for row in worker_request.rows
            ),
        )

    monkeypatch.setattr(
        "src.veriquant.formula_execution.qlib.execute_certified_qlib_operators",
        fake_worker,
    )
    result = FormulaExecutionService(CertifiedQlibExpressionBackend()).execute(
        request=request,
        program=CertifiedQlibProgramBundle(
            translation=translation,
            provider_manifest=manifest,
        ),
        snapshot=snapshot,
    )

    assert result.program_hash == translation.program_hash
    assert result.runtime_hash == QlibRuntimeIdentity.expected().runtime_hash
    assert len(result.values) == len(snapshot.rows)
    assert result.artifact_hash == FormulaExecutionService(CertifiedQlibExpressionBackend()).execute(
        request=request,
        program=CertifiedQlibProgramBundle(translation=translation, provider_manifest=manifest),
        snapshot=snapshot,
    ).artifact_hash
