"""Download daily prices and corporate actions from Yahoo Finance.

What this module does:
1. Asks Yahoo Finance (through the yfinance library) for each stock's daily open,
   high, low, close, adjusted close, volume, dividends and splits, plus the
   NIFTY 50 index, whose trading days become our calendar.
2. Reshapes Yahoo's "wide" table (one column per field per ticker) into a "long"
   table with one row per (date, ticker), which is the shape a database wants.
3. Calculates adjusted open/high/low prices (explained in build_price_rows).
4. Writes everything to the database with upserts, so reruns are safe.

Why it matters in a real trading firm: every signal, order and profit number
downstream is only as good as the data that went in. Firms treat ingestion as
its own product: every setting is explicit (so a library upgrade can't silently
change the numbers), flaky networks get retries, and if data truly can't be
fetched the job fails loudly instead of trading on stale prices.
"""

import logging
import time
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

from pipeline import db
from pipeline.config import MARKET_TZ, now_ist

log = logging.getLogger(__name__)

# yfinance 1.7 still uses pandas features that pandas 3.1 has deprecated, and
# prints a long warning on every download. It's the library's issue, so hide it.
warnings.filterwarnings("ignore", message="The inplace keyword")

MAX_ATTEMPTS = 3     # try a download up to 3 times...
BACKOFF_SECONDS = 2  # ...waiting 2s, then 4s, between attempts ("exponential backoff")
OVERLAP_DAYS = 7     # daily mode re-downloads the last week to pick up late corrections

# Yahoo's column names -> ours.
YF_COLUMNS = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
    "Dividends": "dividends",
    "Stock Splits": "stock_splits",
}
REQUIRED_YF_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
PRICE_FIELDS = ["open", "high", "low", "close", "adj_close"]
LONG_COLUMNS = ["date", "ticker"] + list(YF_COLUMNS.values())
PRICE_COLUMNS = [
    "date", "ticker", "open", "high", "low", "close", "adj_close", "volume",
    "adj_open", "adj_high", "adj_low", "adj_factor", "ingested_at",
]


class IngestError(Exception):
    """Raised when we cannot get usable data, even after retrying."""


def fetch_yfinance(tickers, start, end):
    """Make one call to Yahoo Finance, with every setting written out explicitly.

    `end` is exclusive: end="2026-10-23" returns days up to and including 2026-10-22.
    """
    return yf.download(
        tickers,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=False,       # keep raw OHLC *and* the separate Adj Close column
        actions=True,            # include the Dividends and Stock Splits columns
        back_adjust=False,
        repair=False,            # don't let yfinance "fix" prices; our quality checks look for problems
        keepna=False,
        group_by="column",       # columns are (field, ticker), e.g. ("Close", "TCS.NS")
        multi_level_index=True,  # two-level columns even for a single ticker
        threads=True,
        progress=False,
        timeout=30,
    )


def to_long(raw, tickers):
    """Reshape yfinance's wide table into one row per (date, ticker)."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=LONG_COLUMNS)

    if isinstance(raw.columns, pd.MultiIndex):
        # Two header rows: one holds field names ("Close"), the other ticker names.
        # Work out which is which instead of assuming, since it has changed before.
        field_level = 0 if "Close" in raw.columns.get_level_values(0) else 1
        ticker_level = 1 - field_level
        pieces = []
        for ticker in raw.columns.get_level_values(ticker_level).unique():
            one = raw.xs(ticker, axis=1, level=ticker_level).copy()
            one["ticker"] = ticker
            pieces.append(one)
        wide = pd.concat(pieces)
    elif len(tickers) == 1:
        wide = raw.copy()
        wide["ticker"] = tickers[0]
    else:
        raise IngestError("yfinance returned single-level columns for several tickers")

    missing = [c for c in REQUIRED_YF_COLUMNS if c not in wide.columns]
    if missing:
        raise IngestError(f"yfinance response has no {missing} columns; has the library changed?")
    wide = wide.rename(columns=YF_COLUMNS)
    for col in ("dividends", "stock_splits"):  # absent when no ticker had any actions
        if col not in wide.columns:
            wide[col] = 0.0

    # Dates: if Yahoo sends timezone-aware timestamps, convert to Indian time
    # first and only then keep the calendar date, so "2026-10-23" always means
    # the NSE trading day of 23 October.
    stamps = pd.DatetimeIndex(wide.index)
    if stamps.tz is not None:
        stamps = stamps.tz_convert(MARKET_TZ).tz_localize(None)
    wide["date"] = stamps.strftime("%Y-%m-%d")
    wide = wide.reset_index(drop=True)

    # Downloading many tickers together gives each ticker an all-empty row on
    # any date only *other* tickers traded. Drop those. Keep partly-empty rows,
    # so the quality checks can flag them instead of us hiding them.
    wide = wide.dropna(subset=PRICE_FIELDS, how="all")
    return wide[LONG_COLUMNS]


def download(tickers, start, end):
    """Download `tickers`, retrying any that came back empty.

    Returns (long_table, tickers_that_never_returned_data). Raises IngestError
    if nothing at all could be downloaded.
    """
    remaining = list(tickers)
    pieces = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            got = to_long(fetch_yfinance(remaining, start, end), remaining)
        except IngestError:
            raise  # an unexpected data format won't fix itself; retrying is pointless
        except Exception as exc:  # network errors, rate limits, Yahoo outages
            log.warning("download attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, exc)
            got = pd.DataFrame(columns=LONG_COLUMNS)

        if not got.empty:
            pieces.append(got)
        received = set(got["ticker"])
        remaining = [t for t in remaining if t not in received]
        if not remaining:
            break
        if attempt < MAX_ATTEMPTS:
            wait = BACKOFF_SECONDS * 2 ** (attempt - 1)
            log.warning("no data yet for %s; retrying in %ds", remaining, wait)
            time.sleep(wait)

    if not pieces:
        raise IngestError(
            f"no data for any ticker after {MAX_ATTEMPTS} attempts ({start} to {end}); "
            "check the internet connection or whether Yahoo Finance is down"
        )
    if remaining:
        log.warning("gave up on %s after %d attempts", remaining, MAX_ATTEMPTS)
    return pd.concat(pieces, ignore_index=True), remaining


def build_price_rows(long):
    """Add the adjusted prices and a download timestamp to the stock rows.

    ADJUSTED PRICES. When a company pays a Rs 10 dividend, its share price drops
    by about Rs 10 on the "ex-dividend" day. Shareholders didn't lose anything:
    they received the Rs 10 in cash. But a return calculated from raw closes would
    show a fake loss. Yahoo's "Adj Close" fixes this by scaling all *earlier*
    prices down slightly, so close-to-close returns include the dividend.

    adj_factor = Adj Close / Close is the scaling Yahoo applied on each day.
    Multiplying open, high and low by the same factor puts the whole daily bar
    on the same adjusted scale, so the strategy, fills and PnL can all use
    adjusted prices consistently.

    A Yahoo quirk: its "Close" is already adjusted for splits and bonus issues
    (Nestle India's 1-for-10 split in January 2024 shows no price drop). So in
    this project "raw" means "not dividend-adjusted", and adj_factor mostly
    captures dividends.
    """
    rows = long.copy()
    rows["adj_factor"] = (rows["adj_close"] / rows["close"]).replace([np.inf, -np.inf], np.nan)
    for field in ("open", "high", "low"):
        rows[f"adj_{field}"] = rows[field] * rows["adj_factor"]
    rows["ingested_at"] = now_ist().isoformat(timespec="seconds")
    return rows[PRICE_COLUMNS]


def build_action_rows(long):
    """Pull dividends and splits out into their own table.

    CORPORATE ACTIONS are company events that change the share price without
    changing what the company is worth: dividends (cash paid out), splits (one
    share becomes several cheaper ones) and bonus issues (free extra shares;
    Yahoo records these as splits, e.g. a 1:1 bonus is a split ratio of 2.0).
    The quality checks use them to explain otherwise suspicious price jumps.
    """
    pieces = []
    for col, action in (("dividends", "dividend"), ("stock_splits", "split")):
        events = long.loc[long[col].fillna(0) != 0, ["date", "ticker", col]]
        pieces.append(events.rename(columns={col: "value"}).assign(action=action))
    return pd.concat(pieces, ignore_index=True)[["date", "ticker", "action", "value"]]


def download_market_data(cfg, start, end):
    """Download every configured stock plus the calendar index for [start, end)."""
    cal_ticker = cfg["calendar_ticker"]
    log.info("downloading %d tickers + %s from %s to %s (end exclusive)",
             len(cfg["tickers"]), cal_ticker, start, end)
    long, missing = download(cfg["tickers"] + [cal_ticker], start, end)
    if cal_ticker in missing:
        raise IngestError(f"calendar ticker {cal_ticker} returned no data, so we can't tell which days were trading days")

    # Any index row makes the day a trading day, even an incomplete one: Yahoo
    # sometimes has the open but not yet the close for the newest day, and the
    # market was still open. The quality checks deal with incomplete stock rows.
    calendar = long.loc[long["ticker"] == cal_ticker, ["date"]]
    stocks = long[long["ticker"] != cal_ticker]
    return {
        "prices": build_price_rows(stocks),
        "corporate_actions": build_action_rows(stocks),
        "calendar": calendar.drop_duplicates().sort_values("date"),
        "missing_tickers": missing,
    }


def store(conn, data):
    """Upsert downloaded data into the database. Returns rows written per table."""
    return {
        table: db.upsert(conn, table, data[table])
        for table in ("prices", "corporate_actions", "calendar")
    }


def incremental_start(conn, cfg, before):
    """Where daily mode should start downloading: a week before our newest date before `before`.

    The overlap re-fetches recent days in case Yahoo corrected them after we
    first downloaded them; upserts overwrite the old values. If there is no
    stored data before `before`, start from history_start.
    """
    last = conn.execute("SELECT MAX(date) FROM prices WHERE date < ?", (before,)).fetchone()[0]
    if last is None:
        return cfg["history_start"]
    return (date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS)).isoformat()


def price_summary(conn, tickers):
    """Rows, first date and last date per configured ticker (0 rows if none)."""
    counts = db.query(
        conn,
        "SELECT ticker, COUNT(*) AS rows, MIN(date) AS first_date, MAX(date) AS last_date "
        "FROM prices GROUP BY ticker",
    )
    summary = pd.DataFrame({"ticker": tickers}).merge(counts, on="ticker", how="left")
    summary["rows"] = summary["rows"].fillna(0).astype(int)
    return summary
