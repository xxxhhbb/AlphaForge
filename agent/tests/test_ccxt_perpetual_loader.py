"""Historical Binance USD-M data-contract tests."""

from __future__ import annotations

import pytest
import pandas as pd

from backtest.loaders.base import make_loader_cache_key
from backtest.loaders.ccxt_loader import _parse_ccxt_symbol


def _hourly_rows(opens: list[float]) -> list[list[float]]:
    start = int(pd.Timestamp("2024-01-01 00:00:00").timestamp() * 1000)
    hour = 3_600_000
    return [
        [start + i * hour, value, value + 2, value - 2, value + 1, 10 + i]
        for i, value in enumerate(opens)
    ]


class _PerpetualExchange:
    def __init__(self, *, mark_rows: list[list[float]] | None = None) -> None:
        self.trade_rows = _hourly_rows([100.0, 101.0])
        self.mark_rows = mark_rows if mark_rows is not None else _hourly_rows([99.0, 100.0])
        self.calls: list[dict[str, object]] = []

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None, params=None):
        self.calls.append({
            "symbol": symbol,
            "timeframe": timeframe,
            "since": since,
            "limit": limit,
            "params": params,
        })
        return self.mark_rows if params == {"price": "mark"} else self.trade_rows


def test_spot_symbol_keeps_existing_ccxt_contract() -> None:
    assert _parse_ccxt_symbol("BTC-USDT") == ("BTC/USDT", "spot")


def test_perpetual_symbol_maps_to_binance_usdm_contract() -> None:
    assert _parse_ccxt_symbol("BTC-USDT-PERP") == ("BTC/USDT:USDT", "swap")


@pytest.mark.parametrize("code", ["BTC-PERP", "-USDT-PERP", "BTC--PERP"])
def test_malformed_perpetual_symbol_is_rejected(code: str) -> None:
    with pytest.raises(ValueError, match="USD-M perpetual symbol"):
        _parse_ccxt_symbol(code)


def test_spot_and_perpetual_cache_keys_cannot_collide() -> None:
    common = {
        "source": "ccxt",
        "timeframe": "1H",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
        "fields": None,
    }
    spot = make_loader_cache_key(symbol="BTC-USDT", **common)
    perpetual = make_loader_cache_key(symbol="BTC-USDT-PERP", **common)
    assert spot != perpetual


def test_perpetual_fetch_separates_execution_and_mark_prices(monkeypatch) -> None:
    exchange = _PerpetualExchange()
    monkeypatch.setattr(
        "backtest.loaders.ccxt_loader.DataLoader._get_exchange",
        lambda _self, instrument_type="spot": exchange,
    )

    from backtest.loaders.ccxt_loader import DataLoader

    frame = DataLoader().fetch(
        ["BTC-USDT-PERP"], "2024-01-01", "2024-01-01", interval="1H"
    )["BTC-USDT-PERP"]

    assert frame["execution_open"].tolist() == [100.0, 101.0]
    assert frame["mark_open"].tolist() == [99.0, 100.0]
    assert frame["mark_high"].tolist() == [101.0, 102.0]
    assert frame["mark_low"].tolist() == [97.0, 98.0]
    assert frame["mark_close"].tolist() == [100.0, 101.0]
    assert exchange.calls[0]["symbol"] == "BTC/USDT:USDT"
    assert exchange.calls[0]["params"] is None
    assert exchange.calls[1]["params"] == {"price": "mark"}


def test_perpetual_fetch_rejects_unsynchronized_mark_rows(monkeypatch) -> None:
    mark_rows = _hourly_rows([99.0, 100.0])
    mark_rows[1][0] += 60_000
    exchange = _PerpetualExchange(mark_rows=mark_rows)
    monkeypatch.setattr(
        "backtest.loaders.ccxt_loader.DataLoader._get_exchange",
        lambda _self, instrument_type="spot": exchange,
    )

    from backtest.loaders.ccxt_loader import DataLoader

    with pytest.raises(ValueError, match="mark-price timestamps"):
        DataLoader().fetch(
            ["BTC-USDT-PERP"], "2024-01-01", "2024-01-01", interval="1H"
        )
