"""Data-quality checks: find bad or suspicious prices before anything trades on them.

Each check looks for one kind of problem and returns a list of issues. Every
issue has a date, a ticker, the check's name, a severity and a plain-English
explanation:

    ERROR    the data is wrong: don't use this ticker on this day
    WARNING  something is off: some warnings exclude the ticker for the day
             (see EXCLUDING), others are only for a human to look at
    INFO     looked suspicious but has a known explanation

Two levels of response:
* Ticker level: a broken price for one stock excludes that stock for that
  day, and the run carries on with the others.
* Run level: if there are no usable prices at all for the latest trading day
  (freshness ERROR), the run stops. Trading on yesterday's data as if it
  were today's is worse than not trading.

We never delete or "fix" the downloaded rows. The prices table keeps exactly
what Yahoo sent, and dq_issues records what we think of it. Anyone can later
see both the raw data and why a day was skipped.

Why it matters in a real trading firm: bad data costs money. A missing
dividend adjustment looks like a 20% crash, and a strategy that buys "big
losers" would pile into it. A frozen price looks like a stock that never
moves. Firms run checks like these on every dataset, every day, before any
model sees it.
"""

import logging
from datetime import date, timedelta

import pandas as pd

from pipeline import db

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 30  # calendar days of history the look-back checks and price lookups use

CHECKS = [
    "freshness", "missing_days", "ohlc_consistency", "stale_price",
    "jump", "duplicates_nulls", "universe", "placeholder_bar",
]
SEVERITIES = ["ERROR", "WARNING", "INFO"]
ISSUE_COLUMNS = ["run_id", "date", "ticker", "check_name", "severity", "details"]
REQUIRED_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]
PRICE_FIELDS = ["open", "high", "low", "close", "adj_close"]

# Which findings remove a ticker from trading on that day. A stale price is only
# flagged for a human to look at, and a jump explained by a split (INFO) is fine.
EXCLUDING = {
    ("missing_days", "WARNING"),
    ("ohlc_consistency", "ERROR"),
    ("jump", "WARNING"),
    ("duplicates_nulls", "ERROR"),
    ("universe", "WARNING"),
    ("placeholder_bar", "WARNING"),
}


class DataQualityError(Exception):
    """Raised when the data is too broken to run at all."""


def run_checks(prices, calendar_dates, corporate_actions, cfg, run_id, check_dates=None):
    """Run all eight checks and return the issues as one DataFrame (columns ISSUE_COLUMNS).

    prices:            one row per (date, ticker), as built by ingest. It may also
                       contain earlier history, which the stale and jump checks
                       use to look back.
    calendar_dates:    every known trading day.
    corporate_actions: splits and dividends (date, ticker, action, value).
    check_dates:       the trading days to report on (default: all of them).
    """
    calendar_dates = sorted(set(calendar_dates))
    check_dates = calendar_dates if check_dates is None else sorted(set(check_dates))
    if not check_dates:
        raise DataQualityError("no trading days to check: the calendar is empty")

    # Only trading days count. Rows on any other day (Yahoo makes placeholder
    # rows for some holidays, e.g. 2026-01-15) are ignored by every check.
    on_calendar = prices[prices["date"].isin(calendar_dates)]
    window = on_calendar[on_calendar["date"].isin(check_dates)]
    q = cfg["quality"]

    found = (
        check_freshness(window, check_dates[-1])
        + check_missing_days(on_calendar, check_dates, cfg["tickers"])
        + check_ohlc_consistency(window)
        + check_stale_price(on_calendar, q["stale_days"], check_dates)
        + check_jump(on_calendar, corporate_actions, q["jump_threshold"], check_dates)
        + check_duplicates_nulls(window)
        + check_universe(window, cfg["tickers"], check_dates[-1])
        + check_placeholder_bar(window)
    )
    issues = pd.DataFrame(found, columns=ISSUE_COLUMNS[1:])
    issues.insert(0, "run_id", run_id)
    return issues.sort_values(["date", "check_name", "ticker"], na_position="first").reset_index(drop=True)


# ---------------------------------------------------------------------------
# The eight checks. Each returns a list of (date, ticker, check, severity, details).
# ---------------------------------------------------------------------------

def check_freshness(prices, latest_day):
    """1. The latest trading day must have usable prices for at least one stock.

    A row without a close counts as no data: Yahoo sometimes has the open
    but not yet the close for the newest day, and we can't trade on that.
    """
    usable = prices[(prices["date"] == latest_day) & prices["close"].notna() & prices["adj_close"].notna()]
    if usable.empty:
        return [(latest_day, None, "freshness", "ERROR",
                 f"no usable prices (with a close) for the latest trading day {latest_day}")]
    return []


def check_missing_days(prices, trading_days, tickers):
    """2. Every ticker should have a row on every trading day since its first row.

    Days before a stock's first row aren't "missing": it may not have been
    listed yet. A ticker with no rows at all is reported by the universe check.
    """
    present = set(zip(prices["date"], prices["ticker"]))
    first_seen = prices.groupby("ticker")["date"].min()
    found = []
    for ticker in tickers:
        if ticker not in first_seen.index:
            continue
        for day in trading_days:
            if day >= first_seen[ticker] and (day, ticker) not in present:
                found.append((day, ticker, "missing_days", "WARNING", "no price row on a trading day"))
    return found


def check_ohlc_consistency(prices):
    """3. A daily bar must make sense: low <= open/close <= high, prices > 0, volume >= 0.

    The high is the most anyone paid that day and the low the least, so the
    open and close must lie between them. Anything else is a bad data feed.
    Rows with missing values are skipped here; check 6 reports them.
    """
    p = prices.dropna(subset=REQUIRED_COLUMNS)
    rules = {
        "low above open": p["low"] > p["open"],
        "open above high": p["open"] > p["high"],
        "low above close": p["low"] > p["close"],
        "close above high": p["close"] > p["high"],
        "price not positive": (p[PRICE_FIELDS] <= 0).any(axis=1),
        "negative volume": p["volume"] < 0,
    }
    broken = pd.DataFrame(rules)
    found = []
    for idx in broken.index[broken.any(axis=1)]:
        reasons = [name for name in rules if broken.at[idx, name]]
        row = p.loc[idx]
        details = (f"{', '.join(reasons)} (O={row['open']:.2f} H={row['high']:.2f} "
                   f"L={row['low']:.2f} C={row['close']:.2f} V={row['volume']:.0f})")
        found.append((row["date"], row["ticker"], "ohlc_consistency", "ERROR", details))
    return found


def check_stale_price(prices, stale_days, report_days=None):
    """4. Flag a price that has stopped updating.

    A day is "stale" if its raw close equals the previous trading day's
    close, or no shares traded (volume 0). A real, liquid stock almost never
    does either for several days running; it usually means the data feed is
    repeating an old value. We flag every day on which the run of stale days
    has reached `stale_days`.
    """
    report_days = None if report_days is None else set(report_days)
    found = []
    for ticker, rows in _rows_by_ticker(prices):
        stale = rows["close"].eq(rows["close"].shift()) | rows["volume"].eq(0)
        # Length of the current run of stale days: each non-stale day starts a
        # new group, and inside a group we count the stale days so far.
        streak = stale.astype(int).groupby((~stale).cumsum()).cumsum()
        for day, length in zip(rows["date"], streak):
            if length >= stale_days and _wanted(day, report_days):
                found.append((day, ticker, "stale_price", "WARNING",
                              f"close unchanged or zero volume for {length} trading days in a row"))
    return found


def check_jump(prices, corporate_actions, threshold, report_days=None):
    """5. Flag a raw close-to-close move bigger than `threshold` (e.g. 20%).

    Large stocks rarely move 20% in a day. When one does, it is either real
    news or a data problem. A common data problem is a split that wasn't
    adjusted for: after a 1-for-10 split the price drops 90% overnight, but
    shareholders own 10x as many shares and lost nothing. So if a split or
    bonus issue is recorded for that date, the jump is explained (INFO).
    Otherwise it is a WARNING and the ticker is excluded that day.
    """
    splits = corporate_actions[corporate_actions["action"] == "split"]
    split_ratio = {(d, t): v for d, t, v in zip(splits["date"], splits["ticker"], splits["value"])}
    report_days = None if report_days is None else set(report_days)
    found = []
    for ticker, rows in _rows_by_ticker(prices):
        ret = rows["close"] / rows["close"].shift() - 1
        for day, r in zip(rows["date"], ret):
            if pd.isna(r) or abs(r) <= threshold or not _wanted(day, report_days):
                continue
            if (day, ticker) in split_ratio:
                found.append((day, ticker, "jump", "INFO",
                              f"close moved {r:+.1%}; explained by corporate action "
                              f"(split ratio {split_ratio[(day, ticker)]:g})"))
            else:
                found.append((day, ticker, "jump", "WARNING",
                              f"close moved {r:+.1%} with no recorded split or bonus"))
    return found


def check_duplicates_nulls(prices):
    """6. Exactly one row per (date, ticker), with no missing required values.

    Two rows for the same stock and day mean we can't know which is right.
    A missing value (e.g. no close) means the bar is incomplete.
    """
    found = []
    counts = prices.groupby(["date", "ticker"]).size()
    for (day, ticker), n in counts[counts > 1].items():
        found.append((day, ticker, "duplicates_nulls", "ERROR", f"{n} rows for the same date and ticker"))

    nulls = prices[REQUIRED_COLUMNS].isna()
    for day, ticker, row_nulls in zip(prices["date"], prices["ticker"], nulls.itertuples(index=False)):
        missing = [col for col, is_null in zip(REQUIRED_COLUMNS, row_nulls) if is_null]
        if missing:
            found.append((day, ticker, "duplicates_nulls", "ERROR", f"missing {', '.join(missing)}"))
    return found


def check_universe(prices, tickers, report_day):
    """7. Every configured ticker should have returned at least some data.

    A ticker that returns nothing may have been renamed, merged or delisted
    (HDFC.NS disappeared when HDFC Ltd merged into HDFC Bank in 2023), and the
    strategy is then silently trading a smaller list than you think.
    """
    present = set(prices["ticker"])
    return [(report_day, t, "universe", "WARNING", "configured ticker returned no data in this window")
            for t in tickers if t not in present]


def check_placeholder_bar(prices):
    """8. Flag a fake "placeholder" bar: zero volume and open = high = low = close.

    When Yahoo is missing a day's data it sometimes fills in a flat bar at the
    previous close. On 2025-03-18, a normal trading day, it did this for 29 of
    our 30 stocks. Such a bar passes every other check, but no trade ever
    happened at that price, so the ticker is excluded that day. A flat bar
    *with* volume can be real (a stock frozen at its circuit limit trades all
    day at one price), so it is not flagged. (This check was added beyond the
    original spec after finding these bars in the real data.)
    """
    p = prices.dropna(subset=REQUIRED_COLUMNS)
    flat = (p["volume"] == 0) & (p["open"] == p["high"]) & (p["high"] == p["low"]) & (p["low"] == p["close"])
    return [(day, ticker, "placeholder_bar", "WARNING", f"volume 0 and open = high = low = close = {close:.2f}")
            for day, ticker, close in zip(p.loc[flat, "date"], p.loc[flat, "ticker"], p.loc[flat, "close"])]


# ---------------------------------------------------------------------------
# Helpers for the rest of the pipeline.
# ---------------------------------------------------------------------------

def summarize(issues):
    """Count issues by check and severity: one row per check, one column per severity."""
    counts = issues.groupby(["check_name", "severity"]).size().unstack(fill_value=0)
    return counts.reindex(index=CHECKS, columns=SEVERITIES, fill_value=0)


def fatal_issues(issues):
    """The issues that must stop the run (a freshness ERROR)."""
    return issues[(issues["check_name"] == "freshness") & (issues["severity"] == "ERROR")]


def excluded_pairs(issues):
    """The set of (date, ticker) pairs that must not be traded, according to EXCLUDING."""
    # A boolean Series, not a plain list: pandas reads an *empty* list as "no columns".
    mask = pd.Series([(c, s) in EXCLUDING for c, s in zip(issues["check_name"], issues["severity"])],
                     index=issues.index, dtype=bool)
    chosen = issues[mask]
    return set(zip(chosen["date"], chosen["ticker"]))


def usable_window(conn, day, lookback_days=LOOKBACK_DAYS):
    """Adjusted prices for the `lookback_days` up to and including `day`, plus the
    (date, ticker) pairs the checks excluded in that period.

    Nothing after `day` is loaded, so nothing from the future can leak into a decision.
    """
    start = (date.fromisoformat(day) - timedelta(days=lookback_days)).isoformat()
    prices = db.query(conn, "SELECT date, ticker, adj_open, adj_close FROM prices "
                            "WHERE date >= ? AND date <= ?", (start, day))
    issues = db.query(conn, "SELECT date, ticker, check_name, severity FROM dq_issues "
                            "WHERE date >= ? AND date <= ?", (start, day))
    return prices, excluded_pairs(issues)


def _rows_by_ticker(prices):
    """Yield (ticker, rows sorted by date) with one row per day and a usable close.

    Duplicates and missing closes are reported by check 6. Here we drop them,
    so the look-back checks compare each day with the previous good day.
    """
    clean = (prices.dropna(subset=["close"])
                   .drop_duplicates(subset=["date", "ticker"], keep="last")
                   .sort_values(["ticker", "date"]))
    for ticker, rows in clean.groupby("ticker"):
        yield ticker, rows.reset_index(drop=True)


def _wanted(day, report_days):
    return report_days is None or day in report_days
