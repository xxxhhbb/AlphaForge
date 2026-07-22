from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pytest

from src.veriquant.bitemporal_features import (
    BitemporalError,
    BitemporalStore,
    EvidenceEligibility,
    SHANGHAI,
    SourceKind,
    UnknownTimePolicy,
    as_of_join,
    build_feature_view,
    build_historical_feature_rows,
    canonical_record_hash,
    normalize_row,
    normalize_rows,
)


def dt(day: int, hour: int = 17) -> datetime:
    return datetime(2024, 4, day, hour, tzinfo=SHANGHAI)


def daily(close: float = 10.0, day: str = "20240401", revision: str = "daily-r1"):
    return {
        "ts_code": "000001.SZ",
        "trade_date": day,
        "open": close - 0.2,
        "high": close + 0.2,
        "low": close - 0.4,
        "close": close,
        "pre_close": close - 0.1,
        "change": 0.1,
        "pct_chg": 1.0,
        "vol": 1000,
        "amount": 10000,
        "revision_id": revision,
    }


@pytest.mark.parametrize(
    ("source", "row", "expected"),
    [
        ("daily", daily(), "close"),
        (
            "daily_basic",
            {"ts_code": "000001.SZ", "trade_date": "20240401", "total_mv": 100, "pe_ttm": 10},
            "market_cap",
        ),
        (
            "adj_factor",
            {"ts_code": "000001.SZ", "trade_date": "20240401", "adj_factor": 1.2},
            "adjustment_factor",
        ),
        ("index_daily", daily(), "index_close"),
        (
            "moneyflow",
            {"ts_code": "000001.SZ", "trade_date": "20240401", "net_mf_amount": 8},
            "net_moneyflow_amount",
        ),
        (
            "income",
            {"ts_code": "000001.SZ", "ann_date": "20240401", "end_date": "20231231", "revenue": 8},
            "revenue",
        ),
        (
            "balancesheet",
            {"ts_code": "000001.SZ", "ann_date": "20240401", "end_date": "20231231", "total_liab": 8},
            "total_liabilities",
        ),
        (
            "cashflow",
            {"ts_code": "000001.SZ", "ann_date": "20240401", "end_date": "20231231", "n_cashflow_act": 8},
            "operating_cashflow",
        ),
        (
            "fina_indicator",
            {"ts_code": "000001.SZ", "ann_date": "20240401", "end_date": "20231231", "roe": 8},
            "roe",
        ),
        (
            "forecast",
            {"ts_code": "000001.SZ", "ann_date": "20240401", "end_date": "20231231", "p_change_min": 8},
            "forecast_profit_change_min",
        ),
        (
            "express",
            {"ts_code": "000001.SZ", "ann_date": "20240401", "end_date": "20231231", "n_income": 8},
            "express_net_income",
        ),
        ("shibor", {"date": "20240401", "on": 1.8}, "shibor_overnight"),
    ],
)
def test_supported_sources_normalize_to_numeric_features(source, row, expected):
    record = normalize_row(source, row)
    assert expected in record.values
    assert record.published_at <= record.available_at
    assert record.decision_time is None
    assert record.eligibility is EvidenceEligibility.FEATURE_ELIGIBLE


def test_financial_uses_actual_announcement_and_conservative_next_open():
    record = normalize_row(
        "income",
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240328",
            "f_ann_date": "20240402",
            "end_date": "20231231",
            "revenue": 100,
        },
    )
    assert record.event_time.date().isoformat() == "2023-12-31"
    assert record.published_at.date().isoformat() == "2024-04-02"
    assert record.available_at == dt(3, 9).replace(minute=30)


def test_conservative_availability_skips_weekend():
    record = normalize_row(
        "forecast",
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240405",
            "end_date": "20240331",
            "p_change_min": 10,
        },
    )
    assert record.available_at.date().isoformat() == "2024-04-08"


def test_frozen_exchange_calendar_controls_next_available_session():
    record = normalize_row(
        "forecast",
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240403",
            "end_date": "20240331",
            "p_change_min": 10,
        },
        trading_sessions=(
            date(2024, 4, 3),
            # 4–7 April are absent from the frozen SSE/SZSE calendar.
            date(2024, 4, 8),
        ),
    )
    assert record.available_at.date() == date(2024, 4, 8)


def test_frozen_calendar_without_future_session_fails_closed():
    with pytest.raises(BitemporalError, match="next session"):
        normalize_row(
            "shibor",
            {"date": "20240403", "on": 1.8},
            trading_sessions=(date(2024, 4, 3),),
        )


def test_daily_basic_derives_safe_valuation_yields():
    positive = normalize_row(
        "daily_basic",
        {
            "ts_code": "000001.SZ",
            "trade_date": "20240401",
            "pe_ttm": 10,
            "pb": 2,
        },
    )
    assert positive.values["earnings_yield"] == pytest.approx(0.1)
    assert positive.values["book_to_market"] == pytest.approx(0.5)
    loss = normalize_row(
        "daily_basic",
        {
            "ts_code": "000001.SZ",
            "trade_date": "20240401",
            "pe_ttm": -3,
            "pb": 0,
        },
    )
    assert loss.values["earnings_yield"] is None
    assert loss.values["book_to_market"] is None


def test_unknown_time_semantics_are_auxiliary_only_or_fail_closed():
    batch = normalize_rows("mystery_api", ({"value": 1},))
    assert batch.records == ()
    assert batch.eligibility is EvidenceEligibility.AUXILIARY_ONLY
    assert batch.issues[0].code == "UNKNOWN_TIME_SEMANTICS"

    with pytest.raises(BitemporalError, match="unknown time semantics"):
        normalize_rows(
            "mystery_api",
            ({"value": 1},),
            unknown_policy=UnknownTimePolicy.FAIL_CLOSED,
        )


def test_decision_time_gate_rejects_unavailable_record():
    with pytest.raises(BitemporalError, match="unavailable"):
        normalize_row("daily", daily(), decision_time=dt(1, 15))


def test_store_is_append_only_and_revision_history_is_preserved():
    first = normalize_row(
        "income",
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240401",
            "end_date": "20231231",
            "revenue": 100,
            "revision_id": "r1",
        },
    )
    revised = normalize_row(
        "income",
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240403",
            "end_date": "20231231",
            "revenue": 120,
            "revision_id": "r2",
        },
    )
    store = BitemporalStore((first, revised))
    assert [item.revision_id for item in store.history(source="income", entity_id="000001.SZ")] == [
        "r1",
        "r2",
    ]
    assert store.latest_as_of(
        source="income",
        entity_id="000001.SZ",
        event_time=dt(5),
        decision_time=dt(2),
    ).values["revenue"] == 100
    assert store.latest_as_of(
        source="income",
        entity_id="000001.SZ",
        event_time=dt(5),
        decision_time=dt(4),
    ).values["revenue"] == 120

    with pytest.raises(BitemporalError, match="different content"):
        store.append((replace(first, values={"revenue": 999}),))


def test_store_reuses_canonical_order_and_hash_until_append_invalidates_cache():
    first = normalize_row("daily", daily())
    store = BitemporalStore((first,))
    ordered = store.records
    digest = store.artifact_hash

    assert store.records is ordered
    assert store.artifact_hash is digest

    second = normalize_row("daily", daily(11, day="20240402", revision="daily-r2"))
    store.append((second,))
    assert store.records is not ordered
    assert store.artifact_hash != digest


def test_latest_as_of_large_history_preserves_revision_and_future_guards():
    records = tuple(
        normalize_row(
            "daily",
            daily(float(day), day=f"2024{month:02d}{day_of_month:02d}", revision=f"r-{day}"),
        )
        for day in range(1, 337)
        for month, day_of_month in (((day - 1) // 28 + 1, (day - 1) % 28 + 1),)
        if month <= 12
    )
    store = BitemporalStore(records)
    selected = store.latest_as_of(
        source="daily",
        entity_id="000001.SZ",
        event_time=dt(5).replace(year=2024, month=6),
        decision_time=dt(5).replace(year=2024, month=6),
    )
    assert selected is not None
    assert selected.event_time <= dt(5).replace(year=2024, month=6)
    assert selected.available_at <= selected.decision_time

    raw = store.latest_as_of(
        source="daily",
        entity_id="000001.SZ",
        event_time=dt(5).replace(year=2024, month=6),
        decision_time=dt(5).replace(year=2024, month=6),
        bind_decision_time=False,
    )
    assert raw is not None
    assert raw.decision_time is None
    with pytest.raises(BitemporalError, match="must be boolean"):
        store.latest_as_of(
            source="daily",
            entity_id="000001.SZ",
            event_time=dt(5).replace(year=2024, month=6),
            decision_time=dt(5).replace(year=2024, month=6),
            bind_decision_time="false",
        )


def test_as_of_join_combines_financial_market_and_global_macro_features():
    records = [
        normalize_row("daily", daily()),
        normalize_row(
            "fina_indicator",
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240320",
                "end_date": "20231231",
                "roe": 12.5,
            },
        ),
        normalize_row("shibor", {"date": "20240329", "on": 1.75}),
    ]
    row = as_of_join(
        BitemporalStore(records),
        entity_id="000001.SZ",
        event_time=dt(1),
        decision_time=dt(1),
        fields=("close", "roe", "shibor_overnight"),
    )
    assert row.values == {"close": 10.0, "roe": 12.5, "shibor_overnight": 1.75}
    assert set(row.revision_ids) == set(row.values)


def test_future_perturbation_does_not_change_historical_feature_view():
    baseline = BitemporalStore((normalize_row("daily", daily()),))
    future = normalize_row("daily", daily(99, day="20240405", revision="future"))
    shocked = BitemporalStore((*baseline.records, future))
    kwargs = {
        "anchors": (("000001.SZ", dt(1)),),
        "decision_time": dt(1),
        "fields": ("close",),
    }
    left = build_feature_view(baseline, **kwargs)
    right = build_feature_view(shocked, **kwargs)
    assert left.artifact_hash == right.artifact_hash
    assert left.rows == right.rows


def test_feature_view_is_numeric_inert_and_canonically_ordered():
    records = (
        normalize_row("daily", daily(day="20240401", revision="a")),
        normalize_row("daily", daily(11, day="20240402", revision="b")),
    )
    view = build_feature_view(
        BitemporalStore(records),
        anchors=(("000001.SZ", dt(2)), ("000001.SZ", dt(1))),
        decision_time=dt(2),
        fields=("close", "volume"),
        sources=(SourceKind.DAILY,),
    )
    assert [row.event_time.day for row in view.rows] == [1, 2]
    assert all(
        value is None or isinstance(value, (int, float))
        for row in view.rows
        for value in row.values.values()
    )
    assert not hasattr(view, "execute")


def test_canonical_record_hash_is_independent_of_row_and_key_order():
    first = normalize_row("daily", daily())
    second = normalize_row("shibor", {"date": "20240401", "1w": 2, "on": 1})
    assert canonical_record_hash((first, second)) == canonical_record_hash((second, first))
    a = normalize_row("shibor", {"date": "20240401", "1w": 2, "on": 1})
    b = normalize_row("shibor", {"on": 1, "1w": 2, "date": "20240401"})
    assert a.artifact_hash == b.artifact_hash


def test_historical_feature_rows_do_not_backfill_later_revision():
    original = normalize_row(
        "income",
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240401",
            "end_date": "20231231",
            "revenue": 100,
            "revision_id": "r1",
        },
    )
    revised = normalize_row(
        "income",
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240403",
            "end_date": "20231231",
            "revenue": 120,
            "revision_id": "r2",
        },
    )
    rows = build_historical_feature_rows(
        BitemporalStore((original, revised)),
        anchors=(
            ("000001.SZ", dt(2, 15), dt(2, 17)),
            ("000001.SZ", dt(4, 15), dt(4, 17)),
        ),
        fields=("revenue",),
        sources=("income",),
    )
    assert [row.values["revenue"] for row in rows] == [100.0, 120.0]
