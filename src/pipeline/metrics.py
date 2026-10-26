"""Profit and loss (PnL) and the performance statistics a trading desk looks at.

Two parts:
1. daily_pnl: how much money the strategy made or lost on one day, split into
   "gross" (before trading costs) and "net" (after slippage and fees).
2. performance: the summary statistics for the whole backtest, with gross and
   net side by side. The gap between the two columns shows how much of the
   strategy's edge the costs eat.

Accounting choice: the strategy always invests the same Rs 10 lakh
(capital_inr) and never reinvests profits. Daily return is therefore
PnL / capital_inr, and returns *add up* over time rather than compound.

Why it matters in a real trading firm: a strategy is judged on its
risk-adjusted return after costs, not on its best day. These are the numbers
a risk manager asks for first.
"""

import math

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252  # roughly how many days a year the market is open


def mark_prices(prices, excluded, day):
    """The price each stock is valued at on `day` ("marking to market").

    That's the day's adjusted close. If that price was excluded by the
    quality checks, we use the last good close before it rather than a
    number we don't trust.
    """
    usable = prices[(prices["date"] <= day) & prices["adj_close"].notna()]
    keep = pd.Series([(d, t) not in excluded for d, t in zip(usable["date"], usable["ticker"])],
                     index=usable.index, dtype=bool)
    usable = usable[keep]
    latest = usable.sort_values("date").groupby("ticker").tail(1)
    return dict(zip(latest["ticker"], latest["adj_close"]))


def daily_pnl(day, positions_before, fills, marks_before, marks_today, equity_before, capital_inr):
    """One day's PnL for the actual (simulated) book.

    holding PnL: shares held overnight x (today's price - yesterday's price)
    trading PnL: shares bought or sold today x (today's close - the fill price)
    net PnL     = holding PnL + trading PnL - fees      (slippage is already in the fill prices)
    gross PnL   = net PnL + fees + slippage             (what we'd have made trading at the open for free)
    """
    needed = set(positions_before) | set(fills.loc[fills["filled_qty"] > 0, "ticker"])
    missing = sorted(t for t in needed if t not in marks_today or (t in positions_before and t not in marks_before))
    if missing:
        raise ValueError(f"{day}: no usable price to value {missing}")

    holding = sum(shares * (marks_today[t] - marks_before[t]) for t, shares in positions_before.items())
    done = fills[fills["filled_qty"] > 0]
    signed_qty = np.where(done["side"] == "BUY", done["filled_qty"], -done["filled_qty"])
    trading = sum(q * (marks_today[t] - p) for q, t, p in zip(signed_qty, done["ticker"], done["fill_price"]))
    fees = float(done["fees_inr"].sum())
    slippage = float(done["slippage_inr"].sum())

    net = float(holding + trading - fees)
    return {
        "date": day,
        "expected_equity": None,  # filled in by reconcile.py, which runs the expected book
        "actual_equity": equity_before + net,
        "actual_return": net / capital_inr,
        "gross_pnl": net + fees + slippage,
        "net_pnl": net,
        "fees_inr": fees,
        "slippage_inr": slippage,
    }


def performance(pnl, fills, capital_inr):
    """Summary statistics, gross and net side by side.

    pnl:   the pnl_daily table (one row per day)
    fills: the fills table (for turnover)
    """
    pnl = pnl.sort_values("date")
    traded = fills.assign(value=fills["filled_qty"] * fills["fill_price"].fillna(0))
    traded_per_day = traded.groupby("fill_date")["value"].sum().reindex(pnl["date"], fill_value=0)

    columns = {}
    for label, col in (("gross", "gross_pnl"), ("net", "net_pnl")):
        r = pnl[col] / capital_inr
        columns[label] = {
            "total return": r.sum(),
            "annualised return": r.mean() * TRADING_DAYS_PER_YEAR,
            "annualised volatility": r.std() * math.sqrt(TRADING_DAYS_PER_YEAR),
            "Sharpe ratio": sharpe(r),
            "max drawdown": max_drawdown(r),
            "avg daily turnover": traded_per_day.mean() / capital_inr,
            "hit rate": (r > 0).sum() / max((r != 0).sum(), 1),
        }
    return pd.DataFrame(columns)


def sharpe(daily_returns):
    """SHARPE RATIO: average return per unit of risk, scaled to a year.

    Sharpe = mean(daily return) / std(daily return) x sqrt(252), with the
    risk-free rate taken as 0. Return grows ~252x over a year but volatility
    only ~sqrt(252)x, hence the square root. Rough guide: below 0 = losing
    money, 0.5 = weak, 1 = decent, 2+ = excellent (and worth double-checking).
    """
    std = daily_returns.std()
    return float("nan") if std == 0 or np.isnan(std) else daily_returns.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def max_drawdown(daily_returns):
    """MAX DRAWDOWN: the worst fall from a peak to a later low, as a share of capital.

    It answers "what's the most I'd have lost if I'd started at the worst
    possible moment?" Because returns add up here, we work with cumulative
    return: drawdown = cumulative return - its highest value so far. A value
    of -0.25 means at worst you were down 25% of capital from the best point.
    """
    cumulative = pd.concat([pd.Series([0.0]), daily_returns.cumsum()], ignore_index=True)
    return float((cumulative - cumulative.cummax()).min())
