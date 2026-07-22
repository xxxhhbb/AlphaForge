from __future__ import annotations

from veriquant.approved_proxy_scale_adapter_v2 import AllowlistedProxyScaleClient
from veriquant.research500_harness.industry_probe import probe_industry_history


class _Sdk:
    def index_member_all(self, **kwargs):
        assert kwargs.get("ts_code") == "000001.SZ"
        return [{
            "l1_code": "801000",
            "l1_name": "银行",
            "ts_code": "000001.SZ",
            "in_date": "20200101",
            "out_date": None,
        }]

    def index_member(self, **kwargs):
        return []


def test_probe_reports_dated_history_without_credential_or_endpoint() -> None:
    first = probe_industry_history(AllowlistedProxyScaleClient(_Sdk()), symbol="000001.SZ")
    second = probe_industry_history(AllowlistedProxyScaleClient(_Sdk()), symbol="000001.SZ")
    assert first == second
    assert first["status"] == "AVAILABLE"
    assert first["source"] == "APPROVED_PROXY_DATA_VENDOR"
    assert first["official_tushare"] is False
    rendered = str(first).lower()
    assert "token" not in rendered
    assert "authorization" not in rendered
    assert "http" not in rendered
    assert first["scope"] == {"symbol": "000001.SZ"}


def test_probe_fails_closed_on_undated_rows() -> None:
    class Undated(_Sdk):
        def index_member_all(self, **kwargs):
            return [{"ts_code": "000001.SZ", "l1_name": "银行"}]

    value = probe_industry_history(AllowlistedProxyScaleClient(Undated()), symbol="000001.SZ")
    assert value["status"] == "UNAVAILABLE"
    assert value["probes"][0]["status"] == "EMPTY"
