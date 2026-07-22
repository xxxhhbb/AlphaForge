from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from types import SimpleNamespace
from typing import Any, Mapping

from src.veriquant.campaign_automation import (
    AutomationRequest,
    AutomationRuntimeMode,
    AutomationStatus,
    CampaignAutomationService,
)
from src.veriquant.llm_desks.api import OpenAICompatibleConfig, OpenAICompatibleResponse


class StageAwareClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Mapping[str, str], ...]] = []
        self._lock = Lock()

    def complete_json(
        self,
        *,
        messages: tuple[Mapping[str, str], ...],
        model: str,
        timeout_seconds: float,
        max_tokens: int,
        temperature: float,
        top_p: float,
        response_format: str,
        extra: Mapping[str, Any],
    ) -> OpenAICompatibleResponse:
        with self._lock:
            self.calls.append(messages)
        system = messages[0]["content"]
        if "hypothesis" in system:
            payload = {
                "state": "DRAFT",
                "statement": "Visible 50-day price continuation may forecast cross-sectional returns.",
                "economic_mechanism": "Investor underreaction and demand persistence can support short-term continuation.",
                "expected_sign": "POSITIVE",
                "falsification_tests": [
                    "Reject if validation net evidence is weak.",
                    "Reject if turnover-adjusted stability is poor.",
                ],
            }
        elif "factor_draft" in system:
            payload = {
                "state": "DRAFT",
                "name": "llm_visible_momentum_50d",
                "description": "Draft-only rank of visible 50-session close-to-close momentum.",
                "expression": "rank_cross_section(pct_change(close, 50))",
                "variables": ["close"],
                "lookback_bars": 50,
                "neutralization": [],
            }
        else:
            payload = {
                "state": "DRAFT",
                "explanation": "The draft should continue only through deterministic validation and visible evidence review.",
            }
        return OpenAICompatibleResponse(payload=payload, input_tokens=8, output_tokens=12)


def _request(tmp_path) -> AutomationRequest:
    return AutomationRequest(
        question="挖一下最近50天大动量的因子",
        run_id="campaign.real.llm.smoke",
        runtime_mode=AutomationRuntimeMode.REAL_LLM,
        provider_available=True,
        real_llm_from_env=True,
        output_root=str(tmp_path),
        decision_time=datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc),
        seed=7,
    )


def test_campaign_automation_uses_real_llm_provider_boundary(tmp_path, monkeypatch) -> None:
    client = StageAwareClient()
    config = OpenAICompatibleConfig(model="real-llm-test-model")

    monkeypatch.setattr(
        "src.veriquant.campaign_automation.service.load_real_llm_from_env",
        lambda **_: SimpleNamespace(client=client, config=config),
    )

    result = CampaignAutomationService().run(_request(tmp_path))

    assert result.status is AutomationStatus.COMPLETED
    assert len(client.calls) >= 3

    import json

    payload = json.loads(open(result.json_report_path, encoding="utf-8").read())
    assert payload["report"]["runtime_mode"] == "REAL_LLM"
    assert payload["desk_outputs"]
    assert set(payload["report"]["selected_desks"]) == set(payload["desk_outputs"])
    for output in payload["desk_outputs"].values():
        assert output["status"] == "DRAFT"
        assert output["factor_draft"]["expression"] == "rank_cross_section(pct_change(close, 50))"
        assert "promotion" not in json.dumps(output, ensure_ascii=False).lower()
