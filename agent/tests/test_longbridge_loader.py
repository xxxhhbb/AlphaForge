"""Contract tests for the optional Longbridge historical-data loader."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.loaders import longbridge as loader_mod
from backtest.loaders.base import NoAvailableSourceError


def test_wide_date_range_never_truncates_silently() -> None:
    start = dt.date(2000, 1, 1)
    end = start + dt.timedelta(days=loader_mod._MAX_WINDOW_DAYS * loader_mod._MAX_WINDOWS)

    with pytest.raises(NoAvailableSourceError, match="exceeds.*window limit"):
        loader_mod._date_windows(start, end)


@pytest.mark.parametrize("interval", ["2D", "4h", "garbage"])
def test_unsupported_intervals_fail_instead_of_changing_fidelity(
    interval: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_openapi = SimpleNamespace(Period=SimpleNamespace(Day="day"))
    monkeypatch.setattr(loader_mod, "_require_longbridge", lambda: fake_openapi)

    with pytest.raises(NoAvailableSourceError, match="unsupported Longbridge interval"):
        loader_mod._to_longport_period(interval)


def test_normalize_frame_converts_intraday_timestamps_to_naive_utc() -> None:
    bars = [
        SimpleNamespace(
            timestamp=pd.Timestamp("2026-07-14 09:30:00", tz="Asia/Hong_Kong"),
            open=10,
            high=11,
            low=9,
            close=10.5,
            volume=100,
        )
    ]

    frame = loader_mod._normalize_frame(bars)

    assert frame.index.tz is None
    assert frame.index[0] == pd.Timestamp("2026-07-14 01:30:00")


def test_is_available_does_not_make_a_market_data_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LONGBRIDGE_APP_KEY", "key")
    monkeypatch.setenv("LONGBRIDGE_APP_SECRET", "secret")
    monkeypatch.setenv("LONGBRIDGE_ACCESS_TOKEN", "token")
    fake_openapi = SimpleNamespace()
    monkeypatch.setattr(loader_mod, "_require_longbridge", lambda: fake_openapi)

    assert loader_mod.LongbridgeLoader().is_available() is True


def test_fetch_rejects_missing_credentials_before_sdk_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LONGBRIDGE_APP_KEY", raising=False)
    monkeypatch.delenv("LONGBRIDGE_APP_SECRET", raising=False)
    monkeypatch.delenv("LONGBRIDGE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        loader_mod,
        "_require_longbridge",
        lambda: (_ for _ in ()).throw(AssertionError("SDK must not initialize")),
    )

    with pytest.raises(NoAvailableSourceError, match="credentials are not configured"):
        loader_mod.LongbridgeLoader().fetch(
            ["AAPL"], "2026-01-01", "2026-01-02"
        )


def _configured_loader() -> loader_mod.LongbridgeLoader:
    loader = loader_mod.LongbridgeLoader.__new__(loader_mod.LongbridgeLoader)
    loader._app_key = "key"
    loader._app_secret = "secret"
    loader._access_token = "token"
    return loader


def test_fetch_combines_all_windows_and_caches_complete_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[dt.date, dt.date]] = []
    cached: list[pd.DataFrame] = []

    class FakeQuoteContext:
        def __init__(self, config: object) -> None:
            assert config == ("key", "secret", "token")

        def history_candlesticks_by_date(
            self,
            symbol: str,
            period: str,
            adjust_type: str,
            *,
            start: dt.date,
            end: dt.date,
        ) -> list[SimpleNamespace]:
            assert symbol == "AAPL.US"
            assert period == "day"
            assert adjust_type == "none"
            calls.append((start, end))
            return [
                SimpleNamespace(
                    timestamp=pd.Timestamp(start),
                    open=10,
                    high=11,
                    low=9,
                    close=10.5,
                    volume=100,
                )
            ]

    fake_openapi = SimpleNamespace(
        Config=lambda *args: args,
        QuoteContext=FakeQuoteContext,
        Period=SimpleNamespace(Day="day"),
        AdjustType=SimpleNamespace(NoAdjust="none"),
    )
    monkeypatch.setattr(loader_mod, "_require_longbridge", lambda: fake_openapi)
    monkeypatch.setattr(loader_mod, "loader_cache_get", lambda **kwargs: None)
    monkeypatch.setattr(
        loader_mod,
        "loader_cache_put",
        lambda **kwargs: cached.append(kwargs["frame"].copy()),
    )

    result = _configured_loader().fetch(
        ["AAPL"], "2026-01-01", "2026-07-01", interval="1D"
    )

    assert calls == [
        (dt.date(2026, 1, 1), dt.date(2026, 6, 29)),
        (dt.date(2026, 6, 30), dt.date(2026, 7, 1)),
    ]
    assert list(result) == ["AAPL"]
    assert len(result["AAPL"]) == 2
    assert len(cached) == 1
    pd.testing.assert_frame_equal(cached[0], result["AAPL"])


def test_fetch_rejects_partial_history_when_a_window_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeQuoteContext:
        def __init__(self, config: object) -> None:
            pass

        def history_candlesticks_by_date(self, *args: object, **kwargs: object):
            if kwargs["start"] == dt.date(2026, 6, 30):
                raise RuntimeError("quota exhausted")
            return [
                SimpleNamespace(
                    timestamp=pd.Timestamp(kwargs["start"]),
                    open=10,
                    high=11,
                    low=9,
                    close=10.5,
                    volume=100,
                )
            ]

    fake_openapi = SimpleNamespace(
        Config=lambda *args: args,
        QuoteContext=FakeQuoteContext,
        Period=SimpleNamespace(Day="day"),
        AdjustType=SimpleNamespace(NoAdjust="none"),
    )
    monkeypatch.setattr(loader_mod, "_require_longbridge", lambda: fake_openapi)
    monkeypatch.setattr(loader_mod, "loader_cache_get", lambda **kwargs: None)
    monkeypatch.setattr(
        loader_mod,
        "loader_cache_put",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not cache")),
    )

    with pytest.raises(NoAvailableSourceError, match="incomplete Longbridge history"):
        _configured_loader().fetch(
            ["AAPL"], "2026-01-01", "2026-07-01", interval="1D"
        )
