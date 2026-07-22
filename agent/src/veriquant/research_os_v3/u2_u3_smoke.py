"""Opt-in real-model qualification against approved frozen U3 research tools."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ..llm_desks.safety import canonical_hash
from ..specialist_research import (
    SpecialistResearchBudget,
    SpecialistResearchRequest,
    build_configured_specialist_provider,
    get_specialist_profile,
)
from .service import ResearchOSV3Service
from .tools import ResearchCapabilityStatus, build_frozen_research_registry, load_frozen_research_context


QUESTIONS = {
    "fundamental": "基于冻结且在决策时点可得的财务质量、估值和变化字段，提出多个可证伪的大A横截面 Alpha 草案。",
    "expectations_events": "研究盈利预期和公告事件的首次可交易反应；若冻结数据不足，明确报告数据缺口。",
    "market_structure": "基于冻结价格、成交量、流动性与成本字段，提出多个可证伪的大A市场行为 Alpha 草案。",
    "alternative_data": "评价冻结另类信息的来源、时间可得性、覆盖和实体映射；不足时明确报告数据缺口。",
    "macro_regime": "基于决策时点可知的利率和市场状态字段，提出多个条件化且可证伪的大A Alpha 草案。",
    "meta_alpha": "基于可见因子目录与可见研究证据，提出多个低重复、待增量验证的大A Alpha 草案。",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one U2/U3 real-model specialist qualification")
    parser.add_argument("--specialist", required=True, choices=tuple(QUESTIONS))
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--dotenv", type=Path, default=Path(".env"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--visible-evidence", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    args = parser.parse_args(argv)

    decision_time = datetime(2026, 7, 20, 8, tzinfo=timezone.utc)
    context = load_frozen_research_context(
        args.manifest,
        decision_time=decision_time,
        visible_evidence_path=args.visible_evidence,
    )
    capability, missing = context.capability(args.specialist)
    if capability is not ResearchCapabilityStatus.AVAILABLE:
        output = {
            "authority": "DRAFT_ONLY",
            "missing_capabilities": missing,
            "operational_status": capability.value,
            "specialist_id": args.specialist,
            "source_label": context.source_label,
            "context_hash": context.artifact_hash,
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0

    material = {"specialist": args.specialist, "attempt": args.attempt, "seed": args.seed, "context_hash": context.artifact_hash}
    run_id = "u2-u3-real-" + args.specialist + "-" + canonical_hash(material).split(":", 1)[1][:12]
    profile = get_specialist_profile(args.specialist)
    provider = build_configured_specialist_provider(args.dotenv)
    request = SpecialistResearchRequest(
        run_id=run_id,
        campaign_hash=canonical_hash({"kind": "u2-u3-real-qualification", **material}),
        specialist_id=args.specialist,
        question=QUESTIONS[args.specialist],
        universe="cn-ashare-approved-proxy-pit",
        label_horizon_sessions=5,
        decision_time=decision_time,
        available_fields=context.available_fields,
        requested_lookback_sessions=None,
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
    service = ResearchOSV3Service(
        root=args.artifact_root,
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        data_snapshot_hash=context.artifact_hash,
        policy_hash=canonical_hash({"policy": "u2-u3-draft-qualification-v1"}),
        code_hash=canonical_hash({"runtime": "research-os-v3", "wave": "u2-u3"}),
    )
    result = service.run_single_specialist(
        request=request,
        provider=provider,
        tools=build_frozen_research_registry(profile, context=context),
    )
    output = {
        "accepted_preflight_count": result.accepted_preflight_count,
        "authority": "DRAFT_ONLY",
        "candidate_count": result.candidate_count,
        "context_hash": context.artifact_hash,
        "manifest_hash": result.manifest_hash,
        "model": provider.model_id,
        "operational_status": "QUALIFIED" if result.accepted_preflight_count >= 1 else "FAILED",
        "result_hash": result.artifact_hash,
        "result_kind": result.result_kind.value,
        "run_id": run_id,
        "source_label": context.source_label,
        "specialist_id": args.specialist,
        "typed_failures": result.typed_failures,
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0 if result.candidate_count >= 4 and result.accepted_preflight_count >= 1 else 2


if __name__ == "__main__":
    raise SystemExit(main())
