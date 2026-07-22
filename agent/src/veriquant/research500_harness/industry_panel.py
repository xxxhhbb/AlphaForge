"""Freeze and project dated industry membership into a PIT research panel."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from ..approved_proxy_scale_adapter_v2 import AllowlistedProxyScaleClient
from ..data import MarketSnapshot, SnapshotRow, canonical_content_hash
from ..integration.capture_research_os_v2_data import _load_proxy_settings
from ..integration.research_os_v2 import FrozenValidationDataRepository
from ..research_toolbox.contracts import canonical_hash
from ..tushare_sdk_proxy import ProxySdkConfig, create_proxy_client


SCHEMA_VERSION = "alphaforge.research500-industry-panel/v1"
SOURCE_LABEL = "APPROVED_PROXY_DATA_VENDOR"
AVAILABILITY_POLICY = "NEXT_FROZEN_TRADING_SESSION_AFTER_IN_DATE_V1"
FIELDS = (
    "l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name",
    "ts_code", "name", "in_date", "out_date", "is_new",
)


@dataclass(frozen=True)
class IndustryInterval:
    symbol: str
    member_from: date
    member_until: date | None
    industry_code: str
    industry_name: str
    end_inferred: bool = False

    def canonical(self) -> Mapping[str, Any]:
        return {
            "symbol": self.symbol,
            "member_from": self.member_from.isoformat(),
            "member_until": None if self.member_until is None else self.member_until.isoformat(),
            "industry_code": self.industry_code,
            "industry_name": self.industry_name,
            "end_inferred": self.end_inferred,
        }


def normalize_industry_intervals(rows: Iterable[Mapping[str, Any]]) -> tuple[IndustryInterval, ...]:
    """Validate dates, deduplicate rows and reject ambiguous overlaps."""

    unique: dict[tuple[Any, ...], IndustryInterval] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or "").strip()
        code = str(row.get("l1_code") or "").strip()
        name = str(row.get("l1_name") or "").strip()
        if not symbol or not code or not name:
            continue
        member_from = _provider_date(row.get("in_date"), required=True)
        member_until = _provider_date(row.get("out_date"), required=False)
        if member_until is not None and member_until <= member_from:
            raise ValueError("industry member interval has invalid date order")
        item = IndustryInterval(symbol, member_from, member_until, code, name)
        unique[(symbol, member_from, member_until, code, name)] = item
    parsed = tuple(sorted(
        unique.values(),
        key=lambda item: (
            item.symbol, item.member_from,
            item.member_until or date.max, item.industry_code, item.industry_name,
        ),
    ))
    if not parsed:
        raise ValueError("dated industry membership is empty")
    by_symbol: dict[str, list[IndustryInterval]] = {}
    for item in parsed:
        by_symbol.setdefault(item.symbol, []).append(item)
    normalized: list[IndustryInterval] = []
    for intervals in by_symbol.values():
        for index, item in enumerate(intervals):
            following = intervals[index + 1] if index + 1 < len(intervals) else None
            if following is not None and following.member_from == item.member_from:
                if (following.industry_code, following.industry_name) != (item.industry_code, item.industry_name):
                    raise ValueError("ambiguous same-date industry membership")
            if following is not None and item.member_until is None and following.member_from > item.member_from:
                # The approved source occasionally emits a later effective
                # change while leaving the prior out_date open.  The later
                # in_date is an observable upper bound; closing at that date is
                # deterministic and more conservative than carrying the stale
                # classification forward.
                item = IndustryInterval(
                    item.symbol, item.member_from, following.member_from,
                    item.industry_code, item.industry_name, True,
                )
            if following is not None and item.member_until is not None and following.member_from < item.member_until:
                if (following.industry_code, following.industry_name) != (item.industry_code, item.industry_name):
                    raise ValueError("ambiguous overlapping industry membership")
            normalized.append(item)
    return tuple(sorted(
        normalized,
        key=lambda item: (
            item.symbol, item.member_from,
            item.member_until or date.max, item.industry_code, item.industry_name,
        ),
    ))


def freeze_industry_history(
    client: AllowlistedProxyScaleClient, *, output_path: str | Path
) -> Mapping[str, Any]:
    raw = client.query("index_member_all", params={}, fields=FIELDS)
    intervals = normalize_industry_intervals(raw)
    value = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "dated-industry-history",
        "source": SOURCE_LABEL,
        "official_tushare": False,
        "availability_policy": AVAILABILITY_POLICY,
        "row_count": len(intervals),
        "symbol_count": len({item.symbol for item in intervals}),
        "inferred_end_count": sum(item.end_inferred for item in intervals),
        "rows": [item.canonical() for item in intervals],
    }
    value["artifact_hash"] = canonical_hash(value)
    _atomic_json(Path(output_path), value)
    return value


def enrich_snapshot_with_industry(
    snapshot: MarketSnapshot,
    intervals: Sequence[IndustryInterval],
    *,
    minimum_coverage: float = 0.80,
) -> tuple[MarketSnapshot, Mapping[str, Any]]:
    if "industry" in snapshot.fields:
        raise ValueError("industry field already exists")
    if not 0.0 < minimum_coverage <= 1.0:
        raise ValueError("minimum_coverage must be in (0, 1]")
    sessions = tuple(sorted({row.event_time.date() for row in snapshot.rows}))
    by_symbol: dict[str, list[IndustryInterval]] = {}
    for item in intervals:
        by_symbol.setdefault(item.symbol, []).append(item)
    availability = {
        item: next((session for session in sessions if session > item.member_from), None)
        for item in intervals
    }
    enriched_rows: list[SnapshotRow] = []
    non_null = 0
    for row in snapshot.rows:
        active = [
            item for item in by_symbol.get(row.symbol, ())
            if availability[item] is not None
            and row.event_time.date() >= availability[item]
            and (item.member_until is None or row.event_time.date() < item.member_until)
        ]
        labels = {(item.industry_code, item.industry_name) for item in active}
        if len(labels) > 1:
            raise ValueError("ambiguous industry value at decision time")
        value = next(iter(labels))[1] if labels else None
        values = dict(row.values)
        values["industry"] = value
        non_null += value is not None
        enriched_rows.append(SnapshotRow(
            event_time=row.event_time, symbol=row.symbol, values=values
        ))
    coverage = non_null / len(enriched_rows) if enriched_rows else 0.0
    if coverage < minimum_coverage:
        raise ValueError("dated industry coverage is below the qualified minimum")
    coverage_by_field = dict(snapshot.coverage_by_field)
    coverage_by_field["industry"] = coverage
    enriched = MarketSnapshot(
        dataset_id=snapshot.dataset_id + ".dated-industry",
        decision_time=snapshot.decision_time,
        start_time=snapshot.start_time,
        fields=tuple(snapshot.fields) + ("industry",),
        rows=tuple(enriched_rows),
        historical_universe=snapshot.historical_universe,
        coverage_by_field=coverage_by_field,
    )
    diagnostics = {
        "availability_policy": AVAILABILITY_POLICY,
        "industry_coverage": coverage,
        "matched_row_count": non_null,
        "row_count": len(enriched_rows),
        "matched_symbol_count": len({row.symbol for row in enriched_rows if row.values["industry"] is not None}),
        "panel_symbol_count": len({row.symbol for row in enriched_rows}),
    }
    return enriched, diagnostics


def build_industry_panel(
    *, base_root: str | Path, history_path: str | Path, output_root: str | Path
) -> Mapping[str, Any]:
    base = FrozenValidationDataRepository(Path(base_root).resolve()).load(
        require_strict_provenance=True
    )
    if base is None:
        raise ValueError("base frozen panel is unavailable")
    history = json.loads(Path(history_path).read_text(encoding="utf-8"))
    claimed = history.pop("artifact_hash", None)
    if claimed != canonical_hash(history):
        raise ValueError("industry history hash mismatch")
    intervals = tuple(
        IndustryInterval(
            symbol=str(row["symbol"]),
            member_from=date.fromisoformat(str(row["member_from"])),
            member_until=None if row.get("member_until") is None else date.fromisoformat(str(row["member_until"])),
            industry_code=str(row["industry_code"]),
            industry_name=str(row["industry_name"]),
            end_inferred=bool(row.get("end_inferred", False)),
        )
        for row in history["rows"]
    )
    enriched, diagnostics = enrich_snapshot_with_industry(base.snapshot, intervals)
    qualification_hash = canonical_content_hash({
        "schema_version": SCHEMA_VERSION,
        "base_qualification_hash": base.qualification_hash,
        "base_snapshot_hash": base.snapshot.artifact_hash,
        "industry_history_hash": claimed,
        "inferred_end_count": int(history.get("inferred_end_count", 0)),
        "availability_policy": AVAILABILITY_POLICY,
        "enriched_snapshot_hash": enriched.artifact_hash,
    })
    derived = replace(base, snapshot=enriched, qualification_hash=qualification_hash)
    output = Path(output_root).resolve()
    FrozenValidationDataRepository(output).save(derived)
    registered_receipt_hash = _carry_registered_baselines(
        base_root=Path(base_root).resolve(),
        output_root=output,
        base_snapshot_hash=base.snapshot.artifact_hash,
        derived=derived,
    )
    replay = FrozenValidationDataRepository(output).load(require_strict_provenance=True)
    if replay is None or replay.artifact_hash != derived.artifact_hash:
        raise ValueError("industry panel replay identity mismatch")
    value = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "dated-industry-panel-receipt",
        "status": "PASSED",
        "source": SOURCE_LABEL,
        "official_tushare": False,
        "base_data_hash": base.artifact_hash,
        "industry_history_hash": claimed,
        "data_hash": derived.artifact_hash,
        "snapshot_hash": enriched.artifact_hash,
        "qualification_hash": qualification_hash,
        "replay_verified": True,
        "registered_baselines_receipt_hash": registered_receipt_hash,
        **diagnostics,
    }
    value["artifact_hash"] = canonical_hash(value)
    _atomic_json(output / "industry_panel_receipt.json", value)
    return value


def _carry_registered_baselines(
    *, base_root: Path, output_root: Path, base_snapshot_hash: str, derived: Any
) -> str | None:
    path = base_root / "registered_baselines_receipt.json"
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    claimed = raw.pop("artifact_hash", None)
    if claimed != canonical_hash(raw) or raw.get("status") != "PASSED":
        raise ValueError("registered baseline receipt is invalid")
    if raw.get("snapshot_hash") != base_snapshot_hash:
        raise ValueError("registered baseline receipt belongs to another base snapshot")
    fields = set(derived.snapshot.fields)
    if any(str(item.get("field_name")) not in fields for item in raw.get("factors", ())):
        raise ValueError("registered baseline field is absent after industry enrichment")
    value = dict(raw)
    value.update({
        "base_data_hash": raw.get("data_hash"),
        "data_hash": derived.artifact_hash,
        "snapshot_hash": derived.snapshot.artifact_hash,
        "qualification_hash": derived.qualification_hash,
        "carry_forward_policy": "VALUE_PRESERVING_SNAPSHOT_ENRICHMENT_V1",
        "source_receipt_hash": claimed,
    })
    value["artifact_hash"] = canonical_hash(value)
    _atomic_json(output_root / "registered_baselines_receipt.json", value)
    return value["artifact_hash"]


def _provider_date(value: Any, *, required: bool) -> date | None:
    if value is None or str(value).strip() in {"", "None", "nan", "NaT"}:
        if required:
            raise ValueError("industry interval requires in_date")
        return None
    raw = str(value).strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError("invalid provider industry date")
    return datetime.strptime(raw, "%Y%m%d").date()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze and project dated industry history")
    parser.add_argument("--dotenv", default="agent/.env")
    parser.add_argument("--history-path", required=True)
    parser.add_argument("--base-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    if not args.allow_network:
        print(json.dumps({"status": "BLOCKED", "failure_codes": ["NETWORK_DISABLED"]}, sort_keys=True))
        return 2
    settings = _load_proxy_settings(Path(args.dotenv))
    sdk = create_proxy_client(ProxySdkConfig(
        endpoint=settings["TUSHARE_PROXY_URL"], token=settings["TUSHARE_TOKEN"]
    ))
    history = freeze_industry_history(
        AllowlistedProxyScaleClient(sdk), output_path=args.history_path
    )
    receipt = build_industry_panel(
        base_root=args.base_root, history_path=args.history_path, output_root=args.output_root
    )
    print(json.dumps({
        "status": receipt["status"],
        "history_hash": history["artifact_hash"],
        "history_row_count": history["row_count"],
        "history_symbol_count": history["symbol_count"],
        "data_hash": receipt["data_hash"],
        "industry_coverage": receipt["industry_coverage"],
        "artifact_hash": receipt["artifact_hash"],
    }, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AVAILABILITY_POLICY",
    "IndustryInterval",
    "build_industry_panel",
    "enrich_snapshot_with_industry",
    "freeze_industry_history",
    "normalize_industry_intervals",
]
