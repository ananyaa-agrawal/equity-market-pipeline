"""The trading strategy: turn one day's closing prices into orders for the next day.

The rule (a "short-term reversal" strategy): after the market closes on day
T, measure each stock's return that day. Pick the 5 biggest losers, put equal
money into each, and hold them until the next close, when we pick again.

The idea behind it: a big one-day drop is sometimes an over-reaction that
partly bounces back the next day. Whether that holds here, after trading
costs, is what the backtest measures. The strategy is deliberately simple; the
point of the project is testing it honestly.

LOOK-AHEAD BIAS is the classic way backtests lie: using information you
couldn't have had when the decision was made. Here that would be using day
T+1's prices to choose what to buy at T+1's open. A backtest with look-ahead
bias can look brilliant and then lose money from the first live day. Every
function below only reads day T and the day before it, and
tests/test_strategy.py proves that changing T+1 prices changes nothing.

Why it matters in a real trading firm: the strategy code should be small,
pure (same inputs give the same outputs, no hidden state) and easy to test,
so that what was backtested is exactly what trades live.
"""

import math
from datetime import date, timedelta

import pandas as pd

SIGNAL_COLUMNS = ["ticker", "adj_close", "ret_1d", "rank", "selected"]
ORDER_COLUMNS = ["order_id", "created_date", "trade_date", "ticker", "side", "qty", "signal_price"]


def compute_signal(prices, day, prev_day, excluded, n_stocks):
    """Rank stocks by their return on `day` and select the `n_stocks` biggest losers.

    prices:   rows with date, ticker and adj_close. Other days may be present;
              only `day` and `prev_day` are read.
    excluded: (date, ticker) pairs the quality checks ruled out. A stock must
              be usable on both days, otherwise its return isn't trustworthy.

    ret_1d = adj_close(T) / adj_close(T-1) - 1. Adjusted closes are used so a
    dividend isn't mistaken for a loss.

    Returns one row per eligible stock: ticker, adj_close, ret_1d, rank
    (1 = biggest loser) and selected (True for the chosen ones).
    """
    today = _usable_closes(prices, day, excluded)
    before = _usable_closes(prices, prev_day, excluded)
    signal = today.merge(before, on="ticker", suffixes=("", "_prev"))
    signal["ret_1d"] = signal["adj_close"] / signal["adj_close_prev"] - 1
    # Sort by return, then by ticker, so ties are always broken the same way.
    signal = signal.sort_values(["ret_1d", "ticker"]).reset_index(drop=True)
    signal["rank"] = range(1, len(signal) + 1)
    signal["selected"] = signal["rank"] <= n_stocks
    return signal[SIGNAL_COLUMNS]


def target_positions(signal, capital_inr, n_stocks):
    """How many shares we want of each selected stock: equal money in each.

    target shares = floor(capital per stock / adj_close(T)). We round down
    because you can't buy a fraction of a share and must not spend more
    than the money assigned.
    """
    per_stock = capital_inr / n_stocks
    chosen = signal[signal["selected"].astype(bool)]
    return {ticker: math.floor(per_stock / price) for ticker, price in zip(chosen["ticker"], chosen["adj_close"])}


def make_orders(targets, current, marks, day):
    """Orders for the next trading day = target shares - shares we actually hold.

    A stock we hold that is no longer selected has a target of 0, so it gets
    sold. Comparing with *actual* holdings (not what we hoped to hold) means a
    partly-filled order from yesterday is topped up automatically today.

    marks: the price of each stock on `day` (its adjusted close, or the last
    good one if today's was excluded), recorded as the order's signal price.
    """
    rows = []
    for ticker in sorted(set(targets) | set(current)):
        change = targets.get(ticker, 0) - current.get(ticker, 0)
        if change == 0:
            continue
        rows.append({
            "order_id": f"{day}_{ticker}",
            "created_date": day,
            "trade_date": next_weekday(day),
            "ticker": ticker,
            "side": "BUY" if change > 0 else "SELL",
            "qty": abs(change),
            "signal_price": marks[ticker],
        })
    return pd.DataFrame(rows, columns=ORDER_COLUMNS)


def next_weekday(day):
    """The planned trading date: the next Monday-to-Friday after `day`.

    We can't know about a holiday in advance from price data. If the next
    weekday turns out to be a holiday, the order simply executes on the next
    real trading day, which the fill records as its fill_date.
    """
    d = date.fromisoformat(day) + timedelta(days=1)
    while d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        d += timedelta(days=1)
    return d.isoformat()


def _usable_closes(prices, day, excluded):
    rows = prices[(prices["date"] == day) & prices["adj_close"].notna()]
    keep = [(day, ticker) not in excluded for ticker in rows["ticker"]]
    return rows.loc[keep, ["ticker", "adj_close"]]
