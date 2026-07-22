from __future__ import annotations

import sys
from dataclasses import replace
from types import MappingProxyType
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.data import canonical_content_hash
from veriquant.proxy_research_smoke.api import (
    AUXILIARY_EVIDENCE_CLASS,
    AuxiliaryFactorDefinition,
    AuxiliaryFactorStatus,
    ProxyResearchSmokeController,
    SourceQualification,
    default_auxiliary_factors,
)
from veriquant.real_research.api import (
    FrozenSnapshotEnvelope,
    build_offline_real_research_fixture,
    build_reader_manifest,
)


class FakeProxySnapshotProvider:
    def __init__(self, envelope: FrozenSnapshotEnvelope) -> None:
        self.envelope = envelope

    def load_envelope(self, *, snapshot_id: str) -> FrozenSnapshotEnvelope:
        assert snapshot_id == self.envelope.manifest.snapshot_id
        return self.envelope


def _run(envelope=None, *, factors=None, qualification=SourceQualification.UNKNOWN):
    fixture = build_offline_real_research_fixture()
    selected = envelope or fixture.envelope
    return ProxyResearchSmokeController(FakeProxySnapshotProvider(selected)).run(
        snapshot_id=selected.manifest.snapshot_id,
        decision_time=fixture.decision_time,
        split_manifest=fixture.split_manifest,
        feature_fields=fixture.feature_fields,
        factors=factors,
        source_qualification=qualification,
        seed=7,
        run_id="proxy-research-smoke-test",
    )


def _perturb_future_envelope():
    fixture = build_offline_real_research_fixture()
    target_time = fixture.calendar[10]
    target_symbol = fixture.symbols[0]
    changed = False
    rows = []
    for row in fixture.snapshot.rows:
        if row.event_time == target_time and row.symbol == target_symbol and not changed:
            values = dict(row.values)
            values["close"] = float(values["close"]) + 321.0
            rows.append(replace(row, values=MappingProxyType(values)))
            changed = True
        else:
            rows.append(row)
    assert changed
    snapshot_hash = canonical_content_hash(
        {"base": fixture.snapshot.snapshot_hash, "perturbed_event": target_time, "symbol": target_symbol}
    )
    snapshot = replace(
        fixture.snapshot,
        rows=tuple(rows),
        snapshot_hash=snapshot_hash,
        dataset_card=replace(fixture.snapshot.dataset_card, snapshot_hash=snapshot_hash),
    )
    manifest = build_reader_manifest(
        snapshot_id=fixture.snapshot_id,
        snapshot=snapshot,
        trading_calendar=fixture.calendar,
        row_market_state=fixture.envelope.row_market_state,
        revision="future-perturbation-r1",
    )
    return fixture, FrozenSnapshotEnvelope(
        manifest=manifest,
        snapshot=snapshot,
        row_market_state=fixture.envelope.row_market_state,
    ), target_time


def test_five_day_labels_are_separate_and_cannot_enter_features() -> None:
    forbidden_label_factor = AuxiliaryFactorDefinition(
        factor_id="forbidden-label-feature",
        payload={
            "schema_version": "veriquant.factor-dag/v1",
            "dag_id": "forbidden-label-feature",
            "nodes": [{"id": "label", "op": "field", "field": "future_return_5d"}],
            "output_node": "label",
        },
    )

    manifest = _run(factors=(forbidden_label_factor,))

    assert manifest.label_forward_days == 5
    assert manifest.label_name == "future_5d_return_lag1"
    assert "future_return_5d" not in manifest.feature_fields
    assert manifest.factors[0].status is AuxiliaryFactorStatus.REJECTED
    assert manifest.factors[0].failure_code == FailureCode.UNKNOWN_FIELD.value


def test_same_frozen_input_produces_same_manifest_hash() -> None:
    first = _run()
    second = _run()

    assert first.artifact_hash == second.artifact_hash
    assert first.bridge_bundle_hash == second.bridge_bundle_hash
    assert all(item.status is AuxiliaryFactorStatus.EVALUATED for item in first.factors)
    assert all(item.evidence_class == AUXILIARY_EVIDENCE_CLASS for item in first.factors)


def test_future_perturbation_does_not_change_past_factor_values() -> None:
    fixture, perturbed, target_time = _perturb_future_envelope()
    factor = (default_auxiliary_factors()[0],)

    base = _run(fixture.envelope, factors=factor)
    changed = _run(perturbed, factors=factor)
    base_past = tuple(
        (item.event_time, item.symbol, item.value)
        for item in base.factors[0].values
        if item.event_time < target_time
    )
    changed_past = tuple(
        (item.event_time, item.symbol, item.value)
        for item in changed.factors[0].values
        if item.event_time < target_time
    )

    assert base.snapshot_hash != changed.snapshot_hash
    assert base_past
    assert base_past == changed_past


def test_incomplete_row_market_state_blocks_bridge() -> None:
    fixture = build_offline_real_research_fixture()
    states = list(fixture.envelope.row_market_state)
    states[0] = replace(states[0], limit_up=None)
    incomplete = FrozenSnapshotEnvelope(
        manifest=fixture.envelope.manifest,
        snapshot=fixture.snapshot,
        row_market_state=tuple(states),
    )

    with pytest.raises(ContractValidationError) as exc:
        _run(incomplete)

    assert exc.value.code is FailureCode.INCOMPLETE_RESULT
    assert "market state" in str(exc.value).lower()


def test_unknown_operator_and_python_payload_are_rejected_without_output() -> None:
    unknown = AuxiliaryFactorDefinition(
        factor_id="unknown-operator",
        payload={
            "schema_version": "veriquant.factor-dag/v1",
            "dag_id": "unknown-operator",
            "nodes": [
                {"id": "close", "op": "field", "field": "close"},
                {"id": "unsafe", "op": "python_callable", "inputs": ["close"]},
            ],
            "output_node": "unsafe",
        },
    )
    python_payload = AuxiliaryFactorDefinition(
        factor_id="python-payload",
        payload={
            "schema_version": "veriquant.factor-dag/v1",
            "dag_id": "python-payload",
            "nodes": [{"id": "close", "op": "field", "field": "close"}],
            "output_node": "close",
            "python": "import os; os.system('forbidden')",
        },
    )

    manifest = _run(factors=(unknown, python_payload))

    assert all(item.status is AuxiliaryFactorStatus.REJECTED for item in manifest.factors)
    assert all(item.output_hash is None and item.dag_hash is None and item.values == () for item in manifest.factors)
    assert {item.failure_code for item in manifest.factors} == {
        FailureCode.INVALID_FACTOR_DAG.value,
        FailureCode.UNKNOWN_FIELD.value,
    }


@pytest.mark.parametrize(
    "qualification",
    [
        SourceQualification.UNKNOWN,
        SourceQualification.TUSHARE_COMPATIBLE_PROXY,
        SourceQualification.OFFICIAL_TUSHARE,
    ],
)
def test_source_qualification_never_grants_promotion(qualification) -> None:
    manifest = _run(qualification=qualification)

    assert manifest.source_qualification is qualification
    assert manifest.evidence_class == AUXILIARY_EVIDENCE_CLASS
    assert manifest.promotion_eligible is False
    assert all(item.promotion_eligible is False for item in manifest.factors)
    assert "Promotion" in manifest.authority_note
