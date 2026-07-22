from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from veriquant.candidate_translation import (
    CandidateDraftEnvelope,
    CandidateTranslationResult,
    CandidateTranslationStatus,
    SafeCandidateTranslator,
)
from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.data import MarketSnapshot, SnapshotRow
from veriquant.evidence.ledger import TrialLedger
from veriquant.evidence.critics import EvidenceCourtPolicy
from veriquant.evidence.models import CostModel, CourtOutcome, WalkForwardSpec
from veriquant.research_evidence_bridge import (
    CandidateEvidenceValidationRequest,
    FrozenEvidenceInputIdentity,
    ResearchEvidenceBridge,
    ValidationBridgeFailureCode,
    ValidationBridgeStatus,
)


H = "sha256:" + "a" * 64


def _snapshot(*, include_volume: bool = True) -> MarketSnapshot:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fields = ("close", "volume") if include_volume else ("close",)
    rows: list[SnapshotRow] = []
    for day in range(7):
        event_time = start + timedelta(days=day)
        for index, symbol in enumerate(("AAA", "BBB", "CCC")):
            close = 10.0 + day * (index + 1) + index * 0.25
            values = {"close": close}
            if include_volume:
                values["volume"] = 1000.0 + index * 100.0 + day
            rows.append(SnapshotRow(event_time=event_time, symbol=symbol, values=values))
    universe = {str((start + timedelta(days=day)).isoformat()): ("AAA", "BBB", "CCC") for day in range(7)}
    coverage = {"close": 1.0}
    if include_volume:
        coverage["volume"] = 1.0
    return MarketSnapshot(
        dataset_id="fixture.dataset",
        decision_time=start + timedelta(days=6, hours=23),
        start_time=start,
        fields=fields,
        rows=tuple(rows),
        historical_universe=universe,
        coverage_by_field=coverage,
    )


def _labels(snapshot: MarketSnapshot) -> dict[str, float]:
    labels: dict[str, float] = {}
    for row in snapshot.rows:
        day = (row.event_time - snapshot.start_time).days
        sign = 1.0 if row.symbol in {"AAA", "BBB"} else -1.0
        labels[f"{row.event_time.isoformat()}|{row.symbol}"] = sign * (0.001 + day * 0.0001)
    return labels


def _translation(expression: str = "rank_cross_section(pct_change(close, 1))") -> CandidateTranslationResult:
    draft = CandidateDraftEnvelope(
        campaign_hash=H,
        group_id="research.group",
        candidate_id="c1",
        expression=expression,
        requested_lookback_sessions=1,
        declared_lookback_sessions=5,
        allowed_fields=("close",),
        source_draft_hash=H,
    )
    return SafeCandidateTranslator().translate(draft)


def _request(
    tmp_path: Path,
    *,
    candidate_id: str = "c1",
    family_id: str = "family-1",
    family_budget: int = 3,
    translation: CandidateTranslationResult | None = None,
    snapshot: MarketSnapshot | None = None,
    labels: dict[str, float] | None = None,
    cost_model: CostModel | None = None,
    approver_id: str = "evidence.court",
    court_policy: EvidenceCourtPolicy | None = None,
) -> CandidateEvidenceValidationRequest:
    snapshot = snapshot or _snapshot()
    labels = labels if labels is not None else _labels(snapshot)
    identity = FrozenEvidenceInputIdentity.from_snapshot_labels(
        snapshot=snapshot,
        labels=labels,
        source_label="APPROVED_PROXY_DATA_VENDOR",
    )
    return CandidateEvidenceValidationRequest(
        candidate_id=candidate_id,
        research_group_id="research.group",
        family_id=family_id,
        proposer_id="llm.researcher",
        translation=translation or _translation(),
        snapshot=snapshot,
        labels=labels,
        input_identity=identity,
        walk_forward=WalkForwardSpec(train_periods=2, validation_periods=1, sealed_periods=1, step_periods=1),
        cost_model=cost_model or CostModel(spread_bps=1.0, linear_impact_bps=2.0),
        family_budget=family_budget,
        seed=7,
        approver_id=approver_id,
        court_policy=court_policy,
    )


def _bridge(tmp_path: Path) -> ResearchEvidenceBridge:
    return ResearchEvidenceBridge(ledger_path=tmp_path / "trial-ledger.sqlite3")


def test_accepted_candidate_runs_official_visible_evidence_and_consumes_budget(tmp_path: Path) -> None:
    result = _bridge(tmp_path).validate_candidate(_request(tmp_path))

    assert result.result_ref.status is ValidationBridgeStatus.OFFICIAL_EVIDENCE_READY
    assert result.result_ref.budget_consumed is True
    assert result.result_ref.evidence_hashes
    assert result.visible_metrics is not None
    assert result.disposition is not None
    assert result.disposition.sealed_passed is None
    assert result.disposition.proposer_id != result.disposition.approver_id
    assert result.disposition.outcome is not CourtOutcome.PROMOTE_CHALLENGER
    assert result.visible_metrics.evidence_class == "OFFICIAL_EVIDENCE"
    assert "validation_icir" in result.visible_metrics.metrics
    assert all("sealed" not in key for key in result.visible_metrics.metrics)

    replay = TrialLedger(tmp_path / "trial-ledger.sqlite3").replay()
    assert replay.families["family-1"].consumed == 1
    assert replay.families["family-1"].terminal_trials == 1


def test_same_identity_is_idempotent_and_does_not_consume_twice(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    request = _request(tmp_path)

    first = bridge.validate_candidate(request)
    second = bridge.validate_candidate(request)

    assert first.result_ref.identity_hash == second.result_ref.identity_hash
    assert second.result_ref.status is ValidationBridgeStatus.IDEMPOTENT_REPLAYED
    assert second.visible_metrics is not None
    replay = TrialLedger(tmp_path / "trial-ledger.sqlite3").replay()
    assert replay.families["family-1"].consumed == 1
    assert replay.families["family-1"].terminal_trials == 1


def test_non_accepted_translation_is_terminal_parse_rejection(tmp_path: Path) -> None:
    bad = _translation("ts_argmax(close, 1)")
    assert bad.status is CandidateTranslationStatus.UNSUPPORTED

    result = _bridge(tmp_path).validate_candidate(_request(tmp_path, translation=bad))

    assert result.result_ref.status is ValidationBridgeStatus.PARSE_REJECTED
    assert result.result_ref.failure_code is ValidationBridgeFailureCode.NON_ACCEPTED_TRANSLATION
    assert result.result_ref.budget_consumed is True
    assert TrialLedger(tmp_path / "trial-ledger.sqlite3").replay().families["family-1"].consumed == 1


def test_corrupt_accepted_factor_dag_is_terminal_parse_rejection(tmp_path: Path) -> None:
    corrupt = CandidateTranslationResult(
        draft_hash=H,
        status=CandidateTranslationStatus.ACCEPTED,
        normalized_expression="rank_cross_section(pct_change(close, 1))",
        factor_dag_hash="sha256:" + "b" * 64,
        factor_dag_json="{not-json",
    )

    result = _bridge(tmp_path).validate_candidate(_request(tmp_path, translation=corrupt))

    assert result.result_ref.status is ValidationBridgeStatus.PARSE_REJECTED
    assert result.result_ref.failure_code is ValidationBridgeFailureCode.FACTOR_DAG_RECONSTRUCTION_FAILED
    assert result.result_ref.budget_consumed is True


def test_missing_label_fails_closed_after_budget_reservation(tmp_path: Path) -> None:
    snapshot = _snapshot()
    labels = _labels(snapshot)
    missing_key = f"{(snapshot.start_time + timedelta(days=1)).isoformat()}|AAA"
    labels.pop(missing_key)

    result = _bridge(tmp_path).validate_candidate(_request(tmp_path, snapshot=snapshot, labels=labels))

    assert result.result_ref.status is ValidationBridgeStatus.PIT_REJECTED
    assert result.result_ref.failure_code is ValidationBridgeFailureCode.LABEL_MISSING
    assert result.result_ref.budget_consumed is True
    replay = TrialLedger(tmp_path / "trial-ledger.sqlite3").replay()
    assert replay.families["family-1"].consumed == 1
    assert replay.families["family-1"].terminal_trials == 1


def test_volume_is_required_for_cost_aware_official_evidence(tmp_path: Path) -> None:
    snapshot = _snapshot(include_volume=False)
    labels = _labels(snapshot)

    result = _bridge(tmp_path).validate_candidate(_request(tmp_path, snapshot=snapshot, labels=labels))

    assert result.result_ref.status is ValidationBridgeStatus.PIT_REJECTED
    assert result.result_ref.failure_code is ValidationBridgeFailureCode.VOLUME_MISSING
    assert result.result_ref.budget_consumed is True


def test_family_budget_exhaustion_does_not_forge_terminal_success(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    first = _request(tmp_path, candidate_id="c1", family_budget=1)
    second = _request(tmp_path, candidate_id="c2", family_budget=1)

    assert bridge.validate_candidate(first).result_ref.status is ValidationBridgeStatus.OFFICIAL_EVIDENCE_READY
    result = bridge.validate_candidate(second)

    assert result.result_ref.status is ValidationBridgeStatus.BUDGET_EXHAUSTED
    assert result.result_ref.budget_consumed is False
    assert result.visible_metrics is None
    replay = TrialLedger(tmp_path / "trial-ledger.sqlite3").replay()
    assert replay.families["family-1"].consumed == 1
    assert replay.families["family-1"].terminal_trials == 1


def test_gross_positive_but_net_negative_is_not_validated(tmp_path: Path) -> None:
    snapshot = _snapshot()
    labels = {}
    for row in snapshot.rows:
        base = {"AAA": -0.002, "BBB": 0.0, "CCC": 0.003}[row.symbol]
        labels[f"{row.event_time.isoformat()}|{row.symbol}"] = base
    policy = EvidenceCourtPolicy(
        min_observations=1,
        min_rank_ic=-999.0,
        min_icir=-999.0,
        max_turnover=10.0,
        max_drawdown=10.0,
        min_net_return=0.0,
        min_incremental_ic=-999.0,
    )

    result = _bridge(tmp_path).validate_candidate(
        _request(
            tmp_path,
            labels=labels,
            cost_model=CostModel(spread_bps=10000.0, linear_impact_bps=10000.0),
            court_policy=policy,
        )
    )

    assert result.disposition is not None
    assert result.disposition.outcome is CourtOutcome.REJECT
    assert "COSTS_ERASE_ALPHA" in result.disposition.reason_codes


def test_visible_disposition_requires_independent_approver(tmp_path: Path) -> None:
    with pytest.raises(ContractValidationError) as exc:
        _request(tmp_path, approver_id="llm.researcher")
    assert exc.value.code is FailureCode.SELF_APPROVAL


def test_source_label_cannot_masquerade_as_official_tushare() -> None:
    snapshot = _snapshot()
    try:
        FrozenEvidenceInputIdentity.from_snapshot_labels(
            snapshot=snapshot,
            labels=_labels(snapshot),
            source_label="OFFICIAL_TUSHARE",
        )
    except Exception as exc:
        assert "official Tushare" in str(exc)
    else:  # pragma: no cover - fail-closed assertion
        raise AssertionError("OFFICIAL_TUSHARE masquerade was accepted")


def test_bridge_source_has_no_default_factor_or_generated_python_path() -> None:
    source = Path(__file__).parents[3].joinpath("src/veriquant/research_evidence_bridge/bridge.py").read_text(encoding="utf-8")
    assert "default_factor_candidates" not in source
    assert "fixed 3-day" not in source
    assert "factor.py" not in source
    assert "PromotionService" not in source
