from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from veriquant.approved_proxy_scale_adapter_v2 import (
    API_FIELD_ALLOWLIST,
    ApprovedProxyScaleAdapterV2,
    ProxyScaleAcquisitionRequest,
    ProxyScaleAdapterStatus,
    ProxyScaleFailureCode,
)
from veriquant.research_real_data_v2 import (
    InMemoryFrozenResearchDataProvider,
    ResearchDataRequest,
    ResearchDataStatus,
    ResearchRealDataService,
)
from veriquant.tushare_sdk_proxy import TushareProxyError
from veriquant.approved_proxy_scale_adapter_v2.client import _rows, _validate_query
from veriquant.integration.capture_research_os_v2_data import build_validation_data


CN = timezone(timedelta(hours=8))


def _business_dates(start: str, end: str):
    cursor, stop = date.fromisoformat(start), date.fromisoformat(end)
    output = []
    while cursor <= stop:
        if cursor.weekday() < 5:
            output.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return output


class FakeProxyClient:
    offline_fixture = True

    def __init__(self, *, dated_industry: bool = True):
        self.sessions = _business_dates("2024-01-01", "2025-02-28")
        self.symbols = tuple(f"{index:06d}.SZ" for index in range(30))
        self.entrant = "688999.SH"
        self.dated_industry = dated_industry
        self.calls = []

    def query(self, api_name, *, params, fields):
        assert api_name in API_FIELD_ALLOWLIST
        assert set(fields).issubset(API_FIELD_ALLOWLIST[api_name])
        self.calls.append((api_name, dict(params), tuple(fields)))
        if api_name == "trade_cal":
            return [{"exchange": params["exchange"], "cal_date": session, "is_open": 1, "pretrade_date": ""} for session in self.sessions]
        if api_name == "index_weight":
            first = self.sessions[0]
            middle = self.sessions[len(self.sessions) // 2]
            rows = [
                {"index_code": params["index_code"], "con_code": symbol, "trade_date": first, "weight": 100.0 - index}
                for index, symbol in enumerate(self.symbols)
            ]
            rows.extend(
                {"index_code": params["index_code"], "con_code": symbol, "trade_date": middle, "weight": 100.0 - index}
                for index, symbol in enumerate(self.symbols[:-1] + (self.entrant,))
            )
            return rows
        if api_name == "stock_basic":
            if params["list_status"] != "L":
                return []
            output = []
            for index, symbol in enumerate(self.symbols + (self.entrant,)):
                row = {
                    "ts_code": symbol, "industry": f"industry-{index % 5}",
                    "list_date": "20200101", "delist_date": None, "list_status": "L",
                }
                if self.dated_industry:
                    row.update({"industry_effective_date": "20200101", "industry_available_date": "20200102"})
                output.append(row)
            return output
        symbol = params["ts_code"]
        symbol_index = (self.symbols + (self.entrant,)).index(symbol)
        output = []
        for session_index, session in enumerate(self.sessions):
            close = 10.0 + symbol_index * 0.1 + session_index * 0.01
            if api_name == "daily":
                output.append({
                    "ts_code": symbol, "trade_date": session, "open": close - 0.05,
                    "high": close + 0.10, "low": close - 0.10, "close": close,
                    "vol": 1_000_000 + symbol_index, "amount": close * (1_000_000 + symbol_index),
                })
            elif api_name == "adj_factor":
                output.append({"ts_code": symbol, "trade_date": session, "adj_factor": 1.0})
            elif api_name == "daily_basic":
                output.append({"ts_code": symbol, "trade_date": session, "total_mv": 1_000_000 + symbol_index * 1000})
        return output


class NetworkClient(FakeProxyClient):
    offline_fixture = False


class DatedDelistClient(FakeProxyClient):
    def query(self, api_name, *, params, fields):
        rows = super().query(api_name, params=params, fields=fields)
        if api_name == "stock_basic":
            for row in rows:
                if row.get("ts_code") == self.symbols[0]:
                    row["list_status"] = "D"
                    row["delist_date"] = self.sessions[-20]
        return rows


class LargeHistoricalUnionClient(FakeProxyClient):
    """Two valid historical snapshots whose candidate union is above fifty."""

    def __init__(self):
        super().__init__()
        self.symbols = tuple(f"{index:06d}.SZ" for index in range(70))
        self.entrant = "688999.SH"

    def query(self, api_name, *, params, fields):
        if api_name != "index_weight":
            return super().query(api_name, params=params, fields=fields)
        self.calls.append((api_name, dict(params), tuple(fields)))
        first = self.sessions[0]
        middle = self.sessions[len(self.sessions) // 2]
        rows = [
            {
                "index_code": params["index_code"],
                "con_code": symbol,
                "trade_date": first,
                "weight": 100.0 - index,
            }
            for index, symbol in enumerate(self.symbols[:40])
        ]
        rows.extend(
            {
                "index_code": params["index_code"],
                "con_code": symbol,
                "trade_date": middle,
                "weight": 100.0 - index,
            }
            for index, symbol in enumerate(self.symbols[30:70])
        )
        return rows


def _request(**kwargs):
    values = dict(
        dataset_id="approved-proxy-scale-v2-fixture",
        index_code="000300.SH",
        start_date="20240101",
        end_date="20250228",
        capture_time=datetime(2025, 2, 28, 20, tzinfo=CN),
        target_symbols=30,
        required_sessions=260,
    )
    values.update(kwargs)
    return ProxyScaleAcquisitionRequest(**values)


@pytest.fixture(scope="module")
def built():
    return ApprovedProxyScaleAdapterV2(FakeProxyClient()).capture(_request())


def test_builds_30_by_260_frozen_input_with_approved_labels(built):
    assert built.status is ProxyScaleAdapterStatus.READY
    frozen = built.frozen_input
    assert frozen is not None
    assert len(frozen.calendar_sessions) == 260
    assert min(len(item.symbols) for item in frozen.historical_universe) == 30
    assert max(len(item.symbols) for item in frozen.historical_universe) == 30
    assert frozen.provider_class == "APPROVED_PROXY_DATA_VENDOR"
    assert frozen.source_class == "TUSHARE_COMPATIBLE_PROXY"
    assert "future_return_5d" not in frozen.feature_fields
    assert frozen.label_fields == ("future_return_5d",)
    assert all(row.event_time <= row.available_at for row in frozen.rows)


def test_capture_freezes_both_sse_and_szse_calendars():
    client = FakeProxyClient()
    result = ApprovedProxyScaleAdapterV2(client).capture(_request())
    assert result.status is ProxyScaleAdapterStatus.READY
    exchanges = {
        call[1]["exchange"]
        for call in client.calls
        if call[0] == "trade_cal"
    }
    assert exchanges == {"SSE", "SZSE"}
    assert result.frozen_input.calendar_id == "sse-szse-approved-proxy-v2"


def test_output_is_accepted_by_research_real_data_service_as_fixture(built):
    outcome = ResearchRealDataService().prepare(
        InMemoryFrozenResearchDataProvider(built.frozen_input),
        ResearchDataRequest(request_id="proxy-scale-v2-test", dataset_id="approved-proxy-scale-v2-fixture", lookback_sessions=50),
    )
    assert outcome.qualification.status is ResearchDataStatus.FIXTURE_ONLY
    assert outcome.qualification.manifest.recommended_50d_history_satisfied is True


def test_historical_membership_uses_asof_snapshot_not_future_union(built):
    frozen = built.frozen_input
    assert frozen is not None
    assert "688999.SH" not in frozen.historical_universe[0].symbols
    assert "688999.SH" in frozen.historical_universe[-1].symbols


def test_same_fake_capture_has_stable_content_and_manifest_hashes():
    first = ApprovedProxyScaleAdapterV2(FakeProxyClient()).capture(_request()).frozen_input
    second = ApprovedProxyScaleAdapterV2(FakeProxyClient()).capture(_request()).frozen_input
    assert first.data_hash == second.data_hash
    assert first.adapter_manifest_hash == second.adapter_manifest_hash
    assert first.artifact_hash == second.artifact_hash


def test_non_default_label_horizon_is_bound_to_rows_and_policy_hash(built):
    result = ApprovedProxyScaleAdapterV2(FakeProxyClient()).capture(
        _request(required_sessions=250, label_horizon_sessions=10)
    )
    assert result.status is ProxyScaleAdapterStatus.READY
    frozen = result.frozen_input
    assert frozen is not None
    assert frozen.label_fields == ("future_return_10d",)
    assert "future_return_10d" not in frozen.feature_fields
    assert all(row.labels["future_return_10d"] is not None for row in frozen.rows)
    default = built.frozen_input
    assert default is not None
    assert frozen.availability_policy_hash != default.availability_policy_hash


def test_network_is_disabled_by_default_without_calling_client():
    client = NetworkClient()
    result = ApprovedProxyScaleAdapterV2(client).capture(_request())
    assert result.status is ProxyScaleAdapterStatus.UNAVAILABLE
    assert result.failure_codes == (ProxyScaleFailureCode.NETWORK_NOT_ENABLED,)
    assert client.calls == []


def test_price_core_request_is_ready_without_dated_industry_and_does_not_backfill():
    result = ApprovedProxyScaleAdapterV2(FakeProxyClient(dated_industry=False)).capture(_request())
    assert result.status is ProxyScaleAdapterStatus.READY
    assert "industry" not in result.frozen_input.feature_fields
    assert all("industry" not in row.features for row in result.frozen_input.rows)
    assert all(row.features["status"] == "LISTED" for row in result.frozen_input.rows)


def test_industry_neutral_request_is_typed_unavailable_without_dated_history():
    request = _request(
        required_feature_fields=("open", "high", "low", "close", "volume", "amount", "adj_factor", "market_cap", "status", "industry"),
        optional_feature_fields=(),
    )
    result = ApprovedProxyScaleAdapterV2(FakeProxyClient(dated_industry=False)).capture(request)
    assert result.status is ProxyScaleAdapterStatus.UNAVAILABLE
    assert result.failure_codes == (ProxyScaleFailureCode.INDUSTRY_HISTORY_UNAVAILABLE,)
    assert result.frozen_input is None


def test_manifest_records_missing_optional_industry_without_silent_fill():
    result = ApprovedProxyScaleAdapterV2(FakeProxyClient(dated_industry=False)).capture(_request())
    outcome = ResearchRealDataService().prepare(
        InMemoryFrozenResearchDataProvider(result.frozen_input),
        ResearchDataRequest(request_id="price-core-no-industry", dataset_id="approved-proxy-scale-v2-fixture"),
    )
    manifest = outcome.qualification.manifest
    assert outcome.qualification.status is ResearchDataStatus.FIXTURE_ONLY
    assert manifest.available_optional_feature_fields == ()
    assert manifest.missing_optional_feature_fields == ("industry",)
    assert manifest.limitations == ("OPTIONAL_FIELD_UNAVAILABLE:industry",)
    assert manifest.coverage_by_field["industry"] == 0.0


def test_allowlist_rejects_unknown_api_field_and_credential_injection():
    _validate_query(
        "income",
        params={"ts_code": "000001.SZ"},
        fields=("ts_code", "ann_date", "revenue"),
    )
    with pytest.raises(TushareProxyError):
        _validate_query("unknown_api", params={}, fields=("revenue",))
    with pytest.raises(TushareProxyError):
        _validate_query("daily", params={"token": "redacted"}, fields=("ts_code",))
    with pytest.raises(TushareProxyError):
        _validate_query("daily", params={}, fields=("future_return_5d",))


def test_last_five_research_session_labels_use_frozen_label_only_tail():
    frozen = ApprovedProxyScaleAdapterV2(FakeProxyClient()).capture(_request()).frozen_input
    last_sessions = set(frozen.calendar_sessions[-5:])
    trailing = [row for row in frozen.rows if row.session in last_sessions]
    assert trailing
    assert all(row.labels["future_return_5d"] is not None for row in trailing)
    assert all("future_return_5d" not in row.features for row in frozen.rows)


def test_no_secret_or_official_tushare_is_serialized(built):
    rendered = built.canonical_json().lower()
    assert "token" not in rendered
    assert "api_key" not in rendered
    assert "official_tushare" not in rendered


def test_status_is_derived_from_list_delist_events_not_current_status():
    frozen = ApprovedProxyScaleAdapterV2(DatedDelistClient()).capture(_request()).frozen_input
    symbol = "000000.SZ"
    rows = [row for row in frozen.rows if row.symbol == symbol]
    assert any(row.features["status"] == "LISTED" for row in rows)
    assert any(row.features["status"] == "INACTIVE" for row in rows)


def test_daily_universe_limit_is_independent_from_full_period_acquisition_union():
    client = LargeHistoricalUnionClient()
    result = ApprovedProxyScaleAdapterV2(client).capture(_request())
    assert result.status is ProxyScaleAdapterStatus.READY
    frozen = result.frozen_input
    assert frozen is not None
    assert len({symbol for item in frozen.historical_universe for symbol in item.symbols}) == 60
    assert {len(item.symbols) for item in frozen.historical_universe} == {30}
    assert len({call[1]["ts_code"] for call in client.calls if call[0] == "daily"}) == 70


def test_explicit_acquisition_union_limit_still_fails_closed():
    result = ApprovedProxyScaleAdapterV2(LargeHistoricalUnionClient()).capture(
        _request(max_acquisition_symbols=60)
    )
    assert result.status is ProxyScaleAdapterStatus.UNAVAILABLE
    assert result.failure_codes == (ProxyScaleFailureCode.SYMBOL_SCALE_OUT_OF_BOUNDS,)
    assert result.frozen_input is None


@pytest.mark.parametrize("value", [29, True])
def test_explicit_acquisition_union_limit_must_cover_target(value):
    with pytest.raises(ValueError):
        _request(max_acquisition_symbols=value)


@pytest.mark.parametrize(
    ("target_symbols", "required_sessions"),
    [(1, 20), (300, 1200), (5000, 5000)],
)
def test_request_contract_has_no_small_sample_product_ceiling(
    target_symbols, required_sessions
):
    request = _request(
        target_symbols=target_symbols,
        required_sessions=required_sessions,
        max_acquisition_symbols=None,
    )
    assert request.target_symbols == target_symbols
    assert request.required_sessions == required_sessions
    assert request.max_acquisition_symbols is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("target_symbols", 0), ("required_sessions", 0)],
)
def test_dynamic_scale_still_rejects_non_positive_requirements(field, value):
    with pytest.raises(ValueError):
        _request(**{field: value})


def test_ready_capture_projects_to_hashable_research_os_validation_data(built):
    frozen = built.frozen_input
    assert frozen is not None
    projected = build_validation_data(
        frozen,
        qualification_hash="sha256:" + "1" * 64,
    )
    assert projected.source_label == "APPROVED_PROXY_DATA_VENDOR"
    assert len(frozen.calendar_sessions) == 260
    assert len(projected.snapshot.historical_universe) == 260
    assert len(projected.snapshot.rows) == 30 * 260
    assert len(projected.labels) == 30 * 260
    assert all(
        f"{row.event_time.isoformat()}|{row.symbol}" in projected.labels
        for row in projected.snapshot.rows
    )
    assert projected.artifact_hash.startswith("sha256:")


def test_provider_table_nan_is_normalized_before_canonical_freeze():
    rows = _rows([{"ts_code": "000001.SZ", "roe": float("nan"), "value": 1.5}])

    assert rows == [{"ts_code": "000001.SZ", "roe": None, "value": 1.5}]
