"""Capture and publish a bounded real A-share snapshot for Research OS V2.

This operator command is network-disabled unless ``--allow-network`` is
supplied.  It reads only the approved proxy endpoint and credential keys from
the selected dotenv file, never serializes them, and publishes only a qualified
``APPROVED_PROXY_DATA_VENDOR`` snapshot.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

from veriquant.approved_proxy_scale_adapter_v2 import (
    AllowlistedProxyScaleClient,
    ApprovedProxyScaleAdapterV2,
    ProxyScaleAcquisitionRequest,
    ProxyScaleAdapterStatus,
)
from veriquant.data import MarketSnapshot, SnapshotRow
from veriquant.integration.research_os_v2 import FrozenValidationDataRepository
from veriquant.real_ashare_scale import FrozenAshareScaleInput
from veriquant.research_real_data_v2 import (
    InMemoryFrozenResearchDataProvider,
    ResearchDataRequest,
    ResearchDataPolicy,
    ResearchDataStatus,
    ResearchRealDataService,
)
from veriquant.research_validation_pipeline_v2 import FrozenValidationData
from veriquant.research_validation_pipeline_v2.models import FrozenRowProvenance
from veriquant.tushare_sdk_proxy.client import ProxySdkConfig, create_proxy_client


CN = timezone(timedelta(hours=8))
SOURCE_LABEL = "APPROVED_PROXY_DATA_VENDOR"
OPERATIONAL_SCHEMA = "alphaforge.research-os-real-data-capture/v1"


def build_validation_data(
    frozen: FrozenAshareScaleInput,
    *,
    qualification_hash: str,
) -> FrozenValidationData:
    """Project the qualified frozen rows into the existing evidence input."""

    declared_labels = tuple(getattr(frozen, "label_fields", ()))
    if not declared_labels and frozen.rows:
        declared_labels = tuple(sorted(frozen.rows[0].labels))
    if len(declared_labels) != 1 or not declared_labels[0].startswith("future_return_"):
        raise ValueError("qualified frozen input requires exactly one future-return label")
    label_field = declared_labels[0]
    eligible_source_rows = tuple(
        row
        for row in frozen.rows
        if row.labels.get(label_field) is not None
    )
    rows = tuple(
        SnapshotRow(
            event_time=row.event_time,
            symbol=row.symbol,
            values={field: row.features.get(field) for field in frozen.feature_fields},
        )
        for row in sorted(
            eligible_source_rows,
            key=lambda item: (item.event_time, item.symbol, item.available_at, item.revision_id),
        )
    )
    if not rows:
        raise ValueError("qualified frozen input has no rows")
    eligible_sessions = {row.session for row in eligible_source_rows}
    event_clock_by_session = {
        row.session: row.event_time.isoformat()
        for row in eligible_source_rows
    }
    historical_universe = {
        event_clock_by_session[item.session]: item.symbols
        for item in frozen.historical_universe
        if item.session in eligible_sessions and item.session in event_clock_by_session
    }
    row_count = len(rows)
    coverage = {
        field: sum(row.values.get(field) is not None for row in rows) / row_count
        for field in frozen.feature_fields
    }
    labels = {
        f"{row.event_time.isoformat()}|{row.symbol}": float(value)
        for row in eligible_source_rows
        if (value := row.labels.get(label_field)) is not None
    }
    row_provenance = tuple(
        FrozenRowProvenance(
            symbol=row.symbol,
            event_time=row.event_time,
            # The approved proxy row exposes one conservative knowledge clock,
            # not a distinct exchange publication timestamp.  Record that fact
            # at capture time instead of inventing a clock for legacy snapshots.
            published_at=row.available_at,
            available_at=row.available_at,
            revision_id=row.revision_id,
            source_kind="daily",
            source_label=SOURCE_LABEL,
            provider_class=frozen.provider_class,
            source_class=frozen.source_class,
            input_origin=frozen.input_origin.value,
            captured_at=frozen.capture_time,
            source_record_hash=row.artifact_hash,
            adapter_manifest_hash=frozen.adapter_manifest_hash,
            calendar_hash=frozen.calendar_hash,
            universe_hash=frozen.universe_hash,
            availability_policy_hash=frozen.availability_policy_hash,
            publication_time_policy="AVAILABLE_AT_CONSERVATIVE_PROXY",
        )
        for row in sorted(
            eligible_source_rows,
            key=lambda item: (item.event_time, item.symbol, item.available_at, item.revision_id),
        )
    )
    last_eligible_session = max(eligible_sessions)
    decision_time = next(
        item.decision_time
        for item in frozen.vintages
        if item.session == last_eligible_session
    )
    snapshot = MarketSnapshot(
        dataset_id=frozen.dataset_id,
        decision_time=decision_time,
        start_time=min(row.event_time for row in rows),
        fields=frozen.feature_fields,
        rows=rows,
        historical_universe=historical_universe,
        coverage_by_field=coverage,
    )
    return FrozenValidationData(
        snapshot=snapshot,
        labels=labels,
        qualification_hash=qualification_hash,
        source_label=SOURCE_LABEL,
        row_provenance=row_provenance,
    )


def capture_and_publish(
    *,
    root: Path,
    dotenv_path: Path,
    dataset_id: str,
    index_code: str,
    start_date: str,
    end_date: str,
    target_symbols: int,
    max_acquisition_symbols: int | None,
    required_sessions: int,
    allow_network: bool,
) -> Mapping[str, object]:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    request = ProxyScaleAcquisitionRequest(
        dataset_id=dataset_id,
        index_code=index_code,
        start_date=start_date,
        end_date=end_date,
        capture_time=datetime.now(CN),
        target_symbols=target_symbols,
        max_acquisition_symbols=max_acquisition_symbols,
        required_sessions=required_sessions,
        allow_network=allow_network,
    )
    if not allow_network:
        record = _failure_record(request, "NETWORK_NOT_ENABLED", "network capture was not explicitly enabled")
        _atomic_json(root / "real_data_capture.json", record)
        return record

    settings = _load_proxy_settings(dotenv_path)
    sdk_client = create_proxy_client(
        ProxySdkConfig(
            endpoint=settings["TUSHARE_PROXY_URL"],
            token=settings["TUSHARE_TOKEN"],
        )
    )
    result = ApprovedProxyScaleAdapterV2(AllowlistedProxyScaleClient(sdk_client)).capture(request)
    if result.status is not ProxyScaleAdapterStatus.READY or result.frozen_input is None:
        code = result.failure_codes[0].value if result.failure_codes else "ADAPTER_UNAVAILABLE"
        record = _failure_record(request, code, result.message)
        _atomic_json(root / "real_data_capture.json", record)
        return record

    frozen = result.frozen_input
    # Qualification scale is bound to the explicit acquisition request.  The
    # default legacy 30x180 smoke profile must not become a hidden product cap
    # or reject a correctly requested smaller/larger Campaign snapshot.
    qualification_policy = ResearchDataPolicy(
        min_symbols_per_session=target_symbols,
        max_symbols_per_session=target_symbols,
        min_sessions=required_sessions,
        recommended_sessions_for_50d=required_sessions,
        max_sessions=max(required_sessions, 320),
    )
    prepared = ResearchRealDataService(policy=qualification_policy).prepare(
        InMemoryFrozenResearchDataProvider(frozen),
        ResearchDataRequest(
            request_id=f"qualify-{dataset_id}",
            dataset_id=dataset_id,
            lookback_sessions=50,
        ),
    )
    if prepared.qualification.status is not ResearchDataStatus.READY:
        codes = [code.value for code in prepared.qualification.failure_codes]
        record = _failure_record(
            request,
            codes[0] if codes else "QUALIFICATION_FAILED",
            prepared.qualification.message,
        )
        record["qualification_failure_codes"] = codes
        _atomic_json(root / "real_data_capture.json", record)
        return record

    validation_data = build_validation_data(
        frozen,
        qualification_hash=prepared.qualification.artifact_hash,
    )
    repository = FrozenValidationDataRepository(root / "data")
    published_hash = repository.save(validation_data)
    daily_counts = tuple(len(item.symbols) for item in frozen.historical_universe)
    acquisition_symbols = {
        row.symbol
        for row in frozen.rows
    }
    record = {
        "schema_version": OPERATIONAL_SCHEMA,
        "status": "READY",
        "operational_status": "IMPLEMENTED_AND_TESTED",
        "source": SOURCE_LABEL,
        "source_class": frozen.source_class,
        "official_tushare": False,
        "request_hash": request.artifact_hash,
        "dataset_id": frozen.dataset_id,
        "calendar_hash": frozen.calendar_hash,
        "universe_hash": frozen.universe_hash,
        "content_hash": frozen.data_hash,
        "api_manifest_hash": frozen.adapter_manifest_hash,
        "qualification_hash": prepared.qualification.artifact_hash,
        "published_validation_hash": published_hash,
        "session_count": len(frozen.calendar_sessions),
        "minimum_daily_symbols": min(daily_counts),
        "maximum_daily_symbols": max(daily_counts),
        "historical_research_symbol_count": len(acquisition_symbols),
        "row_count": len(frozen.rows),
        "label_count": len(validation_data.labels),
        "evaluation_session_count": len(validation_data.snapshot.historical_universe),
        "label_unavailable_tail_sessions": (
            len(frozen.calendar_sessions)
            - len(validation_data.snapshot.historical_universe)
        ),
        "frozen_validation_path": str(repository.path.relative_to(root)),
        "captured_at": frozen.capture_time.isoformat(),
    }
    _atomic_json(root / "real_data_capture.json", record)
    return record


def _load_proxy_settings(path: Path) -> dict[str, str]:
    selected: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key in {"TUSHARE_PROXY_URL", "TUSHARE_TOKEN"}:
                selected[key] = value.strip().strip("\"'")
    for key in ("TUSHARE_PROXY_URL", "TUSHARE_TOKEN"):
        if not selected.get(key):
            selected[key] = os.environ.get(key, "").strip()
        if not selected[key]:
            raise ValueError(f"{key} is not configured")
    return selected


def _failure_record(request: ProxyScaleAcquisitionRequest, code: str, message: str) -> dict[str, object]:
    return {
        "schema_version": OPERATIONAL_SCHEMA,
        "status": "UNAVAILABLE",
        "operational_status": "UNAVAILABLE",
        "source": SOURCE_LABEL,
        "official_tushare": False,
        "request_hash": request.artifact_hash,
        "failure_codes": [code],
        "message": message,
    }


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        json.loads(temporary.read_text(encoding="utf-8"))
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze real approved-proxy data for Research OS V2")
    parser.add_argument("--root", default=".autobuild/veriquant/integration/research_os_v2")
    parser.add_argument("--dotenv", default="agent/.env")
    parser.add_argument("--dataset-id", default="approved-proxy-ashare-dynamic-v3")
    parser.add_argument("--index-code", default="000300.SH")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--target-symbols", type=int, default=30)
    parser.add_argument(
        "--max-acquisition-symbols",
        type=int,
        default=None,
        help="Optional operational acquisition cap; omitted means no product-scale cap.",
    )
    parser.add_argument(
        "--required-sessions",
        type=int,
        default=260,
        help="Research sessions derived by the Campaign planner in product runs.",
    )
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    record = capture_and_publish(
        root=Path(args.root),
        dotenv_path=Path(args.dotenv),
        dataset_id=args.dataset_id,
        index_code=args.index_code,
        start_date=args.start_date,
        end_date=args.end_date,
        target_symbols=args.target_symbols,
        max_acquisition_symbols=args.max_acquisition_symbols,
        required_sessions=args.required_sessions,
        allow_network=args.allow_network,
    )
    print(json.dumps({key: value for key, value in record.items() if key != "message"}, ensure_ascii=False))
    return 0 if record["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
