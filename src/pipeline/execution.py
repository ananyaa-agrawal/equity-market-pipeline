"""A simulated broker: decides how each order would have been filled.

Real orders don't execute at the price you saw when you decided. This module
models three realities of trading:

* SLIPPAGE: by the time your order reaches the market the price has moved a
  little, usually against you (buying pushes the price up, selling pushes it
  down). We model it as a fixed cost: buys pay 5 bps (0.05%) above the open,
  sells receive 5 bps below it.
* FEES: brokerage, exchange charges and taxes. Modelled as 10 bps of the
  traded value, on every buy and every sell.
* PARTIAL FILLS: sometimes only part of an order executes (not enough
  sellers at your price, for example). Here 5% of orders fill only 50-90%.

Orders created after day T's close are filled at day T+1's adjusted open, the
first price at which we could really have traded.

"Random" partial fills use a random-number generator seeded from
(random_seed, date, ticker), so rerunning a day gives exactly the same fills.
A backtest that changes every time you run it can't be checked or trusted.

Why it matters in a real trading firm: the gap between "the price I wanted"
and "the price I got" (implementation shortfall) is often what separates a
profitable strategy from a losing one. A backtest that ignores it is fiction.
"""

import math
import zlib

import numpy as np
import pandas as pd

FILL_COLUMNS = ["order_id", "fill_date", "ticker", "side", "ordered_qty", "filled_qty",
                "fill_price", "slippage_bps", "fees_inr"]
# Extra columns kept in memory for the PnL calculation, not stored in the fills table.
EXTRA_COLUMNS = ["open_price", "slippage_inr"]


def simulate_fills(orders, day, prices, excluded, cfg):
    """Fill `orders` at the adjusted open of `day`.

    If a stock has no usable open that day (excluded by the quality checks,
    or no data), its order doesn't fill: we record filled_qty = 0 with no
    price, and tomorrow's orders will try again.
    """
    slip_bps = cfg["costs"]["slippage_bps"]
    fee_rate = cfg["costs"]["fee_bps"] / 10_000
    today = prices[(prices["date"] == day) & prices["adj_open"].notna()]
    opens = {t: o for t, o in zip(today["ticker"], today["adj_open"]) if (day, t) not in excluded}

    rows = []
    for order in orders.itertuples(index=False):
        row = {"order_id": order.order_id, "fill_date": day, "ticker": order.ticker,
               "side": order.side, "ordered_qty": order.qty}
        open_price = opens.get(order.ticker)
        if open_price is None:
            rows.append({**row, "filled_qty": 0, "fill_price": None, "slippage_bps": None,
                         "fees_inr": 0.0, "open_price": None, "slippage_inr": 0.0})
            continue

        filled = fill_quantity(order.qty, day, order.ticker, cfg["broker"])
        direction = 1 if order.side == "BUY" else -1  # buys pay more, sells receive less
        price = open_price * (1 + direction * slip_bps / 10_000)
        rows.append({**row, "filled_qty": filled, "fill_price": price, "slippage_bps": slip_bps,
                     "fees_inr": filled * price * fee_rate, "open_price": open_price,
                     "slippage_inr": filled * abs(price - open_price)})
    return pd.DataFrame(rows, columns=FILL_COLUMNS + EXTRA_COLUMNS)


def fill_quantity(qty, day, ticker, broker):
    """How many of `qty` shares get filled: usually all, occasionally only part.

    The seed combines random_seed, the date and the ticker, so each (day,
    ticker) gets its own repeatable "dice roll". zlib.crc32 turns the ticker
    into a number. Python's built-in hash() would not work here: it changes
    every time Python starts.
    """
    seed = [broker["random_seed"], int(day.replace("-", "")), zlib.crc32(ticker.encode())]
    rng = np.random.default_rng(seed)
    if rng.random() < broker["partial_fill_prob"]:
        fraction = rng.uniform(broker["partial_fill_min"], broker["partial_fill_max"])
        return math.floor(qty * fraction)
    return qty


def apply_fills(positions, fills):
    """Holdings after the fills: buys add shares, sells remove them. Zero holdings are dropped."""
    new = dict(positions)
    for ticker, side, qty in zip(fills["ticker"], fills["side"], fills["filled_qty"]):
        new[ticker] = new.get(ticker, 0) + (qty if side == "BUY" else -qty)
    return {ticker: int(shares) for ticker, shares in new.items() if shares != 0}
