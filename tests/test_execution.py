"""Tests for the simulated broker: costs go the right way, and fills are repeatable."""

import pandas as pd
import pytest

from pipeline import execution

DAY = "2026-01-09"
CFG = {
    "costs": {"slippage_bps": 5, "fee_bps": 10},
    "broker": {"partial_fill_prob": 0.0, "partial_fill_min": 0.5, "partial_fill_max": 0.9, "random_seed": 42},
}
PRICES = pd.DataFrame([
    {"date": DAY, "ticker": "AAA.NS", "adj_open": 100.0, "adj_close": 101.0},
    {"date": DAY, "ticker": "BBB.NS", "adj_open": 200.0, "adj_close": 199.0},
])


def order(ticker, side, qty):
    return {"order_id": f"2026-01-08_{ticker}", "created_date": "2026-01-08", "trade_date": DAY,
            "ticker": ticker, "side": side, "qty": qty, "signal_price": 99.0}


def test_buy_pays_slippage_and_fees():
    fills = execution.simulate_fills(pd.DataFrame([order("AAA.NS", "BUY", 10)]), DAY, PRICES, set(), CFG)
    fill = fills.iloc[0]
    assert fill["filled_qty"] == 10
    assert fill["fill_price"] == pytest.approx(100.05)            # open 100 + 5 bps
    assert fill["fees_inr"] == pytest.approx(10 * 100.05 * 0.001)  # 10 bps of traded value
    assert fill["slippage_inr"] == pytest.approx(10 * 0.05)


def test_sell_receives_less_than_the_open():
    fills = execution.simulate_fills(pd.DataFrame([order("BBB.NS", "SELL", 4)]), DAY, PRICES, set(), CFG)
    assert fills.iloc[0]["fill_price"] == pytest.approx(199.90)    # open 200 - 5 bps


def test_excluded_stock_does_not_fill():
    fills = execution.simulate_fills(pd.DataFrame([order("AAA.NS", "BUY", 10)]), DAY, PRICES,
                                     {(DAY, "AAA.NS")}, CFG)
    assert fills.iloc[0]["filled_qty"] == 0 and pd.isna(fills.iloc[0]["fill_price"])


def test_partial_fills_are_repeatable_and_about_5_percent():
    broker = {**CFG["broker"], "partial_fill_prob": 0.05}
    days = pd.bdate_range("2022-01-01", periods=400).strftime("%Y-%m-%d")
    tickers = ["AAA.NS", "BBB.NS", "CCC.NS", "DDD.NS", "EEE.NS"]
    first = [execution.fill_quantity(100, d, t, broker) for d in days for t in tickers]
    second = [execution.fill_quantity(100, d, t, broker) for d in days for t in tickers]
    assert first == second                       # same seed, same day, same ticker -> same fill
    partial = [q for q in first if q < 100]
    assert 0.03 < len(partial) / len(first) < 0.07
    assert all(50 <= q <= 90 for q in partial)   # between partial_fill_min and partial_fill_max


def test_apply_fills_updates_holdings():
    fills = pd.DataFrame([{"ticker": "AAA.NS", "side": "BUY", "filled_qty": 5},
                          {"ticker": "BBB.NS", "side": "SELL", "filled_qty": 4}])
    assert execution.apply_fills({"AAA.NS": 2, "BBB.NS": 4}, fills) == {"AAA.NS": 7}
