"""Question-driven approved-proxy acquisition and bitemporal freezing.

The service owns no credentials. A reviewed transport is injected by the
operational entrypoint, and network access must be enabled explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from ..bitemporal_features import BitemporalStore, normalize_rows
from ..dynamic_acquisition import (
    AcquisitionScope,
    CheckpointStore,
    ContentAddressedRowStore,
    DEFAULT_REGISTRY,
    DynamicAcquirer,
    PartitionStatus,
    ResourceBudget,
    build_plan,
    stable_content_hash,
)
from ..dynamic_data_planning import DynamicDataPlan


SOURCE_LABEL = "APPROVED_PROXY_DATA_VENDOR"
SCHEMA_VERSION = "veriquant.dynamic-data-freeze/v1"


_FIELDS: dict[str, tuple[str, ...]] = {
    "daily": (
        "ts_code", "trade_date", "open", "high", "low", "close", "vol",
        "amount",
    ),
    "adj_factor": ("ts_code", "trade_date", "adj_factor"),
    "daily_basic": (
        "ts_code", "trade_date", "turnover_rate", "pe_ttm", "pb",
        "total_mv", "circ_mv",
    ),
    "trade_cal": ("exchange", "cal_date", "is_open", "pretrade_date"),
    "stock_basic": (
        "ts_code", "industry", "list_date", "delist_date", "list_status",
    ),
    "income": (
        "ts_code", "ann_date", "f_ann_date", "end_date", "revenue",
        "n_income", "n_income_attr_p", "update_flag",
    ),
    "balancesheet": (
        "ts_code", "ann_date", "f_ann_date", "end_date", "total_assets",
        "total_liab", "total_hldr_eqy_exc_min_int", "money_cap", "update_flag",
    ),
    "cashflow": (
        "ts_code", "ann_date", "f_ann_date", "end_date",
        "n_cashflow_act", "n_cashflow_inv_act", "n_cash_flows_fnc_act",
        "update_flag",
    ),
    "fina_indicator": (
        "ts_code", "ann_date", "end_date", "roe", "roa",
        "grossprofit_margin", "netprofit_margin", "debt_to_assets",
        "current_ratio", "quick_ratio", "assets_turn", "or_yoy",
        "netprofit_yoy", "ocf_to_or", "bps", "update_flag",
    ),
    "forecast": (
        "ts_code", "ann_date", "end_date", "type", "p_change_min",
        "p_change_max", "net_profit_min", "net_profit_max",
    ),
    "express": (
        "ts_code", "ann_date", "end_date", "revenue", "operate_profit",
        "total_profit", "n_income", "total_assets", "diluted_eps",
        "diluted_roe", "yoy_net_profit",
    ),
    "moneyflow": (
        "ts_code", "trade_date", "buy_lg_amount", "sell_lg_amount",
        "buy_elg_amount", "sell_elg_amount", "net_mf_vol", "net_mf_amount",
    ),
    "index_daily": (
        "ts_code", "trade_date", "close", "pct_chg", "vol", "amount",
    ),
    "shibor": ("date", "on", "1w", "1m", "3m", "6m", "1y"),
}

_FEATURE_APIS = frozenset({
    "daily",
    "adj_factor",
    "daily_basic",
    "income",
    "balancesheet",
    "cashflow",
    "fina_indicator",
    "forecast",
    "express",
    "moneyflow",
    "index_daily",
    "shibor",
})

# Approved-proxy endpoints that support complete-market date queries without a
# caller-supplied ``ts_code``.  They must share one AcquisitionPlan so the
# deterministic acquirer can apply bounded concurrency across date partitions.
# Creating one independent run per date serialized the old full-market path;
# leaving the remaining APIs symbol-scoped multiplied a freeze by the complete
# historical universe.
_FULL_MARKET_EXACT_DATE_APIS = frozenset({
    "daily",
    "daily_basic",
})

_FULL_MARKET_SYMBOL_BATCH_APIS = frozenset({
    "adj_factor",
    "moneyflow",
})

# ``express`` has been verified against the approved proxy with a complete
# market date-range query.  Per-symbol queries returned false EMPTY results,
# while one bounded range returned the actual cross-section below the provider
# row ceiling.  Keep it separate from exact trading-day market endpoints.
_FULL_MARKET_RANGE_APIS = frozenset({"express"})

# The approved proxy has been verified to require exactly one ``ts_code`` for
# these endpoints.  A comma-separated shard returns an empty response and a
# query without ``ts_code`` is rejected, so batching would silently turn real
# financial/event coverage into false EMPTY receipts.
_FULL_MARKET_PER_SYMBOL_APIS = frozenset({
    "income",
    "balancesheet",
    "cashflow",
    "fina_indicator",
    "forecast",
})

_FULL_MARKET_DATE_APIS = (
    _FULL_MARKET_EXACT_DATE_APIS
    | _FULL_MARKET_SYMBOL_BATCH_APIS
    | _FULL_MARKET_RANGE_APIS
    | _FULL_MARKET_PER_SYMBOL_APIS
)


@dataclass(frozen=True, slots=True)
class DynamicDataFreezeResult:
    status: str
    source_label: str
    plan_hash: str
    manifest_hash: str
    manifest_path: str
    api_statuses: Mapping[str, str]
    row_counts: Mapping[str, int]
    normalized_record_count: int
    normalized_store_hash: str
    api_calls_used: int
    planned_partition_count: int


class DynamicCampaignDataFreezer:
    """Acquire exactly the compiled scope; never shorten it silently."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def freeze(
        self,
        *,
        plan: DynamicDataPlan,
        transport: object,
        start_date: str,
        end_date: str,
        symbols: tuple[str, ...],
        index_codes: tuple[str, ...] = ("000001.SH",),
        acquisition_mode: str = "SYMBOL",
        allow_network: bool = False,
        budget: ResourceBudget | None = None,
    ) -> DynamicDataFreezeResult:
        if not isinstance(plan, DynamicDataPlan):
            raise ValueError("plan must be DynamicDataPlan")
        if acquisition_mode not in {"SYMBOL", "FULL_MARKET"}:
            raise ValueError("acquisition_mode must be SYMBOL or FULL_MARKET")
        selected_symbols = tuple(sorted(set(symbols)))
        selected_indexes = tuple(sorted(set(index_codes)))
        if not selected_indexes or any(not value or "." not in value for value in selected_indexes):
            raise ValueError("index_codes must contain explicit market identifiers")
        raw_requested = tuple(
            api for api in plan.requested_api_names if api in _FIELDS
        )
        requested = tuple(
            api for api in ("trade_cal", "stock_basic") if api in raw_requested
        ) + tuple(
            api for api in raw_requested if api not in {"trade_cal", "stock_basic"}
        )
        unsupported = tuple(
            api
            for api in plan.requested_api_names
            if api and api not in _FIELDS and api != "index_weight"
        )
        if unsupported:
            raise ValueError(
                "compiled plan contains APIs without a frozen adapter: "
                + ",".join(unsupported)
            )
        symbol_scoped = tuple(
            api
            for api in requested
            if DEFAULT_REGISTRY.get(api).pagination.value == "SYMBOL_DATE"
            and api != "index_daily"
        )
        if symbol_scoped and not selected_symbols and acquisition_mode == "SYMBOL":
            raise ValueError(
                "symbol-scoped acquisition requires an explicit or discovered universe"
            )

        statuses: dict[str, str] = {}
        row_counts: dict[str, int] = {}
        run_hashes: dict[str, str] = {}
        row_stores: dict[str, ContentAddressedRowStore] = {}
        reference_partitions: dict[str, tuple[str, ...]] = {}
        frozen_sessions: tuple[str, ...] = ()
        api_calls_used: dict[str, int] = {}
        planned_partition_counts: dict[str, int] = {}
        remaining_api_calls = budget.max_api_calls if budget else None
        for api_name in requested:
            api_root = self.root / "apis" / api_name
            row_store = ContentAddressedRowStore(api_root / "partitions")
            row_stores[api_name] = row_store
            variants = _scope_variants(
                api_name,
                start_date=start_date,
                end_date=end_date,
                symbols=selected_symbols,
                index_codes=selected_indexes,
                acquisition_mode=acquisition_mode,
                trading_sessions=frozen_sessions,
            )
            reference_partitions[api_name] = tuple(label for label, _scope_value in variants)
            variant_statuses: list[PartitionStatus] = []
            variant_hashes: dict[str, str] = {}
            total_rows = 0
            for label, scope in variants:
                acquisition_plan = build_plan(
                    scope,
                    budget=budget,
                    # A complete-market response can approach the provider row
                    # ceiling in one session. Never combine dates and risk
                    # silent truncation. The single plan still contains every
                    # date partition, enabling bounded parallel execution and
                    # partition-level resume.
                    date_chunk_days=(
                        1
                        if acquisition_mode == "FULL_MARKET"
                        and api_name in _FULL_MARKET_EXACT_DATE_APIS
                        else 20
                        if acquisition_mode == "FULL_MARKET"
                        and api_name in {"adj_factor", "moneyflow"}
                        else 365
                    ),
                    symbol_chunk_size=(
                        1
                        if acquisition_mode == "FULL_MARKET"
                        and api_name in _FULL_MARKET_PER_SYMBOL_APIS
                        else
                        100
                        if acquisition_mode == "FULL_MARKET"
                        and api_name in _FULL_MARKET_SYMBOL_BATCH_APIS
                        else 1
                    ),
                )
                planned_partition_counts[api_name] = (
                    planned_partition_counts.get(api_name, 0)
                    + acquisition_plan.estimate.partition_count
                )
                checkpoint_name = "checkpoint.json" if label == "default" else f"checkpoint-{label}.json"
                result = DynamicAcquirer(
                    transport,
                    checkpoint=CheckpointStore(api_root / checkpoint_name),
                    row_sink=row_store,
                    source_label=SOURCE_LABEL,
                    max_attempts=2,
                    continue_after_partition_failure=True,
                ).run(
                    acquisition_plan,
                    budget=(
                        replace(budget, max_api_calls=remaining_api_calls)
                        if budget is not None
                        else None
                    ),
                    allow_network=allow_network,
                )
                variant_statuses.append(result.status)
                api_calls_used[api_name] = (
                    api_calls_used.get(api_name, 0) + result.api_calls_used
                )
                if remaining_api_calls is not None:
                    remaining_api_calls = max(
                        0, remaining_api_calls - result.api_calls_used
                    )
                total_rows += sum(item.row_count for item in result.partitions)
                variant_hashes[label] = result.manifest_hash
            statuses[api_name] = _aggregate_status(variant_statuses).value
            row_counts[api_name] = total_rows
            run_hashes[api_name] = stable_content_hash(variant_hashes)
            if api_name == "trade_cal" and statuses[api_name] == PartitionStatus.SUCCESS.value:
                frozen_sessions = tuple(sorted({
                    str(row.get("cal_date") or "").replace("-", "")
                    for row in row_store.load_all()
                    if str(row.get("is_open")) in {"1", "1.0", "True", "true"}
                    and start_date <= str(row.get("cal_date") or "").replace("-", "") <= end_date
                }))

        calendar_rows = (
            row_stores["trade_cal"].load_all()
            if statuses.get("trade_cal") == PartitionStatus.SUCCESS.value
            else ()
        )
        trading_sessions = tuple(
            sorted({
                datetime.strptime(str(row["cal_date"]), "%Y%m%d").date()
                for row in calendar_rows
                if str(row.get("is_open")) in {"1", "1.0", "True", "true"}
            })
        )
        normalized = BitemporalStore()
        for api_name in requested:
            if (
                statuses.get(api_name)
                in {PartitionStatus.SUCCESS.value, PartitionStatus.EMPTY.value}
                and api_name in _FEATURE_APIS
            ):
                batch = normalize_rows(
                    api_name,
                    row_stores[api_name].load_all(),
                    trading_sessions=trading_sessions or None,
                )
                normalized.append(batch.records)

        terminal = _terminal_status(statuses)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": terminal,
            "source_label": SOURCE_LABEL,
            "official_tushare": False,
            "data_plan_hash": plan.plan_hash,
            "registry_version": DEFAULT_REGISTRY.version,
            "requested_start_date": start_date,
            "requested_end_date": end_date,
            "requested_symbol_count": len(selected_symbols),
            "requested_symbols_hash": stable_content_hash(selected_symbols),
            "requested_index_codes": list(selected_indexes),
            "requested_index_codes_hash": stable_content_hash(selected_indexes),
            "acquisition_mode": acquisition_mode,
            # Only APIs actually executed by this freezer belong here.  The
            # price/universe spine may satisfy index_weight separately via the
            # approved scale adapter, so it is recorded as an external
            # reference rather than being presented as a completed request.
            "requested_apis": list(requested),
            "external_reference_apis": [
                api for api in plan.requested_api_names if api == "index_weight"
            ],
            "api_statuses": dict(sorted(statuses.items())),
            "api_row_counts": dict(sorted(row_counts.items())),
            "api_manifest_hashes": dict(sorted(run_hashes.items())),
            "api_calls_used": dict(sorted(api_calls_used.items())),
            "total_api_calls_used": sum(api_calls_used.values()),
            "planned_partition_counts": dict(sorted(planned_partition_counts.items())),
            "planned_partition_count": sum(planned_partition_counts.values()),
            "reference_partitions": {
                key: list(value)
                for key, value in sorted(reference_partitions.items())
            },
            "calendar_hash": stable_content_hash(calendar_rows),
            "calendar_session_count": len(trading_sessions),
            "universe_reference_hash": stable_content_hash(
                row_stores["stock_basic"].load_all()
                if "stock_basic" in row_stores
                else ()
            ),
            "normalized_record_count": len(normalized.records),
            "normalized_store_hash": normalized.artifact_hash,
        }
        manifest_hash = stable_content_hash(manifest)
        manifest["artifact_hash"] = manifest_hash
        manifest_path = self.root / "freeze_manifest.json"
        _atomic_json(manifest_path, manifest)
        return DynamicDataFreezeResult(
            status=terminal,
            source_label=SOURCE_LABEL,
            plan_hash=plan.plan_hash,
            manifest_hash=manifest_hash,
            manifest_path=str(manifest_path),
            api_statuses=dict(sorted(statuses.items())),
            row_counts=dict(sorted(row_counts.items())),
            normalized_record_count=len(normalized.records),
            normalized_store_hash=normalized.artifact_hash,
            api_calls_used=sum(api_calls_used.values()),
            planned_partition_count=sum(planned_partition_counts.values()),
        )


def _scope(
    api_name: str,
    *,
    start_date: str,
    end_date: str,
    symbols: tuple[str, ...],
    index_codes: tuple[str, ...] = ("000001.SH",),
    reference_value: str | None = None,
) -> AcquisitionScope:
    fields = _FIELDS[api_name]
    if api_name == "stock_basic":
        return AcquisitionScope(
            api_name=api_name,
            fields=fields,
            fixed_params={"list_status": reference_value or "L"},
            offset_page_count=2,
            offset_page_size=5000,
        )
    if api_name == "trade_cal":
        calendar_end = (
            datetime.strptime(end_date, "%Y%m%d").date()
            # Financial APIs may return a revision whose actual announcement
            # date follows the requested report/query window. Freeze enough
            # reference calendar to resolve that publication to a tradable
            # session; this expands only the calendar, never feature scope.
            + timedelta(days=120)
        ).strftime("%Y%m%d")
        return AcquisitionScope(
            api_name=api_name,
            fields=fields,
            start_date=start_date,
            end_date=calendar_end,
            fixed_params={"exchange": reference_value or "SSE", "is_open": "1"},
        )
    if api_name == "shibor":
        return AcquisitionScope(
            api_name=api_name,
            fields=fields,
            start_date=start_date,
            end_date=end_date,
        )
    scoped_symbols = index_codes if api_name == "index_daily" else symbols
    return AcquisitionScope(
        api_name=api_name,
        fields=fields,
        start_date=start_date,
        end_date=end_date,
        symbols=scoped_symbols,
    )


def _scope_variants(
    api_name: str,
    *,
    start_date: str,
    end_date: str,
    symbols: tuple[str, ...],
    index_codes: tuple[str, ...] = ("000001.SH",),
    acquisition_mode: str = "SYMBOL",
    trading_sessions: tuple[str, ...] = (),
) -> tuple[tuple[str, AcquisitionScope], ...]:
    if acquisition_mode == "FULL_MARKET":
        if api_name in _FULL_MARKET_EXACT_DATE_APIS:
            if (
                api_name in {"daily", "daily_basic"}
                and not trading_sessions
            ):
                raise ValueError(
                    "FULL_MARKET market acquisition requires frozen trading sessions"
                )
            return (
                (
                    "FULL_MARKET_DATE",
                    AcquisitionScope(
                        api_name=api_name,
                        fields=_FIELDS[api_name],
                        start_date=start_date,
                        end_date=end_date,
                    ),
                ),
            )
        if api_name in _FULL_MARKET_SYMBOL_BATCH_APIS:
            if not symbols:
                raise ValueError(
                    "FULL_MARKET batched acquisition requires the frozen historical universe"
                )
            return (
                (
                    "FULL_MARKET_SYMBOL_BATCH",
                    AcquisitionScope(
                        api_name=api_name,
                        fields=_FIELDS[api_name],
                        start_date=start_date,
                        end_date=end_date,
                        symbols=symbols,
                    ),
                ),
            )
        if api_name in _FULL_MARKET_RANGE_APIS:
            return (
                (
                    "FULL_MARKET_DATE_RANGE",
                    AcquisitionScope(
                        api_name=api_name,
                        fields=_FIELDS[api_name],
                        start_date=start_date,
                        end_date=end_date,
                    ),
                ),
            )
        if api_name in _FULL_MARKET_PER_SYMBOL_APIS:
            if not symbols:
                raise ValueError(
                    "FULL_MARKET per-symbol acquisition requires the frozen historical universe"
                )
            return (
                (
                    "FULL_MARKET_PER_SYMBOL",
                    AcquisitionScope(
                        api_name=api_name,
                        fields=_FIELDS[api_name],
                        start_date=start_date,
                        end_date=end_date,
                        symbols=symbols,
                    ),
                ),
            )
    values = (
        ("SSE", "SZSE")
        if api_name == "trade_cal"
        else ("L", "D", "P")
        if api_name == "stock_basic"
        else (None,)
    )
    return tuple(
        (
            str(value or "default"),
            _scope(
                api_name,
                start_date=start_date,
                end_date=end_date,
                symbols=symbols,
                index_codes=index_codes,
                reference_value=value,
            ),
        )
        for value in values
    )


def _exact_date_scope(api_name: str, parameter: str, value: str) -> AcquisitionScope:
    return AcquisitionScope(
        api_name=api_name,
        fields=_FIELDS[api_name],
        fixed_params={parameter: value},
    )


def _calendar_days(start_date: str, end_date: str) -> tuple[str, ...]:
    cursor = datetime.strptime(start_date, "%Y%m%d").date()
    stop = datetime.strptime(end_date, "%Y%m%d").date()
    output = []
    while cursor <= stop:
        output.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return tuple(output)


def _aggregate_status(values: list[PartitionStatus]) -> PartitionStatus:
    if not values:
        return PartitionStatus.FAILED
    for status in (
        PartitionStatus.NETWORK_DISABLED,
        PartitionStatus.BUDGET_EXHAUSTED,
        PartitionStatus.FAILED,
        PartitionStatus.TRUNCATED,
    ):
        if status in values:
            return status
    if PartitionStatus.SUCCESS in values:
        return PartitionStatus.SUCCESS
    return PartitionStatus.EMPTY


def _terminal_status(statuses: Mapping[str, str]) -> str:
    values = set(statuses.values())
    if not values:
        return "UNAVAILABLE"
    if values <= {PartitionStatus.SUCCESS.value, PartitionStatus.EMPTY.value}:
        return "SUCCESS"
    if PartitionStatus.NETWORK_DISABLED.value in values:
        return "NETWORK_DISABLED"
    if PartitionStatus.BUDGET_EXHAUSTED.value in values:
        return "BUDGET_EXHAUSTED"
    return "FAILED"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=path.parent,
    )
    temporary = Path(handle.name)
    try:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
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


__all__ = [
    "DynamicCampaignDataFreezer",
    "DynamicDataFreezeResult",
    "SCHEMA_VERSION",
    "SOURCE_LABEL",
]
