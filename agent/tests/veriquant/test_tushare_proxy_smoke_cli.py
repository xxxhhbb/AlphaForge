from __future__ import annotations

import json

import pytest

from src.veriquant.tushare_sdk_proxy import smoke
from src.veriquant.tushare_sdk_proxy.client import TushareProxyError


def test_cli_is_offline_by_default_and_writes_machine_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(smoke, "_repo_root", lambda: tmp_path)
    code = smoke.main(
        [
            "--calendar-manifest",
            str(tmp_path / "missing-calendar.json"),
            "--output",
            ".autobuild/veriquant/integration/v1_tushare_proxy_stage/offline",
        ]
    )
    assert code == 0
    result = json.loads(
        (
            tmp_path
            / ".autobuild/veriquant/integration/v1_tushare_proxy_stage/offline/tushare_proxy_smoke_result.json"
        ).read_text(encoding="utf-8")
    )
    assert result["status"] == "OFFLINE_DISABLED"
    assert result["official_v1_tushare_smoke"] is False


def test_cli_rejects_output_path_escape(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(smoke, "_repo_root", lambda: tmp_path)
    with pytest.raises(TushareProxyError, match="escapes"):
        smoke.main(
            [
                "--calendar-manifest",
                str(tmp_path / "calendar.json"),
                "--output",
                str(tmp_path / "outside"),
            ]
        )


def test_cli_rejects_unbounded_symbol_request_before_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(smoke, "_repo_root", lambda: tmp_path)
    with pytest.raises(TushareProxyError, match="bounded"):
        smoke.main(
            [
                "--calendar-manifest",
                str(tmp_path / "calendar.json"),
                "--output",
                ".autobuild/veriquant/integration/v1_tushare_proxy_stage/bounds",
                "--codes",
                "000001.SZ",
                "000002.SZ",
                "600000.SH",
                "600519.SH",
                "--enable-network",
            ]
        )
