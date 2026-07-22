"""One governed bridge from approved provider data to Campaign Evidence inputs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from dataclasses import replace
from typing import Any, Mapping

from ..dynamic_acquisition import ContentAddressedRowStore, ResourceBudget, stable_content_hash
from ..dynamic_data_integration import CampaignEvidenceDatasetBuilder, DynamicCampaignDataFreezer
from ..integration.campaign_evidence_dataset import CampaignEvidenceDatasetRepository
from ..integration.research_os_v2 import FrozenValidationDataRepository
from ..research_validation_pipeline_v2 import FrozenValidationData
from .auxiliary import AuxiliaryArtifactFreezer, ReadOnlyTool
from .models import (
    DataAuthority,
    DataSourceReceipt,
    GatewayFailureCode,
    GatewayStatus,
    ResearchDataGatewayManifest,
    ResearchDataGatewayRequest,
    ResearchDataGatewayResult,
)
from .market_spine import build_market_spine
from .plans import OFFICIAL_API_NAMES, build_comprehensive_plan, build_reference_plan


class ResearchDataGateway:
    """Two-stage, resumable full-history acquisition and Campaign projection."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(
        self,
        *,
        request: ResearchDataGatewayRequest,
        transport: object,
        budget: ResourceBudget | None = None,
        base_data: FrozenValidationData | None = None,
        campaign_plan_id: str | None = None,
        campaign_repository: CampaignEvidenceDatasetRepository | None = None,
        required_fields: tuple[str, ...] = (),
        optional_fields: tuple[str, ...] = (),
        auxiliary_tools: Mapping[str, ReadOnlyTool] | None = None,
        auxiliary_queries: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ResearchDataGatewayResult:
        reference_root = self.root / "reference"
        official_root = self.root / "official"
        reference = DynamicCampaignDataFreezer(reference_root).freeze(
            plan=build_reference_plan(start_date=request.start_date, end_date=request.end_date),
            transport=transport,
            start_date=request.start_date,
            end_date=request.end_date,
            symbols=(),
            index_codes=request.index_codes,
            allow_network=request.allow_network,
            budget=budget,
        )
        if reference.status != "SUCCESS":
            return self._blocked(request, reference.status, GatewayFailureCode.REFERENCE_DATA_UNAVAILABLE)

        symbols = discover_historical_symbols(
            reference_root,
            start_date=request.start_date,
            end_date=request.end_date,
        )
        if not symbols:
            return self._blocked(request, "EMPTY", GatewayFailureCode.HISTORICAL_UNIVERSE_EMPTY)

        official = DynamicCampaignDataFreezer(official_root).freeze(
            plan=build_comprehensive_plan(start_date=request.start_date, end_date=request.end_date),
            transport=transport,
            start_date=request.start_date,
            end_date=request.end_date,
            symbols=symbols,
            index_codes=request.index_codes,
            acquisition_mode="FULL_MARKET",
            allow_network=request.allow_network,
            budget=(
                replace(
                    budget,
                    max_api_calls=(
                        None
                        if budget.max_api_calls is None
                        else max(0, budget.max_api_calls - reference.api_calls_used)
                    ),
                )
                if budget is not None
                else None
            ),
        )
        official_receipts = tuple(
            DataSourceReceipt(
                source_id=api,
                authority=DataAuthority.OFFICIAL_INPUT,
                status=str(official.api_statuses.get(api, "MISSING")),
                row_count=int(official.row_counts.get(api, 0)),
                content_hash=_api_content_hash(official_root, api)
                if official.api_statuses.get(api) in {"SUCCESS", "EMPTY"}
                else None,
                failure_code=(
                    None
                    if official.api_statuses.get(api) == "SUCCESS"
                    else GatewayFailureCode.REQUIRED_SOURCE_FAILED
                ),
            )
            for api in OFFICIAL_API_NAMES
        )

        # Do not spend minutes materializing a market spine and 1M+ projected
        # revisions when a required official input is already known to be
        # EMPTY/FAILED.  Preserve every receipt in the terminal manifest; an
        # auxiliary investigation may still be frozen, but it cannot upgrade
        # the missing official authority.
        failed_official = tuple(
            item for item in official_receipts if item.status != "SUCCESS"
        )
        if failed_official:
            auxiliary_receipts: tuple[DataSourceReceipt, ...] = ()
            auxiliary_hash = None
            if auxiliary_queries:
                auxiliary_receipts, auxiliary_hash = AuxiliaryArtifactFreezer(
                    self.root / "auxiliary"
                ).freeze(tools=auxiliary_tools or {}, queries=auxiliary_queries)
            receipts = tuple(
                sorted(official_receipts + auxiliary_receipts, key=lambda item: item.source_id)
            )
            failures = tuple(
                sorted(
                    {item.failure_code for item in receipts if item.failure_code},
                    key=lambda item: item.value,
                )
            )
            manifest = ResearchDataGatewayManifest(
                request_hash=request.artifact_hash,
                status=GatewayStatus.BLOCKED,
                source_label="APPROVED_PROXY_DATA_VENDOR",
                official_tushare=False,
                historical_symbol_count=len(symbols),
                historical_symbols_hash=stable_content_hash(symbols),
                calendar_exchanges=("SSE", "SZSE"),
                official_freeze_hash=official.manifest_hash,
                campaign_dataset_hash=None,
                receipts=receipts,
                failures=failures,
            )
            manifest_path = self.root / "gateway_manifest.json"
            _atomic_json(
                manifest_path,
                {**manifest.to_dict(), "artifact_hash": manifest.artifact_hash},
            )
            return ResearchDataGatewayResult(
                manifest=manifest,
                manifest_path=str(manifest_path),
                official_freeze_root=str(official_root),
                campaign_dataset_hash=None,
                auxiliary_manifest_hash=auxiliary_hash,
            )

        repository = campaign_repository or CampaignEvidenceDatasetRepository(self.root / "campaign_datasets")
        plan_id = campaign_plan_id or "gateway-default"
        campaign_hash = _load_projection_checkpoint(
            repository=repository,
            plan_id=plan_id,
            dynamic_freeze_hash=official.manifest_hash,
            required_fields=tuple(sorted(set(required_fields))),
            optional_fields=tuple(sorted(set(optional_fields))),
        )
        if campaign_hash is None:
            spine_repository = FrozenValidationDataRepository(self.root / "market_spine")
            market_spine = base_data or _load_matching_market_spine(
                spine_repository,
                official_freeze_hash=official.manifest_hash,
                label_horizon_sessions=request.label_horizon_sessions,
            )
            if market_spine is None:
                market_spine = build_market_spine(
                    official_root,
                    dataset_id="alphaforge-full-history-ashare",
                    label_horizon_sessions=request.label_horizon_sessions,
                )
                spine_repository.save(market_spine)
            bundle = CampaignEvidenceDatasetBuilder().build(
                base=market_spine,
                freeze_root=official_root,
                required_fields=required_fields,
                optional_fields=optional_fields,
            )
            if bundle.manifest.missing_required_fields:
                raise ValueError("campaign dataset is missing required fields")
            campaign_hash = repository.save(plan_id, bundle)
            _write_projection_checkpoint(
                repository=repository,
                plan_id=plan_id,
                bundle_hash=campaign_hash,
                dynamic_freeze_hash=official.manifest_hash,
                required_fields=bundle.manifest.required_fields,
                optional_fields=bundle.manifest.optional_fields,
            )

        auxiliary_receipts: tuple[DataSourceReceipt, ...] = ()
        auxiliary_hash = None
        if auxiliary_queries:
            auxiliary_receipts, auxiliary_hash = AuxiliaryArtifactFreezer(
                self.root / "auxiliary"
            ).freeze(tools=auxiliary_tools or {}, queries=auxiliary_queries)

        receipts = tuple(sorted(official_receipts + auxiliary_receipts, key=lambda item: item.source_id))
        # A content-addressed EMPTY partition is safely resumable, but it does
        # not satisfy a required official data source and cannot make the
        # gateway READY.
        status = GatewayStatus.READY if not failed_official else GatewayStatus.BLOCKED
        failures = tuple(sorted({item.failure_code for item in receipts if item.failure_code}, key=lambda item: item.value))
        manifest = ResearchDataGatewayManifest(
            request_hash=request.artifact_hash,
            status=status,
            source_label="APPROVED_PROXY_DATA_VENDOR",
            official_tushare=False,
            historical_symbol_count=len(symbols),
            historical_symbols_hash=stable_content_hash(symbols),
            calendar_exchanges=("SSE", "SZSE"),
            official_freeze_hash=official.manifest_hash,
            campaign_dataset_hash=campaign_hash,
            receipts=receipts,
            failures=failures,
        )
        manifest_path = self.root / "gateway_manifest.json"
        _atomic_json(manifest_path, {**manifest.to_dict(), "artifact_hash": manifest.artifact_hash})
        return ResearchDataGatewayResult(
            manifest=manifest,
            manifest_path=str(manifest_path),
            official_freeze_root=str(official_root),
            campaign_dataset_hash=campaign_hash,
            auxiliary_manifest_hash=auxiliary_hash,
        )

    def _blocked(self, request, source_status: str, code: GatewayFailureCode) -> ResearchDataGatewayResult:
        receipt = DataSourceReceipt(
            source_id="reference",
            authority=DataAuthority.OFFICIAL_INPUT,
            status=source_status,
            row_count=0,
            content_hash=None,
            failure_code=code,
        )
        manifest = ResearchDataGatewayManifest(
            request_hash=request.artifact_hash,
            status=GatewayStatus.BLOCKED,
            source_label="APPROVED_PROXY_DATA_VENDOR",
            official_tushare=False,
            historical_symbol_count=0,
            historical_symbols_hash=stable_content_hash(()),
            calendar_exchanges=("SSE", "SZSE"),
            official_freeze_hash=None,
            campaign_dataset_hash=None,
            receipts=(receipt,),
            failures=(code,),
        )
        path = self.root / "gateway_manifest.json"
        _atomic_json(path, {**manifest.to_dict(), "artifact_hash": manifest.artifact_hash})
        return ResearchDataGatewayResult(manifest=manifest, manifest_path=str(path), official_freeze_root=None, campaign_dataset_hash=None, auxiliary_manifest_hash=None)


def discover_historical_symbols(root: str | Path, *, start_date: str, end_date: str) -> tuple[str, ...]:
    rows = ContentAddressedRowStore(Path(root) / "apis" / "stock_basic" / "partitions").load_all()
    symbols: set[str] = set()
    for row in rows:
        symbol = str(row.get("ts_code") or "").strip().upper()
        if not (symbol.endswith(".SH") or symbol.endswith(".SZ")):
            continue
        listed = _compact_date(row.get("list_date"))
        delisted = _compact_date(row.get("delist_date")) if row.get("delist_date") else None
        if listed and listed <= end_date and (delisted is None or delisted >= start_date):
            symbols.add(symbol)
    return tuple(sorted(symbols))


def _compact_date(value: Any) -> str | None:
    text = str(value or "").replace("-", "").strip()
    return text if len(text) == 8 and text.isdigit() else None


def _load_matching_market_spine(
    repository: FrozenValidationDataRepository,
    *,
    official_freeze_hash: str,
    label_horizon_sessions: int,
) -> FrozenValidationData | None:
    """Resume only a spine cryptographically bound to this freeze and policy."""

    existing = repository.load(require_strict_provenance=True)
    if existing is None or not existing.row_provenance:
        return None
    expected_policy_hash = stable_content_hash({
        "schema": "alphaforge.gateway-market-availability/v1",
        "event_time": "15:00 Asia/Shanghai",
        "available_at": "16:00 Asia/Shanghai",
        "decision_time": "17:00 Asia/Shanghai",
        "label_horizon_sessions": label_horizon_sessions,
    })
    if any(
        item.adapter_manifest_hash != official_freeze_hash
        or item.availability_policy_hash != expected_policy_hash
        for item in existing.row_provenance
    ):
        return None
    return existing


def _api_content_hash(root: Path, api: str) -> str:
    rows = ContentAddressedRowStore(root / "apis" / api / "partitions").load_all()
    return stable_content_hash(rows)


_PROJECTION_FILES = (
    "evidence_dataset_manifest.json",
    "frozen_validation_data.json",
    "projected_records.json",
)


def _projection_root(repository: CampaignEvidenceDatasetRepository, plan_id: str) -> Path:
    # Let the repository's own path guard remain the single path authority.
    return repository._plan_root(plan_id)  # noqa: SLF001 - same bounded persistence unit


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def _write_projection_checkpoint(
    *,
    repository: CampaignEvidenceDatasetRepository,
    plan_id: str,
    bundle_hash: str,
    dynamic_freeze_hash: str,
    required_fields: tuple[str, ...],
    optional_fields: tuple[str, ...],
) -> None:
    plan_root = _projection_root(repository, plan_id)
    artifacts = {}
    for name in _PROJECTION_FILES:
        digest, size = _file_digest(plan_root / name)
        artifacts[name] = {"sha256": digest, "size": size}
    payload = {
        "schema_version": "alphaforge.campaign-projection-checkpoint/v1",
        "plan_id": plan_id,
        "bundle_hash": bundle_hash,
        "dynamic_freeze_hash": dynamic_freeze_hash,
        "required_fields": list(required_fields),
        "optional_fields": list(optional_fields),
        "artifacts": artifacts,
    }
    payload["artifact_hash"] = stable_content_hash(payload)
    _atomic_json(plan_root / "projection_checkpoint.json", payload)


def _load_projection_checkpoint(
    *,
    repository: CampaignEvidenceDatasetRepository,
    plan_id: str,
    dynamic_freeze_hash: str,
    required_fields: tuple[str, ...],
    optional_fields: tuple[str, ...],
) -> str | None:
    plan_root = _projection_root(repository, plan_id)
    checkpoint_path = plan_root / "projection_checkpoint.json"
    if not checkpoint_path.exists():
        return None
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("campaign projection checkpoint is not an object")
    claimed = payload.pop("artifact_hash", None)
    if claimed != stable_content_hash(payload):
        raise ValueError("campaign projection checkpoint hash mismatch")
    expected_identity = (
        payload.get("schema_version") == "alphaforge.campaign-projection-checkpoint/v1"
        and payload.get("plan_id") == plan_id
        and payload.get("dynamic_freeze_hash") == dynamic_freeze_hash
        and tuple(payload.get("required_fields", ())) == required_fields
        and tuple(payload.get("optional_fields", ())) == optional_fields
    )
    if not expected_identity:
        return None
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(_PROJECTION_FILES):
        raise ValueError("campaign projection checkpoint artifact set is invalid")
    for name in _PROJECTION_FILES:
        expected = artifacts[name]
        if not isinstance(expected, dict):
            raise ValueError("campaign projection checkpoint artifact is invalid")
        path = plan_root / name
        if not path.is_file():
            raise ValueError("campaign projection artifact is missing")
        digest, size = _file_digest(path)
        if digest != expected.get("sha256") or size != expected.get("size"):
            raise ValueError("campaign projection artifact hash mismatch")
    manifest_payload = json.loads(
        (plan_root / "evidence_dataset_manifest.json").read_text(encoding="utf-8")
    )
    if not isinstance(manifest_payload, dict):
        raise ValueError("campaign Evidence dataset manifest is invalid")
    if manifest_payload.get("bundle_hash") != payload.get("bundle_hash"):
        raise ValueError("campaign projection bundle hash mismatch")
    return str(payload["bundle_hash"])


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent)
    temporary = Path(handle.name)
    try:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


__all__ = ["ResearchDataGateway", "discover_historical_symbols"]
