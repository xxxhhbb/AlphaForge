"""Explicit-network operator entrypoint for the full ResearchDataGateway."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..approved_proxy_scale_adapter_v2 import AllowlistedProxyScaleClient
from ..dynamic_acquisition import ResourceBudget
from ..integration.capture_research_os_v2_data import _load_proxy_settings
from ..tushare_sdk_proxy import ProxySdkConfig, create_proxy_client
from .auxiliary import default_vibe_tools
from .gateway import ResearchDataGateway
from .harness import ResearchDataGatewayHarness
from .models import ResearchDataGatewayRequest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze and qualify the complete AlphaForge A-share research data plane")
    parser.add_argument("--root", default="../.autobuild/veriquant/research_data_gateway")
    parser.add_argument("--dotenv", default=".env")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--label-horizon", type=int, default=5)
    parser.add_argument("--index-codes", default="000001.SH,399001.SZ")
    parser.add_argument("--aux-symbol", default="000001.SZ")
    parser.add_argument("--max-api-calls", type=int)
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--max-storage-bytes", type=int)
    parser.add_argument("--max-parallel-requests", type=int, default=4)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    if not args.allow_network:
        print(json.dumps({"status": "BLOCKED", "failure_codes": ["NETWORK_DISABLED"]}, ensure_ascii=False))
        return 2
    settings = _load_proxy_settings(Path(args.dotenv).resolve())
    sdk = create_proxy_client(ProxySdkConfig(endpoint=settings["TUSHARE_PROXY_URL"], token=settings["TUSHARE_TOKEN"]))
    transport = AllowlistedProxyScaleClient(sdk)
    request = ResearchDataGatewayRequest(
        start_date=args.start_date,
        end_date=args.end_date,
        index_codes=tuple(item.strip() for item in args.index_codes.split(",") if item.strip()),
        label_horizon_sessions=args.label_horizon,
        allow_network=True,
    )
    result = ResearchDataGateway(args.root).prepare(
        request=request,
        transport=transport,
        budget=ResourceBudget(
            max_api_calls=args.max_api_calls,
            max_rows=args.max_rows,
            max_storage_bytes=args.max_storage_bytes,
            max_parallel_requests=args.max_parallel_requests,
        ),
        # The already server-loaded approved-proxy client is injected into the
        # read-only auxiliary wrapper.  The wrapper never reads credentials or
        # environment variables itself and still freezes northbound data as
        # AUXILIARY_RESEARCH_EVIDENCE only.
        auxiliary_tools=default_vibe_tools(northbound_proxy_client=sdk),
        auxiliary_queries={
            "northbound": {"lookback_days": 250},
            "dragon_tiger": {"date": f"{args.end_date[:4]}-{args.end_date[4:6]}-{args.end_date[6:]}"},
            "stock_news": {"scope": "global", "limit": 50},
            "research_reports": {"code": args.aux_symbol, "limit": 50},
        },
    )
    verdict = ResearchDataGatewayHarness(Path(args.root) / "harness").evaluate(result)
    print(json.dumps({
        "status": verdict.status,
        "gateway_status": result.manifest.status.value,
        "manifest_hash": result.manifest.artifact_hash,
        "manifest_path": result.manifest_path,
        "historical_symbol_count": result.manifest.historical_symbol_count,
        "campaign_dataset_hash": result.campaign_dataset_hash,
        "acceptance_hash": verdict.artifact_hash,
        "failures": list(verdict.failures),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if verdict.status == "PASSED" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
