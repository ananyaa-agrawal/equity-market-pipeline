"""The SQLite database: connection, table definitions and safe write helpers.

Why it matters in a real trading firm: the database is the firm's memory: what
the prices were, what we decided, what we traded, and what went wrong. Two rules
keep that memory trustworthy:

1. Primary keys. Each table says which columns identify a row (for prices: the
   date and the ticker). The database then physically cannot hold two different
   prices for TCS on the same day.
2. Idempotent writes. "Idempotent" means doing something twice has the same
   effect as doing it once. If the morning job crashes and you rerun it, or cron
   accidentally runs it twice, every table must end up exactly as after a single
   run, with nothing doubled. Every write in this project goes through `upsert`
   or `replace_dates` below to guarantee that.
"""

import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.config import now_ist

SCHEMA = """
-- Daily prices. Raw values exactly as downloaded, plus dividend-adjusted versions.
CREATE TABLE IF NOT EXISTS prices (
    date        TEXT NOT NULL,      -- trading day, YYYY-MM-DD
    ticker      TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    adj_close   REAL,
    volume      INTEGER,
    adj_open    REAL,
    adj_high    REAL,
    adj_low     REAL,
    adj_factor  REAL,               -- adj_close / close
    ingested_at TEXT NOT NULL,      -- when we downloaded this row
    PRIMARY KEY (date, ticker)
);

-- Dividends and splits (Yahoo reports bonus issues as splits too).
CREATE TABLE IF NOT EXISTS corporate_actions (
    date   TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,           -- 'dividend' or 'split'
    value  REAL NOT NULL,           -- dividend in INR per share, or split ratio
    PRIMARY KEY (date, ticker, action)
);

-- The official list of trading days (the days the NIFTY 50 index has data).
CREATE TABLE IF NOT EXISTS calendar (
    date TEXT PRIMARY KEY
);

-- Every data-quality problem found. No primary key: a day can have many issues.
CREATE TABLE IF NOT EXISTS dq_issues (
    run_id     TEXT NOT NULL,
    date       TEXT,
    ticker     TEXT,
    check_name TEXT NOT NULL,
    severity   TEXT NOT NULL CHECK (severity IN ('ERROR', 'WARNING', 'INFO')),
    details    TEXT
);
CREATE INDEX IF NOT EXISTS idx_dq_issues_date ON dq_issues (date);

-- The strategy's view of each stock on each day.
CREATE TABLE IF NOT EXISTS signals (
    date     TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    ret_1d   REAL,
    rank     INTEGER,
    selected INTEGER NOT NULL,      -- 1 = chosen, 0 = not
    PRIMARY KEY (date, ticker)
);

-- Orders we send to the (simulated) broker.
CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    created_date TEXT NOT NULL,     -- day T: the signal day
    trade_date   TEXT NOT NULL,     -- day T+1: the day it should execute
    ticker       TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty          INTEGER NOT NULL,
    signal_price REAL NOT NULL      -- adj_close on day T
);

-- What the broker actually did with each order.
CREATE TABLE IF NOT EXISTS fills (
    order_id     TEXT PRIMARY KEY,
    fill_date    TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    side         TEXT NOT NULL,
    ordered_qty  INTEGER NOT NULL,
    filled_qty   INTEGER NOT NULL,
    fill_price   REAL,
    slippage_bps REAL,
    fees_inr     REAL
);

-- End-of-day holdings in the two books that reconciliation compares.
CREATE TABLE IF NOT EXISTS positions_expected (
    date   TEXT NOT NULL,
    ticker TEXT NOT NULL,
    shares INTEGER NOT NULL,
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS positions_actual (
    date   TEXT NOT NULL,
    ticker TEXT NOT NULL,
    shares INTEGER NOT NULL,
    PRIMARY KEY (date, ticker)
);

-- Daily profit and loss.
CREATE TABLE IF NOT EXISTS pnl_daily (
    date            TEXT PRIMARY KEY,
    expected_equity REAL,
    actual_equity   REAL,
    actual_return   REAL,
    gross_pnl       REAL,
    net_pnl         REAL,
    fees_inr        REAL,
    slippage_inr    REAL
);

-- Differences between the expected and actual books.
CREATE TABLE IF NOT EXISTS recon_breaks (
    run_id     TEXT NOT NULL,
    date       TEXT NOT NULL,
    ticker     TEXT,
    break_type TEXT NOT NULL,
    expected   REAL,
    actual     REAL,
    difference REAL
);
CREATE INDEX IF NOT EXISTS idx_recon_breaks_date ON recon_breaks (date);

-- One row per pipeline run: the audit trail of what ran, when, and how it ended.
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    mode        TEXT NOT NULL,      -- backfill / daily / report
    as_of_date  TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,      -- RUNNING / SUCCESS / FAILED
    message     TEXT
);
"""

TABLES = {
    "prices", "corporate_actions", "calendar", "dq_issues", "signals", "orders",
    "fills", "positions_expected", "positions_actual", "pnl_daily",
    "recon_breaks", "runs",
}


def connect(db_path):
    """Open (or create) the SQLite database file."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def init_schema(conn):
    """Create any tables that don't exist yet. Safe to call on every run."""
    with conn:
        conn.executescript(SCHEMA)


def upsert(conn, table, df):
    """Insert rows; a row whose primary key already exists is replaced, not duplicated.

    This is how prices, orders, fills and so on stay idempotent: writing the
    same (date, ticker) twice leaves one row holding the newest values.
    Returns the number of rows written.
    """
    if df.empty:
        return 0
    sql, rows = _insert_statement("INSERT OR REPLACE", table, df)
    with conn:  # one transaction: all rows are written, or none are
        conn.executemany(sql, rows)
    return len(rows)


def replace_dates(conn, table, df, dates, date_col="date"):
    """Delete every existing row for `dates`, then insert `df`, in one transaction.

    For tables without a primary key (dq_issues, recon_breaks), where a day can
    have any number of rows. Rerunning a date wipes that date's old rows first,
    so the result is the same as running it once. `dates` is passed separately
    because a rerun that finds nothing must still clear the old rows.
    """
    _check_table(table)
    with conn:
        conn.executemany(
            f"DELETE FROM {table} WHERE {date_col} = ?", [(d,) for d in sorted(set(dates))]
        )
        if not df.empty:
            sql, rows = _insert_statement("INSERT", table, df)
            conn.executemany(sql, rows)
    return len(df)


def query(conn, sql, params=()):
    """Run a SELECT and return the result as a pandas DataFrame."""
    return pd.read_sql_query(sql, conn, params=params)


def start_run(conn, run_id, mode, as_of_date):
    """Record that a run has started (status RUNNING)."""
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, mode, as_of_date, started_at, status) VALUES (?, ?, ?, ?, ?)",
            (run_id, mode, as_of_date, _timestamp(), "RUNNING"),
        )


def finish_run(conn, run_id, status, message=""):
    """Record how a run ended. A row left at RUNNING means the process died mid-run."""
    with conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, message = ? WHERE run_id = ?",
            (_timestamp(), status, message, run_id),
        )


def _insert_statement(verb, table, df):
    """Build "INSERT ... (cols) VALUES (?, ?, ...)" plus the rows as plain Python values."""
    _check_table(table)
    cols = list(df.columns)
    placeholders = ", ".join("?" for _ in cols)
    sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    rows = [tuple(_to_sql_value(v) for v in row) for row in df.itertuples(index=False, name=None)]
    return sql, rows


def _to_sql_value(value):
    """Convert pandas/numpy values into types sqlite3 understands. Missing values become NULL."""
    if value is None or value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, np.generic):  # numpy int64 / float64 / bool_
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _check_table(table):
    """Table names are put straight into SQL text, so only allow the ones we created."""
    if table not in TABLES:
        raise ValueError(f"unknown table: {table}")


def _timestamp():
    return now_ist().isoformat(timespec="seconds")
