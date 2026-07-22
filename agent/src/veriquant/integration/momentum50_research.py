"""Real approved-proxy 50-session momentum research report.

This is an operator-invoked, bounded backend research command.  It deliberately
keeps the LLM at DRAFT authority, freezes compatible-proxy inputs, executes only
typed FactorDAGs, computes visible official Evidence through the deterministic
Evidence Service, charges every candidate to the Trial Ledger, and fails closed
at promotion when production exposure/capacity inputs are absent.

The compatible proxy is always labelled APPROVED_PROXY_DATA_VENDOR /
TUSHARE_COMPATIBLE_PROXY.  It is not OFFICIAL_TUSHARE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import fmean
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from veriquant.alphaforge_llm.config import AlphaForgeLLMConfigLoader
from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.contracts.base import canonicalize
from veriquant.data import FieldRole, MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.evidence import (
    AttemptController,
    CostModel,
    EvaluationRow,
    EvidenceCourt,
    EvidenceCourtPolicy,
    EvidenceService,
    ExperimentIdentity,
    RegisteredFactor,
    TrialLedger,
    TrialOutcome,
)
from veriquant.evidence.service import row_key
from veriquant.factors import execute_factor_dag, parse_compile_factor_dag
from veriquant.real_llm_provider.client import OpenAICompatibleHTTPClient, ProviderFailure
from veriquant.tushare_sdk_proxy.client import ProxySdkConfig, TushareProxyError, create_proxy_client


SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = "alphaforge.momentum50-research/v1"
PROVIDER_CLASS = "APPROVED_PROXY_DATA_VENDOR"
SOURCE_CLASS = "TUSHARE_COMPATIBLE_PROXY"
INDEX_CODE = "000300.SH"
VARIANTS = ("momentum_50_rank", "momentum_50_skip5", "momentum_50_vol20")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(canonicalize(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("wb", delete=False, dir=str(path.parent), suffix=".tmp")
    temporary = Path(handle.name)
    try:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_bytes(path, _json_bytes(payload))


def _atomic_text(path: Path, text: str) -> None:
    _atomic_bytes(path, (text.rstrip() + "\n").encode("utf-8"))


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(table: Any) -> list[dict[str, Any]]:
    if table is None:
        return []
    if isinstance(table, list):
        return [dict(item) for item in table if isinstance(item, Mapping)]
    if hasattr(table, "to_dict"):
        try:
            return [dict(item) for item in table.to_dict(orient="records")]
        except Exception:
            return []
    return []


def _date_text(value: Any) -> str:
    text = str(value).replace("-", "")[:8]
    datetime.strptime(text, "%Y%m%d")
    return text


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} is not numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} is not finite")
    return number


def _hash_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    return canonical_content_hash(tuple(dict(sorted(item.items())) for item in rows))


def _factor_payloads() -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType(
        {
            "momentum_50_rank": {
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "real-momentum-50-rank",
                "nodes": [
                    {"id": "close", "op": "field", "field": "close"},
                    {"id": "m50", "op": "pct_change", "inputs": ["close"], "periods": 50},
                    {"id": "rank", "op": "rank_cross_section", "inputs": ["m50"]},
                ],
                "output_node": "rank",
            },
            "momentum_50_skip5": {
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "real-momentum-50-skip5",
                "nodes": [
                    {"id": "close", "op": "field", "field": "close"},
                    {"id": "m50", "op": "pct_change", "inputs": ["close"], "periods": 50},
                    {"id": "m5", "op": "pct_change", "inputs": ["close"], "periods": 5},
                    {"id": "skip", "op": "subtract", "inputs": ["m50", "m5"]},
                    {"id": "rank", "op": "rank_cross_section", "inputs": ["skip"]},
                ],
                "output_node": "rank",
            },
            "momentum_50_vol20": {
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "real-momentum-50-vol20",
                "nodes": [
                    {"id": "close", "op": "field", "field": "close"},
                    {"id": "m50", "op": "pct_change", "inputs": ["close"], "periods": 50},
                    {"id": "r1", "op": "pct_change", "inputs": ["close"], "periods": 1},
                    {"id": "vol20", "op": "rolling_std", "inputs": ["r1"], "window": 20},
                    {"id": "scaled", "op": "divide_safe", "inputs": ["m50", "vol20"], "epsilon": 1e-12},
                    {"id": "rank", "op": "rank_cross_section", "inputs": ["scaled"]},
                ],
                "output_node": "rank",
            },
        }
    )


def _baseline_payload() -> Mapping[str, Any]:
    return {
        "schema_version": "veriquant.factor-dag/v1",
        "dag_id": "registered-momentum-20-baseline",
        "nodes": [
            {"id": "close", "op": "field", "field": "close"},
            {"id": "m20", "op": "pct_change", "inputs": ["close"], "periods": 20},
            {"id": "rank", "op": "rank_cross_section", "inputs": ["m20"]},
        ],
        "output_node": "rank",
    }


def _validate_agent_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    fields = {
        "statement",
        "economic_mechanism",
        "expected_sign",
        "falsification_tests",
        "selected_variant",
        "selection_reason",
    }
    if set(normalized) != fields:
        raise ContractValidationError(FailureCode.PROVIDER_SCHEMA_REJECTED, "agent returned an inexact schema")
    if normalized["selected_variant"] not in VARIANTS:
        raise ContractValidationError(FailureCode.PROVIDER_SCHEMA_REJECTED, "agent selected an unregistered variant")
    if normalized["expected_sign"] not in {"POSITIVE", "NEGATIVE", "NON_MONOTONIC"}:
        raise ContractValidationError(FailureCode.PROVIDER_SCHEMA_REJECTED, "agent returned an invalid sign")
    for name in ("statement", "economic_mechanism", "selection_reason"):
        if not isinstance(normalized[name], str) or not normalized[name].strip():
            raise ContractValidationError(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{name} must be text")
    tests = normalized["falsification_tests"]
    if not isinstance(tests, list) or not tests or any(not isinstance(item, str) or not item.strip() for item in tests):
        raise ContractValidationError(FailureCode.PROVIDER_SCHEMA_REJECTED, "falsification_tests must be strings")
    rendered = json.dumps(normalized, ensure_ascii=False).lower()
    for forbidden in ("official_metrics", "promotion_verdict", "risk_approval", "target_weights", "orders", "factor.py", "import ", "eval(", "exec("):
        if forbidden in rendered:
            raise ContractValidationError(FailureCode.UNAUTHORIZED_CAPABILITY, "agent output contains forbidden authority or code")
    return normalized


def _agent_proposal(*, env_path: Path, as_of: date, symbols: int) -> tuple[dict[str, Any], dict[str, Any]]:
    configuration = AlphaForgeLLMConfigLoader(environ={}, dotenv_path=env_path).load()
    if not configuration.enabled or configuration.settings is None or not configuration.runtime_credential:
        raise ContractValidationError(FailureCode.ADAPTER_UNAVAILABLE, "real LLM configuration is unavailable")
    settings = configuration.settings
    client = OpenAICompatibleHTTPClient(settings=settings, runtime_credential=configuration.runtime_credential)
    system = {
        "role": "system",
        "content": (
            "你是 AlphaForge 市场结构研究 Desk。你只拥有 DRAFT 提案权限。"
            "只返回严格 JSON，并且只能填写研究假设、反证条件和受限变体选择。"
            "从给定的三个受限 FactorDAG 变体中选择一个，不得发明第四个。"
        ),
    }
    user = {
        "role": "user",
        "content": json.dumps(
            {
                "research_objective": "研究 A 股横截面最近 50 个交易日的大动量效应，预测未来 5 日收益",
                "as_of_date": as_of.isoformat(),
                "universe": f"CSI300 historical members, bounded top {symbols}",
                "data_fields": ["qfq_close", "volume", "amount"],
                "allowed_variants": list(VARIANTS),
                "return_keys": [
                    "statement",
                    "economic_mechanism",
                    "expected_sign",
                    "falsification_tests",
                    "selected_variant",
                    "selection_reason",
                ],
                "field_types": {
                    "statement": "string",
                    "economic_mechanism": "string",
                    "expected_sign": "POSITIVE|NEGATIVE|NON_MONOTONIC",
                    "falsification_tests": "array<string>",
                    "selected_variant": "one of allowed_variants",
                    "selection_reason": "string",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    }
    response = client.complete_json(
        messages=(system, user),
        model=settings.model,
        timeout_seconds=settings.timeout_seconds,
        max_tokens=min(settings.max_tokens, 1400),
        temperature=0.0,
        top_p=1.0,
        response_format="json_object",
        extra={},
    )
    proposal = _validate_agent_payload(response.payload)
    trace = {
        "provider": "OPENAI_COMPATIBLE_REAL_LLM",
        "model": settings.model,
        "endpoint_host": configuration.public_view().get("endpoint_host"),
        "input_hash": canonical_content_hash((system, user)),
        "output_hash": canonical_content_hash(proposal),
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "state": "DRAFT",
        "authority": "PROPOSAL_ONLY",
    }
    return proposal, trace


@dataclass(frozen=True, kw_only=True)
class FrozenResearchData:
    records: tuple[dict[str, Any], ...]
    sessions: tuple[str, ...]
    historical_universe: Mapping[str, tuple[str, ...]]
    symbols: tuple[str, ...]
    manifest: Mapping[str, Any]


def _next_session(sessions: Sequence[str], session: str) -> str | None:
    try:
        index = sessions.index(session)
    except ValueError:
        return None
    return sessions[index + 1] if index + 1 < len(sessions) else None


def _fetch_real_data(
    *,
    env_path: Path,
    as_of: date,
    symbol_count: int,
    history_sessions: int,
) -> FrozenResearchData:
    settings = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"TUSHARE_PROXY_URL", "TUSHARE_TOKEN"}:
            settings[key.strip()] = value.strip().strip("\"'")
    sdk = create_proxy_client(
        ProxySdkConfig(endpoint=settings.get("TUSHARE_PROXY_URL", ""), token=settings.get("TUSHARE_TOKEN", ""))
    )
    query_start = as_of - timedelta(days=520)
    calendar_rows = _rows(
        sdk.trade_cal(
            exchange="SSE",
            start_date=query_start.strftime("%Y%m%d"),
            end_date=(as_of + timedelta(days=15)).strftime("%Y%m%d"),
            fields="exchange,cal_date,is_open,pretrade_date",
        )
    )
    open_sessions = tuple(
        sorted({_date_text(row["cal_date"]) for row in calendar_rows if int(row.get("is_open", 0)) == 1})
    )
    as_of_text = as_of.strftime("%Y%m%d")
    completed = tuple(item for item in open_sessions if item <= as_of_text)
    if len(completed) < history_sessions + 10:
        raise ContractValidationError(FailureCode.INCOMPLETE_RESULT, "trading calendar has insufficient history")
    data_sessions = completed[-history_sessions:]
    start_text, end_text = data_sessions[0], data_sessions[-1]
    weight_rows = _rows(
        sdk.index_weight(
            index_code=INDEX_CODE,
            start_date=(datetime.strptime(start_text, "%Y%m%d").date() - timedelta(days=100)).strftime("%Y%m%d"),
            end_date=end_text,
        )
    )
    by_weight_date: dict[str, dict[str, float]] = {}
    for row in weight_rows:
        try:
            d = _date_text(row["trade_date"])
            code = str(row["con_code"])
            weight = _finite(row.get("weight", 0.0), "weight")
        except (KeyError, TypeError, ValueError):
            continue
        by_weight_date.setdefault(d, {})[code] = weight
    dates = sorted(by_weight_date)
    eligible_dates = [item for item in dates if item <= end_text]
    if not eligible_dates:
        raise ContractValidationError(FailureCode.INCOMPLETE_RESULT, "historical index universe is unavailable")
    within_window = [item for item in eligible_dates if item <= end_text and item >= start_text]
    if not within_window:
        within_window = eligible_dates[-1:]
    stable = set(by_weight_date[within_window[0]])
    for item in within_window[1:]:
        stable &= set(by_weight_date[item])
    latest_date = eligible_dates[-1]
    ranked = sorted(
        ((code, weight) for code, weight in by_weight_date[latest_date].items() if code in stable),
        key=lambda item: (-item[1], item[0]),
    )
    symbols = tuple(code for code, _ in ranked[:symbol_count])
    if len(symbols) != symbol_count:
        raise ContractValidationError(FailureCode.INCOMPLETE_RESULT, "stable CSI300 sample is smaller than requested")

    capture_entries: list[dict[str, Any]] = [
        {"api": "trade_cal", "row_count": len(calendar_rows), "content_hash": _hash_rows(calendar_rows)},
        {"api": "index_weight", "row_count": len(weight_rows), "content_hash": _hash_rows(weight_rows)},
    ]
    all_records: list[dict[str, Any]] = []
    for code in symbols:
        daily_rows = _rows(
            sdk.daily(
                ts_code=code,
                start_date=start_text,
                end_date=end_text,
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )
        )
        adj_rows = _rows(
            sdk.adj_factor(
                ts_code=code,
                start_date=start_text,
                end_date=end_text,
                fields="ts_code,trade_date,adj_factor",
            )
        )
        capture_entries.extend(
            (
                {"api": "daily", "symbol": code, "row_count": len(daily_rows), "content_hash": _hash_rows(daily_rows)},
                {"api": "adj_factor", "symbol": code, "row_count": len(adj_rows), "content_hash": _hash_rows(adj_rows)},
            )
        )
        adj_by_date = {
            _date_text(row["trade_date"]): _finite(row["adj_factor"], "adj_factor")
            for row in adj_rows
            if row.get("trade_date") is not None and row.get("adj_factor") is not None
        }
        if not adj_by_date:
            continue
        anchor = adj_by_date[max(adj_by_date)]
        if anchor <= 0.0:
            continue
        for row in daily_rows:
            try:
                session = _date_text(row["trade_date"])
                adj = adj_by_date[session]
                close = _finite(row["close"], "close") * adj / anchor
                volume = _finite(row["vol"], "vol")
                amount = _finite(row["amount"], "amount")
            except (KeyError, TypeError, ValueError):
                continue
            next_text = _next_session(open_sessions, session)
            if next_text is None:
                continue
            next_date = datetime.strptime(next_text, "%Y%m%d").date()
            available_at = datetime.combine(next_date, time(9, 25), SHANGHAI)
            decision_time = datetime.combine(as_of, time(23, 0), SHANGHAI)
            if available_at > decision_time:
                continue
            eligible_weight_dates = [item for item in eligible_dates if item <= session]
            if not eligible_weight_dates or code not in by_weight_date[eligible_weight_dates[-1]]:
                continue
            all_records.append(
                {
                    "symbol": code,
                    "session": session,
                    "event_time": datetime.combine(
                        datetime.strptime(session, "%Y%m%d").date(), time(15, 0), SHANGHAI
                    ).isoformat(),
                    "available_at": available_at.isoformat(),
                    "close": close,
                    "volume": volume,
                    "amount": amount,
                }
            )
    all_records.sort(key=lambda item: (item["session"], item["symbol"]))
    if not all_records:
        raise ContractValidationError(FailureCode.INCOMPLETE_RESULT, "proxy returned no usable price rows")
    used_sessions = tuple(sorted({item["session"] for item in all_records}))
    universe: dict[str, tuple[str, ...]] = {}
    for session in used_sessions:
        available = [item for item in eligible_dates if item <= session]
        members = set(by_weight_date[available[-1]]) if available else set()
        universe[session] = tuple(sorted(set(symbols) & members))
    records_hash = canonical_content_hash(tuple(all_records))
    calendar_hash = canonical_content_hash(open_sessions)
    universe_hash = canonical_content_hash(universe)
    capture_hash = canonical_content_hash(tuple(capture_entries))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "provider_class": PROVIDER_CLASS,
        "source_class": SOURCE_CLASS,
        "official_tushare_source": False,
        "index_code": INDEX_CODE,
        "as_of_date": as_of.isoformat(),
        "decision_time": datetime.combine(as_of, time(23, 0), SHANGHAI).isoformat(),
        "data_start": min(used_sessions),
        "data_end": max(used_sessions),
        "symbol_count": len(symbols),
        "session_count": len(used_sessions),
        "row_count": len(all_records),
        "symbols": list(symbols),
        "fields": ["qfq_close_as_close", "volume", "amount"],
        "availability_policy": "daily-close-next-session-open/v1",
        "records_hash": records_hash,
        "calendar_hash": calendar_hash,
        "universe_hash": universe_hash,
        "capture_hash": capture_hash,
        "api_calls": capture_entries,
    }
    manifest["manifest_hash"] = canonical_content_hash(manifest)
    return FrozenResearchData(
        records=tuple(all_records),
        sessions=used_sessions,
        historical_universe=MappingProxyType(universe),
        symbols=symbols,
        manifest=MappingProxyType(manifest),
    )


def _snapshot(data: FrozenResearchData, *, as_of: date) -> MarketSnapshot:
    rows = tuple(
        SnapshotRow(
            event_time=datetime.fromisoformat(item["event_time"]),
            symbol=item["symbol"],
            values={"close": item["close"], "volume": item["volume"], "amount": item["amount"]},
        )
        for item in data.records
    )
    return MarketSnapshot(
        dataset_id="approved-proxy-csi300-momentum50",
        decision_time=datetime.combine(as_of, time(23, 0), SHANGHAI),
        start_time=min(row.event_time for row in rows),
        fields=("close", "volume", "amount"),
        rows=rows,
        historical_universe=data.historical_universe,
        coverage_by_field={"close": 1.0, "volume": 1.0, "amount": 1.0},
    )


def _labels_and_partitions(data: FrozenResearchData) -> tuple[dict[tuple[datetime, str], float], dict[str, set[str]], dict[str, str]]:
    sessions = list(data.sessions)
    close = {(item["session"], item["symbol"]): float(item["close"]) for item in data.records}
    labels: dict[tuple[datetime, str], float] = {}
    breadth: dict[str, float] = {}
    for index, session in enumerate(sessions):
        if index > 0:
            changes = []
            previous = sessions[index - 1]
            for symbol in data.symbols:
                if (previous, symbol) in close and (session, symbol) in close and close[(previous, symbol)] > 0.0:
                    changes.append(close[(session, symbol)] / close[(previous, symbol)] - 1.0)
            breadth[session] = fmean(changes) if changes else 0.0
        if index + 6 >= len(sessions):
            continue
        entry, exit_ = sessions[index + 1], sessions[index + 6]
        event_time = datetime.combine(datetime.strptime(session, "%Y%m%d").date(), time(15, 0), SHANGHAI)
        for symbol in data.symbols:
            if (entry, symbol) in close and (exit_, symbol) in close and close[(entry, symbol)] > 0.0:
                labels[(event_time, symbol)] = close[(exit_, symbol)] / close[(entry, symbol)] - 1.0
    eligible = sessions[50:-6]
    if len(eligible) < 90:
        raise ContractValidationError(FailureCode.INCOMPLETE_RESULT, "50-day factor has insufficient evaluable sessions")
    train_end = max(50, int(len(eligible) * 0.55))
    validation_end = max(train_end + 20, int(len(eligible) * 0.78))
    validation_end = min(validation_end, len(eligible) - 20)
    partitions = {
        "TRAIN": set(eligible[:train_end]),
        "VALIDATION": set(eligible[train_end:validation_end]),
        "SEALED": set(eligible[validation_end:]),
    }
    regimes = {session: ("POSITIVE_BREADTH" if breadth.get(session, 0.0) >= 0.0 else "NEGATIVE_BREADTH") for session in sessions}
    return labels, partitions, regimes


def _evaluation_rows(
    *,
    factor_result: Any,
    data: FrozenResearchData,
    labels: Mapping[tuple[datetime, str], float],
    sessions: set[str],
    regimes: Mapping[str, str],
) -> tuple[EvaluationRow, ...]:
    volumes = {
        (datetime.fromisoformat(item["event_time"]), item["symbol"]): float(item["volume"])
        for item in data.records
    }
    output: list[EvaluationRow] = []
    for item in factor_result.values:
        session = item.event_time.strftime("%Y%m%d")
        key = (item.event_time, item.symbol)
        if session not in sessions or item.value is None or key not in labels:
            continue
        volume = volumes.get(key)
        if volume is None or volume <= 0.0:
            continue
        output.append(
            EvaluationRow(
                event_time=item.event_time,
                symbol=item.symbol,
                score=float(item.value),
                label=float(labels[key]),
                market_regime=regimes.get(session, "UNKNOWN"),
                volume=volume,
            )
        )
    if not output:
        raise ContractValidationError(FailureCode.INCOMPLETE_RESULT, "factor produced no evaluation rows")
    return tuple(sorted(output, key=lambda row: (row.event_time, row.symbol)))


def _p_value(evidence: Any) -> float:
    metrics = evidence.metrics
    if metrics.ic_mean <= 0.0 or metrics.ic_std <= 0.0 or metrics.period_count < 2:
        return 1.0
    statistic = metrics.ic_mean / (metrics.ic_std / math.sqrt(metrics.period_count))
    return max(1e-12, min(1.0, math.erfc(abs(statistic) / math.sqrt(2.0))))


def _sealed_terminal(evidence: Any) -> str:
    metrics = evidence.metrics
    passed = (
        metrics.observation_count >= 200
        and metrics.rank_ic > 0.0
        and metrics.icir > 0.0
        and metrics.net_return > 0.0
        and metrics.subperiod_positive_fraction >= 0.5
        and metrics.regime_positive_fraction >= 0.5
    )
    return "SEALED_PASS_REDACTED" if passed else "SEALED_REJECT_REDACTED"


def _metric_view(evidence: Any) -> dict[str, float]:
    metrics = evidence.metrics
    return {
        "pearson_ic": metrics.pearson_ic,
        "rank_ic": metrics.rank_ic,
        "ic_mean": metrics.ic_mean,
        "ic_std": metrics.ic_std,
        "icir": metrics.icir,
        "quantile_spread": metrics.quantile_spread,
        "turnover": metrics.turnover,
        "gross_return": metrics.gross_return,
        "net_return": metrics.net_return,
        "spread_cost": metrics.spread_cost,
        "linear_impact_cost": metrics.linear_impact_cost,
        "max_drawdown": metrics.max_drawdown,
        "subperiod_positive_fraction": metrics.subperiod_positive_fraction,
        "regime_positive_fraction": metrics.regime_positive_fraction,
        "max_registered_abs_correlation": metrics.max_registered_abs_correlation,
        "incremental_ic_mean": metrics.incremental_ic_mean,
        "observation_count": float(metrics.observation_count),
        "period_count": float(metrics.period_count),
    }


def _visible_score(metrics: Mapping[str, float]) -> float:
    return (
        metrics["icir"]
        + 4.0 * metrics["net_return"]
        + 0.25 * metrics["subperiod_positive_fraction"]
        + 0.25 * metrics["regime_positive_fraction"]
        + 0.50 * metrics["incremental_ic_mean"]
        - 0.25 * metrics["turnover"]
        - 0.50 * metrics["max_registered_abs_correlation"]
    )


def _agent_feedback(
    *,
    env_path: Path,
    proposal: Mapping[str, Any],
    selected_factor: str,
    visible_metrics: Mapping[str, float],
) -> tuple[str | None, dict[str, Any]]:
    configuration = AlphaForgeLLMConfigLoader(environ={}, dotenv_path=env_path).load()
    if not configuration.enabled or configuration.settings is None or not configuration.runtime_credential:
        return None, {"status": "UNAVAILABLE", "failure_code": "ADAPTER_UNAVAILABLE"}
    settings = configuration.settings
    client = OpenAICompatibleHTTPClient(settings=settings, runtime_credential=configuration.runtime_credential)
    messages = (
        {
            "role": "system",
            "content": (
                "你是 AlphaForge 研究反馈 Desk。只解释允许公开的 TRAIN/VALIDATION 摘要。"
                "不得猜测未提供的数据，也不得作出任何下游决定。只返回 {\"explanation\": string}。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "hypothesis_hash": canonical_content_hash(proposal),
                    "selected_factor": selected_factor,
                    "visible_validation_metrics": dict(visible_metrics),
                },
                sort_keys=True,
                ensure_ascii=False,
            ),
        },
    )
    try:
        response = client.complete_json(
            messages=messages,
            model=settings.model,
            timeout_seconds=settings.timeout_seconds,
            max_tokens=min(settings.max_tokens, 800),
            temperature=0.0,
            top_p=1.0,
            response_format="json_object",
            extra={},
        )
        if set(response.payload) != {"explanation"} or not isinstance(response.payload["explanation"], str):
            raise ContractValidationError(FailureCode.PROVIDER_SCHEMA_REJECTED, "feedback schema is invalid")
        return response.payload["explanation"], {
            "status": "COMPLETED_DRAFT",
            "input_hash": canonical_content_hash(messages),
            "output_hash": canonical_content_hash(response.payload),
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
        }
    except (ProviderFailure, ContractValidationError) as exc:
        code = getattr(getattr(exc, "code", None), "value", None) or type(exc).__name__
        return None, {"status": "FAILED_CLOSED", "failure_code": code}


def _markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# AlphaForge 真实 A 股 50 日动量研究报告",
        "",
        f"- 运行状态：`{payload['status']}`",
        f"- 运行 ID：`{payload['run_id']}`",
        f"- 数据来源：`{PROVIDER_CLASS}` / `{SOURCE_CLASS}`（不是 `OFFICIAL_TUSHARE`）",
        f"- 决策时点：`{payload['data']['decision_time']}`",
        f"- 样本：{payload['data']['symbol_count']} 只股票，{payload['data']['session_count']} 个交易日，{payload['data']['row_count']} 行",
        f"- 冻结数据 hash：`{payload['data']['records_hash']}`",
        "",
        "## Agent 研究草案",
        "",
        f"- 状态：`DRAFT`；模型：`{payload['agent']['trace']['model']}`",
        f"- 假设：{payload['agent']['proposal']['statement']}",
        f"- 机制：{payload['agent']['proposal']['economic_mechanism']}",
        f"- Agent 首选变体：`{payload['agent']['proposal']['selected_variant']}`",
        f"- 理由：{payload['agent']['proposal']['selection_reason']}",
        "- 反证条件：",
    ]
    lines.extend(f"  - {item}" for item in payload["agent"]["proposal"]["falsification_tests"])
    lines.extend(
        [
            "",
            "## 确定性评价",
            "",
            "| 因子 | Court | Validation RankIC | ICIR | 净收益 | 换手 | 与 20 日动量相关 | 增量 IC | p-value |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in payload["factors"]:
        metrics = item.get("validation_metrics", {})
        if item["status"] != "EVALUATED":
            lines.append(f"| {item['factor_id']} | {item['status']} | — | — | — | — | — | — | — |")
            continue
        lines.append(
            "| {factor} | {court} | {rank:.4f} | {icir:.3f} | {net:.4f} | {turnover:.3f} | {corr:.3f} | {inc:.4f} | {p:.4g} |".format(
                factor=item["factor_id"],
                court=item["court_outcome"],
                rank=metrics["rank_ic"],
                icir=metrics["icir"],
                net=metrics["net_return"],
                turnover=metrics["turnover"],
                corr=metrics["max_registered_abs_correlation"],
                inc=metrics["incremental_ic_mean"],
                p=item["visible_p_value"],
            )
        )
    selected = payload["selection"]
    lines.extend(
        [
            "",
            "## 结论",
            "",
            f"- 仅依据 TRAIN/VALIDATION 复合分数选出的候选：`{selected['selected_factor']}`。",
            f"- Evidence Court：`{selected['court_outcome']}`。",
            f"- Sealed 终态：`{selected['sealed_terminal_decision']}`；报告不包含 sealed 指标。",
            f"- Promotion：`{selected['promotion_status']}`。缺少生产级行业、市值、容量及借券暴露输入，因此没有创建 CHALLENGER。",
            f"- Trial Ledger：预算 {payload['ledger']['budget_consumed']}/{payload['ledger']['family_budget']} 已消耗，tip `{payload['ledger']['tip_hash']}`。",
            "",
            "## Agent 脱敏反馈",
            "",
            payload["agent"].get("feedback") or "真实模型反馈不可用；研究结果仍由确定性服务保留。",
            "",
            "## 边界与限制",
            "",
            "- 50 日动量使用兼容代理的日线与复权因子，close 在冻结层转换为窗口末端基准的前复权价格。",
            "- future 5-day return 仅作标签，从未进入 FactorDAG 特征。",
            "- 本报告验证研究链，不是投资建议，也不连接 broker、真实订单或真实资金。",
            "- 缺少生产级行业、市值、benchmark、ADV/容量和借券信息时，Promotion 必须 fail-closed。",
            "- 可见统计来自有限样本；没有候选通过也属于正确结果。",
            "",
            "## 可复现身份",
            "",
            f"- Factor family hash：`{payload['identity']['factor_family_hash']}`",
            f"- Split hash：`{payload['identity']['split_hash']}`",
            f"- Cost hash：`{payload['identity']['cost_hash']}`",
            f"- Code hash：`{payload['identity']['code_hash']}`",
            f"- 报告 JSON hash：写入后记录在 `artifact_manifest.json`。",
        ]
    )
    return "\n".join(lines)


def run(
    *,
    output_root: Path,
    as_of: date,
    seed: int,
    symbol_count: int,
    history_sessions: int,
) -> dict[str, Any]:
    if not 10 <= symbol_count <= 30:
        raise ValueError("symbol_count must be between 10 and 30")
    if not 160 <= history_sessions <= 250:
        raise ValueError("history_sessions must be between 160 and 250")
    env_path = _repo_root() / "agent/.env"
    proposal, agent_trace = _agent_proposal(env_path=env_path, as_of=as_of, symbols=symbol_count)
    data = _fetch_real_data(
        env_path=env_path,
        as_of=as_of,
        symbol_count=symbol_count,
        history_sessions=history_sessions,
    )
    snapshot = _snapshot(data, as_of=as_of)
    labels, partitions, regimes = _labels_and_partitions(data)
    payloads = _factor_payloads()
    factor_family_hash = canonical_content_hash(payloads)
    split_hash = canonical_content_hash({key: tuple(sorted(value)) for key, value in partitions.items()})
    cost_model = CostModel(spread_bps=3.0, linear_impact_bps=5.0)
    code_hash = canonical_content_hash(
        {
            "runner": _file_hash(Path(__file__)),
            "factor_dag": _file_hash(Path(__file__).resolve().parents[1] / "factors/dag.py"),
            "evidence_service": _file_hash(Path(__file__).resolve().parents[1] / "evidence/service.py"),
        }
    )
    run_id = f"momentum50-{as_of.strftime('%Y%m%d')}-seed{seed}"
    data_hash = str(data.manifest["records_hash"])
    run_root = output_root / run_id / data_hash.removeprefix("sha256:")[:16]
    run_root.mkdir(parents=True, exist_ok=True)
    _atomic_json(run_root / "frozen_input.json", {"manifest": dict(data.manifest), "records": data.records, "historical_universe": data.historical_universe})
    _atomic_json(run_root / "agent_proposal.json", {"proposal": proposal, "trace": agent_trace})

    baseline_dag = parse_compile_factor_dag(_baseline_payload(), field_roles={"close": FieldRole.FEATURE})
    baseline_result = execute_factor_dag(baseline_dag, snapshot, seed=seed, config={"role": "REGISTERED_BASELINE"})
    baseline_scores = {
        row_key(
            EvaluationRow(
                event_time=item.event_time,
                symbol=item.symbol,
                score=float(item.value),
                label=0.0,
                market_regime="BASELINE",
                volume=1.0,
            )
        ): float(item.value)
        for item in baseline_result.values
        if item.value is not None
    }
    registered = RegisteredFactor(
        factor_id="registered_momentum_20d",
        factor_hash=baseline_dag.artifact_hash,
        scores=baseline_scores,
    )

    ledger = TrialLedger(run_root / "trial_ledger.sqlite3", clock=lambda: snapshot.decision_time)
    family_id = f"{run_id}-family"
    ledger.create_family(family_id, len(payloads))
    attempts = AttemptController(ledger)
    service = EvidenceService()
    court = EvidenceCourt(
        EvidenceCourtPolicy(
            min_observations=200,
            min_rank_ic=0.01,
            min_icir=0.30,
            max_turnover=0.80,
            max_drawdown=0.30,
            min_net_return=0.0,
            max_registered_abs_correlation=0.90,
            min_incremental_ic=0.0,
        )
    )
    results: list[dict[str, Any]] = []
    sealed_by_factor: dict[str, str] = {}
    for index, (factor_id, factor_payload) in enumerate(payloads.items()):
        identity = ExperimentIdentity(
            experiment_id=f"{run_id}-{factor_id}",
            dataset_hash=data_hash,
            universe_hash=str(data.manifest["universe_hash"]),
            factor_hash=canonical_content_hash(factor_payload),
            split_hash=split_hash,
            label_hash=canonical_content_hash({"label": "future_return_5d", "lag": 1, "horizon": 5}),
            cost_hash=cost_model.artifact_hash,
            code_hash=code_hash,
            availability_hash=snapshot.artifact_hash,
            seed=seed,
        )
        attempt = attempts.start(
            family_id=family_id,
            trial_id=f"{run_id}-trial-{index:02d}",
            identity=identity,
            proposer_id="real-llm-market-structure-desk",
        )
        phase = "PARSE"
        try:
            dag = parse_compile_factor_dag(factor_payload, field_roles={"close": FieldRole.FEATURE})
            phase = "PIT"
            factor_result = execute_factor_dag(dag, snapshot, seed=seed, config={"run_id": run_id})
            phase = "EVALUATION"
            evidence: dict[str, Any] = {}
            metrics: dict[str, Any] = {}
            for partition in ("TRAIN", "VALIDATION", "SEALED"):
                rows = _evaluation_rows(
                    factor_result=factor_result,
                    data=data,
                    labels=labels,
                    sessions=partitions[partition],
                    regimes=regimes,
                )
                official = service.evaluate(
                    identity=identity,
                    rows=rows,
                    partition=partition,
                    cost_model=cost_model,
                    registered_factors=(registered,),
                )
                evidence[partition] = official
                if partition != "SEALED":
                    metrics[partition] = _metric_view(official)
                else:
                    sealed_by_factor[factor_id] = _sealed_terminal(official)
            decision = court.decide(
                evidence=evidence["VALIDATION"],
                identity_hash=identity.identity_hash,
                proposer_id="real-llm-market-structure-desk",
                approver_id="deterministic-evidence-court",
                decided_at=snapshot.decision_time,
                pit_valid=True,
                sealed_passed=None,
            )
            terminal = attempts.commit_success(attempt)
            results.append(
                {
                    "factor_id": factor_id,
                    "status": "EVALUATED",
                    "factor_dag_hash": dag.artifact_hash,
                    "factor_output_hash": factor_result.artifact_hash,
                    "identity_hash": identity.identity_hash,
                    "train_evidence_hash": evidence["TRAIN"].artifact_hash,
                    "validation_evidence_hash": evidence["VALIDATION"].artifact_hash,
                    "train_metrics": metrics["TRAIN"],
                    "validation_metrics": metrics["VALIDATION"],
                    "visible_p_value": _p_value(evidence["VALIDATION"]),
                    "court_outcome": decision.outcome.value,
                    "court_reason_codes": decision.reason_codes,
                    "court_decision_hash": decision.artifact_hash,
                    "ledger_terminal_hash": terminal.event_hash,
                    "visible_selection_score": _visible_score(metrics["VALIDATION"]),
                }
            )
        except ContractValidationError as exc:
            outcome = TrialOutcome.PARSE_REJECTION if phase == "PARSE" else TrialOutcome.PIT_REJECTION if phase == "PIT" else TrialOutcome.EVALUATION_REJECTION
            terminal = attempts.commit_failure(attempt, outcome=outcome, failure_code=exc.code)
            results.append(
                {
                    "factor_id": factor_id,
                    "status": "FAILED_CONSUMED",
                    "failure_code": exc.code.value,
                    "failure_message": str(exc),
                    "ledger_terminal_hash": terminal.event_hash,
                }
            )
    evaluated = [item for item in results if item["status"] == "EVALUATED"]
    if not evaluated:
        raise ContractValidationError(FailureCode.INCOMPLETE_RESULT, "all 50-day factor candidates failed")
    selected = max(evaluated, key=lambda item: (item["visible_selection_score"], item["factor_id"] ))
    feedback, feedback_trace = _agent_feedback(
        env_path=env_path,
        proposal=proposal,
        selected_factor=selected["factor_id"],
        visible_metrics=selected["validation_metrics"],
    )
    replay = ledger.replay()
    family = replay.families[family_id]
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETED_RESEARCH_FLOW",
        "run_id": run_id,
        "data": dict(data.manifest),
        "agent": {
            "proposal": proposal,
            "trace": agent_trace,
            "feedback": feedback,
            "feedback_trace": feedback_trace,
        },
        "identity": {
            "factor_family_hash": factor_family_hash,
            "split_hash": split_hash,
            "cost_hash": cost_model.artifact_hash,
            "code_hash": code_hash,
            "seed": seed,
        },
        "factors": results,
        "selection": {
            "selected_factor": selected["factor_id"],
            "agent_preferred_factor": proposal["selected_variant"],
            "court_outcome": selected["court_outcome"],
            "sealed_terminal_decision": sealed_by_factor.get(selected["factor_id"], "SEALED_NOT_RUN"),
            "promotion_status": "BLOCKED_MISSING_PRODUCTION_EXPOSURE_CAPACITY_INPUTS",
            "challenger_created": False,
        },
        "ledger": {
            "family_id": family_id,
            "family_budget": family.budget_limit,
            "budget_consumed": family.consumed,
            "terminal_trials": family.terminal_trials,
            "tip_hash": replay.last_hash,
        },
        "security": {
            "generated_python_executed": False,
            "eval_exec_used": False,
            "live_broker_path": False,
            "credentials_written": False,
            "sealed_metrics_exposed": False,
        },
    }
    report_json = run_root / "research_report.json"
    report_md = run_root / "research_report.md"
    _atomic_json(report_json, payload)
    _atomic_text(report_md, _markdown(payload))
    artifact_manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "artifacts": {
            "frozen_input.json": _file_hash(run_root / "frozen_input.json"),
            "agent_proposal.json": _file_hash(run_root / "agent_proposal.json"),
            "research_report.json": _file_hash(report_json),
            "research_report.md": _file_hash(report_md),
            "trial_ledger.sqlite3": _file_hash(run_root / "trial_ledger.sqlite3"),
        },
    }
    artifact_manifest["manifest_hash"] = canonical_content_hash(artifact_manifest)
    _atomic_json(run_root / "artifact_manifest.json", artifact_manifest)
    _atomic_json(output_root / "latest.json", {"run_id": run_id, "run_root": str(run_root), "manifest_hash": artifact_manifest["manifest_hash"]})
    return {**payload, "run_root": str(run_root), "artifact_manifest": artifact_manifest}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run real approved-proxy 50-session momentum research")
    parser.add_argument("--output-root", type=Path, default=_repo_root() / ".autobuild/veriquant/research_reports")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--symbols", type=int, default=20)
    parser.add_argument("--history-sessions", type=int, default=220)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    allowed = (_repo_root() / ".autobuild/veriquant/research_reports").resolve()
    output = args.output_root.resolve()
    try:
        output.relative_to(allowed)
    except ValueError:
        print(json.dumps({"status": "FAILED", "failure_code": "UNAUTHORIZED_OUTPUT_PATH"}))
        return 2
    try:
        result = run(
            output_root=output,
            as_of=args.as_of,
            seed=args.seed,
            symbol_count=args.symbols,
            history_sessions=args.history_sessions,
        )
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "run_id": result["run_id"],
                    "selected_factor": result["selection"]["selected_factor"],
                    "court_outcome": result["selection"]["court_outcome"],
                    "promotion_status": result["selection"]["promotion_status"],
                    "challenger_created": result["selection"]["challenger_created"],
                    "run_root": result["run_root"],
                    "manifest_hash": result["artifact_manifest"]["manifest_hash"],
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        return 0
    except (ContractValidationError, ProviderFailure, TushareProxyError, OSError, ValueError) as exc:
        code = getattr(getattr(exc, "code", None), "value", None) or type(exc).__name__
        print(json.dumps({"status": "FAILED", "failure_code": code, "failure_type": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
