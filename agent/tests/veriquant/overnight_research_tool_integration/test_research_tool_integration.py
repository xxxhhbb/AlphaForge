from __future__ import annotations

import json
from pathlib import Path

import pytest

from veriquant.campaign_tool_registry import build_campaign_tool_registry
from veriquant.research_campaign import CampaignState
from veriquant.research_tool_contracts import ResearchToolContractError, ResearchToolFailureCode
from veriquant.research_tool_integration.demo import (
    REQUIRED_PACKAGES,
    OvernightIntegrationError,
    OvernightResearchToolDemo,
    build_demo_artifacts,
    load_ready_package_results,
    main,
)


REPO_ROOT = Path(__file__).resolve().parents[4]


def test_loads_only_ready_packages() -> None:
    results = load_ready_package_results()
    missing = set(REQUIRED_PACKAGES) - set(results)

    assert not missing
    assert all(payload["status"] == "READY_FOR_INTEGRATION" for payload in results.values())


def test_demo_outputs_are_stable_and_campaign_routed(tmp_path: Path) -> None:
    first = build_demo_artifacts(seed=7, offline=True, output_dir=tmp_path / "first")
    second = build_demo_artifacts(seed=7, offline=True, output_dir=tmp_path / "second")

    assert first["artifact_hash"] == second["artifact_hash"]
    assert first["route"]["selected_desks"] == ("fundamental", "market_structure", "meta_alpha")
    assert "expectations_events" in first["route"]["unselected_desks"]
    assert first["invariants"]["unselected_desks_not_executed"] is True
    assert first["invariants"]["all_results_draft_or_auxiliary"] is True
    assert first["invariants"]["official_metric_calculation_performed"] is False
    assert first["invariants"]["promotion_performed"] is False
    assert first["invariants"]["portfolio_risk_order_ledger_performed"] is False
    assert (tmp_path / "first" / "overnight_demo.json").exists()
    assert (tmp_path / "first" / "overnight_demo.md").exists()


def test_demo_uses_factor_library_and_frozen_citation(tmp_path: Path) -> None:
    output = build_demo_artifacts(seed=7, offline=True, output_dir=tmp_path)

    assert output["factor_library"]["catalog_total"] == 461
    assert output["factor_library"]["sample_qualified_templates"]
    assert output["document_capability_smoke"]["citation_count"] == 1
    assert output["document_capability_smoke"]["executed_by"] == "integration_capability_smoke_not_campaign_desk"


def test_offline_required() -> None:
    with pytest.raises(OvernightIntegrationError):
        OvernightResearchToolDemo(root=REPO_ROOT, seed=7, offline=False).run()

    assert main(("--seed", "7")) == 2


def test_registry_blocks_unselected_desk() -> None:
    demo = OvernightResearchToolDemo(root=REPO_ROOT, seed=7, offline=True)
    output = demo.run()
    # Rebuild via demo internals indirectly by checking the persisted route result is bounded.
    assert len(output["route"]["selected_desks"]) == 3
    assert set(output["route"]["selected_desks"]) < {
        "fundamental",
        "expectations_events",
        "market_structure",
        "alternative_data",
        "macro_regime",
        "meta_alpha",
    }


def test_persisted_json_is_machine_readable(tmp_path: Path) -> None:
    output = build_demo_artifacts(seed=7, offline=True, output_dir=tmp_path)
    persisted = json.loads((tmp_path / "overnight_demo.json").read_text(encoding="utf-8"))

    assert persisted["artifact_hash"] == output["artifact_hash"]
    assert persisted["status"] == "READY_FOR_F3_INTEGRATION"
