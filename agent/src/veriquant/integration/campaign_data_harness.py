"""Strict operator harness for the Campaign -> Evidence data plane.

The command is network-disabled by default and prints only content hashes,
coverage counts and typed status.  Provider credentials are never serialized.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from veriquant.approved_proxy_scale_adapter_v2 import (
    AllowlistedProxyScaleClient,
    ApprovedProxyScaleAdapterV2,
)
from veriquant.dynamic_acquisition import ResourceBudget
from veriquant.integration.campaign_data_preparation import (
    CampaignDataPreparationService,
    CampaignDataPreparationStatus,
)
from veriquant.integration.campaign_evidence_dataset import (
    CampaignEvidenceDatasetRepository,
)
from veriquant.integration.capture_research_os_v2_data import (
    _load_proxy_settings,
)
from veriquant.integration.research_os_v2 import FrozenValidationDataRepository
from veriquant.research_os_v3.product_service import ResearchOSV3ProductService
from veriquant.tushare_sdk_proxy import ProxySdkConfig, create_proxy_client


DEFAULT_QUESTION = (
    "研究大A市场中ROE质量、盈利成长、资金流和SHIBOR市场状态对未来5日收益的预测能力"
)


def run(args: argparse.Namespace) -> dict[str, object]:
    if not args.allow_network:
        return {
            "schema_version": "alphaforge.campaign-data-harness/v1",
            "status": "BLOCKED",
            "failure_codes": ["NETWORK_NOT_ENABLED"],
        }
    repo_root = Path(args.repo_root).resolve()
    harness_root = Path(args.root).resolve()
    active = FrozenValidationDataRepository(
        repo_root / ".autobuild/veriquant/integration/research_os_v2/data_active"
    )
    fallback = FrozenValidationDataRepository(
        repo_root / ".autobuild/veriquant/integration/research_os_v2/data"
    )
    campaign_repository = CampaignEvidenceDatasetRepository(
        harness_root / "campaign_data"
    )
    product = ResearchOSV3ProductService(
        root=harness_root / "product",
        data_loader=lambda: active.load() or fallback.load(),
        dotenv_path=Path(args.dotenv).resolve(),
        campaign_data_repository=campaign_repository,
    )
    try:
        plan = product.create_plan({
            "question": args.question,
            "depth": "标准研究",
            "interactionStyle": "标准式",
        })
        settings = _load_proxy_settings(Path(args.dotenv).resolve())
        sdk = create_proxy_client(ProxySdkConfig(
            endpoint=settings["TUSHARE_PROXY_URL"],
            token=settings["TUSHARE_TOKEN"],
        ))
        client = AllowlistedProxyScaleClient(sdk)
        result = CampaignDataPreparationService(
            root=harness_root / "preparation",
            scale_adapter=ApprovedProxyScaleAdapterV2(client),
            dynamic_transport=client,
        ).prepare(
            product_service=product,
            plan_id=str(plan["campaignId"]),
            end_date=args.end_date,
            capture_time=datetime.now(timezone.utc),
            index_code=args.index_code,
            target_symbols=args.target_symbols,
            max_acquisition_symbols=args.max_acquisition_symbols,
            allow_network=True,
            resource_budget=ResourceBudget(
                max_api_calls=args.max_api_calls,
                max_rows=args.max_rows,
                max_storage_bytes=args.max_storage_bytes,
            ),
        )
        payload = asdict(result)
        payload["status"] = result.status.value
        payload.update({
            "schema_version": "alphaforge.campaign-data-harness/v1",
            "question": args.question,
            "source_label": "APPROVED_PROXY_DATA_VENDOR",
            "official_tushare": False,
        })
        _atomic_json(harness_root / "result.json", payload)
        return payload
    finally:
        product.control.close()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        handle.close()
        temporary.replace(path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare and prove Campaign Evidence data")
    parser.add_argument("--repo-root", default="..")
    parser.add_argument("--root", default="../.autobuild/veriquant/campaign_data_harness")
    parser.add_argument("--dotenv", default=".env")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--index-code", default="000300.SH")
    parser.add_argument("--target-symbols", type=int, default=30)
    parser.add_argument("--max-acquisition-symbols", type=int, default=None)
    parser.add_argument("--max-api-calls", type=int, default=500)
    parser.add_argument("--max-rows", type=int, default=2_000_000)
    parser.add_argument("--max-storage-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    payload = run(args)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("status") == CampaignDataPreparationStatus.READY.value else 2


if __name__ == "__main__":
    raise SystemExit(main())
