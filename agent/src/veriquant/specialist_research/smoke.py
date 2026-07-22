"""Explicit opt-in real-model smoke for one proposal-only specialist agent."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ..llm_desks.safety import canonical_hash
from .checkpoint import SpecialistCheckpointStore
from .loop import BoundedResearchAgentLoop
from .models import SpecialistResearchBudget, SpecialistResearchRequest, SpecialistStage
from .profiles import get_specialist_profile
from .provider import build_configured_specialist_provider
from .tools import build_fixture_registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded AlphaForge specialist DRAFT smoke")
    parser.add_argument("--specialist", default="market_structure")
    parser.add_argument("--question", default="挖掘最近 50 日大 A 动量相关 Alpha")
    parser.add_argument("--lookback", type=int, default=50)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--dotenv", type=Path, default=Path("agent/.env"))
    parser.add_argument("--artifact-root", type=Path, default=Path(".autobuild/veriquant/specialist_research_smoke"))
    parser.set_defaults(question="挖掘最近 50 日大 A 动量相关 Alpha")
    args = parser.parse_args(argv)

    profile = get_specialist_profile(args.specialist)
    material = {
        "specialist": args.specialist,
        "question": args.question,
        "lookback": args.lookback,
        "horizon": args.horizon,
        "seed": args.seed,
    }
    run_id = "specialist-smoke-" + canonical_hash(material).split(":", 1)[1][:16]
    fields = ("close", "volume", "market_cap", "roe", "available_at", "shibor_overnight")
    request = SpecialistResearchRequest(
        run_id=run_id,
        campaign_hash=canonical_hash({"kind": "specialist-smoke", **material}),
        specialist_id=args.specialist,
        question=args.question,
        universe="cn-ashare-pit-visible-smoke",
        label_horizon_sessions=args.horizon,
        decision_time=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        available_fields=fields,
        requested_lookback_sessions=args.lookback,
        budget=SpecialistResearchBudget(
            max_iterations=16,
            max_tool_calls=6,
            max_tokens=200_000,
            max_input_tokens=120_000,
            max_output_tokens=80_000,
            max_ideas=30,
            max_candidate_families=12,
            max_combinations=64,
            max_submitted_candidates=12,
            max_revisions=2,
            timeout_seconds=600.0,
            family_trial_budget=12,
        ),
        seed=args.seed,
    )
    store = SpecialistCheckpointStore(args.artifact_root, run_id=run_id, specialist_id=args.specialist)
    outcome = BoundedResearchAgentLoop().run(
        request=request,
        profile=profile,
        provider=build_configured_specialist_provider(args.dotenv),
        tools=build_fixture_registry(profile, available_fields=fields),
        checkpoint_store=store,
    )
    public = {
        "run_id": run_id,
        "specialist": args.specialist,
        "status": outcome.status.value,
        "result_kind": outcome.result_kind.value,
        "outcome_hash": outcome.artifact_hash,
        "candidate_count": len(outcome.candidates),
        "accepted_preflight_count": sum(item.status.value == "ACCEPTED" for item in outcome.preflight_results),
        "usage": outcome.usage.to_dict(),
        "failure_code": outcome.failure_code,
        "quarantined_item_count": len(outcome.draft_issues),
        "artifact_directory": str(store.directory),
        "authority": "DRAFT_ONLY",
    }
    print(json.dumps(public, ensure_ascii=False, sort_keys=True))
    return 0 if outcome.status is SpecialistStage.COMPLETED else 2


if __name__ == "__main__":
    raise SystemExit(main())
