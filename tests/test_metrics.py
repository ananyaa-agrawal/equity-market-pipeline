"""Tests for PnL and performance statistics, on examples small enough to check by hand."""

import pandas as pd
import pytest

from pipeline import metrics


def test_daily_pnl_by_hand():
    # Held 10 AAA overnight: price 100 -> 110, so +100.
    # Bought 5 BBB at the open of 50 plus 5 bps slippage (fill 50.025); it closed at 52.
    fills = pd.DataFrame([{"ticker": "BBB.NS", "side": "BUY", "filled_qty": 5, "fill_price": 50.025,
                           "fees_inr": 0.25, "slippage_inr": 0.125}])
    pnl = metrics.daily_pnl("2026-01-09", {"AAA.NS": 10}, fills,
                            marks_before={"AAA.NS": 100.0}, marks_today={"AAA.NS": 110.0, "BBB.NS": 52.0},
                            equity_before=1_000_000, capital_inr=1_000_000)
    trading = 5 * (52 - 50.025)
    assert pnl["net_pnl"] == pytest.approx(100 + trading - 0.25)
    assert pnl["gross_pnl"] == pytest.approx(100 + 5 * (52 - 50))  # as if bought at the open, free
    assert pnl["actual_equity"] == pytest.approx(1_000_000 + pnl["net_pnl"])


def test_missing_price_raises_a_clear_error():
    empty = pd.DataFrame(columns=["ticker", "side", "filled_qty", "fill_price", "fees_inr", "slippage_inr"])
    with pytest.raises(ValueError, match="no usable price"):
        metrics.daily_pnl("2026-01-09", {"AAA.NS": 10}, empty, {"AAA.NS": 100.0}, {}, 1_000_000, 1_000_000)


def test_performance_on_known_returns():
    pnl = pd.DataFrame({"date": ["d1", "d2", "d3", "d4"],
                        "gross_pnl": [1000.0, -500.0, -1000.0, 2000.0],
                        "net_pnl": [900.0, -600.0, -1100.0, 1900.0]})
    fills = pd.DataFrame({"fill_date": ["d1"], "filled_qty": [100], "fill_price": [400.0]})
    perf = metrics.performance(pnl, fills, capital_inr=100_000)
    assert perf.loc["total return", "gross"] == pytest.approx(0.015)
    assert perf.loc["total return", "net"] == pytest.approx(0.011)
    assert perf.loc["max drawdown", "gross"] == pytest.approx(-0.015)  # from +1% down to -0.5%
    assert perf.loc["hit rate", "gross"] == pytest.approx(0.5)
    assert perf.loc["avg daily turnover", "net"] == pytest.approx(40_000 / 4 / 100_000)


def test_sharpe_of_constant_returns_is_undefined():
    assert pd.isna(metrics.sharpe(pd.Series([0.01, 0.01, 0.01])))
