from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import sqlite3

from src.veriquant.contracts import Component, DatasetCard
from src.veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from src.veriquant.evidence import ExperimentIdentity, TrialLedger
from src.veriquant.evidence.models import CostModel, WalkForwardSpec
from src.veriquant.factor_review import FactorReviewAgent, FactorReviewPolicy, FactorReviewRequest, FactorReviewVerdict
from src.veriquant.formula_execution import (
    FactorScoreArtifact,
    FactorScorePoint,
    FormulaExecutionService,
    FormulaProgramKind,
)
from src.veriquant.frozen_qlib_provider import FrozenQlibProvider, QlibDataRequirement
from src.veriquant.qlib_candidate_translation import (
    QlibCandidateDraftEnvelope,
    SafeQlibCandidateTranslator,
)
from src.veriquant.research_campaign import CampaignState
from src.veriquant.research_evidence_bridge import (
    CandidateEvidenceValidationRequest,
    FrozenEvidenceInputIdentity,
    ResearchEvidenceBridge,
    ValidationBridgeStatus,
)
from src.veriquant.specialist_research import CandidatePreflightResult, PreflightStatus, SpecialistCandidateDraft


def _snapshot() -> MarketSnapshot:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = tuple(
        SnapshotRow(
            event_time=start + timedelta(days=day),
            symbol=symbol,
            values={
                "close": 10.0 + day * (index + 1),
                "volume": 1000.0 + day + index,
            },
        )
        for day in range(7)
        for index, symbol in enumerate(("AAA", "BBB", "CCC"))
    )
    return MarketSnapshot(
        dataset_id="qlib.evidence.fixture",
        decision_time=start + timedelta(days=6, hours=23),
        start_time=start,
        fields=("close", "volume"),
        rows=rows,
        historical_universe={
            (start + timedelta(days=day)).isoformat(): ("AAA", "BBB", "CCC")
            for day in range(7)
        },
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )


class _DeterministicQlibTestBackend:
    kind = FormulaProgramKind.CERTIFIED_QLIB_EXPRESSION

    def execute(self, *, request, program, snapshot):
        return FactorScoreArtifact(
            program_hash=request.program.program_hash,
            snapshot_hash=request.snapshot_hash,
            universe_hash=request.universe_hash,
            runtime_hash=request.program.runtime_hash,
            policy_hash=request.program.policy_hash,
            seed=request.seed,
            values=tuple(
                FactorScorePoint(
                    event_time=row.event_time,
                    symbol=row.symbol,
                    score=float(row.values["close"]),
                )
                for row in snapshot.rows
            ),
        )


def _draft(candidate_id: str, expression: str = "Ref($close,1)") -> SpecialistCandidateDraft:
    return SpecialistCandidateDraft(
        candidate_id=candidate_id,
        family_id="qlib_momentum",
        hypothesis="Lagged close may contain testable cross-sectional information.",
        economic_mechanism="Information diffusion can delay price adjustment.",
        expected_direction="higher score predicts higher forward return",
        forecast_horizon_sessions=1,
        lookback_sessions=1,
        required_fields=("close",),
        expression=expression,
        availability_assumptions=("close is frozen before decision_time",),
        neutralization=(),
        falsification_tests=("net evidence is non-positive",),
        cost_risks=("turnover cost",),
        exposure_risks=("market beta",),
        novelty_explanation="A controlled Qlib identity fixture.",
    )


def _review(draft: SpecialistCandidateDraft, translation):
    preflight = CandidatePreflightResult(
        candidate_id=draft.candidate_id,
        draft_hash=draft.artifact_hash,
        status=(PreflightStatus.ACCEPTED if translation.program_hash else PreflightStatus.REVISE),
        normalized_expression=translation.canonical_expression,
        formula_program_hash=translation.program_hash,
        formula_program_kind="CERTIFIED_QLIB_EXPRESSION" if translation.program_hash else "LEGACY_FACTOR_DAG",
        formula_manifest_hash=translation.manifest_hash if translation.program_hash else None,
        failure_code=None if translation.program_hash else "QLIB_CERTIFICATION_FAILED",
        message=translation.message,
    )
    return FactorReviewAgent().review(FactorReviewRequest(
        proposer_id="research-specialist.market_structure",
        reviewer_id="critic.factor-review",
        candidate=draft,
        preflight=preflight,
        available_fields=("close",),
        policy=FactorReviewPolicy(),
    ))


def test_certified_qlib_candidate_uses_same_trial_and_official_evidence_authority(tmp_path) -> None:
    snapshot = _snapshot()
    translation = SafeQlibCandidateTranslator().translate(
        QlibCandidateDraftEnvelope(
            campaign_hash=canonical_content_hash({"campaign": "confirmed"}),
            campaign_state=CampaignState.CONFIRMED,
            group_id="market_structure",
            candidate_id="qlib.ref.one",
            expression="Ref($close,1)",
            confirmed_field_whitelist=("close",),
            requested_lookback_sessions=1,
            source_draft_hash=canonical_content_hash({"draft": "qlib.ref.one"}),
        )
    )
    card = DatasetCard(
        dataset_id=snapshot.dataset_id,
        source="offline-fixture",
        universe=("AAA", "BBB", "CCC"),
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
    labels = {
        f"{row.event_time.isoformat()}|{row.symbol}": (
            {"AAA": -0.001, "BBB": 0.0, "CCC": 0.001}[row.symbol]
        )
        for row in snapshot.rows
    }
    identity = FrozenEvidenceInputIdentity.from_snapshot_labels(
        snapshot=snapshot,
        labels=labels,
        source_label="APPROVED_PROXY_DATA_VENDOR",
    )
    bridge = ResearchEvidenceBridge(
        ledger_path=tmp_path / "qlib-trial.sqlite3",
        formula_execution_service=FormulaExecutionService(_DeterministicQlibTestBackend()),
    )
    result = bridge.validate_candidate(
        CandidateEvidenceValidationRequest(
            candidate_id="qlib.ref.one",
            research_group_id="market_structure",
            family_id="qlib-family",
            proposer_id="research-specialist.market_structure",
            translation=translation,
            qlib_provider_manifest=manifest,
            snapshot=snapshot,
            labels=labels,
            input_identity=identity,
            walk_forward=WalkForwardSpec(
                train_periods=2,
                validation_periods=1,
                sealed_periods=1,
                step_periods=1,
            ),
            cost_model=CostModel(spread_bps=1.0, linear_impact_bps=2.0),
            family_budget=2,
            seed=7,
        )
    )

    assert result.result_ref.status is ValidationBridgeStatus.OFFICIAL_EVIDENCE_READY
    assert result.result_ref.budget_consumed is True
    assert result.visible_metrics is not None
    assert result.visible_metrics.evidence_class == "OFFICIAL_EVIDENCE"
    assert result.disposition is not None
    assert result.disposition.sealed_passed is None


def test_product_trial_identity_binds_certified_program_manifest_policy_provider_and_review(tmp_path) -> None:
    snapshot = _snapshot()
    draft = _draft("qlib.product.one")
    translation = SafeQlibCandidateTranslator().translate(QlibCandidateDraftEnvelope(
        campaign_hash=canonical_content_hash({"campaign": "product"}),
        campaign_state=CampaignState.CONFIRMED,
        group_id="market_structure",
        candidate_id=draft.candidate_id,
        expression=draft.expression,
        confirmed_field_whitelist=("close",),
        requested_lookback_sessions=1,
        source_draft_hash=draft.artifact_hash,
    ))
    review = _review(draft, translation)
    assert review.verdict is FactorReviewVerdict.ACCEPT_FOR_CERTIFICATION
    card = DatasetCard(
        dataset_id=snapshot.dataset_id,
        source="offline-fixture",
        universe=("AAA", "BBB", "CCC"),
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
    labels = {
        f"{row.event_time.isoformat()}|{row.symbol}": {"AAA": -0.001, "BBB": 0.0, "CCC": 0.001}[row.symbol]
        for row in snapshot.rows
    }
    frozen_identity = FrozenEvidenceInputIdentity.from_snapshot_labels(
        snapshot=snapshot, labels=labels, source_label="APPROVED_PROXY_DATA_VENDOR"
    )
    ledger_path = tmp_path / "product-trial.sqlite3"
    result = ResearchEvidenceBridge(
        ledger_path=ledger_path,
        formula_execution_service=FormulaExecutionService(_DeterministicQlibTestBackend()),
    ).validate_candidate(CandidateEvidenceValidationRequest(
        candidate_id=draft.candidate_id,
        research_group_id="market_structure",
        family_id="qlib-product-family",
        proposer_id="research-specialist.market_structure",
        translation=translation,
        qlib_provider_manifest=manifest,
        snapshot=snapshot,
        labels=labels,
        input_identity=frozen_identity,
        walk_forward=WalkForwardSpec(train_periods=2, validation_periods=1, sealed_periods=1, step_periods=1),
        cost_model=CostModel(spread_bps=1.0, linear_impact_bps=2.0),
        family_budget=2,
        seed=7,
        draft_review=review,
        certified_qlib_only=True,
        frozen_provider_hash=manifest.manifest_hash,
        source_draft_hash=draft.artifact_hash,
    ))

    assert result.result_ref.status is ValidationBridgeStatus.OFFICIAL_EVIDENCE_READY
    with sqlite3.connect(ledger_path) as connection:
        stored = json.loads(connection.execute("SELECT identity_json FROM identities").fetchone()[0])
    assert stored["factor_hash"] == translation.program_hash
    assert stored["operator_manifest_hash"] == translation.manifest_hash
    assert stored["formula_policy_hash"] == translation.policy_hash
    assert stored["frozen_provider_hash"] == manifest.manifest_hash
    assert stored["review_policy_hash"] == review.policy_hash
    assert stored["source_draft_hash"] == draft.artifact_hash


def test_product_certification_failure_reserves_and_consumes_trial_budget(tmp_path) -> None:
    snapshot = _snapshot()
    draft = _draft("qlib.product.bad", "UnknownOperator($close,1)")
    translation = SafeQlibCandidateTranslator().translate(QlibCandidateDraftEnvelope(
        campaign_hash=canonical_content_hash({"campaign": "product-bad"}),
        campaign_state=CampaignState.CONFIRMED,
        group_id="market_structure",
        candidate_id=draft.candidate_id,
        expression=draft.expression,
        confirmed_field_whitelist=("close",),
        requested_lookback_sessions=1,
        source_draft_hash=draft.artifact_hash,
    ))
    review = _review(draft, translation)
    labels = {
        f"{row.event_time.isoformat()}|{row.symbol}": 0.001 for row in snapshot.rows
    }
    frozen_identity = FrozenEvidenceInputIdentity.from_snapshot_labels(snapshot=snapshot, labels=labels)
    provider_input_hash = canonical_content_hash({"snapshot": snapshot.artifact_hash, "fields": ("close",)})
    ledger_path = tmp_path / "failed-certification.sqlite3"
    result = ResearchEvidenceBridge(ledger_path=ledger_path).validate_candidate(
        CandidateEvidenceValidationRequest(
            candidate_id=draft.candidate_id,
            research_group_id="market_structure",
            family_id="failed-certification-family",
            proposer_id="research-specialist.market_structure",
            translation=translation,
            snapshot=snapshot,
            labels=labels,
            input_identity=frozen_identity,
            walk_forward=WalkForwardSpec(train_periods=2, validation_periods=1, sealed_periods=1, step_periods=1),
            cost_model=CostModel(spread_bps=1.0, linear_impact_bps=2.0),
            family_budget=1,
            draft_review=review,
            certified_qlib_only=True,
            frozen_provider_hash=provider_input_hash,
            source_draft_hash=draft.artifact_hash,
        )
    )
    replay = TrialLedger(ledger_path).replay()
    assert result.result_ref.status is ValidationBridgeStatus.PARSE_REJECTED
    assert result.result_ref.budget_consumed is True
    assert replay.families["failed-certification-family"].consumed == 1


def test_each_certified_identity_binding_changes_identity_hash() -> None:
    baseline = ExperimentIdentity(
        experiment_id="identity.binding",
        dataset_hash=canonical_content_hash({"dataset": 1}),
        universe_hash=canonical_content_hash({"universe": 1}),
        factor_hash=canonical_content_hash({"program": 1}),
        split_hash=canonical_content_hash({"split": 1}),
        label_hash=canonical_content_hash({"label": 1}),
        cost_hash=canonical_content_hash({"cost": 1}),
        code_hash=canonical_content_hash({"code": 1}),
        availability_hash=canonical_content_hash({"availability": 1}),
        seed=7,
        operator_manifest_hash=canonical_content_hash({"manifest": 1}),
        formula_policy_hash=canonical_content_hash({"formula_policy": 1}),
        frozen_provider_hash=canonical_content_hash({"provider": 1}),
        review_policy_hash=canonical_content_hash({"review_policy": 1}),
        source_draft_hash=canonical_content_hash({"draft": 1}),
    )
    replacements = {
        "factor_hash": canonical_content_hash({"program": 2}),
        "operator_manifest_hash": canonical_content_hash({"manifest": 2}),
        "formula_policy_hash": canonical_content_hash({"formula_policy": 2}),
        "frozen_provider_hash": canonical_content_hash({"provider": 2}),
        "review_policy_hash": canonical_content_hash({"review_policy": 2}),
        "source_draft_hash": canonical_content_hash({"draft": 2}),
    }
    assert all(replace(baseline, **{name: value}).identity_hash != baseline.identity_hash for name, value in replacements.items())
