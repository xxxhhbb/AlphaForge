from __future__ import annotations

import argparse
import json

import pytest

import src.veriquant.qlib_setup as setup


def test_diagnose_reports_only_secret_booleans_and_never_installs(monkeypatch) -> None:
    monkeypatch.delenv("QLIB_PYTHON", raising=False)
    monkeypatch.delenv("ENABLE_ASHARE_NETWORK", raising=False)
    monkeypatch.delenv("ENABLE_QLIB_ADAPTER", raising=False)
    monkeypatch.setattr(setup, "_token_configured", lambda: True)
    result, code = setup.diagnose()
    rendered = json.dumps(result, sort_keys=True)
    assert code == 0
    assert result["tushare_credential_configured"] is True
    assert result["automatic_install_attempted"] is False
    assert result["automatic_download_attempted"] is False
    assert "token-value" not in rendered


def test_print_install_plan_does_not_execute_subprocess(monkeypatch) -> None:
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subprocess executed")),
    )
    result, code = setup.print_install_plan()
    assert code == 0
    assert result["executed"] is False
    assert any("pip install pyqlib" in command for command in result["commands"])


def test_stage_from_vibe_requires_explicit_network_flag_before_provider_use(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_ASHARE_NETWORK", raising=False)
    monkeypatch.setattr(
        setup,
        "VibeAshareSnapshotAdapter",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider constructed")),
    )
    args = argparse.Namespace(codes=["000001.SZ"], source="tushare")
    result, code = setup.stage_from_vibe(args)
    assert code != 0
    assert result["status"] == "UNAVAILABLE"
    assert result["estimated_remote_requests"] == 2
    assert "no request was made" in result["message"]


def test_build_provider_rejects_nonempty_target_without_invoking_builder(tmp_path, monkeypatch) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "manifest.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "provider"
    target.mkdir()
    (target / "owned.txt").write_text("preserve", encoding="utf-8")
    monkeypatch.setenv("ENABLE_QLIB_ADAPTER", "1")
    monkeypatch.setenv("QLIB_PYTHON", __import__("sys").executable)
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("builder invoked")),
    )
    result, code = setup.build_provider(
        argparse.Namespace(staging=str(staging), target=str(target), region="cn", timeout=1.0)
    )
    assert code != 0
    assert result["status"] == "BLOCKED"
    assert (target / "owned.txt").read_text(encoding="utf-8") == "preserve"


def test_cli_print_plan_is_machine_readable(capsys) -> None:
    assert setup.main(["print-install-plan"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "print-install-plan"
    assert payload["executed"] is False
