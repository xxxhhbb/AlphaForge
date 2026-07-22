"""CLI entry point for AlphaForge Campaign automation."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ..contracts.base import canonicalize
from .models import AutomationRequest, AutomationRuntimeMode, DataVendorClass
from .service import CampaignAutomationService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one AlphaForge Campaign automation flow")
    parser.add_argument("--question", required=True)
    parser.add_argument("--run-id", default="campaign-auto-seed7")
    parser.add_argument("--runtime-mode", choices=[item.value for item in AutomationRuntimeMode], default="DETERMINISTIC_OFFLINE")
    parser.add_argument("--provider-available", action="store_true")
    parser.add_argument("--real-llm-from-env", action="store_true")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--horizon", default="20d")
    parser.add_argument("--universe", default="CN_ASHARE_APPROVED_PIT_SAMPLE")
    parser.add_argument("--output-root", type=Path, default=Path("../.autobuild/veriquant/campaign_automation"))
    parser.add_argument("--approved-proxy", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    request = AutomationRequest(
        question=args.question,
        run_id=args.run_id,
        runtime_mode=AutomationRuntimeMode(args.runtime_mode),
        universe=(args.universe,),
        horizon=args.horizon,
        seed=args.seed,
        output_root=str(args.output_root),
        provider_available=args.provider_available,
        real_llm_from_env=args.real_llm_from_env,
        data_vendor_class=DataVendorClass.APPROVED_PROXY_DATA_VENDOR
        if args.approved_proxy
        else DataVendorClass.DETERMINISTIC_OFFLINE_FIXTURE,
        decision_time=datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc),
    )
    result = CampaignAutomationService().run(request)
    print(json.dumps(canonicalize(result.to_dict()), ensure_ascii=False, sort_keys=True))
    return 0 if result.status.value in {"COMPLETED", "PARTIAL", "AWAITING_CHECKPOINT"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
