"""Command-line entry point: `python -m pipeline.main <command>`.

    python -m pipeline.main backfill                    build history, backtest day by day
    python -m pipeline.main daily [--date YYYY-MM-DD]   the morning run (default: today)
    python -m pipeline.main report --date YYYY-MM-DD    rebuild one morning's HTML report

This is the one front door to the pipeline. It reads the config, sets up
logging, opens the database, gives the run an id, runs the requested command
and turns the outcome into an exit code (0 = success, 1 = failure) that the
Bash script and cron can act on.

Why it matters in a real trading firm: unattended jobs need an audit trail.
Every run gets a row in the `runs` table and lines in logs/pipeline.log, so when
someone asks "did this morning's run work, and what did it see?" there is a
definite answer.

The most important design rule lives here: backfill and daily both run each
trading day through the same `run_day` function. The backtest is literally
the live process replayed over history, so the two can never disagree.
"""

import argparse
import logging
import sys
import uuid
from datetime import date, timedelta

import pandas as pd

from pipeline import db, execution, ingest, metrics, quality, reconcile, report, strategy
from pipeline.config import ConfigError, load_config, now_ist

# Tables the backtest writes. Backfill empties them first and rebuilds from scratch.
TRADING_TABLES = ["signals", "orders", "fills", "positions_expected", "positions_actual",
                  "pnl_daily", "recon_breaks"]

log = logging.getLogger(__name__)


class PipelineError(Exception):
    """A known reason the run can't go ahead (as opposed to a bug)."""


# Failures we expect and explain in one line. Anything else is a bug and gets a full traceback.
KNOWN_FAILURES = (PipelineError, quality.DataQualityError, ingest.IngestError)


def setup_logging(log_dir):
    """Send log lines to logs/pipeline.log and the console as "timestamp LEVEL module message"."""
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(module)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.FileHandler(log_dir / "pipeline.log"), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    logging.getLogger("yfinance").setLevel(logging.WARNING)  # hide the library's chatter
    logging.getLogger("matplotlib").setLevel(logging.WARNING)


def new_run_id(mode):
    """A unique, readable id such as 20261024T081502-daily-3f9c2a1b."""
    return f"{now_ist():%Y%m%dT%H%M%S}-{mode}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Steps shared by backfill and daily
# ---------------------------------------------------------------------------

def ingest_and_check(conn, cfg, run_id, start, end):
    """Download [start, end), run the quality checks, and store prices and issues.

    Backfill and daily both go through here, so they apply exactly the same
    rules to the data. Everything is saved even when the checks find problems:
    the raw data and the verdict on it are the audit trail.
    """
    data = ingest.download_market_data(cfg, start, end)
    check_days = sorted(data["calendar"]["date"])

    # Stored history from just before the window lets the stale and jump checks
    # look back (in daily mode the window is only about a week long).
    lookback_start = (date.fromisoformat(start) - timedelta(days=quality.LOOKBACK_DAYS)).isoformat()
    history = db.query(conn, "SELECT * FROM prices WHERE date >= ? AND date < ?", (lookback_start, start))
    prices = pd.concat([history, data["prices"]], ignore_index=True)
    calendar = set(db.query(conn, "SELECT date FROM calendar")["date"]) | set(check_days)
    actions = pd.concat([db.query(conn, "SELECT * FROM corporate_actions"), data["corporate_actions"]])

    issues = quality.run_checks(prices, calendar, actions, cfg, run_id, check_dates=check_days)

    written = ingest.store(conn, data)
    db.replace_dates(conn, "dq_issues", issues, dates=check_days)
    log.info("rows written: %s, dq_issues: %d", written, len(issues))
    log.info("data-quality summary for %s to %s (issues by check and severity):\n%s",
             check_days[0], check_days[-1], quality.summarize(issues).to_string())
    return issues


def stop_if_fatal(issues):
    """Apply the run-level rule: a freshness ERROR stops the run (exit code 1)."""
    fatal = quality.fatal_issues(issues)
    if not fatal.empty:
        raise quality.DataQualityError("; ".join(fatal["details"]))


def run_day(conn, cfg, run_id, day):
    """Process one trading day `day`, whose closing prices we have just received.

    1. Fill the orders created on the previous trading day at today's open.
    2. Update our holdings and work out today's profit and loss.
    3. Reconcile the expected book against the actual book.
    4. Compute today's signal and the orders for the next trading day.

    Everything is read from and written to the database, and each table's rows
    for `day` are replaced, so rerunning a day gives the same result.
    """
    prev = conn.execute("SELECT MAX(date) FROM calendar WHERE date < ?", (day,)).fetchone()[0]
    capital = cfg["strategy"]["capital_inr"]
    n_stocks = cfg["strategy"]["n_stocks"]
    prices, excluded = quality.usable_window(conn, day)  # nothing after `day` is loaded

    # 1. Fill yesterday's orders at today's open.
    orders = db.query(conn, "SELECT * FROM orders WHERE created_date = ?", (prev,))
    fills = execution.simulate_fills(orders, day, prices, excluded, cfg)
    db.replace_dates(conn, "fills", fills[execution.FILL_COLUMNS], [day], date_col="fill_date")

    # 2. New holdings and today's PnL.
    before = load_positions(conn, "positions_actual", prev)
    after = execution.apply_fills(before, fills)
    db.replace_dates(conn, "positions_actual", positions_frame(day, after), [day])
    marks_before = metrics.mark_prices(prices, excluded, prev) if prev else {}
    marks_today = metrics.mark_prices(prices, excluded, day)
    row = conn.execute("SELECT actual_equity, expected_equity FROM pnl_daily WHERE date = ?", (prev,)).fetchone()
    actual_before, expected_before = row if row else (capital, capital)
    pnl = metrics.daily_pnl(day, before, fills, marks_before, marks_today, actual_before, capital)

    # 3. Reconcile: expected book (full fills at signal price, free) vs actual book.
    recon = reconcile.reconcile_day(day, before, after, orders, fills, marks_before, marks_today,
                                    pnl["net_pnl"], run_id)
    pnl["expected_equity"] = expected_before + recon["bridge"]["expected_pnl"]
    db.upsert(conn, "pnl_daily", pd.DataFrame([pnl]))
    db.replace_dates(conn, "positions_expected", positions_frame(day, recon["expected_positions"]), [day])
    db.replace_dates(conn, "recon_breaks", recon["position_breaks"], [day])

    # 4. Today's signal and the orders for the next trading day.
    if prev is None:  # the very first day has no previous close, so no return to rank on
        signal = pd.DataFrame(columns=strategy.SIGNAL_COLUMNS)
    else:
        signal = strategy.compute_signal(prices, day, prev, excluded, n_stocks)
    targets = strategy.target_positions(signal, capital, n_stocks)
    new_orders = strategy.make_orders(targets, after, marks_today, day)
    signal_rows = signal[["ticker", "ret_1d", "rank", "selected"]].assign(date=day)
    db.replace_dates(conn, "signals", signal_rows, [day])
    db.replace_dates(conn, "orders", new_orders, [day], date_col="created_date")

    return {"fills": fills, "pnl": pnl, "recon": recon, "signal": signal, "orders": new_orders}


def load_positions(conn, table, day):
    """Holdings on `day` as {ticker: shares} ({} if there are none)."""
    rows = conn.execute(f"SELECT ticker, shares FROM {table} WHERE date = ?", (day,)).fetchall()
    return dict(rows)


def positions_frame(day, positions):
    return pd.DataFrame([{"date": day, "ticker": t, "shares": s} for t, s in sorted(positions.items())],
                        columns=["date", "ticker", "shares"])


def log_performance(conn, cfg):
    """Print the strategy's statistics so far, gross and net side by side."""
    pnl = db.query(conn, "SELECT * FROM pnl_daily ORDER BY date")
    fills = db.query(conn, "SELECT * FROM fills")
    perf = metrics.performance(pnl, fills, cfg["strategy"]["capital_inr"])
    shown = perf.copy().astype(object)
    for name in perf.index:
        fmt = "{:.2f}" if name == "Sharpe ratio" else "{:.1%}"
        shown.loc[name] = [fmt.format(v) for v in perf.loc[name]]
    log.info("performance %s to %s:\n%s", pnl["date"].iloc[0], pnl["date"].iloc[-1], shown.to_string())
    return perf


# ---------------------------------------------------------------------------
# The three commands
# ---------------------------------------------------------------------------

def run_backfill(conn, cfg, run_id, run_date):
    """Download and check the full history, then backtest it one day at a time."""
    end = run_date  # end is exclusive: only days that have finished
    issues = ingest_and_check(conn, cfg, run_id, cfg["history_start"], end)
    summary = ingest.price_summary(conn, cfg["tickers"])
    log.info("rows per ticker:\n%s", summary.to_string(index=False))

    days = [r[0] for r in conn.execute(
        "SELECT date FROM calendar WHERE date >= ? AND date < ? ORDER BY date", (cfg["history_start"], end))]
    fatal_days = set(quality.fatal_issues(issues)["date"])
    for table in TRADING_TABLES:  # rebuild the backtest from scratch
        with conn:
            conn.execute(f"DELETE FROM {table}")

    log.info("backtesting %d trading days, %s to %s", len(days), days[0], days[-1])
    totals, worst_residual = {}, 0.0
    for i, day in enumerate(days, start=1):
        if day in fatal_days:
            stop_if_fatal(issues)  # the same rule as the daily run: no usable data, no trading
        bridge = run_day(conn, cfg, run_id, day)["recon"]["bridge"]
        for key, value in bridge.items():
            totals[key] = totals.get(key, 0.0) + value
        worst_residual = max(worst_residual, abs(bridge["residual"]))
        if i % 250 == 0:
            log.info("... %d/%d days done (%s)", i, len(days), day)

    breaks = conn.execute("SELECT COUNT(*) FROM recon_breaks").fetchone()[0]
    log.info("%s\n  largest residual on any single day: Rs %.6f; position breaks: %d",
             reconcile.format_bridge(totals, f"PnL bridge, {days[0]} to {days[-1]} (INR):"),
             worst_residual, breaks)
    perf = log_performance(conn, cfg)
    return (f"{summary['rows'].sum()} price rows, {len(issues)} data-quality issues, "
            f"{len(days)} days backtested, net Sharpe {perf.loc['Sharpe ratio', 'net']:.2f}")


def run_daily(conn, cfg, run_id, run_date):
    """The morning run for `run_date`: bring the data up to the last close, then process it.

    The "data date" T is the last trading day before run_date. Normally only T
    is processed (re-processing it if it was done before is harmless). If
    earlier days were never processed, say because the computer was off, they
    are processed first, in order, so holdings and PnL never skip a day.
    """
    if run_date <= cfg["history_start"]:
        raise PipelineError(f"run date {run_date} is not after history_start {cfg['history_start']}")

    start = ingest.incremental_start(conn, cfg, before=run_date)
    issues = ingest_and_check(conn, cfg, run_id, start, run_date)
    stop_if_fatal(issues)

    data_day = conn.execute("SELECT MAX(date) FROM calendar WHERE date < ?", (run_date,)).fetchone()[0]
    last_done = conn.execute("SELECT MAX(date) FROM pnl_daily WHERE date < ?", (data_day,)).fetchone()[0]
    days = [r[0] for r in conn.execute("SELECT date FROM calendar WHERE date > ? AND date <= ? ORDER BY date",
                                       (last_done or "", data_day))]
    if len(days) > 1:
        log.warning("catching up %d earlier trading day(s) that were never processed: %s to %s",
                    len(days) - 1, days[0], days[-2])
    for day in days:
        result = run_day(conn, cfg, run_id, day)

    fills, orders, recon = result["fills"], result["orders"], result["recon"]
    selected = result["signal"][result["signal"]["selected"].astype(bool)]
    trade_day = strategy.next_weekday(data_day)
    log.info("data date %s: %d fills (%d partial or not filled), net PnL Rs %.2f, %d position breaks",
             data_day, len(fills), int((fills["filled_qty"] < fills["ordered_qty"]).sum()),
             result["pnl"]["net_pnl"], len(recon["position_breaks"]))
    log.info("%s", reconcile.format_bridge(recon["bridge"], f"PnL bridge for {data_day} (INR):"))
    log.info("signal for %s: %s", data_day,
             ", ".join(f"{t} {r:+.2%}" for t, r in zip(selected["ticker"], selected["ret_1d"])))
    log.info("%d orders for the next trading day (planned %s)", len(orders), trade_day)
    perf = log_performance(conn, cfg)
    return (f"data date {data_day}: net PnL Rs {result['pnl']['net_pnl']:,.0f}, {len(orders)} orders "
            f"for {trade_day}, strategy-to-date net Sharpe {perf.loc['Sharpe ratio', 'net']:.2f}")


def run_report(conn, cfg, run_id, run_date):
    """Rebuild reports/<run_date>.html from the database."""
    return f"report written to {report.write_report(conn, cfg, run_date)}"


COMMANDS = {"backfill": run_backfill, "daily": run_daily, "report": run_report}


def write_daily_report(conn, cfg, run_id, run_date, status):
    """Write the morning's report, even when the run failed (the report then shows why).

    If the report itself can't be written, the run counts as failed: nobody
    would see this morning's results.
    """
    try:
        log.info("report written to %s", report.write_report(conn, cfg, run_date, run_id))
        return status
    except Exception as exc:
        log.exception("could not write the report: %s", exc)
        db.finish_run(conn, run_id, "FAILED", f"report failed: {exc}")
        return "FAILED"


def parse_date(text):
    """argparse helper: accept only real YYYY-MM-DD dates."""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m pipeline.main", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill", help="build the full history and run the backtest day by day")
    daily = sub.add_parser("daily", help="the morning run")
    daily.add_argument("--date", type=parse_date, help="run as if it were the morning of this date (default: today)")
    rep = sub.add_parser("report", help="rebuild the HTML report for one run date")
    rep.add_argument("--date", type=parse_date, required=True)
    args = parser.parse_args(argv)
    run_date = getattr(args, "date", None) or now_ist().date().isoformat()

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    setup_logging(cfg["paths"]["logs"])

    try:
        conn = db.connect(cfg["paths"]["db"])
        db.init_schema(conn)
    except Exception as exc:  # can't even open the database: nothing else is safe
        log.exception("cannot open database %s: %s", cfg["paths"]["db"], exc)
        return 1

    run_id = new_run_id(args.command)
    db.start_run(conn, run_id, args.command, run_date)
    log.info("run %s started (run date %s)", run_id, run_date)
    try:
        message = COMMANDS[args.command](conn, cfg, run_id, run_date)
        status = "SUCCESS"
    except Exception as exc:
        if isinstance(exc, KNOWN_FAILURES):
            log.error("run %s FAILED: %s", run_id, exc)  # a known problem: the message says it all
        else:
            log.exception("run %s FAILED: %s", run_id, exc)  # a bug: include the full traceback
        status, message = "FAILED", str(exc)
    db.finish_run(conn, run_id, status, message)

    if args.command == "daily":
        status = write_daily_report(conn, cfg, run_id, run_date, status)
    conn.close()
    log.info("run %s %s: %s", run_id, status, message)
    return 0 if status == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
