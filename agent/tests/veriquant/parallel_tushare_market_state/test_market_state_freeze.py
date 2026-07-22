from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

AGENT_SRC = Path(__file__).resolve().parents[3] / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from veriquant.tushare_market_state.models import (
    MarketStateError,
    MarketStateFailureCode,
    MarketStateInputRow,
    MarketStateRequest,
)
from veriquant.tushare_market_state.service import RestrictedMarketStateClient
from veriquant.tushare_market_state.store import FrozenMarketStateStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
H3 = "sha256:" + "3" * 64
H4 = "sha256:" + "4" * 64


def _event(day: int) -> datetime:
    return datetime(2024, 1, day, 15, 0, tzinfo=SHANGHAI)


def _input_rows() -> tuple[MarketStateInputRow, ...]:
    return (
        MarketStateInputRow(symbol="000001.SZ", trade_date="2024-01-02", event_time=_event(2), close=11.0),
        MarketStateInputRow(symbol="000001.SZ", trade_date="2024-01-03", event_time=_event(3), close=10.5),
        MarketStateInputRow(symbol="600000.SH", trade_date="2024-01-02", event_time=_event(2), close=18.0),
        MarketStateInputRow(symbol="600000.SH", trade_date="2024-01-03", event_time=_event(3), close=20.5),
    )


def _request(**overrides) -> MarketStateRequest:
    values = {
        "dataset_id": "market-state-fixture",
        "codes": ("000001.SZ", "600000.SH"),
        "start_date": "2024-01-02",
        "end_date": "2024-01-03",
        "decision_time": datetime(2024, 1, 4, 10, 0, tzinfo=SHANGHAI),
        "rows": _input_rows(),
        "source_snapshot_hash": H1,
        "calendar_hash": H2,
        "universe_hash": H3,
        "bar_capture_hash": H4,
    }
    values.update(overrides)
    return MarketStateRequest(**values)


class FakeSdk:
    def __init__(
        self,
        *,
        missing_api: str | None = None,
        revised_adj: float | None = None,
        secret_field: bool = False,
        exception_message: str | None = None,
    ) -> None:
        self.missing_api = missing_api
        self.revised_adj = revised_adj
        self.secret_field = secret_field
        self.exception_message = exception_message
        self.calls: list[tuple[str, dict]] = []

    def _return(self, name: str, params: dict, rows: list[dict]) -> list[dict]:
        self.calls.append((name, dict(params)))
        if self.exception_message is not None and name == "stock_basic":
            raise RuntimeError(self.exception_message)
        if self.missing_api == name:
            return []
        if self.secret_field and name == "stock_basic":
            rows[0]["access_token"] = "NEVER_PERSIST_THIS"
        return list(reversed(rows))

    def stock_basic(self, **params):
        code = params["ts_code"]
        row = {
            "ts_code": code,
            "list_status": "L",
            "list_date": "19910403" if code == "000001.SZ" else "19991110",
            "delist_date": None,
        }
        return self._return("stock_basic", params, [row])

    def suspend_d(self, **params):
        rows = []
        if params["ts_code"] == "000001.SZ":
            rows = [{"ts_code": "000001.SZ", "suspend_date": "20240103", "resume_date": "20240104"}]
        return self._return("suspend_d", params, rows)

    def stk_limit(self, **params):
        code = params["ts_code"]
        if code == "000001.SZ":
            rows = [
                {"ts_code": code, "trade_date": "20240102", "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0},
                {"ts_code": code, "trade_date": "20240103", "pre_close": 11.0, "up_limit": 12.1, "down_limit": 9.9},
            ]
        else:
            rows = [
                {"ts_code": code, "trade_date": "20240102", "pre_close": 20.0, "up_limit": 22.0, "down_limit": 18.0},
                {"ts_code": code, "trade_date": "20240103", "pre_close": 18.0, "up_limit": 21.0, "down_limit": 17.0},
            ]
        return self._return("stk_limit", params, rows)

    def adj_factor(self, **params):
        code = params["ts_code"]
        changed = self.revised_adj if self.revised_adj is not None and code == "000001.SZ" else 1.0
        rows = [
            {"ts_code": code, "trade_date": "20240102", "adj_factor": 1.0},
            {"ts_code": code, "trade_date": "20240103", "adj_factor": changed},
        ]
        return self._return("adj_factor", params, rows)


def test_fake_sdk_freezes_complete_row_market_state_and_bindings(tmp_path: Path) -> None:
    sdk = FakeSdk()
    bundle = FrozenMarketStateStore(tmp_path).freeze(_request(), sdk=sdk)
    assert len(bundle.rows) == 4
    assert len(sdk.calls) == 8
    assert {name for name, _ in sdk.calls} == {"stock_basic", "suspend_d", "stk_limit", "adj_factor"}
    assert bundle.manifest.source_snapshot_hash == H1
    assert bundle.manifest.calendar_hash == H2
    assert bundle.manifest.universe_hash == H3
    assert bundle.manifest.bar_capture_hash == H4
    assert bundle.manifest.authority_class == "AUXILIARY_RESEARCH_EVIDENCE"
    assert bundle.manifest.promotion_eligible is False
    assert bundle.manifest.historical_availability_attested is False
    by_key = {(row.symbol, row.trade_date): row for row in bundle.rows}
    assert by_key[("000001.SZ", "2024-01-02")].limit_up is True
    assert by_key[("600000.SH", "2024-01-02")].limit_down is True
    assert by_key[("000001.SZ", "2024-01-03")].suspended is True
    assert by_key[("600000.SH", "2024-01-03")].suspended is False
    assert all(row.delisted is False for row in bundle.rows)
    bridge_row = bundle.rows[0].to_row_market_state()
    assert bridge_row.revision == bundle.rows[0].revision
    assert bridge_row.available_at <= bundle.manifest.decision_time


def test_same_input_is_deterministic_despite_provider_row_order(tmp_path: Path) -> None:
    request = _request()
    first = FrozenMarketStateStore(tmp_path / "one").freeze(request, sdk=FakeSdk())
    second = FrozenMarketStateStore(tmp_path / "two").freeze(request, sdk=FakeSdk())
    assert first.manifest.provider_capture_hash == second.manifest.provider_capture_hash
    assert first.manifest.records_hash == second.manifest.records_hash
    assert first.manifest.vintage_hash == second.manifest.vintage_hash
    assert first.handle.manifest_hash == second.handle.manifest_hash


@pytest.mark.parametrize("missing_api", ["stock_basic", "stk_limit", "adj_factor"])
def test_missing_necessary_metadata_fails_closed(tmp_path: Path, missing_api: str) -> None:
    with pytest.raises(MarketStateError) as caught:
        FrozenMarketStateStore(tmp_path).freeze(_request(), sdk=FakeSdk(missing_api=missing_api))
    assert caught.value.code in {
        MarketStateFailureCode.INCOMPLETE_METADATA,
        MarketStateFailureCode.INVALID_RECORD,
    }


def test_empty_suspension_result_is_a_captured_definitive_not_suspended_state(tmp_path: Path) -> None:
    bundle = FrozenMarketStateStore(tmp_path).freeze(_request(), sdk=FakeSdk(missing_api="suspend_d"))
    assert all(row.suspended is False for row in bundle.rows)
    suspension_calls = [call for call in bundle.capture.calls if call.api_name == "suspend_d"]
    assert len(suspension_calls) == 2
    assert all(call.rows == () for call in suspension_calls)


def test_future_market_state_is_rejected_not_hidden(tmp_path: Path) -> None:
    with pytest.raises(MarketStateError) as caught:
        FrozenMarketStateStore(tmp_path).freeze(
            _request(decision_time=datetime(2024, 1, 3, 17, 0, tzinfo=SHANGHAI)),
            sdk=FakeSdk(),
        )
    assert caught.value.code is MarketStateFailureCode.FUTURE_DATA


def test_changed_input_identity_changes_vintage_hash(tmp_path: Path) -> None:
    first = FrozenMarketStateStore(tmp_path / "one").freeze(_request(), sdk=FakeSdk())
    second = FrozenMarketStateStore(tmp_path / "two").freeze(
        _request(calendar_hash="sha256:" + "9" * 64),
        sdk=FakeSdk(),
    )
    assert first.manifest.records_hash == second.manifest.records_hash
    assert first.manifest.vintage_hash != second.manifest.vintage_hash


def test_revision_creates_new_vintage_without_rewriting_old_bytes(tmp_path: Path) -> None:
    store = FrozenMarketStateStore(tmp_path)
    first = store.freeze(_request(), sdk=FakeSdk())
    first_manifest = Path(first.handle.manifest_path).read_bytes()
    second = store.freeze(_request(), sdk=FakeSdk(revised_adj=1.25))
    assert first.manifest.provider_capture_hash != second.manifest.provider_capture_hash
    assert first.manifest.revision != second.manifest.revision
    assert first.manifest.vintage_hash != second.manifest.vintage_hash
    assert Path(first.handle.manifest_path).read_bytes() == first_manifest
    reopened = store.load(dataset_id=first.manifest.dataset_id, vintage_hash=first.manifest.vintage_hash)
    assert reopened.manifest.artifact_hash == first.manifest.artifact_hash
    assert reopened.rows == first.rows


def test_tampering_fails_closed(tmp_path: Path) -> None:
    store = FrozenMarketStateStore(tmp_path)
    bundle = store.freeze(_request(), sdk=FakeSdk())
    Path(bundle.handle.manifest_path).with_name("records.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(MarketStateError) as caught:
        store.load(dataset_id=bundle.manifest.dataset_id, vintage_hash=bundle.manifest.vintage_hash)
    assert caught.value.code in {
        MarketStateFailureCode.SNAPSHOT_CORRUPT,
        MarketStateFailureCode.HASH_MISMATCH,
    }


def test_secret_like_provider_field_is_rejected_without_echo(tmp_path: Path) -> None:
    with pytest.raises(MarketStateError) as caught:
        FrozenMarketStateStore(tmp_path).freeze(_request(), sdk=FakeSdk(secret_field=True))
    assert caught.value.code is MarketStateFailureCode.SECRET_MATERIAL
    assert "NEVER_PERSIST_THIS" not in str(caught.value)
    assert "NEVER_PERSIST_THIS" not in repr(dict(caught.value.details))
    assert not list(tmp_path.rglob("*.json"))


def test_sdk_exception_is_sanitized_and_never_records_exception_value(tmp_path: Path) -> None:
    marker = "CREDENTIAL_SENTINEL_MUST_NOT_ESCAPE"
    with pytest.raises(MarketStateError) as caught:
        FrozenMarketStateStore(tmp_path).freeze(_request(), sdk=FakeSdk(exception_message=marker))
    assert caught.value.code is MarketStateFailureCode.SDK_FAILURE
    assert marker not in str(caught.value)
    assert marker not in repr(dict(caught.value.details))


def test_restricted_client_rejects_non_allowlisted_api() -> None:
    client = RestrictedMarketStateClient(FakeSdk())
    with pytest.raises(MarketStateError) as caught:
        client._call("daily_basic")
    assert caught.value.code is MarketStateFailureCode.API_OUTSIDE_ALLOWLIST


def test_artifacts_contain_no_secret_or_mcp_material(tmp_path: Path) -> None:
    bundle = FrozenMarketStateStore(tmp_path).freeze(_request(), sdk=FakeSdk())
    artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    lowered = artifact_text.lower()
    assert "access_token" not in lowered
    assert "authorization" not in lowered
    assert "mcp" not in lowered
    manifest = json.loads(Path(bundle.handle.manifest_path).read_text(encoding="utf-8"))
    assert manifest["manifest"]["provider_capture_hash"] == bundle.capture.artifact_hash
