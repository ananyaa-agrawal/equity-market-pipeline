"""End-to-end tests: a tiny made-up market (3 stocks, 6 days) in an in-memory database.

They check the promises the whole design rests on:
* rerunning a trading day changes nothing (idempotency),
* the expected and actual books reconcile every day, and
* the report can be rebuilt from the database alone.
No internet needed: the prices are written straight into the database.
"""

import sqlite3

import pandas as pd
import pytest

from pipeline import db, main, report

DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12"]
CLOSES = {
    "AAA.NS": [100, 95, 97, 92, 96, 99],
    "BBB.NS": [50, 51, 49, 50, 48, 47],
    "CCC.NS": [200, 202, 198, 201, 199, 203],
}
CFG = {
    "tickers": list(CLOSES),
    "strategy": {"n_stocks": 2, "capital_inr": 100_000},
    "costs": {"slippage_bps": 5, "fee_bps": 10},
    "broker": {"partial_fill_prob": 0.5, "partial_fill_min": 0.5, "partial_fill_max": 0.9, "random_seed": 42},
    "quality": {"jump_threshold": 0.20, "stale_days": 3},
}


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    db.init_schema(connection)
    rows = []
    for ticker, closes in CLOSES.items():
        for i, (day, close) in enumerate(zip(DAYS, closes)):
            open_ = closes[i - 1] * 1.002 if i else close
            rows.append({"date": day, "ticker": ticker, "open": open_, "high": max(open_, close) * 1.01,
                         "low": min(open_, close) * 0.99, "close": close, "adj_close": close, "volume": 1000,
                         "adj_open": open_, "adj_high": max(open_, close) * 1.01,
                         "adj_low": min(open_, close) * 0.99, "adj_factor": 1.0,
                         "ingested_at": "2026-01-13T08:30:00+05:30"})
    db.upsert(connection, "prices", pd.DataFrame(rows))
    db.upsert(connection, "calendar", pd.DataFrame({"date": DAYS}))
    yield connection
    connection.close()


def backtest(conn):
    return [main.run_day(conn, CFG, "test", day)["recon"]["bridge"] for day in DAYS]


def snapshot(conn):
    return {t: db.query(conn, f"SELECT * FROM {t} ORDER BY 1, 2").drop(columns="run_id", errors="ignore").to_csv()
            for t in main.TRADING_TABLES}


def test_rerunning_a_day_changes_nothing(conn):
    backtest(conn)
    before = snapshot(conn)
    main.run_day(conn, CFG, "rerun", DAYS[3])  # e.g. the morning job ran twice
    assert snapshot(conn) == before


def test_books_reconcile_every_day(conn):
    bridges = backtest(conn)
    assert any(b["partial_fill"] != 0 for b in bridges)  # partial fills really happened...
    assert all(abs(b["residual"]) < 1e-6 for b in bridges)  # ...and every day still reconciles


def test_report_rebuilds_from_the_database(conn):
    backtest(conn)
    page = report.build_report(conn, CFG, "2026-01-13")
    for heading in ("1. Run status", "2. Data quality", "3. Yesterday's orders", "4. Reconciliation",
                    "5. Today's signal", "6. Strategy to date"):
        assert heading in page
    assert "Data date (last close used)</th><td>2026-01-12" in page
    assert "data:image/png;base64," in page  # the chart is embedded, so the file is self-contained
    assert "Books reconcile" in page


def test_report_for_a_failed_run_shows_why_and_no_stale_trades(conn):
    backtest(conn)
    db.start_run(conn, "run-x", "daily", "2026-01-13")
    db.finish_run(conn, "run-x", "FAILED", "no usable prices (with a close) for the latest trading day")
    page = report.build_report(conn, CFG, "2026-01-13", run_id="run-x")
    assert "FAILED" in page and "no usable prices" in page
    assert "3. Yesterday's orders" not in page
