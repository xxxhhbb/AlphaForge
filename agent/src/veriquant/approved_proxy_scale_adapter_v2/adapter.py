"""Build a dynamically sized PIT-frozen input from the approved proxy boundary."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import math
from typing import Any, Mapping

from ..real_ashare_scale import (
    APPROVED_PROVIDER_CLASS,
    APPROVED_SOURCE_CLASS,
    FrozenAshareScaleInput,
    FrozenDataVintage,
    HistoricalUniverseSnapshot,
    InputOrigin,
    ScaleInputRow,
    stable_content_hash,
)
from ..tushare_sdk_proxy import TushareProxyError
from ..research_real_data_v2 import PRICE_CORE_FEATURE_FIELDS
from .client import ProxyScaleQueryClient
from .models import (
    ProxyScaleAcquisitionRequest,
    ProxyScaleAdapterResult,
    ProxyScaleAdapterStatus,
    ProxyScaleFailureCode,
)


CN = timezone(timedelta(hours=8))
FEATURE_FIELDS = PRICE_CORE_FEATURE_FIELDS
LABEL_FIELDS = ("future_return_5d",)
LABEL_HORIZON_SESSIONS = 5


def _availability_policy_hash(label_horizon: int) -> str:
    return stable_content_hash({
        "schema": "alphaforge.approved-proxy-availability/v2",
        "daily_event_time": "15:00:00+08:00",
        "daily_available_at": "16:00:00+08:00",
        "decision_time": "17:00:00+08:00",
        "industry": "effective-and-available-dated-only",
        f"future_return_{label_horizon}d": "label-only",
        "research_universe": "asof-index-weight-top-target-plus-10; retain first target with complete 16:00 rows for 17:00 decision",
    })


AVAILABILITY_POLICY_HASH = _availability_policy_hash(LABEL_HORIZON_SESSIONS)


class ApprovedProxyScaleAdapterV2:
    def __init__(self, client: ProxyScaleQueryClient) -> None:
        self.client = client
        self._calls: list[dict[str, Any]] = []

    def capture(self, request: ProxyScaleAcquisitionRequest) -> ProxyScaleAdapterResult:
        if not bool(getattr(self.client, "offline_fixture", False)) and not request.allow_network:
            return self._failed(request, ProxyScaleAdapterStatus.UNAVAILABLE, ProxyScaleFailureCode.NETWORK_NOT_ENABLED, "approved proxy acquisition is disabled by default")
        self._calls = []
        try:
            frozen = self._capture(request)
        except _AdapterUnavailable as exc:
            return self._failed(request, ProxyScaleAdapterStatus.UNAVAILABLE, exc.code, str(exc))
        except TushareProxyError:
            return self._failed(request, ProxyScaleAdapterStatus.UNAVAILABLE, ProxyScaleFailureCode.API_UNAVAILABLE, "approved proxy API is unavailable")
        except (KeyError, TypeError, ValueError):
            return self._failed(request, ProxyScaleAdapterStatus.BLOCKED, ProxyScaleFailureCode.API_RESPONSE_INVALID, "approved proxy response failed typed normalization")
        return ProxyScaleAdapterResult(
            request_hash=request.artifact_hash,
            status=ProxyScaleAdapterStatus.READY,
            failure_codes=(),
            message="approved proxy rows were frozen with explicit PIT clocks",
            frozen_input=frozen,
            api_manifest_hash=frozen.adapter_manifest_hash,
        )

    def _capture(self, request: ProxyScaleAcquisitionRequest) -> FrozenAshareScaleInput:
        supported = set(PRICE_CORE_FEATURE_FIELDS) | {"industry"}
        unsupported_required = set(request.required_feature_fields) - supported
        if unsupported_required:
            raise _AdapterUnavailable(ProxyScaleFailureCode.REQUIRED_FIELD_MISSING, "required feature is outside approved proxy capability")
        calendar_by_exchange: dict[str, set[str]] = {}
        for exchange in ("SSE", "SZSE"):
            rows = self._query(
                "trade_cal",
                params={
                    "exchange": exchange,
                    "start_date": request.start_date,
                    "end_date": request.end_date,
                    "is_open": "1",
                },
                fields=("exchange", "cal_date", "is_open", "pretrade_date"),
            )
            calendar_by_exchange[exchange] = {
                _iso_date(row.get("cal_date"))
                for row in rows
                if str(row.get("is_open")) in {"1", "1.0", "True", "true"}
            }
        if any(not sessions for sessions in calendar_by_exchange.values()):
            raise _AdapterUnavailable(
                ProxyScaleFailureCode.CALENDAR_INSUFFICIENT,
                "SSE and SZSE calendars must both be available",
            )
        # A cross-exchange equity research decision is tradable only on a day
        # admitted by both frozen calendars.  Differences remain visible in
        # the per-call manifest rather than being silently unioned.
        open_sessions = tuple(sorted(set.intersection(*calendar_by_exchange.values())))
        label_horizon = request.label_horizon_sessions
        label_field = f"future_return_{label_horizon}d"
        acquisition_session_count = request.required_sessions + label_horizon
        if len(open_sessions) < acquisition_session_count:
            raise _AdapterUnavailable(
                ProxyScaleFailureCode.CALENDAR_INSUFFICIENT,
                (
                    f"fewer than {request.required_sessions} research sessions "
                    f"plus {label_horizon} label-only sessions"
                ),
            )
        acquisition_sessions = open_sessions[-acquisition_session_count:]
        sessions = acquisition_sessions[:request.required_sessions]
        start_api = acquisition_sessions[0].replace("-", "")
        end_api = acquisition_sessions[-1].replace("-", "")
        # The approved proxy caps large responses.  Query bounded date chunks
        # so an older as-of constituent snapshot cannot be silently truncated
        # by a latest-N row limit.
        weight_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
        for chunk_start, chunk_end in _date_chunks(request.start_date, request.end_date, days=120):
            chunk = self._query(
                "index_weight",
                params={"index_code": request.index_code, "start_date": chunk_start, "end_date": chunk_end},
                fields=("index_code", "con_code", "trade_date", "weight"),
            )
            for row in chunk:
                key = (str(row.get("trade_date") or ""), str(row.get("con_code") or ""))
                if all(key):
                    weight_by_key[key] = row
        weight_rows = list(weight_by_key.values())
        rankings = _historical_rankings(weight_rows, sessions, request.target_symbols, buffer_symbols=10)
        candidate_universe = tuple(
            HistoricalUniverseSnapshot(session=session, symbols=tuple(sorted(symbols)))
            for session, symbols in rankings.items()
        )
        union_symbols = tuple(sorted({symbol for item in candidate_universe for symbol in item.symbols}))
        if (
            len(union_symbols) < request.target_symbols
            or (
                request.max_acquisition_symbols is not None
                and len(union_symbols) > request.max_acquisition_symbols
            )
        ):
            raise _AdapterUnavailable(
                ProxyScaleFailureCode.SYMBOL_SCALE_OUT_OF_BOUNDS,
                "historical acquisition union exceeds the explicit bounded limit",
            )

        stock_rows = []
        for list_status in ("L", "D", "P"):
            stock_rows.extend(self._query(
                "stock_basic", params={"exchange": "", "list_status": list_status},
                fields=("ts_code", "list_date", "delist_date", "list_status"),
            ))
        status_events = _status_events(stock_rows, union_symbols)
        industry_requested = "industry" in set(request.required_feature_fields) | set(request.optional_feature_fields)
        industry_required = "industry" in request.required_feature_fields
        industry: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in union_symbols}
        if industry_requested:
            try:
                industry_rows = []
                for list_status in ("L", "D", "P"):
                    industry_rows.extend(self._query(
                        "stock_basic", params={"exchange": "", "list_status": list_status},
                        fields=("ts_code", "industry", "list_date", "delist_date", "list_status", "industry_effective_date", "industry_available_date"),
                    ))
                industry = _industry_history(industry_rows, union_symbols)
            except (TushareProxyError, _AdapterUnavailable):
                self._calls.append({"api_name": "stock_basic.industry_history", "status": "UNAVAILABLE"})
                industry = {symbol: [] for symbol in union_symbols}
        daily: dict[tuple[str, str], Mapping[str, Any]] = {}
        adj: dict[tuple[str, str], Mapping[str, Any]] = {}
        basic: dict[tuple[str, str], Mapping[str, Any]] = {}
        for symbol in union_symbols:
            for row in self._query(
                "daily", params={"ts_code": symbol, "start_date": start_api, "end_date": end_api},
                fields=("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"),
            ):
                daily[(symbol, _iso_date(row.get("trade_date")))] = row
            for row in self._query(
                "adj_factor", params={"ts_code": symbol, "start_date": start_api, "end_date": end_api},
                fields=("ts_code", "trade_date", "adj_factor"),
            ):
                adj[(symbol, _iso_date(row.get("trade_date")))] = row
            for row in self._query(
                "daily_basic", params={"ts_code": symbol, "start_date": start_api, "end_date": end_api},
                fields=("ts_code", "trade_date", "total_mv"),
            ):
                basic[(symbol, _iso_date(row.get("trade_date")))] = row

        universe = _complete_historical_universe(
            rankings=rankings,
            daily=daily,
            adj=adj,
            basic=basic,
            target=request.target_symbols,
            label_sessions=acquisition_sessions,
            label_horizon=label_horizon,
        )
        industry_complete = bool(industry_requested) and all(
            _industry_as_of(industry[symbol], item.session, _clock(item.session, 17)) is not None
            for item in universe for symbol in item.symbols
        )
        if industry_required and not industry_complete:
            raise _AdapterUnavailable(ProxyScaleFailureCode.INDUSTRY_HISTORY_UNAVAILABLE, "effective/available-dated industry history is unavailable")
        members = {item.session: item.symbols for item in universe}
        rows: list[ScaleInputRow] = []
        for session_index, session in enumerate(sessions):
            for symbol in members[session]:
                key = (symbol, session)
                if key not in daily or key not in adj or key not in basic:
                    raise _AdapterUnavailable(ProxyScaleFailureCode.REQUIRED_FIELD_MISSING, f"required market row unavailable for {symbol} {session}")
                bar, factor, daily_basic = daily[key], adj[key], basic[key]
                feature = {
                    "open": _number(bar, "open"), "high": _number(bar, "high"),
                    "low": _number(bar, "low"), "close": _number(bar, "close"),
                    "volume": _number(bar, "vol"), "amount": _number(bar, "amount"),
                    "adj_factor": _number(factor, "adj_factor"),
                    "market_cap": _number(daily_basic, "total_mv") * 10_000.0,
                    "status": _status_as_of(status_events[symbol], session),
                }
                if industry_complete:
                    feature["industry"] = _industry_as_of(industry[symbol], session, _clock(session, 17))
                label = _future_return(
                    daily, adj, symbol, acquisition_sessions, session_index,
                    horizon=label_horizon,
                )
                rows.append(ScaleInputRow(
                    symbol=symbol,
                    session=session,
                    event_time=_clock(session, 15),
                    available_at=_clock(session, 16),
                    revision_id=f"proxy-{session.replace('-', '')}-{symbol.replace('.', '-')}",
                    features=feature,
                    labels={label_field: label},
                ))
        if request.capture_time < _clock(acquisition_sessions[-1], 16):
            raise _AdapterUnavailable(
                ProxyScaleFailureCode.PIT_TIME_SEMANTICS_UNAVAILABLE,
                "capture_time precedes final label-only row availability",
            )
        row_tuple = tuple(rows)
        universe_tuple = tuple(universe)
        api_manifest_hash = stable_content_hash(tuple(self._calls))
        hashes_by_session: dict[str, list[str]] = defaultdict(list)
        for row in row_tuple:
            hashes_by_session[row.session].append(row.artifact_hash)
        vintages_list = []
        previous_content_hash = None
        for session in sessions:
            content_hash = stable_content_hash({
                "previous_content_hash": previous_content_hash,
                "session": session,
                "row_hashes": tuple(sorted(hashes_by_session[session])),
            })
            vintages_list.append(FrozenDataVintage(
                vintage_id=f"proxy-vintage-{session.replace('-', '')}",
                session=session,
                available_at=_clock(session, 16),
                decision_time=_clock(session, 17),
                revision_id=f"proxy-vintage-revision-{session.replace('-', '')}",
                content_hash=content_hash,
            ))
            previous_content_hash = content_hash
        vintages = tuple(vintages_list)
        return FrozenAshareScaleInput(
            dataset_id=request.dataset_id,
            provider_class=APPROVED_PROVIDER_CLASS,
            source_class=APPROVED_SOURCE_CLASS,
            input_origin=InputOrigin.TEST_FIXTURE if bool(getattr(self.client, "offline_fixture", False)) else InputOrigin.FROZEN_REAL,
            capture_time=request.capture_time,
            capture_network_used=not bool(getattr(self.client, "offline_fixture", False)),
            calendar_id="sse-szse-approved-proxy-v2",
            calendar_sessions=sessions,
            calendar_hash=stable_content_hash(sessions),
            historical_universe=universe_tuple,
            universe_hash=stable_content_hash(universe_tuple),
            rows=row_tuple,
            data_hash=stable_content_hash(row_tuple),
            feature_fields=FEATURE_FIELDS + (("industry",) if industry_complete else ()),
            label_fields=(label_field,),
            availability_policy_hash=_availability_policy_hash(label_horizon),
            vintages=vintages,
            adapter_manifest_hash=api_manifest_hash,
        )

    def _query(self, api_name: str, *, params: Mapping[str, Any], fields: tuple[str, ...]) -> list[dict[str, Any]]:
        try:
            rows = self.client.query(api_name, params=params, fields=fields)
        except TushareProxyError as exc:
            # Keep diagnostics useful without serializing credentials, URLs,
            # request headers, or provider response bodies.
            raise _AdapterUnavailable(
                ProxyScaleFailureCode.API_UNAVAILABLE,
                f"approved proxy API unavailable: {api_name}",
            ) from exc
        if not isinstance(rows, list):
            raise _AdapterUnavailable(ProxyScaleFailureCode.API_RESPONSE_INVALID, "API result must be row list")
        normalized = [dict(row) for row in rows if isinstance(row, Mapping)]
        self._calls.append({
            "api_name": api_name,
            "params": dict(sorted(params.items())),
            "fields": fields,
            "row_count": len(normalized),
            "rows_hash": stable_content_hash(normalized),
        })
        return normalized

    @staticmethod
    def _failed(request, status, code, message) -> ProxyScaleAdapterResult:
        return ProxyScaleAdapterResult(
            request_hash=request.artifact_hash,
            status=status,
            failure_codes=(code,),
            message=message,
            frozen_input=None,
            api_manifest_hash=None,
        )


class _AdapterUnavailable(RuntimeError):
    def __init__(self, code: ProxyScaleFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _historical_rankings(rows, sessions, target, *, buffer_symbols):
    by_date: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in rows:
        session = _iso_date(row.get("trade_date"))
        symbol = str(row.get("con_code") or "")
        if symbol:
            by_date[session].append((symbol, float(row.get("weight") or 0.0)))
    dates = sorted(by_date)
    output = {}
    for session in sessions:
        eligible = [value for value in dates if value <= session]
        if not eligible:
            raise _AdapterUnavailable(ProxyScaleFailureCode.HISTORICAL_UNIVERSE_INSUFFICIENT, "no as-of constituent snapshot")
        ranked = sorted(by_date[eligible[-1]], key=lambda item: (-item[1], item[0]))
        if len(ranked) < target:
            raise _AdapterUnavailable(ProxyScaleFailureCode.HISTORICAL_UNIVERSE_INSUFFICIENT, "constituent snapshot is below target size")
        candidate_count = min(len(ranked), target + buffer_symbols)
        output[session] = tuple(symbol for symbol, _ in ranked[:candidate_count])
    return output


def _complete_historical_universe(
    *, rankings, daily, adj, basic, target, label_sessions, label_horizon
):
    session_index = {session: index for index, session in enumerate(label_sessions)}
    output = []
    for session, ranked_symbols in rankings.items():
        index = session_index[session]
        label_session = label_sessions[index + label_horizon]
        complete = [
            symbol for symbol in ranked_symbols
            if (
                (symbol, session) in daily
                and (symbol, session) in adj
                and (symbol, session) in basic
                and (symbol, label_session) in daily
                and (symbol, label_session) in adj
            )
        ]
        if len(complete) < target:
            raise _AdapterUnavailable(
                ProxyScaleFailureCode.REQUIRED_FIELD_MISSING,
                f"fewer than {target} complete as-of members for {session}",
            )
        output.append(HistoricalUniverseSnapshot(session=session, symbols=tuple(sorted(complete[:target]))))
    return tuple(output)


def _status_events(rows, symbols):
    output: dict[str, dict[str, str | None]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        if symbol not in symbols or not row.get("list_date"):
            continue
        output[symbol] = {
            "list_date": _iso_date(row.get("list_date")),
            "delist_date": _iso_date(row.get("delist_date")) if row.get("delist_date") else None,
        }
    if any(symbol not in output for symbol in symbols):
        raise _AdapterUnavailable(ProxyScaleFailureCode.REQUIRED_FIELD_MISSING, "list/delist event dates are unavailable")
    return output


def _status_as_of(event, session):
    return "LISTED" if event["list_date"] <= session and (event["delist_date"] is None or session < event["delist_date"]) else "INACTIVE"


def _industry_history(rows, symbols):
    output: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        if symbol not in output:
            continue
        if not row.get("industry") or not row.get("industry_effective_date") or not row.get("industry_available_date"):
            continue
        output[symbol].append(row)
    return output


def _industry_as_of(rows, session, decision):
    eligible = []
    for row in rows:
        effective = _iso_date(row.get("industry_effective_date"))
        available = _parse_available(row.get("industry_available_date"))
        if effective <= session and available <= decision:
            eligible.append((effective, available, row))
    if not eligible:
        return None
    row = sorted(eligible, key=lambda item: (item[0], item[1]))[-1][2]
    return str(row["industry"])


def _future_return(daily, adj, symbol, sessions, index, *, horizon=5):
    if index + horizon >= len(sessions):
        return None
    left_key, right_key = (
        (symbol, sessions[index]),
        (symbol, sessions[index + horizon]),
    )
    if left_key not in daily or right_key not in daily or left_key not in adj or right_key not in adj:
        return None
    left = _number(daily[left_key], "close") * _number(adj[left_key], "adj_factor")
    right = _number(daily[right_key], "close") * _number(adj[right_key], "adj_factor")
    return None if left == 0 else right / left - 1.0


def _number(row, field):
    value = row.get(field)
    if isinstance(value, bool) or value is None:
        raise _AdapterUnavailable(ProxyScaleFailureCode.REQUIRED_FIELD_MISSING, f"numeric field missing: {field}")
    result = float(value)
    if not math.isfinite(result):
        raise _AdapterUnavailable(ProxyScaleFailureCode.REQUIRED_FIELD_MISSING, f"numeric field non-finite: {field}")
    return result


def _iso_date(value):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    parsed = date.fromisoformat(text)
    return parsed.isoformat()


def _parse_available(value):
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return _clock(_iso_date(text), 0)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN)
    return parsed


def _clock(session, hour):
    return datetime.combine(date.fromisoformat(session), time(hour), tzinfo=CN)


def _date_chunks(start_value: str, end_value: str, *, days: int) -> tuple[tuple[str, str], ...]:
    start = datetime.strptime(start_value, "%Y%m%d").date()
    end = datetime.strptime(end_value, "%Y%m%d").date()
    output = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=days - 1))
        output.append((cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        cursor = chunk_end + timedelta(days=1)
    return tuple(output)
