"""Load config/config.yaml and check that every setting is present and sensible.

Why it matters in a real trading firm: a typo in a config file (capital of
10000000 instead of 1000000, a probability of 5 instead of 0.05) can mean trading
ten times too much. Production systems therefore validate their configuration
before doing anything else and refuse to start if something looks wrong, instead
of failing halfway through the morning run.
"""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

# The project folder: two levels up from this file (src/pipeline/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

# The NSE runs on Indian Standard Time. We always use the market's clock, whatever
# timezone this computer happens to be set to.
MARKET_TZ = ZoneInfo("Asia/Kolkata")


class ConfigError(Exception):
    """Raised when config.yaml is missing a setting or has a bad value."""


def now_ist():
    """The current date and time in India."""
    return datetime.now(MARKET_TZ)


def load_config(path=DEFAULT_CONFIG_PATH):
    """Read the YAML file, validate it, and return it as a plain dictionary.

    Paths in the `paths` section are turned into absolute Path objects, so the
    pipeline works no matter which folder it is started from (cron starts it
    from your home folder, for example).
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path} is empty or not a YAML mapping")

    validate_config(cfg)

    for key in ("db", "logs", "reports"):
        p = Path(cfg["paths"][key])
        cfg["paths"][key] = p if p.is_absolute() else PROJECT_ROOT / p
    return cfg


def validate_config(cfg):
    """Raise ConfigError with a clear message for the first problem found."""
    tickers = _get(cfg, "tickers")
    if not isinstance(tickers, list) or not tickers:
        raise ConfigError("tickers must be a non-empty list")
    if not all(isinstance(t, str) and t.strip() for t in tickers):
        raise ConfigError("every ticker must be a non-empty string")
    duplicates = sorted({t for t in tickers if tickers.count(t) > 1})
    if duplicates:
        raise ConfigError(f"duplicate tickers: {duplicates}")

    if not isinstance(_get(cfg, "calendar_ticker"), str):
        raise ConfigError("calendar_ticker must be a string such as ^NSEI")

    # YAML turns an unquoted 2021-10-24 into a date object; a quoted one stays a string.
    start = _get(cfg, "history_start")
    try:
        start = start if isinstance(start, date) else date.fromisoformat(str(start))
    except ValueError:
        raise ConfigError(f"history_start must be a YYYY-MM-DD date, got {start!r}")
    if start >= now_ist().date():
        raise ConfigError(f"history_start {start} must be in the past")
    cfg["history_start"] = start.isoformat()

    _number(cfg, "strategy.n_stocks", low=1, high=len(tickers), integer=True)
    _number(cfg, "strategy.capital_inr", low=1)
    _number(cfg, "costs.slippage_bps", low=0)
    _number(cfg, "costs.fee_bps", low=0)
    _number(cfg, "broker.partial_fill_prob", low=0, high=1)
    low = _number(cfg, "broker.partial_fill_min", low=0, high=1)
    high = _number(cfg, "broker.partial_fill_max", low=0, high=1)
    if low > high:
        raise ConfigError("broker.partial_fill_min must not be larger than partial_fill_max")
    _number(cfg, "broker.random_seed", low=0, integer=True)
    _number(cfg, "quality.jump_threshold", low=0.01)
    _number(cfg, "quality.stale_days", low=1, integer=True)

    for key in ("db", "logs", "reports"):
        if not isinstance(_get(cfg, f"paths.{key}"), str):
            raise ConfigError(f"paths.{key} must be a path string")


def _get(cfg, dotted_key):
    """Look up "strategy.n_stocks" as cfg["strategy"]["n_stocks"], with a clear error."""
    node = cfg
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"missing required setting: {dotted_key}")
        node = node[part]
    return node


def _number(cfg, dotted_key, low=None, high=None, integer=False):
    """Check that a setting is a number (optionally a whole number) within [low, high]."""
    value = _get(cfg, dotted_key)
    allowed = int if integer else (int, float)
    # bool is a kind of int in Python (True == 1), so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, allowed):
        kind = "a whole number" if integer else "a number"
        raise ConfigError(f"{dotted_key} must be {kind}, got {value!r}")
    if low is not None and value < low:
        raise ConfigError(f"{dotted_key} must be at least {low}, got {value}")
    if high is not None and value > high:
        raise ConfigError(f"{dotted_key} must be at most {high}, got {value}")
    return value
