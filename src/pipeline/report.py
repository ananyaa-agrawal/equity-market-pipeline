"""The daily HTML report: one self-contained page per run, reports/YYYY-MM-DD.html.

The page is named after the run date (the morning the pipeline ran). It
shows the "data date" T, the last close the run used, and has six sections:

1. Run status, timings and data date.
2. Data-quality summary and the list of issues.
3. Yesterday's orders: how they filled, and the implementation shortfall.
4. Reconciliation: position breaks and the PnL bridge.
5. Today's signal: the stocks selected and the orders for the next trading day.
6. Strategy-to-date metrics (gross vs net) and the equity-curve chart.

Everything is read from the database, so `python -m pipeline.main report
--date ...` can rebuild the report for any past morning. The chart is
embedded as base64 text inside the HTML, so the file is self-contained:
you can email it or open it anywhere, with no image files alongside.

Why it matters in a real trading firm: before the market opens, a human
signs off on the overnight run. That review only works if one page shows
whether the run worked, what it saw, what it traded, and whether the books
agree, in a form someone can read in two minutes.
"""

import base64
import html
import io
from datetime import date, datetime, timedelta

import matplotlib
import pandas as pd

from pipeline import db, metrics, quality, reconcile
from pipeline.config import now_ist
from pipeline.strategy import next_weekday

matplotlib.use("Agg")  # draw into memory, not a window: cron has no screen
import matplotlib.pyplot as plt  # must come after matplotlib.use()

DQ_WINDOW_DAYS = 7  # the report lists issues for the last week, the window a daily run re-checks

# Chart colours: the first three slots of a colour-blind-safe categorical palette.
SURFACE, TEXT, TEXT_MUTED, GRID, BASELINE = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#b9b8b2"
SERIES = [("Actual, after costs", "#2a78d6"), ("Gross, before costs", "#eb6834"), ("Expected (paper)", "#1baf7a")]


def write_report(conn, cfg, run_date, run_id=None):
    """Build the report for `run_date` and save it. Returns the file path."""
    path = cfg["paths"]["reports"] / f"{run_date}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(conn, cfg, run_date, run_id), encoding="utf-8")
    return path


def build_report(conn, cfg, run_date, run_id=None):
    """The whole page as one HTML string."""
    run = load_run(conn, run_date, run_id)
    data_day = _scalar(conn, "SELECT MAX(date) FROM calendar WHERE date < ?", run_date)
    parts = [section_status(run, run_date, data_day)]

    if data_day is None:
        parts.append("<p>There are no trading days in the database before this date.</p>")
    else:
        parts.append(section_quality(conn, data_day))
        if run is not None and run["status"] != "SUCCESS":
            parts.append('<p class="note">The run did not finish, so it made no fills, reconciliation '
                         "or new orders. Sections 3 to 6 are left out so that nothing from an earlier "
                         "run is mistaken for this one. The reason is in the status above.</p>")
        else:
            day = load_day(conn, data_day)
            parts += [section_fills(day), section_recon(day), section_signal(day),
                      section_metrics(conn, cfg, data_day)]

    title = f"Market pipeline report: {run_date}"
    return PAGE.format(title=html.escape(title), body="\n".join(parts),
                       generated=now_ist().isoformat(timespec="seconds"))


# ---------------------------------------------------------------------------
# Loading data
# ---------------------------------------------------------------------------

def load_run(conn, run_date, run_id=None):
    """The run this report is about: the given run_id, or the latest daily run for run_date."""
    if run_id:
        rows = db.query(conn, "SELECT * FROM runs WHERE run_id = ?", (run_id,))
    else:
        rows = db.query(conn, "SELECT * FROM runs WHERE mode = 'daily' AND as_of_date = ? "
                              "ORDER BY started_at DESC LIMIT 1", (run_date,))
    return None if rows.empty else rows.iloc[0]


def load_day(conn, data_day):
    """Everything sections 3 to 5 need about one trading day, rebuilt from the database."""
    prev = _scalar(conn, "SELECT MAX(date) FROM calendar WHERE date < ?", data_day)
    prices, excluded = quality.usable_window(conn, data_day)

    orders = db.query(conn, "SELECT * FROM orders WHERE created_date = ? ORDER BY ticker", (prev,))
    fills = db.query(conn, "SELECT f.*, p.adj_open AS open_price FROM fills f LEFT JOIN prices p "
                           "ON p.date = f.fill_date AND p.ticker = f.ticker WHERE f.fill_date = ?", (data_day,))
    trades = orders.merge(fills[["order_id", "filled_qty", "fill_price", "open_price", "fees_inr"]],
                          on="order_id", how="left")
    trades["filled_qty"] = trades["filled_qty"].fillna(0)
    shortfall = reconcile.implementation_shortfall(trades)

    expected = dict(conn.execute("SELECT ticker, shares FROM positions_expected WHERE date = ?", (data_day,)))
    marks_before = metrics.mark_prices(prices, excluded, prev) if prev else {}
    marks_today = metrics.mark_prices(prices, excluded, data_day)
    pnl = db.query(conn, "SELECT * FROM pnl_daily WHERE date = ?", (data_day,))
    actual_pnl = float(pnl["net_pnl"].iloc[0]) if not pnl.empty else 0.0
    bridge = reconcile.pnl_bridge(data_day, expected, trades, shortfall, marks_before, marks_today, actual_pnl)

    return {
        "day": data_day, "prev": prev, "trades": trades, "shortfall": shortfall, "bridge": bridge,
        "breaks": db.query(conn, "SELECT ticker, expected, actual, difference FROM recon_breaks "
                                 "WHERE date = ? ORDER BY ticker", (data_day,)),
        "signal": db.query(conn, "SELECT s.rank, s.ticker, s.ret_1d, p.adj_close FROM signals s "
                                 "JOIN prices p ON p.date = s.date AND p.ticker = s.ticker "
                                 "WHERE s.date = ? AND s.selected = 1 ORDER BY s.rank", (data_day,)),
        "new_orders": db.query(conn, "SELECT ticker, side, qty, signal_price, trade_date FROM orders "
                                     "WHERE created_date = ? ORDER BY side, ticker", (data_day,)),
    }


# ---------------------------------------------------------------------------
# The six sections
# ---------------------------------------------------------------------------

def section_status(run, run_date, data_day):
    if run is None:
        status = '<span class="badge">NO RUN RECORDED</span>'
        rows = [("Run", "No daily run is recorded for this date; the report was built from the database.")]
    else:
        ok = run["status"] == "SUCCESS"
        status = f'<span class="badge {"ok" if ok else "bad"}">{"&#10003;" if ok else "&#10007;"} {html.escape(run["status"])}</span>'
        rows = [("Run id", run["run_id"]), ("Mode", run["mode"]), ("Started", run["started_at"]),
                ("Finished", run["finished_at"] or "(still running)"),
                ("Duration", _duration(run["started_at"], run["finished_at"])),
                ("Message", run["message"] or "")]
    rows = [("Run date (the morning of)", run_date),
            ("Data date (last close used)", data_day or "none"),
            ("New orders are for (planned)", next_weekday(data_day) if data_day else "")] + rows
    body = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    return f'<h2>1. Run status {status}</h2><table class="kv">{body}</table>'


def section_quality(conn, data_day):
    start = (date.fromisoformat(data_day) - timedelta(days=DQ_WINDOW_DAYS)).isoformat()
    issues = db.query(conn, "SELECT date, ticker, check_name, severity, details FROM dq_issues "
                            "WHERE date > ? AND date <= ? ORDER BY date, check_name, ticker", (start, data_day))
    summary = quality.summarize(issues)
    summary.columns.name = None  # drop the "severity" header label so the table has a single header row
    summary = summary.reset_index().rename(columns={"check_name": "check"})
    listing = table(issues) if not issues.empty else '<p class="muted">No issues: every check passed.</p>'
    return (f"<h2>2. Data quality</h2><p>Trading days after {start} up to {data_day}. "
            "ERROR = data is wrong, WARNING = suspicious, INFO = explained.</p>"
            f"{table(summary)}<h3>Issues found ({len(issues)})</h3>{listing}")


def section_fills(day):
    t = day["trades"]
    head = (f"<h2>3. Yesterday's orders</h2><p>Created after the close on {day['prev']}, "
            f"filled at the open on {day['day']}. Shortfall = how much worse than the signal "
            "price we traded (positive = a cost), split into the overnight gap and slippage.</p>")
    if t.empty:
        return head + '<p class="muted">There were no orders to fill.</p>'
    t = t.merge(day["shortfall"][["order_id", "gap_bps", "slippage_bps", "shortfall_inr", "shortfall_bps"]],
                on="order_id", how="left")
    t["result"] = [("Not filled" if f == 0 else "Filled" if f == q else f"Partial ({int(f)} of {q})")
                   for f, q in zip(t["filled_qty"], t["qty"])]
    shown = t[["ticker", "side", "qty", "filled_qty", "result", "signal_price", "open_price", "fill_price",
               "gap_bps", "slippage_bps", "shortfall_inr", "shortfall_bps", "fees_inr"]].rename(columns={
                   "qty": "ordered", "filled_qty": "filled", "signal_price": "signal price", "open_price": "open",
                   "fill_price": "fill price", "gap_bps": "gap (bps)", "slippage_bps": "slippage (bps)",
                   "shortfall_inr": "shortfall (Rs)", "shortfall_bps": "shortfall (bps)", "fees_inr": "fees (Rs)"})
    money = "{:,.2f}"
    s = day["shortfall"]
    totals = (f"<p>Total shortfall Rs {s['shortfall_inr'].sum():,.2f} (gap Rs {s['gap_inr'].sum():,.2f} "
              f"+ slippage Rs {s['slippage_inr'].sum():,.2f}); fees Rs {t['fees_inr'].sum():,.2f}.</p>")
    return head + table(shown, {"filled": "{:.0f}", "signal price": money, "open": money, "fill price": money,
                                "gap (bps)": "{:+.1f}", "slippage (bps)": "{:+.1f}", "shortfall (Rs)": money,
                                "shortfall (bps)": "{:+.1f}", "fees (Rs)": money}) + totals


def section_recon(day):
    b = day["bridge"]
    ok = abs(b["residual"]) <= reconcile.RESIDUAL_TOLERANCE_INR
    verdict = ('<p><span class="badge ok">&#10003; Books reconcile</span> the residual is within Rs 1.</p>' if ok else
               f'<p><span class="badge bad">&#10007; WARNING</span> residual Rs {b["residual"]:,.2f}: '
               "the books disagree for a reason we can't explain. Investigate before trading.</p>")
    bridge = pd.DataFrame([
        ("Expected PnL (paper: full fills at signal price, no costs)", b["expected_pnl"]),
        ("Actual PnL (simulated fills, after costs)", b["actual_pnl"]),
        ("Difference", b["difference"]),
        ("= overnight gap", b["gap"]), ("+ slippage", b["slippage"]), ("+ fees", b["fees"]),
        ("+ partial-fill effect", b["partial_fill"]), ("+ residual (should be ~0)", b["residual"]),
    ], columns=["PnL bridge", "Rs"])
    shares = {"expected": "{:.0f}", "actual": "{:.0f}", "difference": "{:+.0f}"}
    breaks = (table(day["breaks"], shares) if not day["breaks"].empty
              else '<p class="muted">No position breaks: both books hold the same shares.</p>')
    return (f"<h2>4. Reconciliation</h2><p>Expected book = every order filled in full at its signal "
            f"price, free. Actual book = the simulated fills.</p>{verdict}"
            f"<h3>Position breaks on {day['day']}</h3>{breaks}"
            f"<h3>PnL bridge for {day['day']}</h3>{table(bridge, {'Rs': '{:,.2f}'})}")


def section_signal(day):
    sig = day["signal"].rename(columns={"ret_1d": "return on the day", "adj_close": "close"})
    orders = day["new_orders"].assign(value=lambda o: o["qty"] * o["signal_price"])
    orders = orders.rename(columns={"signal_price": "signal price", "value": "value (Rs)",
                                    "trade_date": "planned trade date"})
    order_table = (table(orders, {"signal price": "{:,.2f}", "value (Rs)": "{:,.0f}"}) if not orders.empty
                   else '<p class="muted">No orders: holdings already match the target.</p>')
    return (f"<h2>5. Today's signal</h2><p>The {len(sig)} biggest losers on {day['day']} "
            "(adjusted close-to-close return).</p>"
            f"{table(sig, {'return on the day': '{:+.2%}', 'close': '{:,.2f}'})}"
            f"<h3>Orders for the next trading day ({len(orders)})</h3>"
            "<p>Target shares minus the shares we actually hold. They execute at the next open.</p>"
            f"{order_table}")


def section_metrics(conn, cfg, data_day):
    capital = cfg["strategy"]["capital_inr"]
    pnl = db.query(conn, "SELECT * FROM pnl_daily WHERE date <= ? ORDER BY date", (data_day,))
    if pnl.empty:
        return '<h2>6. Strategy to date</h2><p class="muted">No history yet.</p>'
    fills = db.query(conn, "SELECT * FROM fills WHERE fill_date <= ?", (data_day,))
    perf = metrics.performance(pnl, fills, capital)
    shown = perf.reset_index().rename(columns={"index": "metric"})
    for col in ("gross", "net"):
        shown[col] = [f"{v:.2f}" if m == "Sharpe ratio" else f"{v:.1%}" for m, v in zip(shown["metric"], shown[col])]
    chart = equity_chart(pnl, capital)
    return (f"<h2>6. Strategy to date</h2><p>{pnl['date'].iloc[0]} to {data_day}, "
            f"Rs {capital / 1e5:,.0f} lakh capital. Gross = before trading costs, net = after.</p>"
            f"{table(shown)}<h3>Equity curve</h3>"
            f'<img alt="Equity curves in Rs lakh: actual, gross and expected" src="data:image/png;base64,{chart}">')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def equity_chart(pnl, capital):
    """Equity over time for the three views of the strategy, as a base64-encoded PNG.

    Actual  = the simulated account, after all costs
    Gross   = the same trades with no fees or slippage
    Expected = the paper book (trading at the signal price, free)
    """
    dates = pd.to_datetime(pnl["date"])
    values = [pnl["actual_equity"], capital + pnl["gross_pnl"].cumsum(), pnl["expected_equity"]]
    values = [v / 1e5 for v in values]  # in Rs lakh

    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=110)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.axhline(capital / 1e5, color=BASELINE, linewidth=1)  # starting capital
    for (name, color), v in zip(SERIES, values):
        ax.plot(dates, v, color=color, linewidth=2, solid_capstyle="round", solid_joinstyle="round", label=name)
        ax.plot(dates.iloc[-1], v.iloc[-1], "o", color=color, markersize=8, markeredgecolor=SURFACE,
                markeredgewidth=2, zorder=3)

    # Direct labels at the line ends, nudged apart so they never overlap.
    ends = sorted(((v.iloc[-1], name) for (name, _), v in zip(SERIES, values)), reverse=True)
    low, high = ax.get_ylim()
    gap, placed = (high - low) * 0.07, []
    for y, name in ends:
        y_text = min(y, placed[-1] - gap) if placed else y
        placed.append(y_text)
        ax.annotate(f"{name.split(',')[0].split(' (')[0]} {y:,.1f}", xy=(dates.iloc[-1], y),
                    xytext=(10, (y_text - y) / (high - low) * ax.bbox.height * 72 / fig.dpi),
                    textcoords="offset points",
                    va="center", fontsize=9, color=TEXT, annotation_clip=False)

    ax.grid(axis="y", color=GRID, linewidth=1)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=TEXT_MUTED, labelsize=9, length=0)
    ax.set_ylabel("Equity (Rs lakh)", color=TEXT_MUTED, fontsize=9)
    ax.legend(frameon=False, loc="upper left", fontsize=9, labelcolor=TEXT)

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def table(df, formats=None):
    """A DataFrame as a plain HTML table; `formats` maps column -> format string."""
    shown = df.copy()
    for col, fmt in (formats or {}).items():
        shown[col] = ["" if pd.isna(v) else fmt.format(v) for v in shown[col]]
    return f'<div class="wrap">{shown.to_html(index=False, border=0, classes="t", na_rep="")}</div>'


def _duration(started, finished):
    if not finished:
        return ""
    seconds = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
    return f"{seconds:.0f} s"


def _scalar(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #1d1d1b;
         background: #fcfcfb; max-width: 1040px; margin: 24px auto; padding: 0 16px; line-height: 1.45; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  h2 {{ font-size: 17px; margin-top: 36px; padding-bottom: 4px; border-bottom: 1px solid #e4e3df; }}
  h3 {{ font-size: 14px; margin: 18px 0 4px; }}
  p {{ margin: 6px 0; font-size: 14px; }}
  .muted {{ color: #6b6a65; }}
  .note {{ background: #fff6e0; border-left: 3px solid #eda100; padding: 8px 12px; }}
  .wrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; font-size: 13px; margin: 6px 0; font-variant-numeric: tabular-nums; }}
  .t th, .t td {{ padding: 4px 10px; border-bottom: 1px solid #ecebe7; text-align: right; white-space: nowrap; }}
  .t th {{ background: #f3f2ee; font-weight: 600; }}
  .t td:first-child, .t th:first-child {{ text-align: left; }}
  .kv th {{ text-align: left; font-weight: 600; padding: 3px 16px 3px 0; color: #52514e; }}
  .kv td {{ padding: 3px 0; }}
  .badge {{ font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 10px; background: #eceae5;
           vertical-align: middle; }}
  .ok {{ background: #e3f4e8; color: #0b5a2a; }}
  .bad {{ background: #fde7e7; color: #8a1c1c; }}
  img {{ max-width: 100%; height: auto; }}
  footer {{ margin: 40px 0 24px; font-size: 12px; color: #6b6a65; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="muted">Equity market data pipeline &amp; backtester. A simulation: no real orders are sent.</p>
{body}
<footer>Generated {generated}</footer>
</body>
</html>
"""
