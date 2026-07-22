"""Bounded Tencent operational-rehearsal CLI.

This command is auxiliary-only.  It cannot feed official PIT/Evidence,
promotion, CHALLENGER, or the mandatory real-Tushare V1 gate.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .v1_tencent import tencent_workbench_projection
from ..tencent_rehearsal.api import (
    LegacyTencentLoaderTransport,
    TencentBoundedRequest,
    TencentMarketVintageProvider,
    TencentRehearsalError,
    TencentRehearsalSnapshotProvider,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE = "TENCENT_OPERATIONAL_REHEARSAL"
AUTHORITY = "AUXILIARY_RESEARCH_EVIDENCE"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a bounded auxiliary Tencent A-share rehearsal")
    parser.add_argument("--codes", nargs="+", default=("000001.SZ", "600000.SH"))
    parser.add_argument("--start-date", default="2024-01-02")
    parser.add_argument("--end-date", default="2024-03-29")
    parser.add_argument("--calendar-id", default="cn-ashare-sse-szse")
    parser.add_argument("--calendar-hash", required=True)
    parser.add_argument("--dataset-id", default="tencent-operational-smoke")
    parser.add_argument("--output", default=".autobuild/veriquant/integration/v1_tencent_stage")
    parser.add_argument("--enable-network", action="store_true")
    return parser


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _output_root(value: str) -> Path:
    repo = _repo_root()
    allowed = (repo / ".autobuild" / "veriquant" / "integration" / "v1_tencent_stage").resolve()
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise ValueError("output must remain under .autobuild/veriquant/integration/v1_tencent_stage") from exc
    return resolved


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    name = handle.name
    try:
        json.dump(payload, handle, sort_keys=True, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(name):
            os.unlink(name)


def _base_result(*, network_enabled: bool, request_hash: str) -> dict[str, Any]:
    return {
        "schema_version": "veriquant.integration.tencent-smoke/v1",
        "source": SOURCE,
        "authority_class": AUTHORITY,
        "universe_policy": "FIXED_OPERATIONAL_SAMPLE",
        "network_enabled": network_enabled,
        "request_hash": request_hash,
        "adjustment_verdict": "QFQ_ONLY",
        "pit_eligible": False,
        "promotion_eligible": False,
        "official_v1_tushare_smoke": False,
        "changes_official_verdict": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = _output_root(args.output)
    request = TencentBoundedRequest(
        dataset_id=args.dataset_id,
        request_id="bounded-tencent-smoke",
        codes=tuple(sorted(args.codes)),
        start_date=args.start_date,
        end_date=args.end_date,
        calendar_id=args.calendar_id,
        calendar_hash=args.calendar_hash,
    )
    provider = TencentRehearsalSnapshotProvider(
        artifact_root=output / "snapshots",
        calendar_root=_repo_root() / ".autobuild" / "veriquant" / "calendars",
        network_enabled=bool(args.enable_network),
        transport=LegacyTencentLoaderTransport(),
    )
    base = _base_result(network_enabled=bool(args.enable_network), request_hash=request.artifact_hash)
    if not args.enable_network:
        availability = provider.availability(request)
        result = {
            **base,
            "status": availability.status.value,
            "failure_code": availability.failure_code.value if availability.failure_code else None,
            "message": availability.message,
        }
        _atomic_json(output / "tencent_smoke_result.json", result)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    try:
        handle = provider.freeze(request)
        end = date.fromisoformat(args.end_date)
        decision_time = datetime.combine(end + timedelta(days=2), time(9, 30), SHANGHAI)
        vintage = TencentMarketVintageProvider(artifact_root=output / "snapshots").as_of(
            dataset_id=handle.dataset_id,
            snapshot_id=handle.snapshot_id,
            decision_time=decision_time,
        )
        projection = tencent_workbench_projection(vintage)
        result = {
            **base,
            **projection,
            "status": "COMPLETED_AUXILIARY_REHEARSAL",
            "snapshot_id": handle.snapshot_id,
            "manifest_hash": handle.manifest_hash,
            "raw_content_hash": handle.raw_content_hash,
            "normalized_content_hash": handle.normalized_content_hash,
            "calendar_hash": handle.calendar_hash,
            "vintage_hash": vintage.vintage_hash,
            "visible_row_count": len(vintage.rows),
        }
    except (TencentRehearsalError, OSError, ValueError) as exc:
        result = {
            **base,
            "status": "FAILED",
            "failure_code": getattr(getattr(exc, "code", None), "value", "ADAPTER_EXECUTION_FAILED"),
            "message": f"Bounded Tencent rehearsal failed closed ({type(exc).__name__})",
        }
        _atomic_json(output / "tencent_smoke_result.json", result)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 2
    _atomic_json(output / "tencent_smoke_result.json", result)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

