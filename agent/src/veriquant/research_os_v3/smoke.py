"""Explicit opt-in real-model smoke for Research OS V3 U0/U1."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ..llm_desks.safety import canonical_hash
from ..research_run_controller import ResearchRunLifecycle
from ..specialist_research import (
    SpecialistResearchBudget,
    SpecialistResearchRequest,
    build_configured_specialist_provider,
    build_fixture_registry,
    get_specialist_profile,
)
from .service import ResearchOSV3Service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one real-model AlphaForge Research OS V3 DRAFT smoke")
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--specialist", default="market_structure")
    parser.add_argument("--question", default="挖掘最近50日大A动量相关Alpha")
    parser.add_argument("--lookback", type=int, default=50)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--dotenv", type=Path, default=Path("agent/.env"))
    parser.add_argument("--artifact-root", type=Path, default=Path(".autobuild/veriquant/research_os_v3_smoke"))
    args = parser.parse_args(argv)

    material = {
        "attempt": args.attempt,
        "specialist": args.specialist,
        "question": args.question,
        "lookback": args.lookback,
        "horizon": args.horizon,
        "seed": args.seed,
    }
    run_id = "research-os-v3-smoke-" + canonical_hash(material).split(":", 1)[1][:16]
    fields = ("close", "volume", "market_cap", "roe", "available_at", "shibor_overnight")
    profile = get_specialist_profile(args.specialist)
    request = SpecialistResearchRequest(
        run_id=run_id,
        campaign_hash=canonical_hash({"kind": "research-os-v3-real-smoke", **material}),
        specialist_id=args.specialist,
        question=args.question,
        universe="cn_ashare_pit_visible_smoke",
        label_horizon_sessions=args.horizon,
        decision_time=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        available_fields=fields,
        requested_lookback_sessions=args.lookback,
        budget=SpecialistResearchBudget(
            max_iterations=16,
            max_tool_calls=8,
            max_tokens=240_000,
            max_input_tokens=150_000,
            max_output_tokens=90_000,
            max_ideas=30,
            max_candidate_families=12,
            max_combinations=64,
            max_submitted_candidates=12,
            max_revisions=2,
            timeout_seconds=900.0,
            family_trial_budget=12,
        ),
        seed=args.seed,
    )
    provider = build_configured_specialist_provider(args.dotenv)
    service = ResearchOSV3Service(
        root=args.artifact_root,
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        data_snapshot_hash=canonical_hash({"fixture": "visible-read-tools", "fields": fields}),
        policy_hash=canonical_hash({"policy": "research-os-v3-u0-u1"}),
        code_hash=canonical_hash({"runtime": "research-os-v3", "version": 1}),
    )
    result = service.run_single_specialist(
        request=request,
        provider=provider,
        tools=build_fixture_registry(profile, available_fields=fields),
    )
    public = {
        "accepted_preflight_count": result.accepted_preflight_count,
        "artifact_directory": str((args.artifact_root / "research_runs" / run_id).resolve()),
        "authority": "DRAFT_ONLY",
        "candidate_count": result.candidate_count,
        "current_stage": result.current_stage,
        "manifest_hash": result.manifest_hash,
        "model": provider.model_id,
        "result_hash": result.artifact_hash,
        "result_kind": result.result_kind.value,
        "run_id": run_id,
        "typed_failures": result.typed_failures,
    }
    print(json.dumps(public, ensure_ascii=False, sort_keys=True))
    passed = (
        result.current_stage == ResearchRunLifecycle.PARTIAL.value
        and result.candidate_count >= 4
        and result.accepted_preflight_count >= 1
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
