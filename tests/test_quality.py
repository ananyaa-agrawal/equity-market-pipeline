"""Prove that each data-quality check catches the problem it is meant to catch.

Each test starts from a small, perfectly clean dataset (two made-up stocks,
five trading days), breaks exactly one thing, and checks that the right check
reports it with the right severity. It also checks that nothing else fires.
A check that never fires on bad data is worse than no check: it gives false
confidence.
"""

import pandas as pd

from pipeline import quality

DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
TICKERS = ["AAA.NS", "BBB.NS"]
CFG = {"tickers": TICKERS, "quality": {"jump_threshold": 0.20, "stale_days": 3}}
NO_ACTIONS = pd.DataFrame(columns=["date", "ticker", "action", "value"])


def clean_prices():
    """Two tickers x five days of sensible bars: the close rises by 1 each day."""
    rows = []
    for t, ticker in enumerate(TICKERS):
        for d, day in enumerate(DAYS):
            close = 100.0 + 50 * t + d
            rows.append({"date": day, "ticker": ticker, "open": close - 0.5, "high": close + 1,
                         "low": close - 1, "close": close, "adj_close": close, "volume": 1000})
    return pd.DataFrame(rows)


def set_values(prices, day, ticker, **values):
    """Overwrite some columns of one (day, ticker) row."""
    mask = (prices["date"] == day) & (prices["ticker"] == ticker)
    for col, value in values.items():
        prices.loc[mask, col] = value
    return prices


def run(prices, actions=NO_ACTIONS):
    return quality.run_checks(prices, DAYS, actions, CFG, run_id="test")


def only(issues, check):
    return issues[issues["check_name"] == check]


def test_clean_data_has_no_issues():
    issues = run(clean_prices())
    assert issues.empty
    assert quality.excluded_pairs(issues) == set()  # and an empty list of issues excludes nothing


def test_missing_day_is_a_warning_and_excludes_the_ticker():
    prices = clean_prices()
    prices = prices[~((prices["date"] == DAYS[2]) & (prices["ticker"] == "AAA.NS"))]
    issues = run(prices)
    assert len(issues) == 1
    row = issues.iloc[0]
    assert (row["check_name"], row["severity"], row["date"], row["ticker"]) == \
        ("missing_days", "WARNING", DAYS[2], "AAA.NS")
    assert (DAYS[2], "AAA.NS") in quality.excluded_pairs(issues)


def test_broken_ohlc_is_an_error():
    prices = set_values(clean_prices(), DAYS[1], "BBB.NS", high=140.0)  # high below the close of 151
    issues = run(prices)
    assert list(issues["check_name"]) == ["ohlc_consistency"]
    assert issues.iloc[0]["severity"] == "ERROR"
    assert "close above high" in issues.iloc[0]["details"]
    assert (DAYS[1], "BBB.NS") in quality.excluded_pairs(issues)


def test_zero_price_is_an_error():
    prices = set_values(clean_prices(), DAYS[4], "AAA.NS", low=0.0)
    issues = only(run(prices), "ohlc_consistency")
    assert len(issues) == 1 and "price not positive" in issues.iloc[0]["details"]


def test_stale_price_is_a_warning_but_does_not_exclude():
    prices = clean_prices()
    for day in DAYS[1:4]:  # the close stays at 100 on days 2, 3 and 4: three unchanged days
        set_values(prices, day, "AAA.NS", open=100.0, high=101.0, low=99.0, close=100.0, adj_close=100.0)
    issues = run(prices)
    stale = only(issues, "stale_price")
    assert list(stale["date"]) == [DAYS[3]]  # the third unchanged day reaches stale_days=3
    assert set(stale["severity"]) == {"WARNING"}
    assert (DAYS[3], "AAA.NS") not in quality.excluded_pairs(issues)


def test_zero_volume_counts_as_stale():
    prices = clean_prices()
    for day in DAYS[2:5]:
        set_values(prices, day, "BBB.NS", volume=0)
    assert list(only(run(prices), "stale_price")["date"]) == [DAYS[4]]


def test_unexplained_jump_is_a_warning_and_excludes_the_ticker():
    prices = clean_prices()
    later = (prices["ticker"] == "AAA.NS") & (prices["date"] >= DAYS[3])
    prices.loc[later, ["open", "high", "low", "close", "adj_close"]] *= 1.5  # +50% from day 4 on
    issues = run(prices)
    assert len(issues) == 1
    row = issues.iloc[0]
    assert (row["check_name"], row["severity"], row["date"]) == ("jump", "WARNING", DAYS[3])
    assert (DAYS[3], "AAA.NS") in quality.excluded_pairs(issues)


def test_jump_explained_by_split_is_info_and_not_excluded():
    prices = clean_prices()
    later = (prices["ticker"] == "AAA.NS") & (prices["date"] >= DAYS[3])
    prices.loc[later, ["open", "high", "low", "close", "adj_close"]] /= 10  # a 1-for-10 split
    split = pd.DataFrame([{"date": DAYS[3], "ticker": "AAA.NS", "action": "split", "value": 10.0}])
    issues = run(prices, split)
    assert len(issues) == 1
    assert issues.iloc[0]["severity"] == "INFO"
    assert "explained by corporate action" in issues.iloc[0]["details"]
    assert quality.excluded_pairs(issues) == set()


def test_duplicate_row_is_an_error():
    prices = clean_prices()
    prices = pd.concat([prices, prices.iloc[[3]]], ignore_index=True)  # row 3 appears twice
    issues = run(prices)
    assert list(issues["check_name"]) == ["duplicates_nulls"]
    assert issues.iloc[0]["severity"] == "ERROR"
    assert "2 rows" in issues.iloc[0]["details"]


def test_missing_close_is_an_error():
    prices = set_values(clean_prices(), DAYS[2], "BBB.NS", close=None)
    issues = only(run(prices), "duplicates_nulls")
    assert len(issues) == 1 and issues.iloc[0]["details"] == "missing close"


def test_no_usable_prices_on_latest_day_is_fatal():
    # Like Yahoo on 2026-10-23: rows exist for the latest day, but none has a close.
    prices = clean_prices()
    prices.loc[prices["date"] == DAYS[-1], ["close", "adj_close"]] = None
    issues = run(prices)
    fatal = quality.fatal_issues(issues)
    assert len(fatal) == 1 and fatal.iloc[0]["date"] == DAYS[-1]


def test_one_good_ticker_on_latest_day_is_not_fatal():
    prices = set_values(clean_prices(), DAYS[-1], "AAA.NS", close=None, adj_close=None)
    assert quality.fatal_issues(run(prices)).empty


def test_ticker_with_no_data_fails_universe_check():
    prices = clean_prices()
    prices = prices[prices["ticker"] != "BBB.NS"]
    issues = run(prices)
    assert list(issues["check_name"]) == ["universe"]
    assert issues.iloc[0]["ticker"] == "BBB.NS"


def test_placeholder_bar_is_a_warning_and_excludes_the_ticker():
    # Like Yahoo on 2025-03-18: a flat bar at the previous close (101) with no volume.
    prices = set_values(clean_prices(), DAYS[2], "AAA.NS", open=101.0, high=101.0, low=101.0,
                        close=101.0, adj_close=101.0, volume=0)
    issues = run(prices)
    assert list(issues["check_name"]) == ["placeholder_bar"]
    assert issues.iloc[0]["severity"] == "WARNING"
    assert (DAYS[2], "AAA.NS") in quality.excluded_pairs(issues)


def test_flat_bar_with_volume_is_not_a_placeholder():
    # A stock frozen at its circuit limit: one price all day, but shares did trade.
    prices = set_values(clean_prices(), DAYS[2], "AAA.NS", open=102.0, high=102.0, low=102.0,
                        close=102.0, adj_close=102.0, volume=500)
    assert run(prices).empty


def test_rows_on_non_trading_days_are_ignored():
    # Like Yahoo's placeholder rows on the 2026-01-15 holiday, but deliberately broken.
    prices = clean_prices()
    holiday = {"date": "2026-01-10", "ticker": "AAA.NS", "open": 5.0, "high": 1.0,
               "low": 9.0, "close": None, "adj_close": None, "volume": 0}
    prices = pd.concat([prices, pd.DataFrame([holiday])], ignore_index=True)
    assert run(prices).empty


def test_summary_lists_every_check_and_severity():
    prices = clean_prices()
    prices = prices[~((prices["date"] == DAYS[2]) & (prices["ticker"] == "AAA.NS"))]
    summary = quality.summarize(run(prices))
    assert list(summary.index) == quality.CHECKS
    assert summary.loc["missing_days", "WARNING"] == 1
    assert summary.values.sum() == 1
