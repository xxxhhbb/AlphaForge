"""Offline Campaign-routed research-tool demo for AlphaForge overnight build.

The demo proves that the O0-O2 packages can be connected without broadening
authority:

* a confirmed Campaign is routed to a bounded set of desks;
* every selected desk receives only a Campaign-scoped tool pack;
* frozen market/document/factor-library/visible-evidence helpers emit DRAFT
  support only;
* no official Evidence, Promotion, Portfolio, Risk, Order, broker, or Ledger
  action is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from veriquant.campaign_tool_registry import build_campaign_tool_registry
from veriquant.contracts import Component
from veriquant.document_research_adapters import (
    CitationQueryTool,
    DocumentResearchAdapter,
    FrozenFixtureSourceProvider,
)
from veriquant.factor_library import build_factor_library_catalog
from veriquant.factor_qualification import FactorQualificationService
from veriquant.research_campaign import (
    CampaignBudget,
    CampaignState,
    DataAvailability,
    DeterministicCampaignRouter,
    InteractionStyle,
    MethodModel,
    RegisteredResearchContext,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    StopCondition,
    UserCheckpoint,
)
from veriquant.research_explorer_tools import (
    InMemoryVisibleEvidenceReader,
    VisibleEvidenceToolbox,
)
from veriquant.research_tool_contracts import (
    ResearchToolCapability,
    ResearchToolRequest,
    ResearchToolResult,
    ResearchToolVisibility,
    content_hash,
)
from veriquant.vibe_market_adapters import (
    FrozenMarketRecord,
    FrozenMarketResponse,
    FundamentalsResearchAdapter,
    InMemoryMarketResearchProvider,
)


INTEGRATION_SCHEMA_VERSION = "alphaforge.overnight-research-tool-integration/v1"
READY = "READY_FOR_INTEGRATION"
DEFAULT_DECISION_TIME = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
REQUIRED_PACKAGES = (
    "research_tool_contracts",
    "factor_qualification",
    "vibe_market_adapters",
    "document_research_adapters",
    "quanta_research_adapters",
    "research_explorer_tools",
    "campaign_tool_registry",
)


class OvernightIntegrationError(RuntimeError):
    """Typed failure for invariant-breaking demo setup."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        rendered = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
        return rendered[:-6] + "Z"
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def stable_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
    tmp.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_ready_package_results(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load only READY overnight package result files."""

    base = (root or _repo_root()) / ".autobuild" / "veriquant" / "overnight"
    results: dict[str, dict[str, Any]] = {}
    for result_file in sorted(base.glob("*/result.json")):
        package_id = result_file.parent.name
        if package_id == "integration":
            continue
        payload = json.loads(result_file.read_text(encoding="utf-8"))
        if payload.get("status") == READY:
            results[package_id] = payload
    return results


def _assert_required_ready(results: Mapping[str, Mapping[str, Any]]) -> None:
    missing = tuple(package_id for package_id in REQUIRED_PACKAGES if package_id not in results)
    if missing:
        raise OvernightIntegrationError("required overnight packages are not READY: " + ",".join(missing))


def _campaign(seed: int) -> ResearchCampaign:
    subject = ResearchSubject(
        subject_id="subject.cn_ashare_alphaforge_overnight",
        description="AlphaForge 离线冻结 A 股样本研究对象。",
        universe=("CN_ASHARE_APPROVED_PROXY_SAMPLE",),
        horizon="20d",
        decision_time=DEFAULT_DECISION_TIME,
        allowed_data=(
            "events",
            "fundamentals",
            "market_structure",
            "price_volume",
            "registered_factor_summary",
        ),
        existing_factor_ids=("library.fund_roe", "library.qlib158_roc20"),
        portfolio_id="shadow_equity_mn_demo",
    )
    draft = ResearchCampaignDraft(
        campaign_id=f"campaign.alpha_forge_overnight_seed_{seed}",
        question="帮我找一个 20 日 A 股新 Alpha，并给出可证伪实验草案。",
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        research_depth=ResearchDepth.QUICK_EXPLORATION,
        interaction_style=InteractionStyle.STANDARD,
        subject=subject,
        method_models=(MethodModel.HYPOTHESIS_DRIVEN,),
        candidate_agents=(
            "fundamental",
            "expectations_events",
            "market_structure",
            "alternative_data",
            "macro_regime",
            "meta_alpha",
        ),
        # CampaignBudget caps selected Desk count through max_llm_calls.  The
        # demo still does not call an LLM provider; this merely records the
        # finite upper bound that would apply if selected desks had an enabled
        # real-LLM provider.
        budget=CampaignBudget(max_factor_families=3, max_trials=9, max_llm_calls=3, max_runtime_seconds=300),
        user_checkpoints=(
            UserCheckpoint(checkpoint_id="checkpoint.after_research_brief", stage="after_research_brief"),
            UserCheckpoint(checkpoint_id="checkpoint.after_candidate_shortlist", stage="after_candidate_shortlist"),
        ),
        stop_conditions=(
            StopCondition.BUDGET_EXHAUSTED,
            StopCondition.NO_INCREMENTAL_CANDIDATE,
            StopCondition.USER_STOPPED,
            StopCondition.VALIDATION_COMPLETED,
        ),
        data_manifest_hash=HASH_A,
        policy_hash=HASH_B,
        code_hash=HASH_C,
        created_at=DEFAULT_DECISION_TIME - timedelta(minutes=30),
        proposed_by=Component.LLM_RESEARCHER,
        state=CampaignState.NEEDS_CONFIRMATION,
    )
    return ResearchCampaign.confirm(draft, confirmed_at=DEFAULT_DECISION_TIME)


def _route(campaign: ResearchCampaign):
    result = DeterministicCampaignRouter().route(
        campaign,
        data_availability=DataAvailability(
            available_data=campaign.subject.allowed_data,
            universe_ids=campaign.subject.universe,
            data_manifest_hash=campaign.data_manifest_hash,
            calendar_ids=("cn_sse_szse_frozen_calendar",),
        ),
        registered_context=RegisteredResearchContext(
            factor_ids=campaign.subject.existing_factor_ids,
            portfolio_ids=(campaign.subject.portfolio_id,) if campaign.subject.portfolio_id else (),
            factor_domains={
                "library.fund_roe": "fundamental",
                "library.qlib158_roc20": "market_structure",
            },
        ),
    )
    if result.route is None:
        raise OvernightIntegrationError("campaign router returned BLOCKED: " + ",".join(result.blocked_reasons))
    return result.route


def _request(campaign: ResearchCampaign, desk_id: str, tool_name: str, suffix: str) -> ResearchToolRequest:
    return ResearchToolRequest(
        campaign_id=campaign.campaign_id,
        campaign_hash=campaign.artifact_hash,
        desk_id=desk_id,
        decision_time=campaign.subject.decision_time,
        allowed_data=campaign.subject.allowed_data,
        tool_name=tool_name,
        request_id=f"request.{desk_id}.{suffix}",
        offline=True,
    )


def _result_summary(result: ResearchToolResult) -> dict[str, Any]:
    return {
        "result_id": result.result_id,
        "visibility": result.visibility.value,
        "capability": result.capability.value,
        "failure_code": result.failure_code.value if result.failure_code else None,
        "artifact_hash": result.artifact_hash,
    }


def _registry_invoke(registry, campaign: ResearchCampaign, desk_id: str, tool_name: str, suffix: str, payload: Mapping[str, Any]) -> ResearchToolResult:
    request = _request(campaign, desk_id, tool_name, suffix)
    return registry.invoke(request, payload=payload)


def _fundamental_market_result(registry, campaign: ResearchCampaign) -> ResearchToolResult:
    request = _request(campaign, "fundamental", "fundamentals_summary", "fundamentals")
    binding = registry.binding_for("fundamental", "fundamentals_summary")
    registry.pack_for_desk("fundamental").policy.authorize(request, binding.capability)
    t0 = campaign.subject.decision_time
    records = (
        FrozenMarketRecord(
            record_id="fundamental.600000",
            provider="approved_proxy_data_vendor_frozen_fixture",
            source_class="fundamental_record",
            event_time=t0 - timedelta(days=30),
            published_at=t0 - timedelta(days=20),
            available_at=t0 - timedelta(days=18),
            retrieved_at=t0 - timedelta(days=10),
            payload={
                "symbol": "600000.SH",
                "roe": 0.118,
                "revenue_growth": 0.064,
                "inventory_turnover_delta": 0.031,
            },
            source_payload={"symbol": "600000.SH", "roe": 0.118, "source_batch": "frozen_batch_001"},
        ),
        FrozenMarketRecord(
            record_id="fundamental.000001",
            provider="approved_proxy_data_vendor_frozen_fixture",
            source_class="fundamental_record",
            event_time=t0 - timedelta(days=30),
            published_at=t0 - timedelta(days=19),
            available_at=t0 - timedelta(days=17),
            retrieved_at=t0 - timedelta(days=10),
            payload={
                "symbol": "000001.SZ",
                "roe": 0.104,
                "revenue_growth": 0.052,
                "inventory_turnover_delta": 0.018,
            },
            source_payload={"symbol": "000001.SZ", "roe": 0.104, "source_batch": "frozen_batch_001"},
        ),
    )
    response = FrozenMarketResponse(
        provider="approved_proxy_data_vendor_frozen_fixture",
        source_class="fundamental_record",
        records=records,
        manifest={
            "vendor_class": "APPROVED_PROXY_DATA_VENDOR",
            "dataset_id": "cn_ashare_frozen_fixture",
            "row_count": len(records),
            "calendar_id": "cn_sse_szse_frozen_calendar",
        },
    )
    return FundamentalsResearchAdapter(InMemoryMarketResearchProvider("offline_fundamental_fixture", response)).run(request)


def _frozen_document_capability_smoke(campaign: ResearchCampaign) -> dict[str, Any]:
    source = DocumentResearchAdapter().freeze_document(
        source_id="doc001",
        title="AlphaForge frozen operating update",
        body="Inventory turnover improved after supplier consolidation. Margins remained stable.",
        event_time=campaign.subject.decision_time - timedelta(days=7),
        published_at=campaign.subject.decision_time - timedelta(days=6),
        available_at=campaign.subject.decision_time - timedelta(days=5),
        retrieved_at=campaign.subject.decision_time - timedelta(days=4),
    )
    request = ResearchToolRequest(
        campaign_id=campaign.campaign_id,
        campaign_hash=campaign.artifact_hash,
        desk_id="integration_capability_smoke",
        decision_time=campaign.subject.decision_time,
        allowed_data=("events",),
        tool_name="citation_query",
        request_id="request.document.citation",
        offline=True,
    )
    result = CitationQueryTool(provider=FrozenFixtureSourceProvider((source,))).query(
        request,
        terms=("inventory",),
        source_ids=(source.source_id,),
    )
    first = result.payload["citations"][0]
    return {
        "capability": "READ_FROZEN_DOCUMENT",
        "visibility": result.visibility.value,
        "citation_count": result.payload["citation_count"],
        "bundle_hash": first["bundle_hash"],
        "source_hash": first["source_hash"],
        "available_at": first["available_at"],
        "executed_by": "integration_capability_smoke_not_campaign_desk",
        "artifact_hash": result.artifact_hash,
    }


def _visible_reader() -> InMemoryVisibleEvidenceReader:
    return InMemoryVisibleEvidenceReader(
        candidates={
            "candidate.fundamental_inventory_quality": {
                "candidate_id": "candidate.fundamental_inventory_quality",
                "split": "validation",
                "source_component": "EVIDENCE_SERVICE",
                "evidence_class": "VISIBLE_TRAIN_VALIDATION_SUMMARY",
                "metrics": {
                    "net_icir": 0.42,
                    "stability": 0.61,
                    "cost_bps": 8.5,
                    "incrementality": 0.17,
                    "gross_return": 0.022,
                    "net_return": 0.014,
                },
                "reason_codes": ("VISIBLE_ONLY", "DRAFT_RESEARCH_SUPPORT"),
            },
            "candidate.rankic_only_rejected": {
                "candidate_id": "candidate.rankic_only_rejected",
                "split": "validation",
                "source_component": "EVIDENCE_SERVICE",
                "evidence_class": "VISIBLE_TRAIN_VALIDATION_SUMMARY",
                "metrics": {"rank_ic": 0.04},
                "reason_codes": ("RANKIC_ONLY_INSUFFICIENT",),
            },
        },
        budgets={
            "family.inventory_quality": {
                "family_id": "family.inventory_quality",
                "budget_limit": 9,
                "budget_consumed": 2,
                "budget_remaining": 7,
            }
        },
    )


def _factor_library_payload() -> dict[str, Any]:
    catalog = build_factor_library_catalog()
    report = FactorQualificationService().qualify()
    templates = tuple(template.to_dict() for template in report.qualified_templates[:3])
    return {
        "catalog_summary": catalog.summary(),
        "qualification_counts": report.counts_by_status,
        "qualification_hash": report.report_hash,
        "sample_qualified_templates": templates,
    }


def _plan_payload(campaign: ResearchCampaign) -> dict[str, Any]:
    toolbox = VisibleEvidenceToolbox(_visible_reader())
    preregistration = toolbox.preregister_hypothesis(
        {
            "hypothesis_id": "hypothesis.inventory_quality_20d",
            "statement": "库存周转改善可能代理经营质量，并在 20 日窗口形成正向 Alpha 草案。",
            "falsification_tests": ("cost_raise", "sector_neutral_control", "sign_flip_placebo"),
        }
    )
    walk_forward = toolbox.design_walk_forward(
        plan_id="plan.inventory_quality_20d.quick",
        hypothesis_id="hypothesis.inventory_quality_20d",
        horizon=campaign.subject.horizon,
        universe=campaign.subject.universe[0],
        decision_time=campaign.subject.decision_time,
        research_depth=campaign.research_depth.value,
    )
    return {
        "preregistration": preregistration.to_dict(),
        "walk_forward_plan": {
            "plan_id": walk_forward.plan_id,
            "hypothesis_id": walk_forward.hypothesis_id,
            "horizon": walk_forward.horizon,
            "universe": walk_forward.universe,
            "planned_tests": walk_forward.planned_tests,
            "official_evidence_requested": walk_forward.official_evidence_requested,
            "artifact_hash": walk_forward.artifact_hash,
        },
    }


@dataclass(frozen=True)
class OvernightResearchToolDemo:
    root: Path
    seed: int = 7
    offline: bool = True

    def run(self, output_dir: Path | None = None) -> dict[str, Any]:
        if not self.offline:
            raise OvernightIntegrationError("overnight demo is offline-only")
        ready_results = load_ready_package_results(self.root)
        _assert_required_ready(ready_results)
        campaign = _campaign(self.seed)
        route = _route(campaign)
        package_statuses = {
            "research_tool_contracts": True,
            "vibe_market_adapters": "vibe_market_adapters" in ready_results,
            "quanta_research_adapters": "quanta_research_adapters" in ready_results,
            "research_explorer_tools": "research_explorer_tools" in ready_results,
            "factor_library": True,
            "document_research_adapters": "document_research_adapters" in ready_results,
            "macro_provider": False,
        }
        registry = build_campaign_tool_registry(
            campaign,
            route,
            package_statuses=package_statuses,
            provider_availability={"documents": True, "macro": False},
        )
        selected_desks = route.selected_desks
        unselected_desks = tuple(sorted(set(("fundamental", "expectations_events", "market_structure", "alternative_data", "macro_regime", "meta_alpha")) - set(selected_desks)))
        executed_desks: list[str] = []

        dataset = _registry_invoke(
            registry,
            campaign,
            "fundamental",
            "dataset_pit_summary",
            "dataset",
            {
                "dataset_id": "cn_ashare_approved_proxy_fixture",
                "vendor_class": "APPROVED_PROXY_DATA_VENDOR",
                "universe": campaign.subject.universe[0],
                "calendar_id": "cn_sse_szse_frozen_calendar",
                "as_of_rows": 240,
                "future_return_label_excluded_from_features": True,
                "snapshot_digest": HASH_A,
            },
        )
        executed_desks.append("fundamental")
        fundamentals = _fundamental_market_result(registry, campaign)
        executed_desks.append("fundamental")

        factor_payload = _factor_library_payload()
        factor_query = _registry_invoke(
            registry,
            campaign,
            "meta_alpha",
            "query_factor_library",
            "factor_library",
            {
                "catalog_total": factor_payload["catalog_summary"]["total_records"],
                "catalog_hash": factor_payload["catalog_summary"]["catalog_hash"],
                "qualification_counts": factor_payload["qualification_counts"],
                "qualification_hash": factor_payload["qualification_hash"],
                "sample_factor_ids": tuple(item["factor_id"] for item in factor_payload["sample_qualified_templates"]),
            },
        )
        executed_desks.append("meta_alpha")

        visible = VisibleEvidenceToolbox(_visible_reader()).query_visible_evidence("candidate.fundamental_inventory_quality")
        visible_query = _registry_invoke(
            registry,
            campaign,
            "meta_alpha",
            "query_visible_evidence",
            "visible_evidence",
            {
                "candidate_id": visible.payload["candidate_id"],
                "split": visible.payload["split"],
                "metric_summary": visible.payload["metrics"],
                "reason_codes": visible.payload["reason_codes"],
                "visible_only": True,
            },
        )
        executed_desks.append("meta_alpha")

        cost_capacity = _registry_invoke(
            registry,
            campaign,
            "market_structure",
            "cost_capacity_summary",
            "cost_capacity",
            {
                "candidate_id": "candidate.fundamental_inventory_quality",
                "spread_cost_bps": 5.2,
                "linear_impact_bps_per_adv_pct": 1.7,
                "capacity_note": "小预算 QUICK 探索，仅用于草案筛选。",
            },
        )
        executed_desks.append("market_structure")

        document_smoke = _frozen_document_capability_smoke(campaign)
        experiment_plan = _plan_payload(campaign)
        draft_recommendations = (
            {
                "desk_id": "fundamental",
                "state": "DRAFT",
                "candidate_id": "draft.inventory_quality_20d",
                "factor_dag_hint": "field inventory_turnover_delta -> zscore_cross_section -> rank_cross_section",
                "why_now": "冻结样本里库存周转改善与 ROE/收入增速有一致方向，适合作为经营质量代理。",
            },
            {
                "desk_id": "meta_alpha",
                "state": "DRAFT",
                "candidate_id": "draft.library_baseline_contrast",
                "factor_dag_hint": "以 fund_roe 与 qlib158_roc20 库因子作为低复杂度对照。",
                "why_now": "目录资格化显示存在可直接转 FactorDAG 的基线，可作为新草案的相关性/增量性参照。",
            },
        )
        tool_results = {
            "dataset_pit_summary": _result_summary(dataset),
            "fundamentals_summary": _result_summary(fundamentals),
            "query_factor_library": _result_summary(factor_query),
            "query_visible_evidence": _result_summary(visible_query),
            "cost_capacity_summary": _result_summary(cost_capacity),
        }
        bindings_by_desk = {
            desk_id: tuple(
                {
                    "tool_name": binding.tool_name,
                    "capability": binding.capability.value,
                    "visibility": binding.visibility.value,
                    "source_package": binding.source_package,
                    "availability": binding.availability.value,
                    "availability_reason": binding.availability_reason,
                }
                for binding in registry.pack_for_desk(desk_id).bindings
            )
            for desk_id in registry.desk_ids
        }
        invariant_payload = {
            "unselected_desks_not_executed": not (set(executed_desks) & set(unselected_desks)),
            "selected_desk_count": len(selected_desks),
            "all_results_draft_or_auxiliary": all(
                item["visibility"] in {ResearchToolVisibility.DRAFT_SUPPORT.value, ResearchToolVisibility.AUXILIARY_RESEARCH_EVIDENCE.value}
                for item in tool_results.values()
            ),
            "no_network": self.offline,
            "provider_credentials_absent": True,
            "sealed_absent_from_payloads": "sealed" not in stable_json({"tool_results": tool_results, "document_smoke": document_smoke}).lower(),
            "official_metric_calculation_performed": False,
            "promotion_performed": False,
            "portfolio_risk_order_ledger_performed": False,
            "document_smoke_not_routed_to_unselected_desk": document_smoke["executed_by"] == "integration_capability_smoke_not_campaign_desk",
        }
        if not all(value for key, value in invariant_payload.items() if key not in {"selected_desk_count", "official_metric_calculation_performed", "promotion_performed", "portfolio_risk_order_ledger_performed"}):
            raise OvernightIntegrationError("one or more required invariants failed")
        if any(invariant_payload[key] for key in ("official_metric_calculation_performed", "promotion_performed", "portfolio_risk_order_ledger_performed")):
            raise OvernightIntegrationError("forbidden authority action occurred")

        body_without_hash = {
            "schema_version": INTEGRATION_SCHEMA_VERSION,
            "status": "READY_FOR_F3_INTEGRATION",
            "seed": self.seed,
            "offline": self.offline,
            "ready_packages": {key: ready_results[key]["status"] for key in sorted(ready_results)},
            "campaign": {
                "campaign_id": campaign.campaign_id,
                "campaign_hash": campaign.artifact_hash,
                "mode": campaign.mode.value,
                "research_depth": campaign.research_depth.value,
                "interaction_style": campaign.interaction_style.value,
                "question": campaign.question,
                "universe": campaign.subject.universe,
                "horizon": campaign.subject.horizon,
                "decision_time": campaign.subject.decision_time,
            },
            "route": {
                "route_id": route.route_id,
                "route_hash": route.artifact_hash,
                "selected_desks": selected_desks,
                "unselected_desks": unselected_desks,
                "method_models": tuple(method.value for method in route.method_models),
            },
            "registry": {
                "registry_hash": registry.artifact_hash,
                "authority_manifest_hash": registry.authority_manifest.artifact_hash,
                "bindings_by_desk": bindings_by_desk,
            },
            "tool_results": tool_results,
            "factor_library": {
                "catalog_hash": factor_payload["catalog_summary"]["catalog_hash"],
                "catalog_total": factor_payload["catalog_summary"]["total_records"],
                "qualification_hash": factor_payload["qualification_hash"],
                "qualification_counts": factor_payload["qualification_counts"],
                "sample_qualified_templates": factor_payload["sample_qualified_templates"],
            },
            "document_capability_smoke": document_smoke,
            "experiment_plan": experiment_plan,
            "draft_recommendations": draft_recommendations,
            "invariants": invariant_payload,
        }
        artifact_hash = stable_hash(body_without_hash)
        output = dict(body_without_hash)
        output["artifact_hash"] = artifact_hash
        if output_dir is not None:
            _write_demo_outputs(output_dir, output)
        return output


def _write_demo_outputs(output_dir: Path, output: Mapping[str, Any]) -> None:
    json_path = output_dir / "overnight_demo.json"
    md_path = output_dir / "overnight_demo.md"
    _atomic_write_json(json_path, output)
    selected = ", ".join(output["route"]["selected_desks"])
    unselected = ", ".join(output["route"]["unselected_desks"])
    md = f"""# AlphaForge 隔夜研究工具 Demo

状态：`{output["status"]}`

Artifact hash：`{output["artifact_hash"]}`

## 本次 Campaign

- 问题：{output["campaign"]["question"]}
- 模式：{output["campaign"]["mode"]}
- 深度：{output["campaign"]["research_depth"]}（只产出草案，不触发官方 Evidence/Promotion）
- 选中 Desk：{selected}
- 未选 Desk：{unselected}

## 已验证的连接

- 冻结数据/PIT 摘要：`{output["tool_results"]["dataset_pit_summary"]["artifact_hash"]}`
- Vibe 冻结基本面摘要：`{output["tool_results"]["fundamentals_summary"]["artifact_hash"]}`
- 因子目录与资格化：`{output["factor_library"]["qualification_hash"]}`
- 可见 Evidence 摘要：`{output["tool_results"]["query_visible_evidence"]["artifact_hash"]}`
- 冻结文档 citation smoke：`{output["document_capability_smoke"]["artifact_hash"]}`

## 边界

- 未运行网络、真实 LLM、Qlib、Promotion、Portfolio、Risk、Order、Broker 或 Ledger。
- 文档引用能力仅作为 capability smoke；本 Campaign 未选择 expectations_events Desk，因此没有给它上下文或工具。
- 所有输出均为 DRAFT_SUPPORT 或 AUXILIARY_RESEARCH_EVIDENCE。
"""
    _atomic_write_text(md_path, md)


def build_demo_artifacts(root: Path | None = None, *, seed: int = 7, offline: bool = True, output_dir: Path | None = None) -> dict[str, Any]:
    demo_root = root or _repo_root()
    target_dir = output_dir or (demo_root / ".autobuild" / "veriquant" / "overnight" / "integration")
    return OvernightResearchToolDemo(root=demo_root, seed=seed, offline=offline).run(target_dir)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AlphaForge overnight research-tool integration demo.")
    parser.add_argument("--offline", action="store_true", help="Required: run without network/provider credentials.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(tuple(argv if argv is not None else sys.argv[1:]))
    if not args.offline:
        payload = {
            "schema_version": INTEGRATION_SCHEMA_VERSION,
            "status": "FAILED",
            "failure": "OFFLINE_REQUIRED",
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        output = build_demo_artifacts(seed=args.seed, offline=True, output_dir=args.output_dir)
    except Exception as exc:
        payload = {
            "schema_version": INTEGRATION_SCHEMA_VERSION,
            "status": "FAILED",
            "failure": type(exc).__name__,
            "message": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps({"status": output["status"], "artifact_hash": output["artifact_hash"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
