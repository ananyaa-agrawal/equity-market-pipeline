"""Reconciliation: compare what we meant to trade with what actually happened.

We keep two "books" (records of holdings and profit):

* EXPECTED BOOK: the strategy on paper. Every order fills in full at its
  signal price (day T's close, the price the decision was based on), with no
  fees and no slippage.
* ACTUAL BOOK: what the simulated broker really did: fills at day T+1's
  open, with slippage, fees and the occasional partial fill.

Every day we explain the difference between them, three ways:

1. POSITION BREAKS: for each stock, expected shares vs actual shares. Any
   difference (a "break") goes into recon_breaks. Here breaks come from
   partial or missed fills, and are normally gone the next day, because the
   next day's orders are "target minus actual" and top up the shortfall.
   (A break only lasts longer if the top-up order is itself partly filled.)
2. IMPLEMENTATION SHORTFALL, per fill: how much worse our price was than the
   signal price, in rupees and basis points. It's split into
     overnight gap = open(T+1) - close(T): the market moved before we could trade
     slippage      = fill price - open(T+1): our own cost of trading
3. PnL BRIDGE: expected PnL - actual PnL
                 = gap + slippage + fees + partial-fill effect + residual.
   The first four are real, explainable costs. The residual is whatever is
   left over, and it should be about zero. If it isn't (more than Rs 1), the
   two books disagree for a reason we can't explain: a bug, or data that
   changed after the signal was made. Either way, someone must look.

Why it matters in a real trading firm: reconciliation is a daily control.
Operations teams match internal records against the broker's statements,
and every break must be explained before the next trading day. It is how
errors (and occasionally fraud) get caught while they're still small.
"""

import logging

import numpy as np
import pandas as pd

from pipeline import execution

log = logging.getLogger(__name__)

RESIDUAL_TOLERANCE_INR = 1.0
BREAK_COLUMNS = ["run_id", "date", "ticker", "break_type", "expected", "actual", "difference"]
SHORTFALL_COLUMNS = ["order_id", "ticker", "side", "qty", "filled_qty", "signal_price", "open_price",
                     "fill_price", "gap_inr", "slippage_inr", "shortfall_inr",
                     "gap_bps", "slippage_bps", "shortfall_bps"]


def reconcile_day(day, positions_before, positions_after, orders, fills,
                  marks_before, marks_today, actual_pnl, run_id):
    """Reconcile one day: the orders created yesterday and the fills they got today.

    positions_before: actual holdings at yesterday's close
    positions_after:  actual holdings at today's close
    orders:           the orders created yesterday (with their signal prices)
    fills:            today's fills of those orders (from execution.simulate_fills)
    marks_before/marks_today: the prices each book is valued at (metrics.mark_prices)
    actual_pnl:       today's net PnL of the actual book (from metrics.daily_pnl)
    """
    expected = expected_positions(positions_before, orders)
    trades = orders.merge(fills[["order_id", "filled_qty", "fill_price", "open_price", "fees_inr"]],
                          on="order_id", how="left")
    trades["filled_qty"] = trades["filled_qty"].fillna(0)
    shortfall = implementation_shortfall(trades)
    return {
        "expected_positions": expected,
        "position_breaks": position_breaks(day, expected, positions_after, run_id),
        "shortfall": shortfall,
        "bridge": pnl_bridge(day, expected, trades, shortfall, marks_before, marks_today, actual_pnl),
    }


def expected_positions(positions_before, orders):
    """The expected book's holdings: yesterday's holdings plus every order filled in full.

    Since orders are "target minus actual", this is exactly the strategy's target.
    """
    return execution.apply_fills(positions_before, orders.rename(columns={"qty": "filled_qty"}))


def position_breaks(day, expected, actual, run_id):
    """One row per stock where the two books hold a different number of shares."""
    rows = []
    for ticker in sorted(set(expected) | set(actual)):
        e, a = expected.get(ticker, 0), actual.get(ticker, 0)
        if e != a:
            rows.append({"run_id": run_id, "date": day, "ticker": ticker, "break_type": "position",
                         "expected": e, "actual": a, "difference": a - e})
    return pd.DataFrame(rows, columns=BREAK_COLUMNS)


def implementation_shortfall(trades):
    """For each fill: how much worse than the signal price did we trade?

    Positive numbers are costs. For a buy, paying more than the signal price
    is a cost; for a sell, receiving less is a cost. Hence the `direction`
    (+1 buy, -1 sell) in each formula.
    """
    done = trades[trades["filled_qty"] > 0].copy()
    direction = np.where(done["side"] == "BUY", 1, -1)
    qty, signal = done["filled_qty"], done["signal_price"]
    done["gap_inr"] = direction * qty * (done["open_price"] - signal)
    done["slippage_inr"] = direction * qty * (done["fill_price"] - done["open_price"])
    done["shortfall_inr"] = done["gap_inr"] + done["slippage_inr"]
    for part in ("gap", "slippage", "shortfall"):
        done[f"{part}_bps"] = done[f"{part}_inr"] / (qty * signal) * 10_000
    return done[SHORTFALL_COLUMNS]


def pnl_bridge(day, expected, trades, shortfall, marks_before, marks_today, actual_pnl):
    """Explain expected PnL - actual PnL, rupee by rupee.

    expected PnL:        the expected book holds its target from today's
                         signal price to tomorrow's close
    partial-fill effect: for every share we *failed* to trade, the profit
                         (or loss) the expected book made on it and we didn't
    """
    expected_pnl = sum(shares * (marks_today[t] - marks_before[t]) for t, shares in expected.items())

    direction = np.where(trades["side"] == "BUY", 1, -1)
    unfilled = direction * (trades["qty"] - trades["filled_qty"])
    partial = sum(u * (marks_today[t] - s)
                  for u, t, s in zip(unfilled, trades["ticker"], trades["signal_price"]) if u != 0)

    gap = float(shortfall["gap_inr"].sum())
    slippage = float(shortfall["slippage_inr"].sum())
    fees = float(trades["fees_inr"].fillna(0).sum())
    difference = float(expected_pnl - actual_pnl)
    residual = difference - (gap + slippage + fees + partial)
    if abs(residual) > RESIDUAL_TOLERANCE_INR:
        log.warning("%s: PnL bridge does not reconcile, residual Rs %.2f (expected %.2f, actual %.2f). "
                    "This means a bug or data that changed after the signal.",
                    day, residual, expected_pnl, actual_pnl)
    return {"expected_pnl": float(expected_pnl), "actual_pnl": float(actual_pnl), "difference": difference,
            "gap": gap, "slippage": slippage, "fees": fees, "partial_fill": float(partial),
            "residual": float(residual)}


def format_bridge(bridge, title):
    """The bridge as a small readable table (amounts in INR)."""
    lines = [
        title,
        f"  expected PnL (paper: full fills at signal price, no costs) {bridge['expected_pnl']:>16,.2f}",
        f"  actual PnL (simulated fills, after costs)                  {bridge['actual_pnl']:>16,.2f}",
        f"  difference                                                 {bridge['difference']:>16,.2f}",
        f"    = overnight gap                                          {bridge['gap']:>16,.2f}",
        f"    + slippage                                               {bridge['slippage']:>16,.2f}",
        f"    + fees                                                   {bridge['fees']:>16,.2f}",
        f"    + partial-fill effect                                    {bridge['partial_fill']:>16,.2f}",
        f"    + residual (should be ~0)                                {bridge['residual']:>16,.2f}",
    ]
    return "\n".join(lines)
