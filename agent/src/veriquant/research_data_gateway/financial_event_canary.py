"""Explicit-network, bounded financial/event acquisition canary.

This entrypoint deliberately requires representative symbols and never
discovers or fans out to the complete historical universe.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..approved_proxy_scale_adapter_v2 import AllowlistedProxyScaleClient
from ..dynamic_acquisition import ResourceBudget
from ..dynamic_data_integration import DynamicCampaignDataFreezer
from ..integration.capture_research_os_v2_data import _load_proxy_settings
from ..tushare_sdk_proxy import ProxySdkConfig, create_proxy_client
from .plans import build_financial_event_canary_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded approved-proxy financial/event canary"
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--dotenv", default=".env")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--max-api-calls", type=int, default=36)
    parser.add_argument("--max-parallel-requests", type=int, default=3)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    symbols = tuple(sorted({item.strip() for item in args.symbols.split(",") if item.strip()}))
    if not symbols or any("." not in item for item in symbols):
        parser.error("--symbols must contain explicit market-qualified identifiers")
    if not args.allow_network:
        print(json.dumps({"status": "BLOCKED", "failure_codes": ["NETWORK_DISABLED"]}))
        return 2

    settings = _load_proxy_settings(Path(args.dotenv).resolve())
    sdk = create_proxy_client(
        ProxySdkConfig(
            endpoint=settings["TUSHARE_PROXY_URL"], token=settings["TUSHARE_TOKEN"]
        )
    )
    result = DynamicCampaignDataFreezer(Path(args.root).resolve()).freeze(
        plan=build_financial_event_canary_plan(
            start_date=args.start_date, end_date=args.end_date
        ),
        transport=AllowlistedProxyScaleClient(sdk),
        start_date=args.start_date,
        end_date=args.end_date,
        symbols=symbols,
        acquisition_mode="FULL_MARKET",
        allow_network=True,
        budget=ResourceBudget(
            max_api_calls=args.max_api_calls,
            max_parallel_requests=args.max_parallel_requests,
        ),
    )
    payload = {
        "status": result.status,
        "source_label": result.source_label,
        "official_tushare": False,
        "manifest_hash": result.manifest_hash,
        "manifest_path": result.manifest_path,
        "symbol_count": len(symbols),
        "planned_partition_count": result.planned_partition_count,
        "api_calls_used": result.api_calls_used,
        "api_statuses": dict(result.api_statuses),
        "row_counts": dict(result.row_counts),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if result.status == "SUCCESS" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
