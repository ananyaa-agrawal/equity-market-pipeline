# Equity Market Data Pipeline & Backtester

A Python + Bash project that runs every weekday morning, before the Indian stock
market opens at 09:15 IST, and:

1. downloads daily prices for 30 large NSE stocks,
2. runs data-quality checks on them,
3. stores the data in SQLite,
4. simulates how yesterday's orders were filled, and reconciles what we *expected*
   to happen against what *actually* happened,
5. decides today's trades with a simple strategy,
6. writes an HTML report and logs, and exits with a clear exit code (0 = success, 1 = failure).

A **backfill** mode builds 5 years of history and runs the whole backtest day by day,
through exactly the same code as the morning run, so the backtest and the "live"
run can never disagree.

The strategy is deliberately simple. The point of the project is the plumbing a
real trading firm depends on: reliable, tested data; idempotent runs (rerunning
never duplicates anything); validation; logging; monitoring; and reconciliation.
Everything is simulated: no real orders are ever sent.

## The daily flow

```
 08:30 IST, Mon-Fri
 cron ──► run_daily.sh ──► lock (flock): another run going? ──► FAILED line, exit 1
                │
                ├── weekend? ──► SKIPPED line, exit 0
                ▼
     python -m pipeline.main daily
                │
   1  ingest.py      download the last ~week from Yahoo Finance
                     (30 stocks + the NIFTY 50 index as the trading calendar)
   2  quality.py     8 data-quality checks ── no usable prices for the latest day? ──► stop, exit 1
   3  db.py          upsert prices, corporate actions, calendar and issues into SQLite
   4  run_day(T)     execution.py  fill yesterday's orders at today's open (slippage, fees, partial fills)
                     metrics.py    update holdings, calculate PnL
                     reconcile.py  expected book vs actual book, PnL bridge
   5                 strategy.py   rank T's returns, pick the 5 biggest losers, orders for T+1
   6  report.py      reports/<date>.html     + logs/pipeline.log, logs/cron.log, runs table
                │
                ▼
     exit 0 (SUCCESS line)  or  exit 1 (FAILED line + desktop notification)
```

T is the "data date": the last trading day before the run date. If earlier
trading days were missed (the computer was off), the daily run processes them
first, in order, so holdings and PnL never skip a day.

## Project layout

| File | What it does |
|---|---|
| `config/config.yaml` | Every setting: tickers, capital, costs, broker behaviour, quality thresholds, paths |
| `src/pipeline/config.py` | Loads and validates the config; refuses to start if a value looks wrong |
| `src/pipeline/db.py` | SQLite schema (12 tables) and the idempotent write helpers (`upsert`, `replace_dates`) |
| `src/pipeline/ingest.py` | Downloads prices and corporate actions with explicit yfinance settings and retries |
| `src/pipeline/quality.py` | The 8 data-quality checks and the rules for excluding a stock on a day |
| `src/pipeline/strategy.py` | The signal: rank by one-day return, buy the biggest losers, orders = target - actual |
| `src/pipeline/execution.py` | The simulated broker: fills at the next open with slippage, fees and seeded partial fills |
| `src/pipeline/reconcile.py` | Expected vs actual books: position breaks, implementation shortfall, PnL bridge |
| `src/pipeline/metrics.py` | Daily PnL (gross and net) and the summary statistics |
| `src/pipeline/report.py` | The daily HTML report with an embedded equity-curve chart |
| `src/pipeline/main.py` | The command line, run tracking, exit codes and `run_day`, the function both modes share |
| `run_daily.sh` | The Bash wrapper cron calls: lock, weekend skip, logging, failure line and notification |
| `tests/` | 47 pytest tests (see below) |
| `pyproject.toml` | Makes `src/pipeline` installable and tells pytest where the code is |
| `requirements.txt` | Exact library versions |

Generated and not committed: `data/market.db`, `logs/`, `reports/`, `.venv/`.

## Install

You need Python 3.11+, Bash, Git and cron, which means macOS or Linux. On Windows,
install WSL with Ubuntu first and do everything inside it.

```bash
cd market-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On a Mac, `run_daily.sh` also needs `flock`: `brew install flock`.

## Run it

Always activate the environment first: `source .venv/bin/activate`.

| Command | What it does |
|---|---|
| `python -m pytest -q` | Run the 47 tests (no internet needed; a few seconds) |
| `python -m pipeline.main backfill` | Download 5 years of history, check it, and backtest it day by day (under a minute) |
| `python -m pipeline.main daily` | The morning run for today |
| `python -m pipeline.main daily --date 2026-10-26` | The morning run as if it were that date (a dry run) |
| `python -m pipeline.main report --date 2026-10-26` | Rebuild that morning's report from the database |
| `./run_daily.sh` or `./run_daily.sh 2026-10-26` | What cron runs: the daily run plus lock, weekend skip and logging |

Where to look afterwards:

- `reports/<run date>.html`: the morning report (open it in any browser)
- `logs/pipeline.log`: every log line, as `timestamp LEVEL module message`
- `logs/cron.log`: one START line and one SUCCESS / SKIPPED / FAILED line per scheduled run
- the `runs` table: one row per run with its status and message:
  `sqlite3 -header -column data/market.db "SELECT * FROM runs ORDER BY started_at DESC LIMIT 5"`

## Schedule it with cron

Add this line with `crontab -e`, using the full path to your project folder:

```
30 8 * * 1-5 /full/path/to/market-pipeline/run_daily.sh
```

| Field | Value | Meaning |
|---|---|---|
| minute | `30` | at :30 |
| hour | `8` | 8 a.m. in the computer's timezone (set it to IST, or adjust) |
| day of month | `*` | every day of the month |
| month | `*` | every month |
| day of week | `1-5` | Monday (1) to Friday (5) |

Exchange holidays need no cron rule: the pipeline knows the trading calendar.
Check the schedule with `crontab -l`.

**cron only runs while the computer is on and awake** (and, on Windows, while WSL is
running). A missed morning isn't re-run by cron, but the next run catches up the
missed days automatically.

- **macOS:** if the project is inside Documents, Desktop or Downloads, give cron
  Full Disk Access: System Settings → Privacy & Security → Full Disk Access →
  **+** → Cmd+Shift+G → `/usr/sbin/cron`. Failure notifications use `osascript`.
- **Linux / WSL:** make sure the cron service is running (`sudo service cron start`
  in WSL). Failure notifications use `notify-send` if it is installed.

## Results

Backtest over **25 Oct 2021 to 23 Oct 2026**: 1,235 trading days, 30 stocks,
Rs 10 lakh capital (1 lakh = 100,000), 5 stocks held at a time, 5 bps slippage and
10 bps fees per side.

| | Gross (before costs) | Net (after costs) |
|---|---:|---:|
| Total return | +60.2% | −240.3% |
| Annualised return | +12.3% | −49.0% |
| Annualised volatility | 17.6% | 17.6% |
| Sharpe ratio | 0.70 | −2.78 |
| Max drawdown | −35.0% | −240.3% |
| Average daily turnover | 162.2% | 162.2% |
| Hit rate (winning days) | 53.9% | 42.9% |

**What it means.** Before costs, buying each day's biggest losers earned a modest
12.3% a year: big one-day drops do partly bounce back. But the strategy replaces
most of its holdings every day, trading Rs 16 lakh a day on Rs 10 lakh of capital
(Rs 200 crore over 5 years). At 0.15% per rupee traded (0.10% fees + 0.05%
slippage), that costs about 61% of capital a year, which turns +12.3% into −49.0%.
The backtest keeps investing Rs 10 lakh even after losing it; in reality the money
would have run out on 14 March 2023. **An edge before costs means nothing if
turnover × cost per trade is larger than the edge.**

**PnL bridge** (why the actual book differs from the "paper" book, which fills every
order in full at the signal price for free), in rupees over the 5 years:

| | Rs |
|---|---:|
| Expected PnL (paper) | +4,02,020 |
| Actual PnL | −24,02,733 |
| Difference | 28,04,753 |
| = overnight gap | −2,11,058 |
| + slippage | 10,01,605 |
| + fees | 20,03,210 |
| + partial-fill effect | 10,996 |
| + residual (unexplained) | **0.00** |

The two books reconcile to Rs 0.00 on every one of the 1,235 days. There were 558
position breaks (549 partial fills + 9 orders that could not fill); 525 cleared the
next day, and the other 33 were hit by a second partial fill.

**Data quality:** 34 issues across 37,080 price rows:

- 33 Yahoo **placeholder bars** on real trading days (volume 0, open = high = low = close
  = the previous close), 29 of them on 18 March 2025. They pass every standard check;
  the `placeholder_bar` check was added to catch them, and it blocked 9 orders that
  would otherwise have "filled" at prices that never traded.
- 1 unexplained **jump**: Adani Ports −21.1% on 4 June 2024 (election-results day).
  It was a real move, not bad data. A check can only flag a move; it can't tell news
  from a feed error.
- Also seen during development: Yahoo's newest bar arriving without a close (the
  run correctly stopped with exit code 1), and placeholder rows on the 15 January
  2026 holiday (ignored, because only calendar days count).

## Tests

47 pytest tests, no internet needed:

- **Data quality:** each check catches its problem on a small hand-made dataset: a
  missing day, broken OHLC, a stale price, an unexplained jump, a jump explained by
  a split, a duplicate row, missing values, freshness, the universe check and
  placeholder bars.
- **Idempotency:** writing the same rows twice and rerunning a whole trading day
  leave every table unchanged.
- **No look-ahead:** wildly changing day T+1's prices doesn't change day T's signal
  or orders.
- **Execution:** costs go the right way, and partial fills are repeatable and
  about 5% of orders.
- **Reconciliation:** the PnL bridge matches a hand calculation and the residual is
  zero; a deliberately wrong PnL is caught.
- **End to end:** a 3-stock, 6-day market runs through `run_day`, and the report
  rebuilds from the database, including for a failed run.

## Additions beyond the original spec

- The `placeholder_bar` data-quality check (see Results).
- The daily run catches up missed trading days.
- `run_daily.sh` accepts an optional date for dry runs, and uses macOS notifications
  when `notify-send` isn't available.
- `pyproject.toml`, so `python -m pipeline.main` and pytest can find the code in `src/`.

## Limitations

- **Daily data only.** Orders fill at the next day's open; there is no intraday data,
  so the strategy can't trade at the close it decided on.
- **Simulated execution.** Slippage is a flat 5 bps and fees a flat 10 bps. Real
  Indian costs (brokerage, STT, exchange fees, stamp duty, GST) depend on the trade,
  and real orders move the price, especially large ones. Partial fills are random,
  not based on real liquidity.
- **Adjusted-price simplification.** The strategy, fills and PnL all use
  dividend-adjusted prices, a standard simplification for a daily backtest; real
  trades happen at raw prices. Yahoo's "Close" is already adjusted for splits, so
  "raw" here means "not dividend-adjusted". Yahoo also re-scales past adjusted prices
  after every dividend, so in daily mode the PnL bridge may show a small residual
  warning on a dividend day. Its adjusted prices also vary in the 7th significant
  digit between downloads, so two backfills can differ by a few paise.
- **Survivorship bias.** Today's 30 large stocks are applied to the past five years.
  Stocks that shrank, merged (HDFC Ltd) or were delisted are missing, which makes
  any stock-picking backtest look better than it would have been at the time.
- **yfinance is unofficial.** It reads Yahoo Finance's website, has no service
  guarantee, can be rate-limited or break when Yahoo changes, and the data has
  errors (see the data-quality results).
- **The trading calendar comes from NIFTY's price history.** If Yahoo is missing the
  index's latest day entirely, the pipeline can't tell that from a holiday. Planned
  trade dates are "the next weekday", because there's no forward holiday calendar.
- **Fixed capital.** The strategy always invests Rs 10 lakh and never reinvests, so
  returns add up rather than compound, and losses can exceed 100% of capital.
- **One computer, one schedule.** If the computer is asleep at 08:30, that morning's
  run doesn't happen until the next scheduled run (which then catches up).

This is a learning project, not investment advice.
