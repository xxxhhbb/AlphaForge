from __future__ import annotations

import asyncio

import pytest

from src.api.alphaforge_campaign_routes import (
    confirm_campaign,
    create_campaign_draft,
    get_alphaforge_status,
    get_campaign_run,
    list_factor_library,
    reset_alphaforge_campaign_state,
    route_paths,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_route_state() -> None:
    reset_alphaforge_campaign_state()


def test_alphaforge_routes_are_product_shell_only() -> None:
    paths = set(route_paths())

    assert {
        "/veriquant/alphaforge/status",
        "/veriquant/campaign/drafts",
        "/veriquant/campaign/recommendations",
        "/veriquant/campaign/confirmations",
        "/veriquant/campaign/runs/{campaign_id}",
        "/veriquant/factor-library",
    }.issubset(paths)
    assert not any(
        forbidden in path.lower()
        for path in paths
        for forbidden in ("/live", "broker", "order", "oauth", "mandate")
    )


def test_status_admits_ready_ui_and_keeps_d_l_disabled_optional() -> None:
    status = _run(get_alphaforge_status())

    assert status["status"] == "ok"
    assert status["product"] == "AlphaForge（千因研究院）"
    assert status["trading_routes_registered"] is False
    admitted = {item["package"]: item["status"] for item in status["admitted_ready_packages"]}
    optional = {item["package"]: item["status"] for item in status["optional_disabled_packages"]}
    assert admitted["alphaforge_chinese_ui"] == "READY_FOR_INTEGRATION"
    assert optional["real_ashare_extended"] != "READY_FOR_INTEGRATION"
    assert optional["real_llm_campaign_smoke"] != "READY_FOR_INTEGRATION"
    assert status["real_30x180_smoke"] == "DISABLED_OPTIONAL"
    assert status["real_llm_smoke"] == "DISABLED_OPTIONAL"


def test_confirmed_campaign_returns_routed_waiting_run_without_fake_execution() -> None:
    draft = _run(
        create_campaign_draft(
            {
                "question": "最近动量因子为什么失效",
                "mode": "DIAGNOSE_FACTOR_PORTFOLIO",
                "depth": "STANDARD_RESEARCH",
                "interactionStyle": "STANDARD",
            }
        )
    )
    assert draft["state"] == "NEEDS_CONFIRMATION"

    confirmed = _run(
        confirm_campaign(
            {
                "draftId": draft["draftId"],
                "answers": {"portfolio_or_existing_factor_ids": "factor_ids:momentum.v1"},
            }
        )
    )
    assert confirmed["state"] == "CONFIRMED"
    assert confirmed["allowsPromotion"] is False
    assert "campaignId" in confirmed

    by_campaign = _run(get_campaign_run(confirmed["campaignId"]))
    by_draft_alias = _run(get_campaign_run(confirmed["draftId"]))
    assert by_campaign == by_draft_alias
    assert by_campaign["state"] == "ROUTED"
    assert by_campaign["trialsUsed"] == 0
    assert by_campaign["checkpointState"] == "PENDING"
    assert by_campaign["selectedDesks"]
    assert {desk["status"] for desk in by_campaign["selectedDesks"]} == {"WAITING"}
    for desk in by_campaign["selectedDesks"]:
        assert {stage["status"] for stage in desk["stages"]} == {"WAITING"}
        assert any("尚未执行" in stage["detail"] for stage in desk["stages"])


def test_factor_library_is_static_read_only_and_hash_addressed() -> None:
    payload = _run(list_factor_library())

    assert payload["status"] == "ok"
    assert payload["total"] == 461
    assert payload["catalogHash"].startswith("sha256:")
    assert payload["qualificationHash"].startswith("sha256:")
    assert len(payload["items"]) == 461
    first = payload["items"][0]
    assert {
        "factorId",
        "displayName",
        "eligibility",
        "qualificationStatus",
        "compatibilityClass",
        "visibleSummary",
        "actions",
    }.issubset(first)
    forbidden = {"official_metrics", "promotion", "target_weights", "orders", "sealed"}
    assert forbidden.isdisjoint(first)


def test_run_lookup_rejects_path_traversal() -> None:
    with pytest.raises(Exception) as exc:
        _run(get_campaign_run("../secret"))

    assert getattr(exc.value, "status_code", None) == 400
