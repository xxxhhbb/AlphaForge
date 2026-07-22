"""Explicit-network CLI for question-driven approved-proxy data freezing."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from ..approved_proxy_scale_adapter_v2 import AllowlistedProxyScaleClient
from ..dynamic_acquisition import ResourceBudget
from ..dynamic_data_integration import DynamicCampaignDataFreezer
from ..dynamic_data_planning import (
    DataPlanningRequest,
    DynamicDataRequirementCompiler,
)
from ..tushare_sdk_proxy import ProxySdkConfig, create_proxy_client
from .capture_research_os_v2_data import _load_proxy_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze a dynamic AlphaForge Campaign data scope"
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--dotenv", default="agent/.env")
    parser.add_argument("--question", required=True)
    parser.add_argument("--research-mode", default="NEW_ALPHA_EXPLORATION")
    parser.add_argument("--lookback-sessions", type=int)
    parser.add_argument("--label-sessions", type=int, default=5)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated explicit universe; it is never silently truncated.",
    )
    parser.add_argument("--max-api-calls", type=int)
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--max-storage-bytes", type=int)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    symbols = tuple(
        sorted({item.strip() for item in args.symbols.split(",") if item.strip()})
    )
    if not symbols:
        raise ValueError("at least one symbol is required")
    plan = DynamicDataRequirementCompiler().compile(
        DataPlanningRequest(
            question=args.question,
            research_mode=args.research_mode,
            factor_lookback_sessions=args.lookback_sessions,
            label_horizon_sessions=args.label_sessions,
        )
    )
    settings = _load_proxy_settings(Path(args.dotenv))
    sdk = create_proxy_client(
        ProxySdkConfig(
            endpoint=settings["TUSHARE_PROXY_URL"],
            token=settings["TUSHARE_TOKEN"],
        )
    )
    transport = AllowlistedProxyScaleClient(sdk)
    result = DynamicCampaignDataFreezer(args.root).freeze(
        plan=plan,
        transport=transport,
        start_date=args.start_date,
        end_date=args.end_date,
        symbols=symbols,
        allow_network=args.allow_network,
        budget=ResourceBudget(
            max_api_calls=args.max_api_calls,
            max_rows=args.max_rows,
            max_storage_bytes=args.max_storage_bytes,
        ),
    )
    # ``DynamicDataFreezeResult`` contains hashes, counts, public source labels,
    # and paths only. Provider credentials never cross this output boundary.
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.status == "SUCCESS" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
