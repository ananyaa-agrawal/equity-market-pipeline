"""Prove that database writes are idempotent: rerunning a date never duplicates rows.

These tests use an in-memory SQLite database (":memory:"), which disappears
when the test ends, so they never touch data/market.db.
"""

import sqlite3

import pandas as pd
import pytest

from pipeline import db


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def price_rows(close=100.0):
    return pd.DataFrame([
        {"date": day, "ticker": ticker, "open": close, "high": close + 1, "low": close - 1,
         "close": close, "adj_close": close, "volume": 1000, "adj_open": close,
         "adj_high": close + 1, "adj_low": close - 1, "adj_factor": 1.0,
         "ingested_at": "2026-10-24T08:30:00+05:30"}
        for day in ("2026-10-21", "2026-10-22") for ticker in ("AAA.NS", "BBB.NS")
    ])


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_upserting_the_same_prices_twice_keeps_one_copy(conn):
    db.upsert(conn, "prices", price_rows())
    db.upsert(conn, "prices", price_rows())
    assert count(conn, "prices") == 4


def test_upsert_replaces_old_values_with_new_ones(conn):
    db.upsert(conn, "prices", price_rows(close=100.0))
    db.upsert(conn, "prices", price_rows(close=105.0))  # e.g. Yahoo corrected the data
    closes = {row[0] for row in conn.execute("SELECT close FROM prices")}
    assert count(conn, "prices") == 4 and closes == {105.0}


def test_rerunning_a_date_replaces_its_issues(conn):
    issues = pd.DataFrame([
        {"run_id": "run1", "date": "2026-10-22", "ticker": "AAA.NS", "check_name": "missing_days",
         "severity": "WARNING", "details": "x"},
        {"run_id": "run1", "date": "2026-10-22", "ticker": "BBB.NS", "check_name": "missing_days",
         "severity": "WARNING", "details": "x"},
    ])
    db.replace_dates(conn, "dq_issues", issues, dates=["2026-10-22"])
    db.replace_dates(conn, "dq_issues", issues.assign(run_id="run2"), dates=["2026-10-22"])
    assert count(conn, "dq_issues") == 2

    # A rerun that finds no issues must clear the old ones, not leave them behind.
    db.replace_dates(conn, "dq_issues", issues.iloc[0:0], dates=["2026-10-22"])
    assert count(conn, "dq_issues") == 0


def test_numpy_and_missing_values_are_stored_correctly(conn):
    rows = price_rows().astype({"volume": "int64"})
    rows.loc[0, "close"] = float("nan")
    db.upsert(conn, "prices", rows)
    first = conn.execute("SELECT close, volume FROM prices ORDER BY date, ticker").fetchone()
    assert first == (None, 1000)


def test_unknown_table_is_rejected(conn):
    with pytest.raises(ValueError):
        db.upsert(conn, "prices; DROP TABLE prices", price_rows())
