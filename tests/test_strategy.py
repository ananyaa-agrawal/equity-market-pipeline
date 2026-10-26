"""Tests for the strategy, above all that it has no look-ahead bias.

Look-ahead bias means using information from the future. The test here
gives the strategy prices for day T+1 as well, changes them wildly, and
checks that day T's signal and orders don't change at all.
"""

import pandas as pd
import pandas.testing as pdt

from pipeline import strategy

T_MINUS_1, T, T_PLUS_1 = "2026-01-07", "2026-01-08", "2026-01-09"

# Returns on day T: AAA -10%, BBB -5%, CCC 0%, DDD +5%
CLOSES = {"AAA.NS": (100, 90), "BBB.NS": (200, 190), "CCC.NS": (50, 50), "DDD.NS": (80, 84)}


def prices(t_plus_1_close=None):
    rows = []
    for ticker, (before, today) in CLOSES.items():
        rows.append({"date": T_MINUS_1, "ticker": ticker, "adj_open": before, "adj_close": before})
        rows.append({"date": T, "ticker": ticker, "adj_open": today, "adj_close": today})
        if t_plus_1_close is not None:
            rows.append({"date": T_PLUS_1, "ticker": ticker, "adj_open": t_plus_1_close,
                         "adj_close": t_plus_1_close})
    return pd.DataFrame(rows)


def test_selects_the_biggest_losers():
    signal = strategy.compute_signal(prices(), T, T_MINUS_1, excluded=set(), n_stocks=2)
    assert list(signal["ticker"]) == ["AAA.NS", "BBB.NS", "CCC.NS", "DDD.NS"]  # ranked worst first
    assert list(signal["selected"]) == [True, True, False, False]
    assert round(signal.iloc[0]["ret_1d"], 6) == -0.10


def test_excluded_stock_is_not_eligible_on_either_day():
    excluded = {(T, "AAA.NS"), (T_MINUS_1, "BBB.NS")}
    signal = strategy.compute_signal(prices(), T, T_MINUS_1, excluded, n_stocks=2)
    assert list(signal["ticker"]) == ["CCC.NS", "DDD.NS"]


def test_target_shares_are_rounded_down():
    signal = strategy.compute_signal(prices(), T, T_MINUS_1, excluded=set(), n_stocks=2)
    targets = strategy.target_positions(signal, capital_inr=1000, n_stocks=2)
    assert targets == {"AAA.NS": 5, "BBB.NS": 2}  # Rs 500 each: 500/90 = 5.6 -> 5, 500/190 = 2.6 -> 2


def test_orders_are_target_minus_actual_holdings():
    targets = {"AAA.NS": 5, "BBB.NS": 2}
    current = {"AAA.NS": 3, "DDD.NS": 7}  # AAA partly filled yesterday, DDD no longer selected
    marks = {"AAA.NS": 90.0, "BBB.NS": 190.0, "DDD.NS": 84.0}
    orders = strategy.make_orders(targets, current, marks, T).set_index("ticker")
    assert orders.loc["AAA.NS", ["side", "qty"]].tolist() == ["BUY", 2]  # top up the partial fill
    assert orders.loc["BBB.NS", ["side", "qty"]].tolist() == ["BUY", 2]
    assert orders.loc["DDD.NS", ["side", "qty"]].tolist() == ["SELL", 7]  # sell what's no longer wanted
    assert set(orders["trade_date"]) == {T_PLUS_1}


def test_no_look_ahead_future_prices_do_not_change_todays_decision():
    def decide(data):
        signal = strategy.compute_signal(data, T, T_MINUS_1, excluded=set(), n_stocks=2)
        targets = strategy.target_positions(signal, capital_inr=1_000_000, n_stocks=2)
        marks = dict(zip(signal["ticker"], signal["adj_close"]))
        return signal, strategy.make_orders(targets, {}, marks, T)

    signal_a, orders_a = decide(prices())                       # no T+1 data at all
    signal_b, orders_b = decide(prices(t_plus_1_close=1))       # T+1 crashes to Rs 1
    signal_c, orders_c = decide(prices(t_plus_1_close=10_000))  # T+1 soars to Rs 10,000
    for signal, orders in ((signal_b, orders_b), (signal_c, orders_c)):
        pdt.assert_frame_equal(signal, signal_a)
        pdt.assert_frame_equal(orders, orders_a)


def test_next_weekday_skips_the_weekend():
    assert strategy.next_weekday("2026-10-23") == "2026-10-26"  # Friday -> Monday
    assert strategy.next_weekday("2026-10-21") == "2026-10-22"  # Wednesday -> Thursday
