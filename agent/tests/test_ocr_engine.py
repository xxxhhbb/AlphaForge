"""Tests for the pluggable OCR engine factory.

Cloud OCR uploads document pages to a third party, so the qwen-vl engine
must only ever be reachable through an explicit VIBE_TRADING_OCR_ENGINE
choice — never via "auto" fallback.
"""

from __future__ import annotations

import pytest

from src.config.accessor import reset_env_config
from src.tools.ocr import engine as ocr_engine


@pytest.fixture(autouse=True)
def _reset_config():
    """Reset the cached EnvConfig around each test."""
    reset_env_config()
    yield
    reset_env_config()


def test_auto_never_selects_cloud_engine(monkeypatch):
    """auto must not fall through to Qwen-VL even with DASHSCOPE_API_KEY set."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("VIBE_TRADING_OCR_ENGINE", "auto")
    reset_env_config()

    monkeypatch.setattr(
        ocr_engine, "_try_qwen_vl",
        lambda: pytest.fail("auto mode must never try the cloud engine"),
    )
    engine = ocr_engine.get_ocr_engine()
    assert engine is None or engine.name == "rapid"


def test_explicit_qwen_vl_reaches_cloud_factory(monkeypatch):
    """qwen-vl is reachable, but only as an explicit choice."""
    monkeypatch.setenv("VIBE_TRADING_OCR_ENGINE", "qwen-vl")
    reset_env_config()

    sentinel = object()
    monkeypatch.setattr(ocr_engine, "_try_qwen_vl", lambda: sentinel)
    assert ocr_engine.get_ocr_engine() is sentinel


def test_none_disables_ocr(monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_OCR_ENGINE", "none")
    reset_env_config()
    assert ocr_engine.get_ocr_engine() is None


def test_unknown_choice_falls_back_to_local_only(monkeypatch):
    """An unknown engine name degrades to auto, which stays local."""
    monkeypatch.setenv("VIBE_TRADING_OCR_ENGINE", "bogus-engine")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-not-real")
    reset_env_config()

    monkeypatch.setattr(
        ocr_engine, "_try_qwen_vl",
        lambda: pytest.fail("fallback from unknown choice must stay local"),
    )
    engine = ocr_engine.get_ocr_engine()
    assert engine is None or engine.name == "rapid"
