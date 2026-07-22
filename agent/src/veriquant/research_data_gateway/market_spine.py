"""Build an Evidence-ready market spine from verified gateway partitions."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from ..data import MarketSnapshot, SnapshotRow, canonical_content_hash
from ..dynamic_acquisition import ContentAddressedRowStore, stable_content_hash
from ..dynamic_data_integration import SOURCE_LABEL
from ..research_validation_pipeline_v2.models import FrozenRowProvenance, FrozenValidationData


CN = ZoneInfo("Asia/Shanghai")
FEATURE_FIELDS = (
    "open", "high", "low", "close", "volume", "amount", "adj_factor",
    "market_cap", "turnover_rate", "pe_ttm", "pb", "float_market_cap",
)


def build_market_spine(
    freeze_root: str | Path,
    *,
    dataset_id: str,
    label_horizon_sessions: int,
) -> FrozenValidationData:
    root = Path(freeze_root).resolve()
    manifest = _verified_manifest(root)
    daily = _indexed(root, "daily", "trade_date")
    adj = _indexed(root, "adj_factor", "trade_date")
    basic = _indexed(root, "daily_basic", "trade_date")
    stock_rows = ContentAddressedRowStore(root / "apis" / "stock_basic" / "partitions").load_all()
    listing = _listing_intervals(stock_rows)
    calendars = _calendars(root)
    sessions = tuple(sorted(set(calendars["SSE"]) & set(calendars["SZSE"])))
    if len(sessions) <= label_horizon_sessions:
        raise ValueError("frozen calendars do not contain enough label sessions")

    eligible_sessions = sessions[:-label_horizon_sessions]
    rows: list[SnapshotRow] = []
    labels: dict[str, float] = {}
    provenance: list[FrozenRowProvenance] = []
    universe: dict[str, tuple[str, ...]] = {}
    calendar_hash = stable_content_hash(calendars)
    universe_members: dict[str, tuple[str, ...]] = {}
    for session_index, session in enumerate(eligible_sessions):
        label_session = sessions[session_index + label_horizon_sessions]
        members: list[str] = []
        for symbol, interval in sorted(listing.items()):
            if not _listed(interval, session):
                continue
            left = (symbol, session)
            right = (symbol, label_session)
            if left not in daily or left not in adj or left not in basic or right not in daily or right not in adj:
                continue
            left_price = _number(daily[left], "close") * _number(adj[left], "adj_factor")
            right_price = _number(daily[right], "close") * _number(adj[right], "adj_factor")
            if left_price == 0:
                continue
            event = _clock(session, 15)
            values = {
                "open": _number(daily[left], "open"),
                "high": _number(daily[left], "high"),
                "low": _number(daily[left], "low"),
                "close": _number(daily[left], "close"),
                "volume": _number(daily[left], "vol"),
                "amount": _number(daily[left], "amount"),
                "adj_factor": _number(adj[left], "adj_factor"),
                "market_cap": _optional_number(basic[left], "total_mv"),
                "turnover_rate": _optional_number(basic[left], "turnover_rate"),
                "pe_ttm": _optional_number(basic[left], "pe_ttm"),
                "pb": _optional_number(basic[left], "pb"),
                "float_market_cap": _optional_number(basic[left], "circ_mv"),
            }
            row = SnapshotRow(event_time=event, symbol=symbol, values=values)
            rows.append(row)
            labels[f"{event.isoformat()}|{symbol}"] = right_price / left_price - 1.0
            members.append(symbol)
        universe_members[session] = tuple(members)
        universe[_clock(session, 15).isoformat()] = tuple(members)
    if not rows:
        raise ValueError("gateway market spine has no complete PIT rows")

    universe_hash = stable_content_hash(universe_members)
    decision_time = _clock(eligible_sessions[-1], 17)
    availability_policy_hash = stable_content_hash({
        "schema": "alphaforge.gateway-market-availability/v1",
        "event_time": "15:00 Asia/Shanghai",
        "available_at": "16:00 Asia/Shanghai",
        "decision_time": "17:00 Asia/Shanghai",
        "label_horizon_sessions": label_horizon_sessions,
    })
    for row in rows:
        source_record_hash = stable_content_hash({
            "daily": daily[(row.symbol, row.event_time.strftime("%Y%m%d"))],
            "adj_factor": adj[(row.symbol, row.event_time.strftime("%Y%m%d"))],
            "daily_basic": basic[(row.symbol, row.event_time.strftime("%Y%m%d"))],
        })
        provenance.append(FrozenRowProvenance(
            symbol=row.symbol,
            event_time=row.event_time,
            published_at=row.event_time.replace(hour=15, minute=30),
            available_at=row.event_time.replace(hour=16, minute=0),
            revision_id=f"gateway-{row.event_time.strftime('%Y%m%d')}-{row.symbol.replace('.', '-')}",
            source_kind="daily",
            source_label=SOURCE_LABEL,
            provider_class=SOURCE_LABEL,
            source_class="TUSHARE_COMPATIBLE_PROXY",
            input_origin="FROZEN_REAL" if manifest.get("source_label") == SOURCE_LABEL else "TEST_FIXTURE",
            captured_at=decision_time,
            source_record_hash=source_record_hash,
            adapter_manifest_hash=str(manifest["artifact_hash"]),
            calendar_hash=calendar_hash,
            universe_hash=universe_hash,
            availability_policy_hash=availability_policy_hash,
            publication_time_policy="DISTINCT_PROVIDER_MARKET_CLOCKS",
        ))
    rows_tuple = tuple(sorted(rows, key=lambda item: (item.event_time, item.symbol)))
    provenance_tuple = tuple(sorted(provenance, key=lambda item: (item.event_time, item.symbol, item.available_at, item.revision_id)))
    coverage = {
        field: sum(row.values.get(field) is not None for row in rows_tuple) / len(rows_tuple)
        for field in FEATURE_FIELDS
    }
    snapshot = MarketSnapshot(
        dataset_id=dataset_id,
        decision_time=decision_time,
        start_time=rows_tuple[0].event_time,
        fields=FEATURE_FIELDS,
        rows=rows_tuple,
        historical_universe=universe,
        coverage_by_field=coverage,
    )
    qualification_hash = canonical_content_hash({
        "gateway_manifest_hash": manifest["artifact_hash"],
        "calendar_hash": calendar_hash,
        "universe_hash": universe_hash,
        "snapshot_hash": snapshot.artifact_hash,
        "labels_hash": canonical_content_hash(labels),
    })
    return FrozenValidationData(
        snapshot=snapshot,
        labels=labels,
        qualification_hash=qualification_hash,
        source_label=SOURCE_LABEL,
        row_provenance=provenance_tuple,
    )


def _verified_manifest(root: Path):
    import json
    payload = json.loads((root / "freeze_manifest.json").read_text(encoding="utf-8"))
    claimed = payload.pop("artifact_hash", None)
    if claimed != stable_content_hash(payload):
        raise ValueError("gateway freeze manifest hash mismatch")
    payload["artifact_hash"] = claimed
    if payload.get("status") != "SUCCESS":
        raise ValueError("gateway freeze is not complete")
    return payload


def _indexed(root: Path, api: str, date_field: str):
    output = {}
    for row in ContentAddressedRowStore(root / "apis" / api / "partitions").load_all():
        symbol = str(row.get("ts_code") or "")
        session = str(row.get(date_field) or "").replace("-", "")
        if symbol and len(session) == 8:
            output[(symbol, session)] = row
    return output


def _listing_intervals(rows):
    output = {}
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        if not (symbol.endswith(".SH") or symbol.endswith(".SZ")):
            continue
        listed = str(row.get("list_date") or "").replace("-", "")
        delisted = str(row.get("delist_date") or "").replace("-", "") or None
        if len(listed) == 8:
            output[symbol] = (listed, delisted)
    return output


def _calendars(root: Path):
    output = {"SSE": [], "SZSE": []}
    for row in ContentAddressedRowStore(root / "apis" / "trade_cal" / "partitions").load_all():
        exchange = str(row.get("exchange") or "")
        session = str(row.get("cal_date") or "").replace("-", "")
        if exchange in output and str(row.get("is_open")) in {"1", "1.0", "True", "true"}:
            output[exchange].append(session)
    if not all(output.values()):
        raise ValueError("both SSE and SZSE frozen calendars are required")
    return {key: tuple(sorted(set(value))) for key, value in output.items()}


def _listed(interval, session):
    listed, delisted = interval
    return listed <= session and (delisted is None or session <= delisted)


def _clock(session: str, hour: int):
    return datetime.strptime(session, "%Y%m%d").replace(hour=hour, tzinfo=CN)


def _number(row, field):
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"required numeric field missing: {field}")
    return float(value)


def _optional_number(row, field):
    value = row.get(field)
    return None if value is None or value == "" else float(value)


__all__ = ["FEATURE_FIELDS", "build_market_spine"]
