from __future__ import annotations

import json
from pathlib import Path

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.data import canonical_content_hash
from veriquant.integration.v1_real_data_chain import (
    CapturedMarketStateSdk,
    PROXY_HOST,
    PROXY_PATH,
    build_market_state_request,
    load_verified_price_snapshot,
    run_real_auxiliary_chain,
)
from veriquant.integration import v1_real_data_cli
from veriquant.provider_provenance import ProviderEligibility
from veriquant.real_ashare_data.reader import SnapshotIntegrityError
from veriquant.tushare_market_state.store import FrozenMarketStateStore


REPO = Path(__file__).resolve().parents[4]
STAGE = REPO / ".autobuild/veriquant/integration/v1_tushare_proxy_stage"
SNAPSHOT_ROOT = STAGE / "snapshots"
CALENDAR_ROOT = REPO / ".autobuild/veriquant/calendars"
MANIFEST = (
    SNAPSHOT_ROOT
    / "tushare-proxy-operational-smoke"
    / "dd9b48413fae1c89c52e54b5f52cf7ef46de88fd3d98fc6bae776a1a30cd527a"
    / "manifest.json"
)
BAR_CAPTURE = (
    STAGE
    / "captures"
    / "fcd43df20177dfc6df425c2dbafa5855639905831fbe14c88be177296fefb2d7"
    / "capture.json"
)


def _bar_capture_hash() -> str:
    return canonical_content_hash(json.loads(BAR_CAPTURE.read_text(encoding="utf-8")))


class FakeMarketStateSdk:
    def __init__(self) -> None:
        _handle, snapshot = load_verified_price_snapshot(
            manifest_path=MANIFEST,
            snapshot_root=SNAPSHOT_ROOT,
            calendar_root=CALENDAR_ROOT,
        )
        self.close = {
            (row.symbol, row.event_time.date().strftime("%Y%m%d")): float(row.values["close"])
            for row in snapshot.rows
        }

    def stock_basic(self, **params):
        return [
            {
                "ts_code": params["ts_code"],
                "list_status": "L",
                "list_date": "19910403",
                "delist_date": None,
            }
        ]

    def suspend_d(self, **params):
        return []

    def stk_limit(self, **params):
        symbol = params["ts_code"]
        return [
            {
                "ts_code": symbol,
                "trade_date": day,
                "pre_close": close,
                "up_limit": close * 2.0,
                "down_limit": close * 0.5,
            }
            for (code, day), close in sorted(self.close.items())
            if code == symbol and params["start_date"] <= day <= params["end_date"]
        ]

    def adj_factor(self, **params):
        symbol = params["ts_code"]
        return [
            {"ts_code": symbol, "trade_date": day, "adj_factor": 1.0}
            for (code, day), _close in sorted(self.close.items())
            if code == symbol and params["start_date"] <= day <= params["end_date"]
        ]


def _run(tmp_path: Path):
    return run_real_auxiliary_chain(
        sdk=FakeMarketStateSdk(),
        manifest_path=MANIFEST,
        snapshot_root=SNAPSHOT_ROOT,
        calendar_root=CALENDAR_ROOT,
        market_state_root=tmp_path / "market-state",
        bar_capture_hash=_bar_capture_hash(),
        endpoint_host=PROXY_HOST,
        endpoint_path=PROXY_PATH,
        network_used=False,
    )


def test_real_frozen_snapshot_market_state_and_factor_chain_is_reproducible(tmp_path: Path) -> None:
    first = _run(tmp_path / "first")
    second = _run(tmp_path / "second")

    assert first.artifact_hash == second.artifact_hash
    assert first.snapshot_hash == "sha256:dd9b48413fae1c89c52e54b5f52cf7ef46de88fd3d98fc6bae776a1a30cd527a"
    assert first.market_state_row_count == 116
    assert first.factor_count == 2
    assert first.capture_replay_verified is True


def test_compatible_proxy_stops_before_all_official_and_financial_authorities(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.authority_class == "AUXILIARY_RESEARCH_EVIDENCE"
    assert result.provider_eligibility == ProviderEligibility.AUXILIARY_ONLY.value
    assert result.official_evidence_run is False
    assert result.promotion_run is False
    assert result.challenger_created is False
    assert result.real_shadow_replay_run is False
    assert "attestation" in result.gate_blocker


def test_endpoint_and_path_are_fixed_and_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ContractValidationError) as error:
        run_real_auxiliary_chain(
            sdk=FakeMarketStateSdk(),
            manifest_path=MANIFEST,
            snapshot_root=SNAPSHOT_ROOT,
            calendar_root=CALENDAR_ROOT,
            market_state_root=tmp_path,
            bar_capture_hash=_bar_capture_hash(),
            endpoint_host="example.invalid",
            endpoint_path=PROXY_PATH,
            network_used=False,
        )
    assert error.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_captured_sdk_replay_rejects_changed_parameters(tmp_path: Path) -> None:
    handle, snapshot = load_verified_price_snapshot(
        manifest_path=MANIFEST,
        snapshot_root=SNAPSHOT_ROOT,
        calendar_root=CALENDAR_ROOT,
    )
    request = build_market_state_request(
        snapshot=snapshot,
        handle=handle,
        bar_capture_hash=_bar_capture_hash(),
    )
    bundle = FrozenMarketStateStore(tmp_path / "captured").freeze(request, sdk=FakeMarketStateSdk())
    replay = CapturedMarketStateSdk(bundle.capture)

    with pytest.raises(ContractValidationError) as error:
        replay.stock_basic(ts_code="000001.SZ", fields="changed")
    assert error.value.code is FailureCode.INVALID_HASH


def test_cli_defaults_offline_and_does_not_require_provider_keys(tmp_path: Path, monkeypatch) -> None:
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(v1_real_data_cli, "_paths", lambda: {"result": result_path})

    assert v1_real_data_cli.main([]) == 0
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "OFFLINE_DISABLED"
    assert payload["network_used"] is False
    assert payload["official_evidence_run"] is False


def test_snapshot_reader_rejects_manifest_outside_fixed_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "manifest.json"
    outside.parent.mkdir(parents=True)
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError):
        load_verified_price_snapshot(
            manifest_path=outside,
            snapshot_root=SNAPSHOT_ROOT,
            calendar_root=CALENDAR_ROOT,
        )
