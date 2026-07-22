"""Bounded real-data rehearsal through the reviewed Tushare SDK proxy."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from ..data.ashare import (
    AShareDataRequest,
    FrozenAShareSnapshot,
    FrozenSnapshotStore,
    FrozenTradingCalendarStore,
    TushareHistoricalUniverseProvider,
    VibeAshareLoader,
    VibeAshareSnapshotAdapter,
)
from ..data.bitemporal import canonical_content_hash
from .client import ProxySdkConfig, TushareProxyError, create_proxy_client
from .provider import ProxyMarketDataProvider, RecordingProxyClient


SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE = "TUSHARE_COMPATIBLE_PROXY_SDK"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _output_root(value: str) -> Path:
    allowed = (_repo_root() / ".autobuild/veriquant/integration/v1_tushare_proxy_stage").resolve()
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (_repo_root() / candidate).resolve()
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise TushareProxyError("output escapes the Tushare proxy artifact root") from exc
    return resolved


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=str(path.parent), suffix=".tmp"
    )
    temporary = handle.name
    try:
        json.dump(payload, handle, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a bounded Tushare-compatible SDK rehearsal")
    parser.add_argument("--codes", nargs="+", default=("000001.SZ", "600000.SH"))
    parser.add_argument("--index-code", default="000300.SH")
    parser.add_argument("--start-date", default="2024-01-02")
    parser.add_argument("--end-date", default="2024-03-29")
    parser.add_argument("--decision-time", default="2024-04-01T10:00:00+08:00")
    parser.add_argument("--calendar-manifest", required=True)
    parser.add_argument("--output", default=".autobuild/veriquant/integration/v1_tushare_proxy_stage")
    parser.add_argument("--enable-network", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = _output_root(args.output)
    if not args.enable_network:
        result = {
            "schema_version": "veriquant.tushare-proxy-smoke/v1",
            "status": "OFFLINE_DISABLED",
            "source": SOURCE,
            "official_v1_tushare_smoke": False,
        }
        _atomic_json(output / "tushare_proxy_smoke_result.json", result)
        print(json.dumps(result, sort_keys=True))
        return 0
    codes = tuple(sorted(set(args.codes)))
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    if not 1 <= len(codes) <= 3 or end < start or (end - start).days > 366:
        raise TushareProxyError("bounded symbol/date limits are required")
    settings = dotenv_values(_repo_root() / "agent/.env")
    config = ProxySdkConfig(
        endpoint=(settings.get("TUSHARE_PROXY_URL") or "").strip(),
        token=(settings.get("TUSHARE_TOKEN") or "").strip(),
    )
    recorder = RecordingProxyClient(create_proxy_client(config))
    calendar_path = Path(args.calendar_manifest).resolve()
    calendar_root = _repo_root() / ".autobuild/veriquant/calendars"
    calendar = FrozenTradingCalendarStore(calendar_root).load_manifest(calendar_path)
    decision_time = datetime.fromisoformat(args.decision_time)
    if decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise TushareProxyError("decision_time must include an explicit timezone")
    request = AShareDataRequest(
        dataset_id="tushare-proxy-operational-smoke",
        codes=codes,
        index_code=args.index_code,
        start_time=datetime.combine(start, time(15), SHANGHAI),
        end_time=datetime.combine(end, time(15), SHANGHAI),
        frequency="1D",
        adjustment="raw",
        source="tushare",
        decision_time=decision_time,
        fields=("open", "high", "low", "close", "volume", "amount"),
        data_version="tushare-compatible-proxy-sdk/v1",
        cache_policy="reuse-identical",
    )
    previous_network = os.environ.get("ENABLE_ASHARE_NETWORK")
    os.environ["ENABLE_ASHARE_NETWORK"] = "1"
    try:
        built = VibeAshareSnapshotAdapter(
            loader=VibeAshareLoader(
                provider=ProxyMarketDataProvider(recorder), trading_calendar=calendar
            ),
            universe_provider=TushareHistoricalUniverseProvider(client=recorder),
            store=FrozenSnapshotStore(output / "snapshots"),
            trading_calendar=calendar,
        ).build(request)
    finally:
        if previous_network is None:
            os.environ.pop("ENABLE_ASHARE_NETWORK", None)
        else:
            os.environ["ENABLE_ASHARE_NETWORK"] = previous_network
    if not isinstance(built, FrozenAShareSnapshot):
        result = {
            "schema_version": "veriquant.tushare-proxy-smoke/v1",
            "status": getattr(built, "status", "FAILED"),
            "failure_code": getattr(getattr(built, "code", None), "value", None),
            "source": SOURCE,
            "official_v1_tushare_smoke": False,
        }
        _atomic_json(output / "tushare_proxy_smoke_result.json", result)
        print(json.dumps(result, sort_keys=True))
        return 2
    capture = {
        "schema_version": "veriquant.tushare-proxy-sdk-capture/v1",
        "source": SOURCE,
        "endpoint_host": urlsplit(config.endpoint).hostname,
        "request_hash": request.artifact_hash,
        "calls": recorder.captures,
    }
    capture_hash = canonical_content_hash(capture)
    capture_path = output / "captures" / capture_hash.removeprefix("sha256:") / "capture.json"
    _atomic_json(capture_path, capture)
    result = {
        "schema_version": "veriquant.tushare-proxy-smoke/v1",
        "status": "FROZEN_PROXY_REHEARSAL",
        "source": SOURCE,
        "authority_class": "AUXILIARY_RESEARCH_EVIDENCE",
        "official_v1_tushare_smoke": False,
        "snapshot_hash": built.snapshot_hash,
        "dataset_card_hash": built.dataset_card.artifact_hash,
        "calendar_hash": built.trading_calendar.artifact_hash if built.trading_calendar else None,
        "universe_hash": canonical_content_hash(built.historical_universe),
        "capture_hash": capture_hash,
        "record_count": built.dataset_card.record_count,
        "symbols": list(built.dataset_card.universe),
        "sdk_call_count": len(recorder.captures),
        "api_names": sorted({item["api_name"] for item in recorder.captures}),
    }
    _atomic_json(output / "tushare_proxy_smoke_result.json", result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
