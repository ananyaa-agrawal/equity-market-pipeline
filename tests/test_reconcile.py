"""Tests for reconciliation, built around one example small enough to check by hand.

The day before ("T"):  we hold 100 AAA and 10 CCC. The strategy wants to sell
                       all AAA (signal price 50) and buy 50 BBB (signal price 200).
The next day ("T+1"):  AAA opens at 49, BBB opens at 203 (overnight gaps).
                       The AAA sale fills in full; the BBB buy only fills 30 of 50.
                       Closes: AAA 48, BBB 210, CCC 31. Slippage 5 bps, fees 10 bps.

By hand:
  expected PnL = 50 BBB x (210 - 200) + 10 CCC x (31 - 30)          = 510.00
  overnight gap  = sell 100 AAA x (50 - 49) + buy 30 BBB x (203 - 200) = 190.00
  slippage       = 100 x 49 x 0.0005 + 30 x 203 x 0.0005                =   5.495
  fees           = 0.001 x (100 x 48.9755 + 30 x 203.1015)              =  10.990595
  partial fill   = 20 BBB not bought x (210 - 200)                      = 200.00
  so actual PnL should be 510 - 190 - 5.495 - 10.990595 - 200            = 103.514405
"""

import logging

import pandas as pd
import pytest

from pipeline import metrics, reconcile

T, T1 = "2026-01-08", "2026-01-09"
BEFORE = {"AAA.NS": 100, "CCC.NS": 10}
AFTER = {"BBB.NS": 30, "CCC.NS": 10}
MARKS_T = {"AAA.NS": 50.0, "BBB.NS": 200.0, "CCC.NS": 30.0}
MARKS_T1 = {"AAA.NS": 48.0, "BBB.NS": 210.0, "CCC.NS": 31.0}
ORDERS = pd.DataFrame([
    {"order_id": f"{T}_AAA.NS", "created_date": T, "trade_date": T1, "ticker": "AAA.NS",
     "side": "SELL", "qty": 100, "signal_price": 50.0},
    {"order_id": f"{T}_BBB.NS", "created_date": T, "trade_date": T1, "ticker": "BBB.NS",
     "side": "BUY", "qty": 50, "signal_price": 200.0},
])


def fill(ticker, side, ordered, filled, open_price):
    price = open_price * (1 + (0.0005 if side == "BUY" else -0.0005))
    return {"order_id": f"{T}_{ticker}", "fill_date": T1, "ticker": ticker, "side": side,
            "ordered_qty": ordered, "filled_qty": filled, "fill_price": price, "slippage_bps": 5,
            "fees_inr": filled * price * 0.001, "open_price": open_price,
            "slippage_inr": filled * abs(price - open_price)}


FILLS = pd.DataFrame([fill("AAA.NS", "SELL", 100, 100, 49.0), fill("BBB.NS", "BUY", 50, 30, 203.0)])


def reconcile_example(fills=FILLS, after=AFTER, pnl_error=0.0):
    actual = metrics.daily_pnl(T1, BEFORE, fills, MARKS_T, MARKS_T1, 1_000_000, 1_000_000)
    return reconcile.reconcile_day(T1, BEFORE, after, ORDERS, fills, MARKS_T, MARKS_T1,
                                   actual["net_pnl"] + pnl_error, run_id="test")


def test_pnl_bridge_matches_the_hand_calculation_and_residual_is_zero():
    bridge = reconcile_example()["bridge"]
    assert bridge["expected_pnl"] == pytest.approx(510.0)
    assert bridge["actual_pnl"] == pytest.approx(103.514405)
    assert bridge["gap"] == pytest.approx(190.0)
    assert bridge["slippage"] == pytest.approx(5.495)
    assert bridge["fees"] == pytest.approx(10.990595)
    assert bridge["partial_fill"] == pytest.approx(200.0)
    assert abs(bridge["residual"]) < 1e-9


def test_expected_book_holds_the_full_target():
    assert reconcile_example()["expected_positions"] == {"BBB.NS": 50, "CCC.NS": 10}


def test_partial_fill_creates_a_position_break():
    breaks = reconcile_example()["position_breaks"]
    assert len(breaks) == 1
    row = breaks.iloc[0]
    assert (row["ticker"], row["break_type"], row["expected"], row["actual"], row["difference"]) == \
        ("BBB.NS", "position", 50, 30, -20)


def test_implementation_shortfall_per_fill_in_bps():
    shortfall = reconcile_example()["shortfall"].set_index("ticker")
    assert shortfall.loc["AAA.NS", "gap_bps"] == pytest.approx(200.0)      # sold at 49 vs signal 50
    assert shortfall.loc["BBB.NS", "gap_bps"] == pytest.approx(150.0)      # bought at 203 vs signal 200
    assert shortfall.loc["BBB.NS", "slippage_bps"] == pytest.approx(30 * 0.1015 / (30 * 200) * 10_000)
    assert (shortfall["shortfall_inr"] == shortfall["gap_inr"] + shortfall["slippage_inr"]).all()


def test_order_that_never_filled_is_explained_too():
    # BBB was excluded by the quality checks on T+1, so the buy didn't fill at all.
    no_fill = {"order_id": f"{T}_BBB.NS", "fill_date": T1, "ticker": "BBB.NS", "side": "BUY",
               "ordered_qty": 50, "filled_qty": 0, "fill_price": None, "slippage_bps": None,
               "fees_inr": 0.0, "open_price": None, "slippage_inr": 0.0}
    fills = pd.DataFrame([fill("AAA.NS", "SELL", 100, 100, 49.0), no_fill])
    result = reconcile_example(fills, after={"CCC.NS": 10})
    assert result["bridge"]["partial_fill"] == pytest.approx(50 * (210 - 200))
    assert abs(result["bridge"]["residual"]) < 1e-9
    assert result["position_breaks"].iloc[0]["difference"] == -50


def test_books_that_disagree_leave_a_residual_and_log_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        bridge = reconcile_example(pnl_error=5.0)["bridge"]  # pretend the actual PnL was miscalculated
    assert bridge["residual"] == pytest.approx(-5.0)
    assert "does not reconcile" in caplog.text
