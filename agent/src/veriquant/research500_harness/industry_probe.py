"""Credential-safe probe for dated industry membership on the approved proxy.

The probe is deliberately diagnostic only.  It does not qualify the returned
rows for PIT use and never falls back to the current ``stock_basic.industry``
field.  Qualification and panel enrichment are separate, deterministic steps.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from ..approved_proxy_scale_adapter_v2 import AllowlistedProxyScaleClient
from ..integration.capture_research_os_v2_data import _load_proxy_settings
from ..research_toolbox.contracts import canonical_hash
from ..tushare_sdk_proxy import ProxySdkConfig, TushareProxyError, create_proxy_client


SCHEMA_VERSION = "alphaforge.research500-industry-probe/v1"


def probe_industry_history(
    client: AllowlistedProxyScaleClient, *, symbol: str | None
) -> Mapping[str, Any]:
    """Return public capability facts without provider exception text."""

    candidates = (
        (
            "index_member_all",
            (
                "l1_code", "l1_name", "l2_code", "l2_name", "l3_code",
                "l3_name", "ts_code", "name", "in_date", "out_date", "is_new",
            ),
            {"ts_code": symbol} if symbol else {},
        ),
    )
    rows = []
    for api_name, fields, params in candidates:
        try:
            result = client.query(api_name, params=params, fields=fields)
            dated = [row for row in result if row.get("in_date")]
            overlap_summary = _overlap_summary(dated)
            rows.append({
                "api_name": api_name,
                "status": "AVAILABLE" if dated else "EMPTY",
                "row_count": len(result),
                "dated_row_count": len(dated),
                "fields": sorted({str(key) for row in result for key in row}),
                "sample_intervals": [
                    {"in_date": row.get("in_date"), "out_date": row.get("out_date")}
                    for row in dated[:3]
                ],
                **overlap_summary,
            })
        except TushareProxyError as exc:
            rows.append({
                "api_name": api_name,
                "status": "UNAVAILABLE",
                "failure_type": type(exc.__cause__ or exc).__name__,
                "row_count": 0,
                "dated_row_count": 0,
                "fields": [],
                "sample_intervals": [],
                "overlap_symbol_count": 0,
                "overlap_samples": [],
            })
    value = {
        "schema_version": SCHEMA_VERSION,
        "source": "APPROVED_PROXY_DATA_VENDOR",
        "official_tushare": False,
        "scope": {"symbol": symbol} if symbol else {"all_members": True},
        "status": "AVAILABLE" if any(row["dated_row_count"] for row in rows) else "UNAVAILABLE",
        "probes": rows,
    }
    value["artifact_hash"] = canonical_hash(value)
    return value


def _overlap_summary(rows: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_symbol.setdefault(str(row.get("ts_code") or row.get("con_code") or ""), []).append(row)
    samples = []
    overlap_count = 0
    for symbol, values in sorted(by_symbol.items()):
        ordered = sorted(values, key=lambda row: str(row.get("in_date") or ""))
        conflicts = []
        for left, right in zip(ordered, ordered[1:]):
            left_out = str(left.get("out_date") or "99999999")
            if str(right.get("in_date") or "") < left_out:
                conflicts.extend((left, right))
        if not conflicts:
            continue
        overlap_count += 1
        if len(samples) < 5:
            seen = set()
            intervals = []
            for item in conflicts:
                identity = (
                    item.get("in_date"), item.get("out_date"), item.get("l1_code"),
                    item.get("l1_name"), item.get("is_new"),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                intervals.append({
                    "in_date": item.get("in_date"),
                    "out_date": item.get("out_date"),
                    "l1_code": item.get("l1_code"),
                    "l1_name": item.get("l1_name"),
                    "is_new": item.get("is_new"),
                })
            samples.append({"symbol": symbol, "intervals": intervals})
    return {"overlap_symbol_count": overlap_count, "overlap_samples": samples}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe dated industry history")
    parser.add_argument("--dotenv", default="agent/.env")
    parser.add_argument("--symbol", default="000001.SZ")
    parser.add_argument("--all", action="store_true", help="Probe the full membership table")
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    if not args.allow_network:
        print(json.dumps({"status": "BLOCKED", "failure_codes": ["NETWORK_DISABLED"]}, sort_keys=True))
        return 2
    settings = _load_proxy_settings(Path(args.dotenv))
    sdk = create_proxy_client(ProxySdkConfig(
        endpoint=settings["TUSHARE_PROXY_URL"], token=settings["TUSHARE_TOKEN"]
    ))
    value = probe_industry_history(
        AllowlistedProxyScaleClient(sdk), symbol=None if args.all else args.symbol
    )
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0 if value["status"] == "AVAILABLE" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["SCHEMA_VERSION", "probe_industry_history"]
