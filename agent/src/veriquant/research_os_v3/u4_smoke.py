"""Opt-in real-model, Campaign-routed U4 multi-specialist smoke."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..contracts import Component
from ..llm_desks.safety import canonical_hash
from ..research_campaign import (
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
from ..specialist_research import build_configured_specialist_provider
from .multi_service import ResearchOSV3MultiSpecialistService
from .tools import load_frozen_research_context


class _CountingProvider:
    """Transparent operational counter; it does not inspect provider payloads."""

    def __init__(self, provider) -> None:
        self._provider = provider
        self.provider_id = provider.provider_id
        self.model_id = provider.model_id
        self.calls = 0

    def next_turn(self, context):
        self.calls += 1
        return self._provider.next_turn(context)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    json.loads(temporary.read_text(encoding="utf-8"))
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the real U4 Campaign-routed multi-specialist smoke")
    parser.add_argument("--dotenv", type=Path, default=Path(".env"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--visible-evidence", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    decision_time = datetime(2026, 7, 20, 8, tzinfo=timezone.utc)
    context = load_frozen_research_context(
        args.manifest,
        decision_time=decision_time,
        visible_evidence_path=args.visible_evidence,
    )
    subject = ResearchSubject(
        subject_id="subject.cn_ashare_u4_real",
        description="Approved-proxy frozen A-share multi-specialist DRAFT research.",
        universe=("cn_ashare_approved_pit",),
        horizon="5d",
        decision_time=decision_time,
        allowed_data=("price_volume", "fundamentals", "registered_factor_summary"),
    )
    draft = ResearchCampaignDraft(
        campaign_id="campaign.u4.real.multi",
        question="基于冻结且在决策时点可得的大A数据，多专业研究组并行提出50日可证伪 Alpha 草案。",
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        research_depth=ResearchDepth.STANDARD_RESEARCH,
        interaction_style=InteractionStyle.STANDARD,
        subject=subject,
        method_models=(MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
        candidate_agents=(
            "fundamental",
            "expectations_events",
            "market_structure",
            "alternative_data",
            "macro_regime",
            "meta_alpha",
        ),
        budget=CampaignBudget(
            max_factor_families=36,
            max_trials=36,
            max_llm_calls=48,
            max_runtime_seconds=1200,
        ),
        user_checkpoints=(
            UserCheckpoint(checkpoint_id="checkpoint.u4.candidates", stage="after_candidate_shortlist"),
        ),
        stop_conditions=(StopCondition.BUDGET_EXHAUSTED, StopCondition.VALIDATION_COMPLETED),
        data_manifest_hash=context.dataset_manifest_hash,
        policy_hash=canonical_hash({"policy": "u4-draft-only-multi-specialist-v1"}),
        code_hash=canonical_hash({"runtime": "research-os-v3", "wave": "u4"}),
        created_at=decision_time,
        proposed_by=Component.LLM_RESEARCHER,
        state=CampaignState.NEEDS_CONFIRMATION,
    )
    campaign = ResearchCampaign.confirm(draft, confirmed_at=decision_time)
    routed = DeterministicCampaignRouter().route(
        campaign,
        data_availability=DataAvailability(
            available_data=subject.allowed_data,
            universe_ids=subject.universe,
            data_manifest_hash=context.dataset_manifest_hash,
            calendar_ids=("cn_ashare_sse_szse",),
        ),
        registered_context=RegisteredResearchContext(),
        routed_at=decision_time,
    )
    if routed.route is None:
        raise RuntimeError("U4 Campaign did not produce a route")
    route = routed.route
    _atomic_json(args.artifact_root / "u4_campaign.json", campaign.to_dict())
    _atomic_json(args.artifact_root / "u4_route.json", route.to_dict())
    _atomic_json(args.artifact_root / "u4_context.json", context.to_dict())
    providers = {
        specialist_id: _CountingProvider(build_configured_specialist_provider(args.dotenv))
        for specialist_id in route.selected_desks
    }
    run_material = {
        "campaign_hash": campaign.artifact_hash,
        "route_hash": route.artifact_hash,
        "context_hash": context.artifact_hash,
        "seed": args.seed,
    }
    run_id = "u4-real-multi-" + canonical_hash(run_material).split(":", 1)[1][:12]
    service = ResearchOSV3MultiSpecialistService(root=args.artifact_root, max_workers=len(route.selected_desks))
    first = service.run(
        run_id=run_id,
        campaign=campaign,
        route=route,
        context=context,
        providers=providers,
        seed=args.seed,
    )
    calls_before_replay = {name: provider.calls for name, provider in providers.items()}
    replay = service.run(
        run_id=run_id,
        campaign=campaign,
        route=route,
        context=context,
        providers=providers,
        seed=args.seed,
    )
    calls_after_replay = {name: provider.calls for name, provider in providers.items()}
    resume_idempotent = (
        replay.artifact_hash == first.artifact_hash
        and calls_after_replay == calls_before_replay
    )
    payload = {
        "schema_version": "alphaforge.research-os-v3-u4-smoke/v1",
        "authority": "DRAFT_ONLY",
        "source_label": context.source_label,
        "official_tushare": context.official_tushare,
        "run_id": run_id,
        "campaign_hash": campaign.artifact_hash,
        "route_hash": route.artifact_hash,
        "context_hash": context.artifact_hash,
        "selected_specialists": route.selected_desks,
        "provider": next(iter(providers.values())).provider_id,
        "model": next(iter(providers.values())).model_id,
        "result": first.to_dict(),
        "resume_idempotent": resume_idempotent,
        "provider_calls_before_replay": calls_before_replay,
        "provider_calls_after_replay": calls_after_replay,
        "official_evidence_hash": None,
        "promotion_hash": None,
    }
    payload["artifact_hash"] = canonical_hash(payload)
    output_path = args.artifact_root / "u4_smoke_result.json"
    _atomic_json(output_path, payload)
    print(json.dumps({
        "artifact_hash": payload["artifact_hash"],
        "candidate_count": first.candidate_count,
        "accepted_preflight_count": first.accepted_preflight_count,
        "output": str(output_path.resolve()),
        "resume_idempotent": resume_idempotent,
        "run_id": run_id,
        "selected_specialists": route.selected_desks,
        "status": first.status.value,
    }, ensure_ascii=False, sort_keys=True))
    productive = first.candidate_count >= 4 and first.accepted_preflight_count >= 1
    return 0 if productive and resume_idempotent else 2


if __name__ == "__main__":
    raise SystemExit(main())
