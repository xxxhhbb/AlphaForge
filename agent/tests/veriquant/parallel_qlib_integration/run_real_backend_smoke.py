"""Explicit smoke entrypoint for the locally pinned vibe-qlib environment."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src.veriquant.contracts import Component, DatasetCard
from src.veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from src.veriquant.formula_execution import (
    CertifiedQlibProgramBundle,
    FormulaExecutionRequest,
    FormulaExecutionService,
    FormulaProgramIdentity,
    FormulaProgramKind,
    configured_isolated_qlib_backend,
)
from src.veriquant.frozen_qlib_provider import FrozenQlibProvider, QlibDataRequirement
from src.veriquant.qlib_candidate_translation import QlibCandidateDraftEnvelope, SafeQlibCandidateTranslator
from src.veriquant.qlib_operator_worker.api import QlibRuntimeIdentity
from src.veriquant.research_campaign import CampaignState


def main() -> int:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = tuple(
        SnapshotRow(
            event_time=start + timedelta(days=day),
            symbol=symbol,
            values={"close": base + day, "volume": 1000.0 + day},
        )
        for day in range(5)
        for symbol, base in (("AAA", 10.0), ("BBB", 20.0))
    )
    snapshot = MarketSnapshot(
        dataset_id="qlib.real.backend.smoke",
        decision_time=start + timedelta(days=4, hours=23),
        start_time=start,
        fields=("close", "volume"),
        rows=rows,
        historical_universe={
            (start + timedelta(days=day)).isoformat(): ("AAA", "BBB")
            for day in range(5)
        },
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )
    translation = SafeQlibCandidateTranslator().translate(
        QlibCandidateDraftEnvelope(
            campaign_hash=canonical_content_hash({"campaign": "real-smoke"}),
            campaign_state=CampaignState.CONFIRMED,
            group_id="market_structure",
            candidate_id="real.ref.one",
            expression="Ref($close,1)",
            confirmed_field_whitelist=("close", "volume"),
            requested_lookback_sessions=1,
            source_draft_hash=canonical_content_hash({"draft": "real.ref.one"}),
        )
    )
    card = DatasetCard(
        dataset_id=snapshot.dataset_id,
        source="offline-fixture",
        universe=("AAA", "BBB"),
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
        program_id="real.ref.one",
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
    backend = configured_isolated_qlib_backend()
    if backend is None:
        raise RuntimeError("VERIQUANT_QLIB_PYTHON is not a valid pinned local runtime")
    service = FormulaExecutionService(backend)
    bundle = CertifiedQlibProgramBundle(translation=translation, provider_manifest=manifest)
    first = service.execute(request=request, program=bundle, snapshot=snapshot)
    second = service.execute(request=request, program=bundle, snapshot=snapshot)
    if first.artifact_hash != second.artifact_hash:
        raise RuntimeError("real Qlib backend replay hash changed")
    payload = {
        "status": "PASSED",
        "execution_mode": "DEDICATED_STRUCTURED_SUBPROCESS",
        "pyqlib_version": "0.9.7",
        "network_used": False,
        "expression": translation.canonical_expression,
        "program_hash": translation.program_hash,
        "runtime_hash": identity.runtime_hash,
        "provider_manifest_hash": manifest.manifest_hash,
        "score_artifact_hash": first.artifact_hash,
        "row_count": len(first.values),
        "non_null_count": sum(row.score is not None for row in first.values),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
