from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.veriquant.data.ashare import AShareDataRequest
from src.veriquant.tushare_sdk_proxy import (
    ProxyMarketDataProvider,
    ProxySdkConfig,
    RecordingProxyClient,
    TushareProxyError,
    create_proxy_client,
)


SH = ZoneInfo("Asia/Shanghai")


class FakeTable:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, *, orient):
        assert orient == "records"
        return list(self.rows)


class FakeApi:
    def daily(self, **params):
        return FakeTable(
            [{"ts_code": params["ts_code"], "trade_date": "20240208", "open": 10.0,
              "high": 11.0, "low": 9.0, "close": 10.5, "vol": 100.0, "amount": 1000.0}]
        )


class FakeTushare:
    def __init__(self):
        self.token = None
        self.api = FakeApi()

    def pro_api(self, token):
        self.token = token
        return self.api


def request() -> AShareDataRequest:
    return AShareDataRequest(
        dataset_id="proxy-test",
        codes=("000001.SZ",),
        index_code="000300.SH",
        start_time=datetime(2024, 2, 8, 15, tzinfo=SH),
        end_time=datetime(2024, 2, 8, 15, tzinfo=SH),
        frequency="1D",
        adjustment="raw",
        source="tushare",
        decision_time=datetime(2024, 2, 19, 10, tzinfo=SH),
        fields=("open", "close", "volume"),
        data_version="proxy-test/v1",
        cache_policy="reuse-identical",
    )


def test_sdk_client_uses_only_reviewed_endpoint_and_does_not_expose_token() -> None:
    module = FakeTushare()
    config = ProxySdkConfig(endpoint="https://ts.gyzcloud.top/api", token="sensitive-value")
    client = create_proxy_client(config, tushare_module=module)
    assert module.token == "sensitive-value"
    assert client._DataApi__http_url == "https://ts.gyzcloud.top/api"
    assert "sensitive-value" not in repr(config)


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://ts.gyzcloud.top/api",
        "https://example.invalid/api",
        "https://user:secret@ts.gyzcloud.top/api",
        "https://ts.gyzcloud.top/api?token=secret",
        "https://ts.gyzcloud.top/mcp/token=secret",
    ),
)
def test_endpoint_and_mcp_url_injection_fail_closed(endpoint: str) -> None:
    with pytest.raises(TushareProxyError, match="not allowlisted") as captured:
        ProxySdkConfig(endpoint=endpoint, token="test-token")
    assert "secret" not in str(captured.value)


def test_bounded_market_provider_normalizes_daily_rows_and_records_capture() -> None:
    recorder = RecordingProxyClient(FakeApi())
    payload = ProxyMarketDataProvider(recorder).fetch(request())
    assert payload["000001.SZ"][0]["volume"] == 100.0
    assert payload["000001.SZ"][0]["trade_date"] == "20240208"
    assert recorder.captures[0]["api_name"] == "daily"
    assert "token" not in recorder.captures[0]["params"]


def test_unknown_sdk_api_fails_closed() -> None:
    with pytest.raises(TushareProxyError, match="outside"):
        RecordingProxyClient(SimpleNamespace()).call("factor.py")
