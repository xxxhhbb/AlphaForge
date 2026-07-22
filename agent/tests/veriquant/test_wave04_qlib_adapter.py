from __future__ import annotations

import importlib.util
import json
import socket
from datetime import datetime, timezone

import pytest

from src.veriquant.adapters import (
    AdapterFailure,
    FakeQlibAdapter,
    OptionalPyQlibAdapter,
    QlibCostConfig,
    QlibDateSegment,
    QlibManifest,
    QlibVisibleResult,
    QlibVisibleRow,
    default_qlib_adapter,
)
from src.veriquant.contracts import (
    Component,
    ContractValidationError,
    EvidenceClass,
    EvidencePack,
    FailureCode,
)
from src.veriquant.data import FieldRole
from src.veriquant.factors import parse_compile_factor_dag


UTC = timezone.utc


def at(day: int) -> datetime:
    return datetime(2024, 1, day, 16, 0, tzinfo=UTC)


def manifest(provider_uri: str) -> QlibManifest:
    return QlibManifest(
        provider_uri=provider_uri,
        region="cn",
        universe=("AAA", "BBB"),
        train=QlibDateSegment(start=at(1), end=at(2)),
        validation=QlibDateSegment(start=at(3), end=at(4)),
        test=QlibDateSegment(start=at(5), end=at(6)),
        label="future_return_5d",
        model="LinearModel",
        strategy="TopkDropoutStrategy-auxiliary-only",
        cost=QlibCostConfig(
            open_cost_bps=5.0,
            close_cost_bps=15.0,
            linear_impact_bps=2.0,
            minimum_cost=5.0,
        ),
        seed=7,
    )


def candidate():
    return parse_compile_factor_dag(
        {
            "schema_version": "veriquant.factor-dag/v1",
            "dag_id": "qlib-visible-candidate",
            "nodes": [
                {"id": "close", "op": "field", "field": "close"},
                {"id": "mom", "op": "pct_change", "inputs": ["close"], "periods": 20},
            ],
            "output_node": "mom",
        },
        field_roles={"close": FieldRole.FEATURE},
    )


def visible_rows() -> tuple[QlibVisibleRow, ...]:
    return (
        QlibVisibleRow(partition="train", event_time=at(1), symbol="AAA", score=-1.0, label=-0.02),
        QlibVisibleRow(partition="train", event_time=at(2), symbol="BBB", score=1.0, label=0.03),
        QlibVisibleRow(partition="validation", event_time=at(3), symbol="AAA", score=-0.5, label=-0.01),
        QlibVisibleRow(partition="validation", event_time=at(4), symbol="BBB", score=0.5, label=0.02),
    )


def test_fake_qlib_is_default_offline_and_returns_auxiliary_only(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENABLE_QLIB_ADAPTER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network used")))
    adapter = default_qlib_adapter()

    assert isinstance(adapter, FakeQlibAdapter)
    result = adapter.run_visible(
        manifest=manifest(str(tmp_path)),
        candidate=candidate(),
        rows=visible_rows(),
        as_of=at(4),
    )

    assert isinstance(result, QlibVisibleResult)
    assert result.auxiliary_evidence.evidence_class is EvidenceClass.AUXILIARY_RESEARCH_EVIDENCE
    assert result.auxiliary_evidence.source_component is Component.QLIB
    assert result.train_metrics["observations"] == 2.0
    assert result.validation_metrics["rank_ic"] == 1.0


def test_auxiliary_qlib_payload_cannot_create_official_evidence_pack() -> None:
    with pytest.raises(ContractValidationError) as caught:
        EvidencePack.from_payload(
            {
                "schema_version": "1.0.0",
                "contract_type": "EvidencePack",
                "official_metrics": {"RankIC": 1.0},
            },
            component=Component.QLIB,
        )
    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_optional_adapter_missing_pyqlib_returns_typed_unavailable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_QLIB_ADAPTER", "1")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    result = OptionalPyQlibAdapter().run_visible(
        manifest=manifest(str(tmp_path)),
        candidate=candidate(),
        rows=visible_rows(),
        as_of=at(4),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code is FailureCode.ADAPTER_UNAVAILABLE
    assert "pyqlib" in result.message


def test_optional_adapter_disabled_does_not_probe_dependency(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENABLE_QLIB_ADAPTER", raising=False)
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: (_ for _ in ()).throw(AssertionError("dependency probe should not run")),
    )
    result = OptionalPyQlibAdapter().run_visible(
        manifest=manifest(str(tmp_path)),
        candidate=candidate(),
        rows=visible_rows(),
        as_of=at(4),
    )
    assert isinstance(result, AdapterFailure)
    assert result.code is FailureCode.ADAPTER_UNAVAILABLE


def test_visible_result_and_runner_view_contain_no_sealed_or_test_metrics(tmp_path) -> None:
    frozen = manifest(str(tmp_path))
    visible_manifest = frozen.visible()
    result = FakeQlibAdapter().run_visible(
        manifest=frozen,
        candidate=candidate(),
        rows=visible_rows(),
        as_of=at(4),
    )
    rendered = json.dumps(result.to_dict(), sort_keys=True).lower()

    assert "test" not in rendered
    assert "sealed" not in rendered
    assert "test" not in visible_manifest.to_dict()
    assert not hasattr(FakeQlibAdapter(), "run_sealed")


def test_visible_row_rejects_test_partition() -> None:
    with pytest.raises(ContractValidationError) as caught:
        QlibVisibleRow(
            partition="test",
            event_time=at(5),
            symbol="AAA",
            score=1.0,
            label=0.1,
        )
    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_manifest_rejects_remote_provider_uri() -> None:
    with pytest.raises(ContractValidationError):
        manifest("https://example.invalid/qlib-data")
