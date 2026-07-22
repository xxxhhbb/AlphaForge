"""Operator-triggered preparation of query-specific approved-proxy data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
import json
from pathlib import Path
import tempfile

from ..approved_proxy_scale_adapter_v2 import (
    ApprovedProxyScaleAdapterV2,
    ProxyScaleAcquisitionRequest,
    ProxyScaleAdapterStatus,
)
from ..data import canonical_content_hash
from ..dynamic_acquisition import ResourceBudget
from ..dynamic_data_planning import UniverseKind
from ..dynamic_data_integration import (
    CampaignEvidenceDatasetBuilder,
    DynamicCampaignDataFreezer,
)
from ..research_real_data_v2 import (
    DEFAULT_OPTIONAL_FEATURE_FIELDS,
    PRICE_CORE_FEATURE_FIELDS,
)
from ..real_ashare_scale import FrozenAshareScaleInput
from .capture_research_os_v2_data import build_validation_data


class CampaignDataPreparationStatus(str, Enum):
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class CampaignDataPreparationResult:
    status: CampaignDataPreparationStatus
    plan_id: str
    data_plan_hash: str
    dataset_bundle_hash: str | None
    snapshot_hash: str | None
    freeze_manifest_hash: str | None
    symbol_count: int
    session_count: int
    fields: tuple[str, ...]
    failure_codes: tuple[str, ...]
    capability_status: str = "UNAVAILABLE"
    support_level: str = "UNAVAILABLE"
    missing_required_fields: tuple[str, ...] = ()


class CampaignDataPreparationService:
    """Build and bind data; it owns neither credentials nor Evidence authority."""

    def __init__(
        self,
        *,
        root: str | Path,
        scale_adapter: ApprovedProxyScaleAdapterV2,
        dynamic_transport: object,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.scale_adapter = scale_adapter
        self.dynamic_transport = dynamic_transport

    def prepare(
        self,
        *,
        product_service,
        plan_id: str,
        end_date: str,
        capture_time: datetime,
        index_code: str | None = None,
        target_symbols: int | None = None,
        max_acquisition_symbols: int | None = None,
        allow_network: bool = False,
        resource_budget: ResourceBudget | None = None,
    ) -> CampaignDataPreparationResult:
        _validate_date(end_date)
        _draft, _intent, data_plan, requirement = product_service.data_preparation_spec(plan_id)
        repository = getattr(product_service, "campaign_data_repository", None)
        if repository is not None:
            existing = repository.load(plan_id)
            if existing is not None:
                view = product_service.plan_view(plan_id)
                scope = view["dataScope"]
                return CampaignDataPreparationResult(
                    status=CampaignDataPreparationStatus.READY,
                    plan_id=plan_id,
                    data_plan_hash=data_plan.plan_hash,
                    dataset_bundle_hash=existing.artifact_hash,
                    snapshot_hash=existing.frozen_data.snapshot.artifact_hash,
                    freeze_manifest_hash=existing.manifest.dynamic_freeze_hash,
                    symbol_count=existing.manifest.symbol_count,
                    session_count=existing.manifest.session_count,
                    fields=existing.manifest.fields,
                    failure_codes=(),
                    capability_status=str(scope["capabilityStatus"]),
                    support_level=str(scope["supportLevel"]),
                    missing_required_fields=existing.manifest.missing_required_fields,
                )
        required_sessions = requirement.minimum_sessions
        planned_universe = data_plan.universe
        if planned_universe.kind is not UniverseKind.HISTORICAL_INDEX_CONSTITUENTS:
            return CampaignDataPreparationResult(
                status=CampaignDataPreparationStatus.BLOCKED,
                plan_id=plan_id,
                data_plan_hash=data_plan.plan_hash,
                dataset_bundle_hash=None,
                snapshot_hash=None,
                freeze_manifest_hash=None,
                symbol_count=0,
                session_count=0,
                fields=(),
                failure_codes=(f"UNIVERSE_ADAPTER_UNAVAILABLE:{planned_universe.kind.value}",),
            )
        planned_index = str(planned_universe.index_code)
        if index_code is not None and index_code != planned_index:
            return CampaignDataPreparationResult(
                status=CampaignDataPreparationStatus.BLOCKED,
                plan_id=plan_id,
                data_plan_hash=data_plan.plan_hash,
                dataset_bundle_hash=None,
                snapshot_hash=None,
                freeze_manifest_hash=None,
                symbol_count=0,
                session_count=0,
                fields=(),
                failure_codes=("UNIVERSE_OVERRIDE_MISMATCH",),
            )
        selected_symbols = target_symbols or max(20, requirement.minimum_cross_section)
        start = (
            datetime.strptime(end_date, "%Y%m%d").date()
            - timedelta(days=max(365, required_sessions * 2))
        ).strftime("%Y%m%d")
        capture_request = ProxyScaleAcquisitionRequest(
                dataset_id=f"campaign-{plan_id}",
                index_code=planned_index,
                start_date=start,
                end_date=end_date,
                capture_time=capture_time,
                target_symbols=selected_symbols,
                max_acquisition_symbols=max_acquisition_symbols,
                required_sessions=required_sessions,
                label_horizon_sessions=requirement.label_horizon_sessions,
                allow_network=allow_network,
                required_feature_fields=PRICE_CORE_FEATURE_FIELDS,
                optional_feature_fields=DEFAULT_OPTIONAL_FEATURE_FIELDS,
            )
        spine_cache = self.root / plan_id / "market-spine.json"
        cache_identity = canonical_content_hash({
            "schema_version": "alphaforge.campaign-market-spine-cache/v1",
            "dataset_id": capture_request.dataset_id,
            "index_code": capture_request.index_code,
            "start_date": capture_request.start_date,
            "end_date": capture_request.end_date,
            "target_symbols": capture_request.target_symbols,
            "max_acquisition_symbols": capture_request.max_acquisition_symbols,
            "required_sessions": capture_request.required_sessions,
            "label_horizon_sessions": capture_request.label_horizon_sessions,
            "required_feature_fields": capture_request.required_feature_fields,
            "optional_feature_fields": capture_request.optional_feature_fields,
        })
        try:
            spine = _load_spine_cache(spine_cache, cache_identity)
        except (OSError, ValueError, TypeError, KeyError):
            return CampaignDataPreparationResult(
                status=CampaignDataPreparationStatus.BLOCKED,
                plan_id=plan_id,
                data_plan_hash=data_plan.plan_hash,
                dataset_bundle_hash=None,
                snapshot_hash=None,
                freeze_manifest_hash=None,
                symbol_count=0,
                session_count=0,
                fields=(),
                failure_codes=("MARKET_SPINE_CACHE_CORRUPT",),
            )
        capture = None
        if spine is None:
            capture = self.scale_adapter.capture(capture_request)
        if spine is None and (
            capture is None
            or capture.status is not ProxyScaleAdapterStatus.READY
            or capture.frozen_input is None
        ):
            return CampaignDataPreparationResult(
                status=(
                    CampaignDataPreparationStatus.UNAVAILABLE
                    if capture is not None and capture.status is ProxyScaleAdapterStatus.UNAVAILABLE
                    else CampaignDataPreparationStatus.BLOCKED
                ),
                plan_id=plan_id,
                data_plan_hash=data_plan.plan_hash,
                dataset_bundle_hash=None,
                snapshot_hash=None,
                freeze_manifest_hash=None,
                symbol_count=0,
                session_count=0,
                fields=(),
                failure_codes=tuple(item.value for item in capture.failure_codes) if capture is not None else ("MARKET_SPINE_UNAVAILABLE",),
            )
        if spine is None:
            assert capture is not None and capture.frozen_input is not None
            spine = capture.frozen_input
            _save_spine_cache(spine_cache, cache_identity, spine)
        base = build_validation_data(
            spine,
            qualification_hash=canonical_content_hash({
                "schema_version": "alphaforge.campaign-market-spine/v1",
                "request_hash": capture_request.artifact_hash,
                "input_hash": spine.artifact_hash,
                "api_manifest_hash": spine.adapter_manifest_hash,
            }),
        )
        symbols = tuple(sorted({symbol for item in spine.historical_universe for symbol in item.symbols}))
        freeze_root = self.root / plan_id / "dynamic-freeze"
        frozen = DynamicCampaignDataFreezer(freeze_root).freeze(
            plan=data_plan,
            transport=self.dynamic_transport,
            start_date=start,
            end_date=end_date,
            symbols=symbols,
            allow_network=allow_network,
            budget=resource_budget,
        )
        if frozen.status != "SUCCESS":
            return CampaignDataPreparationResult(
                status=CampaignDataPreparationStatus.BLOCKED,
                plan_id=plan_id,
                data_plan_hash=data_plan.plan_hash,
                dataset_bundle_hash=None,
                snapshot_hash=None,
                freeze_manifest_hash=frozen.manifest_hash,
                symbol_count=len(symbols),
                session_count=len(spine.calendar_sessions),
                fields=base.snapshot.fields,
                failure_codes=(f"DYNAMIC_FREEZE_{frozen.status}",),
            )
        bundle = CampaignEvidenceDatasetBuilder().build(
            base=base,
            freeze_root=freeze_root,
            required_fields=_snapshot_required_fields(requirement.required_fields),
        )
        binding = product_service.bind_plan_data(plan_id, bundle)
        scope = binding["dataScope"]
        return CampaignDataPreparationResult(
            status=CampaignDataPreparationStatus.READY,
            plan_id=plan_id,
            data_plan_hash=data_plan.plan_hash,
            dataset_bundle_hash=bundle.artifact_hash,
            snapshot_hash=bundle.frozen_data.snapshot.artifact_hash,
            freeze_manifest_hash=frozen.manifest_hash,
            symbol_count=bundle.manifest.symbol_count,
            session_count=bundle.manifest.session_count,
            fields=bundle.manifest.fields,
            failure_codes=(),
            capability_status=str(scope["capabilityStatus"]),
            support_level=str(scope["supportLevel"]),
            missing_required_fields=bundle.manifest.missing_required_fields,
        )


def _validate_date(value: str) -> None:
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("end_date must be YYYYMMDD") from exc
    if parsed > date.today():
        raise ValueError("end_date cannot be in the future")


def _snapshot_required_fields(fields: tuple[str, ...]) -> tuple[str, ...]:
    """Map requirement vocabulary to materialized snapshot columns."""

    virtual = {"ts_code", "trade_date", "date"}
    aliases = {
        "vol": "volume",
        "total_mv": "market_cap",
        "circ_mv": "float_market_cap",
        "n_income": "net_income",
        "n_income_attr_p": "net_income_parent",
        "n_cashflow_act": "operating_cashflow",
        "grossprofit_margin": "gross_margin",
        "netprofit_margin": "net_margin",
        "assets_turn": "asset_turnover",
        "or_yoy": "revenue_yoy",
        "netprofit_yoy": "net_income_yoy",
        "ocf_to_or": "operating_cashflow_to_revenue",
        "bps": "book_value_per_share",
        "net_mf_amount": "net_moneyflow_amount",
        "on": "shibor_overnight",
        "1w": "shibor_1w",
        "pct_chg": "index_pct_change",
        "p_change_min": "forecast_profit_change_min",
        "p_change_max": "forecast_profit_change_max",
    }
    return tuple(sorted({aliases.get(field, field) for field in fields if field not in virtual}))


def _load_spine_cache(path: Path, identity: str) -> FrozenAshareScaleInput | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("cache_identity") != identity:
        raise ValueError("market spine cache identity mismatch")
    raw = payload.get("frozen_input")
    if not isinstance(raw, dict):
        raise ValueError("market spine cache payload is missing")
    spine = FrozenAshareScaleInput.from_payload(raw)
    if payload.get("artifact_hash") != spine.artifact_hash:
        raise ValueError("market spine cache hash mismatch")
    return spine


def _save_spine_cache(path: Path, identity: str, spine: FrozenAshareScaleInput) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "alphaforge.campaign-market-spine-cache/v1",
        "cache_identity": identity,
        "artifact_hash": spine.artifact_hash,
        "frozen_input": spine.to_dict(),
    }
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


__all__ = [
    "CampaignDataPreparationResult",
    "CampaignDataPreparationService",
    "CampaignDataPreparationStatus",
]
