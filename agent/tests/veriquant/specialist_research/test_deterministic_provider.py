from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veriquant.specialist_research import (
    BoundedResearchAgentLoop,
    DeterministicSpecialistProvider,
    PreflightStatus,
    SPECIALIST_PROFILES,
    SpecialistResearchRequest,
    build_fixture_registry,
    build_preferred_specialist_provider,
)
import veriquant.specialist_research.deterministic_provider as provider_module
from veriquant.research_os_v3.control import ResearchOSV3CampaignControl


def _request(specialist_id: str) -> SpecialistResearchRequest:
    return SpecialistResearchRequest(
        run_id=f"offline-{specialist_id}",
        campaign_hash="sha256:" + "a" * 64,
        specialist_id=specialist_id,
        question="寻找可证伪且成本后可评价的 A 股 Alpha",
        universe="cn_ashare_pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 20, tzinfo=timezone.utc),
        available_fields=(
            "close", "volume", "market_cap", "roe", "book_value",
            "earnings_surprise", "earnings_forecast", "activity_proxy",
            "shibor_overnight", "benchmark_return", "available_at",
        ),
        requested_lookback_sessions=50,
        seed=7,
    )


@pytest.mark.parametrize("specialist_id", tuple(SPECIALIST_PROFILES))
def test_six_deterministic_specialists_produce_bounded_drafts(specialist_id: str):
    profile = SPECIALIST_PROFILES[specialist_id]
    request = _request(specialist_id)
    outcome = BoundedResearchAgentLoop().run(
        request=request,
        profile=profile,
        provider=DeterministicSpecialistProvider(specialist_id),
        tools=build_fixture_registry(profile, available_fields=request.available_fields),
    )

    assert len(outcome.candidates) == 4
    assert len({candidate.family_id for candidate in outcome.candidates}) == 2
    assert all(candidate.forecast_horizon_sessions == 5 for candidate in outcome.candidates)
    assert any(item.status is PreflightStatus.ACCEPTED for item in outcome.preflight_results)
    public_material = outcome.canonical_json().lower()
    assert "official_metrics" not in public_material
    assert "promotion_verdict" not in public_material
    assert "risk_approval" not in public_material
    assert "factor.py" not in public_material


def test_no_llm_configuration_uses_deterministic_provider(tmp_path, monkeypatch):
    for name in (
        "ENABLE_LLM_RESEARCH", "ALPHAFORGE_LLM_ENDPOINT_URL", "ALPHAFORGE_LLM_API_KEY",
        "ALPHAFORGE_LLM_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY", "LANGCHAIN_MODEL_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    provider = build_preferred_specialist_provider("market_structure", tmp_path / "missing.env")
    assert isinstance(provider, DeterministicSpecialistProvider)
    assert provider.provider_id == "deterministic_specialist"


def test_campaign_control_default_factory_is_offline_runnable(tmp_path, monkeypatch):
    for name in (
        "ENABLE_LLM_RESEARCH", "ALPHAFORGE_LLM_ENDPOINT_URL", "ALPHAFORGE_LLM_API_KEY",
        "ALPHAFORGE_LLM_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY", "LANGCHAIN_MODEL_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    control = ResearchOSV3CampaignControl(root=tmp_path / "runtime", dotenv_path=tmp_path / "missing.env")
    try:
        selected = control.provider_factory("fundamental")
        assert isinstance(selected, DeterministicSpecialistProvider)
    finally:
        control.close()


def test_complete_real_configuration_has_priority_and_is_not_wrapped(tmp_path, monkeypatch):
    env_path = tmp_path / "configured.env"
    env_path.write_text(
        "ENABLE_LLM_RESEARCH=1\n"
        "ALPHAFORGE_LLM_ENDPOINT_URL=https://llm.example.test/v1\n"
        "ALPHAFORGE_LLM_API_KEY=not-a-real-secret\n"
        "ALPHAFORGE_LLM_MODEL=test-model\n",
        encoding="utf-8",
    )

    class FailingRealProvider:
        provider_id = "configured_openai_compatible"
        model_id = "test-model"

        def next_turn(self, context):
            del context
            raise RuntimeError("real provider failed")

    real = FailingRealProvider()
    monkeypatch.setattr(provider_module, "build_configured_specialist_provider", lambda path: real)
    selected = build_preferred_specialist_provider("market_structure", env_path)
    assert selected is real
    with pytest.raises(RuntimeError, match="real provider failed"):
        selected.next_turn(None)
