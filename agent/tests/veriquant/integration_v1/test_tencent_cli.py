from __future__ import annotations

import json

import pytest

from veriquant.integration import tencent_smoke
from veriquant.tencent_rehearsal.api import TencentRehearsalError


def test_tencent_cli_default_is_offline_auxiliary_and_machine_readable(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(tencent_smoke, "_repo_root", lambda: tmp_path)
    calendar_hash = "sha256:" + "a" * 64
    exit_code = tencent_smoke.main(
        [
            "--calendar-hash",
            calendar_hash,
            "--output",
            ".autobuild/veriquant/integration/v1_tencent_stage/offline",
        ]
    )

    result_path = (
        tmp_path
        / ".autobuild"
        / "veriquant"
        / "integration"
        / "v1_tencent_stage"
        / "offline"
        / "tencent_smoke_result.json"
    )
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    stdout_payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload == stdout_payload
    assert payload["status"] == "OFFLINE_DISABLED"
    assert payload["authority_class"] == "AUXILIARY_RESEARCH_EVIDENCE"
    assert payload["adjustment_verdict"] == "QFQ_ONLY"
    assert payload["promotion_eligible"] is False
    assert payload["official_v1_tushare_smoke"] is False
    assert payload["changes_official_verdict"] is False


def test_tencent_cli_rejects_unbounded_symbol_set_before_transport(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tencent_smoke, "_repo_root", lambda: tmp_path)
    codes = [f"{index:06d}.SZ" for index in range(11)]
    with pytest.raises(TencentRehearsalError):
        tencent_smoke.main(
            [
                "--calendar-hash",
                "sha256:" + "b" * 64,
                "--codes",
                *codes,
            ]
        )


def test_tencent_cli_output_path_cannot_escape_integration_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tencent_smoke, "_repo_root", lambda: tmp_path)
    with pytest.raises(ValueError):
        tencent_smoke.main(
            [
                "--calendar-hash",
                "sha256:" + "c" * 64,
                "--output",
                "../escape",
            ]
        )

