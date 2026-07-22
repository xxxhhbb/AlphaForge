from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from veriquant.contracts import ContractValidationError
from veriquant.registered_research_context import (
    ContentAddressedRegisteredContextStore,
    FactorCorrelationRecord,
    FactorScorePoint,
    PortfolioMembershipRecord,
    PortfolioScope,
    RegisteredFactorMetadata,
    RegisteredResearchContext,
    RegisteredResearchContextRepository,
    VisibleEvidenceRecord,
    VisiblePartition,
    WorkbenchContextImporter,
)


H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
H3 = "sha256:" + "3" * 64
T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def factor(fid: str = "factor-a", *, available_at=T0) -> RegisteredFactorMetadata:
    return RegisteredFactorMetadata(
        factor_id=fid, version=1, family="reference", factor_hash=H1,
        source_hash=H2, status="REFERENCE", registered_at=T0,
        available_at=available_at, source_artifact_hash=H3,
    )


def test_context_hash_is_stable_and_round_trip_is_canonical():
    left = RegisteredResearchContext(factors=(factor(),))
    right = RegisteredResearchContext(factors=(factor(),))
    assert left.artifact_hash == right.artifact_hash
    assert left.canonical_json() == right.canonical_json()


def test_content_addressed_store_replays_identical_context(tmp_path):
    context = RegisteredResearchContext(factors=(factor(),))
    store = ContentAddressedRegisteredContextStore(tmp_path)
    handle = store.publish(context)
    replayed = store.read(handle)
    assert handle.handle_id == context.artifact_hash == replayed.artifact_hash
    with pytest.raises(ValueError):
        store.read("../.env")


def test_score_rejects_future_availability():
    with pytest.raises(ContractValidationError):
        FactorScorePoint(
            factor_id="factor-a", version=1, symbol="000001.SZ",
            decision_time=T0, available_at=T0 + timedelta(seconds=1), score=1.0,
            source_artifact_hash=H1,
        )


def test_sealed_partition_cannot_enter_visible_context():
    with pytest.raises((ValueError, ContractValidationError)):
        VisibleEvidenceRecord(
            factor_id="factor-a", version=1, identity_hash=H1, evidence_hash=H2,
            partition="SEALED", metrics={"icir": 1.0}, computed_at=T0,
            available_at=T0, source_artifact_hash=H3,
        )


def test_nonvisible_metric_is_rejected():
    with pytest.raises(ContractValidationError):
        VisibleEvidenceRecord(
            factor_id="factor-a", version=1, identity_hash=H1, evidence_hash=H2,
            partition=VisiblePartition.TRAIN, metrics={"sealed_metric": 1.0},
            computed_at=T0, available_at=T0, source_artifact_hash=H3,
        )


def test_reference_portfolio_must_be_explicit_and_named_as_reference():
    with pytest.raises(ContractValidationError):
        PortfolioMembershipRecord(
            portfolio_id="user-live", portfolio_scope=PortfolioScope.REFERENCE_RESEARCH_PORTFOLIO,
            factor_id="factor-a", factor_version=1, weight=1.0,
            effective_at=T0, available_at=T0, source_artifact_hash=H1,
        )


def test_repository_snapshot_is_pit_visible_and_read_only():
    later = factor("factor-b", available_at=T0 + timedelta(days=1))
    context = RegisteredResearchContext(factors=(factor(), later))
    repo = RegisteredResearchContextRepository(context)
    view = repo.snapshot(decision_time=T0)
    assert [item.factor_id for item in view.factors] == ["factor-a"]
    assert repo.artifact_hash == context.artifact_hash
    assert not hasattr(repo, "register")


def test_context_rejects_dangling_relationship():
    with pytest.raises(ContractValidationError):
        RegisteredResearchContext(
            factors=(factor(),),
            correlations=(FactorCorrelationRecord(
                left_factor_id="factor-a", left_version=1,
                right_factor_id="factor-b", right_version=1,
                correlation=0.1, as_of=T0, available_at=T0,
                source_artifact_hash=H1,
            ),),
        )


def _write_realistic_run(root):
    metrics = {
        "pearson_ic": 0.1, "rank_ic": 0.1, "ic_mean": 0.1, "ic_std": 0.2,
        "icir": 0.5, "quantile_spread": 0.01, "turnover": 0.2,
        "gross_return": 0.02, "net_return": 0.01, "spread_cost": 0.001,
        "linear_impact_cost": 0.001, "max_drawdown": 0.1,
        "subperiod_positive_fraction": 0.5, "subperiod_min_ic": -0.1,
        "regime_positive_fraction": 0.5, "regime_min_ic": -0.1,
        "max_registered_abs_correlation": 0.2, "incremental_ic_mean": 0.01,
        "observation_count": 100, "period_count": 20,
    }
    evidence = {"official_run": {
        "decision_time": "2024-01-02T00:00:00Z",
        "factors": [{
            "factor_id": "factor-a", "dag_hash": H1, "factor_output_hash": H2,
            "identity_hash": H3, "promotion_state": "REJECTED",
            "train_evidence_hash": H1, "train_metrics": metrics,
            "validation_evidence_hash": H2, "validation_metrics": metrics,
            "promotion_verdict": "REJECT", "promotion_decision_hash": H3,
        }],
    }}
    shadow = {"daily_records": [{
        "status": "READY", "decision_time": "2024-01-03T01:00:00Z",
        "valuation_time": "2024-01-03T08:00:00Z", "nav_end": 101.0,
        "turnover": 0.1, "attribution": {"return_pct": 0.01},
    }]}
    (root / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    (root / "shadow_portfolio.json").write_text(json.dumps(shadow), encoding="utf-8")


def test_importer_extracts_only_visible_facts_and_reports_missing(tmp_path):
    _write_realistic_run(tmp_path)
    registry_time = T0 + timedelta(days=10)
    context, audit = WorkbenchContextImporter(tmp_path, registered_available_at=registry_time).import_available()
    assert len(context.factors) == 1
    assert len(context.visible_evidence) == 2
    assert all(item.partition is not None and item.partition.value != "SEALED" for item in context.visible_evidence)
    assert len(context.portfolio_returns) == 1
    assert context.portfolio_returns[0].portfolio_scope is PortfolioScope.REFERENCE_RESEARCH_PORTFOLIO
    assert context.visible_evidence[0].available_at == registry_time
    assert RegisteredResearchContextRepository(context).snapshot(decision_time=T0).visible_evidence == ()
    assert set(audit.missing_capabilities) == {"score_history", "correlations", "exposures", "portfolio_membership"}
    serialized = context.canonical_json().lower()
    assert "promotion" not in serialized and "sealed" not in serialized and "order" not in serialized


def test_importer_rejects_arbitrary_filename_and_root(tmp_path):
    _write_realistic_run(tmp_path)
    importer = WorkbenchContextImporter(tmp_path, registered_available_at=T0 + timedelta(days=10))
    with pytest.raises(ValueError):
        from veriquant.registered_research_context.importer import _safe_json
        _safe_json(tmp_path.resolve(), "../.env")
