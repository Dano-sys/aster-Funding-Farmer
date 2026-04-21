"""
Aster DEX - Funding Rate Farmer
================================
Strategy:
  - Scans all perp symbols using ``lastFundingRate`` from ``GET /fapi/v1/premiumIndex``
    (last settled rate per symbol; not always the next interval's predicted rate).
  - Ranks by that rate (highest first) and opens LONG legs on the best eligible pools.
    Per Aster's user docs, a *positive* published rate means longs pay shorts; many venues
    still expose a signed ``lastFundingRate`` — verify your economic direction against
    ``GET /fapi/v1/income`` (FUNDING_FEE rows) for open symbols (the bot logs an
    occasional self-check when live).
  - Funding settles on an exchange-defined interval ``N`` (often 8h; can be 4h/1h etc.).
    The bot learns ``N`` per symbol when ``nextFundingTime`` steps between polls and
    annualizes APR with ``24/N_hours`` fundings per day (default 8h until learned).
  - Uses USDF + ASTER tokens as margin (Multi-Asset Mode) for max Stage 6 points
  - Exits when the chosen funding series falls below ``EXIT_FUNDING_RATE`` (default:
    REST ``lastFundingRate``; optional WebSocket ``r`` field when enabled — see env).
  - Monitors stop loss / optional take profit; rotates when pools fall out of threshold

Margin setup (Multi-Asset Mode on BNB Chain):
  - USDF  -> 99.99% collateral value ratio  (yield-bearing stablecoin)
  - ASTER -> 80%    collateral value ratio  (idle ASTER tokens work as margin)
  Both count toward Aster Asset Points in Stage 6 scoring.
  Bot enables Multi-Asset Mode on startup automatically.

Points earned:
  - Trading Points (entry/exit fees)
  - Position Points (large size * hold time - no cap in Stage 6)
  - Aster Asset Points (USDF + ASTER margin bonus)
  - PnL Points (funding carry profit)
"""

import argparse
import json
import os
import re
import csv
import time
from typing import AbstractSet, List, Optional, Tuple
import logging
import requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from dotenv import load_dotenv
from colorama import init, Fore, Style

load_dotenv()

from config import (
    NEWS_POLL_SEC,
    NEWS_SYMBOL_BOOST_ENABLED,
    NEWS_SYMBOL_BOOST_TTL_SEC,
    X_API_KEY,
    X_API_SECRET,
    X_BEARER_TOKEN,
)
from aster_client import SAPI_BASE, credentials_ok, get
import exchange as ex
init(autoreset=True)

# --- Logging ------------------------------------------------------------------

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

_LOG_MESSAGE_FORMAT = "%(asctime)s %(levelname)s [%(shortname)s] %(message)s"


class _ShortLoggerFormatter(logging.Formatter):
    """ISO-8601 UTC timestamps and last segment of logger name for scanability."""

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    def format(self, record: logging.LogRecord) -> str:
        record.shortname = record.name.rsplit(".", 1)[-1]
        return super().format(record)


class _StripAnsiFormatter(_ShortLoggerFormatter):
    """File logs without embedded colorama / ANSI codes (easier to parse and tail)."""

    def format(self, record: logging.LogRecord) -> str:
        return _ANSI_ESCAPE.sub("", super().format(record))


def _parse_log_level(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip().upper()
    if not raw:
        return default
    level = getattr(logging, raw, None)
    return level if isinstance(level, int) else default


def _configure_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_funding_farmer_logging", False):
        return
    root.setLevel(logging.INFO)
    log_path = os.getenv("FUNDING_FARMER_LOG", "funding_farmer.log").strip() or "funding_farmer.log"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(_StripAnsiFormatter(_LOG_MESSAGE_FORMAT))
    sh = logging.StreamHandler()
    sh.setFormatter(_ShortLoggerFormatter(_LOG_MESSAGE_FORMAT))
    root.addHandler(fh)
    root.addHandler(sh)
    ex_level = _parse_log_level("LOG_EXCHANGE_LEVEL", logging.INFO)
    logging.getLogger("exchange").setLevel(ex_level)
    root._funding_farmer_logging = True  # type: ignore[attr-defined]


_configure_logging()
log = logging.getLogger(__name__)

def log_info(msg):
    # Leading cyan would override embedded Fore.GREEN/RED in many terminals — use plain log for those.
    log.info(Fore.CYAN + msg + Style.RESET_ALL)


def log_info_styled(msg: str) -> None:
    """Multicolor line: no cyan prefix (message must include its own Fore.* / Style.* where needed)."""
    log.info(msg + Style.RESET_ALL)


def log_section(title: str) -> None:
    """Dim section header — use with log_info_styled body lines so cyan log_info does not clash."""
    log_info_styled(
        f"\n{Style.DIM}{Fore.LIGHTBLACK_EX}  --- {title} ---{Style.RESET_ALL}"
    )


def log_success(msg): log.info(Fore.GREEN   + msg + Style.RESET_ALL)
def log_warn(msg):    log.warning(Fore.YELLOW + msg + Style.RESET_ALL)
def log_error(msg):   log.error(Fore.RED    + msg + Style.RESET_ALL)

# --- Config -------------------------------------------------------------------

# Multi-Asset Mode collateral ratios (BNB Chain, from Aster docs)
# 2000 ASTER @ ~$0.70 = $1,400 * 80% = ~$1,120 effective margin
# USDF is 99.99% -- essentially 1:1, best stablecoin on the platform
ASTER_COLLATERAL_RATIO = 0.80
USDF_COLLATERAL_RATIO  = 0.9999

# Hide spot + futures wallet lines below this estimated USD value (0 = show any non-zero)
_dust_raw = os.getenv("BALANCE_DUST_USD", "5").strip()
BALANCE_DUST_USD = float(_dust_raw) if _dust_raw else 0.0

_income_lb = os.getenv("INCOME_LOOKBACK_DAYS", "30").strip()
INCOME_LOOKBACK_DAYS = max(1, int(_income_lb)) if _income_lb else 30

# Risk params - tune in .env
LEVERAGE          = int(os.getenv("LEVERAGE", "3"))
MIN_FUNDING_RATE  = float(os.getenv("MIN_FUNDING_RATE", "0.0005"))
EXIT_FUNDING_RATE = float(os.getenv("EXIT_FUNDING_RATE", "0.0001"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))
# When any perp long is open, sleep this long between cycles (stop loss + funding exit).
# Much shorter than POLL_INTERVAL_SEC so leveraged moves are re-checked frequently.
RISK_POLL_INTERVAL_SEC = int(os.getenv("RISK_POLL_INTERVAL_SEC", "15"))
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT", "0.05"))
# Take profit on mark vs entry (0 = disabled). Typical 0.04–0.08; lower = more churn/fees.
TAKE_PROFIT_PCT   = float(os.getenv("TAKE_PROFIT_PCT", "0"))
# When MARK_PRICE_WS=true: use stream field ``r`` (Binance-style estimated funding) for
# funding_dropped exits if present; else REST lastFundingRate. REST premiumIndex has no
# predicted rate on Aster today — ``r`` may still appear on markPrice WS payloads.
_few_raw = os.getenv("FUNDING_EXIT_USE_WS_ESTIMATED", "false").strip().lower()
FUNDING_EXIT_USE_WS_ESTIMATED = _few_raw in ("1", "true", "yes", "on")
_fssc = os.getenv("FUNDING_SIGN_SELF_CHECK_CYCLES", "36").strip()
try:
    FUNDING_SIGN_SELF_CHECK_CYCLES = max(0, int(_fssc))
except ValueError:
    FUNDING_SIGN_SELF_CHECK_CYCLES = 36

# Fee-aware new opens (optional): skip if |lastFundingRate| is too small vs assumed round-trip taker fees.
# Breakeven funding intervals ≈ (2 * ESTIMATED_TAKER_FEE_BPS / 10000) / abs(rate). When
# MAX_FEE_BREAKEVEN_FUNDING_INTERVALS > 0, skip opens that need more intervals than this to cover fees.
_est_fee_bps = os.getenv("ESTIMATED_TAKER_FEE_BPS", "5").strip()
try:
    ESTIMATED_TAKER_FEE_BPS = max(0.0, float(_est_fee_bps))
except ValueError:
    ESTIMATED_TAKER_FEE_BPS = 5.0
_mfbfi = os.getenv("MAX_FEE_BREAKEVEN_FUNDING_INTERVALS", "0").strip()
try:
    MAX_FEE_BREAKEVEN_FUNDING_INTERVALS = max(0.0, float(_mfbfi))
except ValueError:
    MAX_FEE_BREAKEVEN_FUNDING_INTERVALS = 0.0

BLACKLIST         = [s for s in os.getenv("BLACKLIST", "").split(",") if s]
TRADE_LOG_FILE    = os.getenv("TRADE_LOG_FILE", "trades.csv")

# Pool quality (liquidity): min trailing 24h USDT quote volume from GET /fapi/v1/ticker/24hr.
# 0 = no filter (legacy: chase top funding regardless of depth). Typical values: 1e6–2e7 for liquid alts/majors.
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "0") or 0)
_allow_raw = os.getenv("SYMBOL_ALLOWLIST", "").strip()
SYMBOL_ALLOWLIST: Optional[set] = None
if _allow_raw:
    SYMBOL_ALLOWLIST = {x.strip().upper() for x in _allow_raw.split(",") if x.strip()}

# Wallet-based sizing (same math live and DRY_RUN)
# Deploy notional budget = effective_margin × WALLET_DEPLOY_PCT × LEVERAGE (per cycle).
# DRY_RUN_SIMULATED_MARGIN_USD replaces effective_margin when > 0 so paper runs match live logic.
WALLET_DEPLOY_PCT = float(os.getenv("WALLET_DEPLOY_PCT", "0.80"))
# Never deploy more than this absolute cap (safety ceiling, 0 = no cap)
WALLET_MAX_USD    = float(os.getenv("WALLET_MAX_USD", "0"))
# Never deploy less than this (avoids tiny below-minimum positions)
WALLET_MIN_USD    = float(os.getenv("WALLET_MIN_USD", "20"))

# Dry run mode
# true  = live API reads (rates, marks, balances) + same sizing math as live, but perp orders are
#         simulated in memory only (no POST /order). Wallet: when DRY_RUN_SIMULATED_MARGIN_USD is 0
#         (default), deploy budget uses your real effective margin from GET /fapi/v2/balance (+ cap
#         logic). When DRY_RUN_SIMULATED_MARGIN_USD > 0, sizing uses that USD instead of wallet.
# false = live trading
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
# Dry run: if > 0, replace effective margin for sizing (fixed paper wallet). If 0 / unset, sizing
# uses live API margin like production — still no real orders while DRY_RUN=true.
_drs = os.getenv("DRY_RUN_SIMULATED_MARGIN_USD")
if _drs is None or str(_drs).strip() == "":
    DRY_RUN_SIMULATED_MARGIN_USD = 0.0
else:
    DRY_RUN_SIMULATED_MARGIN_USD = float(str(_drs).strip())

# Dry run: show live futures + spot balance tables in logs (real /fapi + spot reads).
# Default true in dry run when unset so wallet and simulation context stay aligned; set false for quieter logs.
_rwl = os.getenv("DRY_RUN_SHOW_LIVE_WALLET_DETAILS", "").strip().lower()
if _rwl in ("1", "true", "yes"):
    DRY_RUN_SHOW_LIVE_WALLET_DETAILS = True
elif _rwl in ("0", "false", "no"):
    DRY_RUN_SHOW_LIVE_WALLET_DETAILS = False
else:
    DRY_RUN_SHOW_LIVE_WALLET_DETAILS = True


def live_wallet_logs_enabled() -> bool:
    """True = log live futures/spot wallet tables; live mode always True."""
    return (not DRY_RUN) or DRY_RUN_SHOW_LIVE_WALLET_DETAILS


# Delta-neutral mode
# Set to true to enable the Hyperliquid hedge leg (requires HL_PRIVATE_KEY etc. in .env)
# When false (default), bot runs Aster-only funding farm with no HL connection
DELTA_NEUTRAL = os.getenv("DELTA_NEUTRAL", "false").lower() == "true"

# Mark-price WebSocket: push-based stop vs entry (faster than REST alone; requires websocket-client)
MARK_PRICE_WS = os.getenv("MARK_PRICE_WS", "true").lower() == "true"
# Log best bid / ask / mid (+ mark) for open positions and top funding symbols (extra REST calls)
SHOW_BOOK_IN_LOGS = os.getenv("SHOW_BOOK_IN_LOGS", "false").lower() == "true"

# Diversification: max concurrent longs (slots). Set MAX_POSITIONS in .env — higher = more names, thinner slices.
MAX_POSITIONS       = int(os.getenv("MAX_POSITIONS", "7"))
# Rank-weighted sizing: top symbol gets RANK_TOP_PCT% of budget,
# remainder split equally among the rest.
# e.g. 7 positions, RANK_TOP_PCT=0.25:
#   #1 gets 25%, #2-7 each get 75%/6 = 12.5%
RANK_TOP_PCT        = float(os.getenv("RANK_TOP_PCT", "0.25"))
# Hard cap per symbol as % of total budget (prevents over-concentration)
MAX_SINGLE_PCT      = float(os.getenv("MAX_SINGLE_PCT", "0.30"))
# Correlated pairs to avoid holding simultaneously (comma-sep, pipe-delimited groups)
# e.g. "BTCUSDT|WBTCUSDT,ETHUSDT|STETHUSDT" = don't hold BTC+WBTC or ETH+STETH together
CORR_GROUPS_RAW     = os.getenv("CORR_GROUPS", "BTCUSDT|WBTCUSDT,ETHUSDT|STETHUSDT|WETHUSDT")
CORR_GROUPS: list   = [g.split("|") for g in CORR_GROUPS_RAW.split(",") if g]

# Never allocate 100% of max deploy at once — leave headroom for new pool names later.
# RESERVE_SLOT_FOR_NEW_POOLS=true -> reserve 1/MAX_POSITIONS of max deploy (one slot).
# Or set RESERVE_DEPLOY_PCT (e.g. 0.15 = 15% of max deploy stays unused for new opens).
_reserve_slot = os.getenv("RESERVE_SLOT_FOR_NEW_POOLS", "false").lower() == "true"
_reserve_pct_raw = os.getenv("RESERVE_DEPLOY_PCT", "").strip()
if _reserve_pct_raw != "":
    try:
        RESERVE_DEPLOY_PCT = min(0.95, max(0.0, float(_reserve_pct_raw)))
    except ValueError:
        RESERVE_DEPLOY_PCT = 0.0
elif _reserve_slot:
    RESERVE_DEPLOY_PCT = 1.0 / max(1, MAX_POSITIONS)
else:
    RESERVE_DEPLOY_PCT = 0.0
RESERVE_SLOT_FOR_NEW_POOLS = _reserve_slot

# One JSON line per cycle for alerts / advisors (default off).
_cs = os.getenv("CYCLE_SNAPSHOT_ENABLE", "").strip().lower()
CYCLE_SNAPSHOT_ENABLE = _cs in ("1", "true", "yes")
CYCLE_SNAPSHOT_FILE = os.getenv("CYCLE_SNAPSHOT_FILE", "farmer_cycle.jsonl").strip() or "farmer_cycle.jsonl"
_csm = os.getenv("CYCLE_SNAPSHOT_MAX_LINES", "500").strip()
try:
    CYCLE_SNAPSHOT_MAX_LINES = max(10, int(_csm or "500"))
except ValueError:
    CYCLE_SNAPSHOT_MAX_LINES = 500


def farming_halt_active() -> Tuple[bool, str]:
    """Skip NEW long opens; stop-loss / take-profit / funding exits unchanged. File checked every cycle."""
    if os.getenv("FARMING_HALT", "").strip().lower() in ("1", "true", "yes"):
        return True, "FARMING_HALT=true"
    path = os.getenv("FARMING_HALT_FILE", "").strip()
    if path and os.path.isfile(path):
        return True, f"halt file: {path}"
    return False, ""


def _trim_cycle_snapshot_file() -> None:
    if not CYCLE_SNAPSHOT_ENABLE or CYCLE_SNAPSHOT_MAX_LINES <= 0:
        return
    try:
        with open(CYCLE_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= CYCLE_SNAPSHOT_MAX_LINES:
            return
        with open(CYCLE_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines[-CYCLE_SNAPSHOT_MAX_LINES :])
    except OSError:
        pass


def append_cycle_snapshot(
    *,
    open_symbols: set,
    position_sizes: dict,
    avail_budget: float,
    total_budget: float,
    margin_effective: float,
    halted: bool,
    halt_reason: str,
    deploy_cap: Optional[float] = None,
) -> None:
    if not CYCLE_SNAPSHOT_ENABLE:
        return
    row = {
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "open_symbols": sorted(open_symbols),
        "position_sizes": {k: round(float(v), 2) for k, v in position_sizes.items()},
        "avail_budget": round(float(avail_budget), 2),
        "total_budget": round(float(total_budget), 2),
        "margin_effective": round(float(margin_effective), 2),
        "farming_halted": halted,
        "halt_reason": halt_reason or None,
    }
    if deploy_cap is not None:
        row["deploy_cap"] = round(float(deploy_cap), 2)
    if RESERVE_DEPLOY_PCT > 0:
        row["reserve_deploy_pct"] = RESERVE_DEPLOY_PCT
    try:
        with open(CYCLE_SNAPSHOT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        _trim_cycle_snapshot_file()
    except OSError as e:
        log_warn(f"  [snapshot] write failed: {e}")


# --- Trade Logger -------------------------------------------------------------

TRADE_CSV_HEADERS = [
    "timestamp_utc", "action", "symbol", "order_id",
    "quantity", "price", "notional_usdt",
    "funding_rate_8h", "funding_apr_pct",
    "entry_price", "exit_price",
    "fee_entry_usdt", "fee_exit_usdt", "fees_usdt",
    "pnl_gross_usdt", "pnl_usdt", "pnl_pct",
    "funding_income_usdt", "pnl_net_incl_funding_usdt",
    "hold_duration_min", "close_reason",
]

# Fee-aware schema before funding / net-PnL columns (single migration path).
TRADE_CSV_HEADERS_PRE_FUNDING = (
    "timestamp_utc",
    "action",
    "symbol",
    "order_id",
    "quantity",
    "price",
    "notional_usdt",
    "funding_rate_8h",
    "funding_apr_pct",
    "entry_price",
    "exit_price",
    "fee_entry_usdt",
    "fee_exit_usdt",
    "fees_usdt",
    "pnl_gross_usdt",
    "pnl_usdt",
    "pnl_pct",
    "hold_duration_min",
    "close_reason",
)

# Pre–fee-column CSV (single migration path)
LEGACY_TRADE_CSV_HEADERS = (
    "timestamp_utc",
    "action",
    "symbol",
    "order_id",
    "quantity",
    "price",
    "notional_usdt",
    "funding_rate_8h",
    "funding_apr_pct",
    "entry_price",
    "exit_price",
    "pnl_usdt",
    "pnl_pct",
    "hold_duration_min",
    "close_reason",
)

# In-memory store of open trades for PnL on close
# { symbol: { entry_price, quantity, open_time, funding_rate, fee_entry_usdt } }
_open_trades: dict = {}

# Dry run: simulated position store mirrors what Aster would hold
# { symbol: { positionAmt, entryPrice, markPrice } }
_dry_positions: dict = {}
_dry_order_seq: list = [0]  # mutable int for sequence counter

def _dry_order_id(symbol: str) -> str:
    _dry_order_seq[0] += 1
    return f"DRY_{symbol}_{_dry_order_seq[0]}"

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _migrate_legacy_trade_csv_header_if_needed() -> None:
    """Rewrite TRADE_LOG_FILE once if it still uses the pre-fee column header."""
    path = TRADE_LOG_FILE
    try:
        with open(path, newline="", encoding="utf-8") as f:
            r = csv.reader(f)
            header_row = next(r)
    except (StopIteration, OSError):
        return
    norm = tuple((c or "").strip() for c in header_row)
    if norm == tuple(TRADE_CSV_HEADERS):
        return
    if norm == TRADE_CSV_HEADERS_PRE_FUNDING:
        return
    if norm != LEGACY_TRADE_CSV_HEADERS:
        if norm and norm[0] == "timestamp_utc":
            log_warn(
                "  TRADE_LOG_FILE header does not match expected schema; "
                "fee/PnL columns may be misaligned. Fix or rotate the log file."
            )
        return
    rows: list = []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    bak = f"{path}.header_backup"
    try:
        os.replace(path, bak)
    except OSError:
        bak = f"{path}.{int(time.time())}.header_backup"
        os.replace(path, bak)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_CSV_HEADERS)
        w.writeheader()
        for row in rows:
            nr = {}
            for h in TRADE_CSV_HEADERS:
                v = row.get(h, "")
                if v is None:
                    v = ""
                elif isinstance(v, str):
                    v = v.strip()
                nr[h] = v
            act = (row.get("action") or "").strip().upper()
            if act == "CLOSE" and row.get("pnl_usdt"):
                nr["pnl_gross_usdt"] = row["pnl_usdt"]
                nr["pnl_usdt"] = row["pnl_usdt"]
            w.writerow(nr)
    log_warn(
        "  Migrated trade log to fee-aware schema (%d rows). Backup: %s"
        % (len(rows), os.path.basename(bak))
    )


def _migrate_trade_csv_add_funding_columns_if_needed() -> None:
    """Add funding_income_usdt / pnl_net_incl_funding_usdt columns to an existing fee-aware log."""
    path = TRADE_LOG_FILE
    try:
        with open(path, newline="", encoding="utf-8") as f:
            r = csv.reader(f)
            header_row = next(r)
    except (StopIteration, OSError):
        return
    norm = tuple((c or "").strip() for c in header_row)
    if norm == tuple(TRADE_CSV_HEADERS):
        return
    if norm != TRADE_CSV_HEADERS_PRE_FUNDING:
        return
    rows: list = []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    bak = f"{path}.funding_cols_backup"
    try:
        os.replace(path, bak)
    except OSError:
        bak = f"{path}.{int(time.time())}.funding_cols_backup"
        os.replace(path, bak)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_CSV_HEADERS)
        w.writeheader()
        for row in rows:
            nr = {}
            for h in TRADE_CSV_HEADERS:
                v = row.get(h, "")
                if v is None:
                    v = ""
                elif isinstance(v, str):
                    v = v.strip()
                nr[h] = v
            w.writerow(nr)
    log_warn(
        "  Migrated trade log to funding / net-PnL columns (%d rows). Backup: %s"
        % (len(rows), os.path.basename(bak))
    )


def _ensure_csv():
    """Create TRADE_LOG_FILE with headers, or migrate legacy header once."""
    if not os.path.exists(TRADE_LOG_FILE):
        with open(TRADE_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=TRADE_CSV_HEADERS).writeheader()
    else:
        _migrate_legacy_trade_csv_header_if_needed()
        _migrate_trade_csv_add_funding_columns_if_needed()


def _append_csv(row: dict):
    with open(TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_CSV_HEADERS)
        writer.writerow({h: row.get(h, "") for h in TRADE_CSV_HEADERS})

def log_trade_open(
    symbol: str,
    order: dict,
    quantity: float,
    entry_price: float,
    funding_rate: float,
    fee_entry_usdt: float = 0.0,
):
    """Record an opening trade and cache entry data for PnL on close."""
    _ensure_csv()
    notional = quantity * entry_price
    apr      = funding_apr_pct_for_symbol(funding_rate, symbol)
    fee_e = round(fee_entry_usdt, 6) if fee_entry_usdt else ""
    _append_csv({
        "timestamp_utc":   _now_utc(),
        "action":          "OPEN",
        "symbol":          symbol,
        "order_id":        order.get("orderId", ""),
        "quantity":        round(quantity, 6),
        "price":           round(entry_price, 6),
        "notional_usdt":   round(notional, 4),
        "funding_rate_8h": round(funding_rate * 100, 6),
        "funding_apr_pct": round(apr, 2),
        "fee_entry_usdt":  fee_e,
    })
    _open_trades[symbol] = {
        "entry_price":     entry_price,
        "quantity":        quantity,
        "open_time":       time.time(),
        "funding_rate":    funding_rate,
        "fee_entry_usdt":  float(fee_entry_usdt or 0.0),
    }
    fee_s = f"  fee≈${fee_entry_usdt:.4f}" if fee_entry_usdt else ""
    log_success(
        f"  [TRADE LOG] OPEN  {symbol}  qty={quantity:.6f}"
        f"  @ {entry_price:.4f}  funding={format_funding_pct_label(funding_rate, symbol)}{fee_s}"
    )

def log_trade_close(
    symbol: str,
    order: dict,
    quantity: float,
    exit_price: float,
    close_reason: str,
    fee_exit_usdt: float = 0.0,
    entry_price_fallback: float = 0.0,
):
    """
    Record a closing trade. pnl_usdt / pnl_pct are **net of trading fees** (entry + exit).
    pnl_gross_usdt is mark-to-mark price PnL only. pnl_net_incl_funding_usdt adds exchange
    FUNDING_FEE rows (GET /fapi/v1/income) for this symbol during the hold window.
    """
    _ensure_csv()
    entry_data   = _open_trades.pop(symbol, {})
    entry_price  = float(entry_data.get("entry_price") or 0.0)
    if entry_price <= 0 and entry_price_fallback > 0:
        entry_price = float(entry_price_fallback)
    open_time    = float(entry_data.get("open_time", time.time()))
    funding_rate = float(entry_data.get("funding_rate", 0.0))
    fee_entry    = float(entry_data.get("fee_entry_usdt", 0.0))

    notional   = quantity * exit_price
    hold_mins  = round((time.time() - open_time) / 60, 1)
    apr        = funding_apr_pct_for_symbol(funding_rate, symbol)

    fee_ex = float(fee_exit_usdt or 0.0)
    fees_total = fee_entry + fee_ex
    gross = (exit_price - entry_price) * quantity if entry_price else 0.0
    pnl_net = gross - fees_total if entry_price else 0.0
    cost = entry_price * quantity if entry_price else 0.0
    pnl_pct = (pnl_net / cost * 100.0) if cost > 0 else None

    pnl_gross_s = round(gross, 4) if entry_price else ""
    pnl_net_s = round(pnl_net, 4) if entry_price else ""
    pnl_pct_s = round(pnl_pct, 4) if pnl_pct is not None else ""

    funding_usdt = 0.0
    if entry_price:
        start_ms = int(open_time * 1000)
        end_ms = int(time.time() * 1000)
        funding_usdt = sum_funding_fee_income_usdt(symbol, start_ms, end_ms)
    funding_s = round(funding_usdt, 6) if entry_price else ""
    pnl_all = (pnl_net + funding_usdt) if entry_price else 0.0
    pnl_all_s = round(pnl_all, 4) if entry_price else ""

    _append_csv({
        "timestamp_utc":     _now_utc(),
        "action":            "CLOSE",
        "symbol":            symbol,
        "order_id":          order.get("orderId", ""),
        "quantity":          round(quantity, 6),
        "price":             round(exit_price, 6),
        "notional_usdt":     round(notional, 4),
        "funding_rate_8h":   round(funding_rate * 100, 6),
        "funding_apr_pct":   round(apr, 2),
        "entry_price":       round(entry_price, 6) if entry_price else "",
        "exit_price":        round(exit_price, 6),
        "fee_entry_usdt":    round(fee_entry, 6) if fee_entry else "",
        "fee_exit_usdt":     round(fee_ex, 6) if fee_ex else "",
        "fees_usdt":         round(fees_total, 6) if fees_total else "",
        "pnl_gross_usdt":    pnl_gross_s,
        "pnl_usdt":          pnl_net_s,
        "pnl_pct":           pnl_pct_s,
        "funding_income_usdt": funding_s,
        "pnl_net_incl_funding_usdt": pnl_all_s,
        "hold_duration_min": hold_mins,
        "close_reason":      close_reason,
    })
    if entry_price:
        log_success(
            f"  [TRADE LOG] CLOSE {symbol}  qty={quantity:.6f}"
            f"  @ {exit_price:.4f}  pnl_net=${pnl_net_s} ({pnl_pct_s}%)"
            f"  gross=${pnl_gross_s}  fees=${round(fees_total, 6)}"
            f"  funding=${funding_s}  pnl+funding=${pnl_all_s}"
            f"  held={hold_mins}m  reason={close_reason}"
        )
    else:
        log_success(
            f"  [TRADE LOG] CLOSE {symbol}  qty={quantity:.6f}"
            f"  @ {exit_price:.4f}  pnl=n/a (no entry cache)"
            f"  held={hold_mins}m  reason={close_reason}"
        )

# --- Account Setup ------------------------------------------------------------

def enable_multi_asset_mode():
    """
    Switch to Multi-Asset Mode so ASTER and USDF both count as margin.
    Required to use non-USDT collateral. Safe to call repeatedly.
    """
    if DRY_RUN:
        log_warn("  [DRY RUN] Would enable Multi-Asset Mode (skipping API call)")
        return
    try:
        result = ex.signed_get("/fapi/v1/multiAssetsMargin", {})
        if result.get("multiAssetsMargin") is True:
            log_success("  [OK] Multi-Asset Mode already active")
            return
    except Exception:
        pass

    try:
        ex.signed_post("/fapi/v1/multiAssetsMargin", {"multiAssetsMargin": "true"})
        log_success("  [OK] Multi-Asset Mode ENABLED -- ASTER + USDF both count as margin")
    except RuntimeError as e:
        if "No need" in str(e):
            log_success("  [OK] Multi-Asset Mode already active")
        else:
            log_warn(f"  [!!] Could not enable Multi-Asset Mode: {e}")
            log_warn("       Deposit ASTER/USDF to your Aster account on BNB Chain first")


def _estimate_asset_usd(
    asset: str, amount: float, aster_price: float, mark_cache: dict
) -> float:
    """Rough USD notional for dust filtering (stables ~1, ASTER uses mark, else {ASSET}USDT mark)."""
    if amount <= 0:
        return 0.0
    a = asset.upper()
    if a in ("USDT", "USDF", "BUSD", "DAI", "FDUSD"):
        return amount
    if a == "ASTER":
        return amount * aster_price if aster_price > 0 else 0.0
    sym = f"{a}USDT"
    if sym not in mark_cache:
        try:
            d = get("/fapi/v1/premiumIndex", {"symbol": sym}, signed=False)
            mark_cache[sym] = float(d.get("markPrice", 0) or 0)
        except Exception:
            mark_cache[sym] = 0.0
    m = mark_cache[sym]
    return amount * m if m > 0 else 0.0


def _fetch_spot_balances_non_dust(aster_price: float, mark_cache: dict) -> list:
    """Spot balances from GET /api/v3/account; drops dust vs BALANCE_DUST_USD."""
    try:
        acct = get("/api/v3/account", signed=True, base_url=SAPI_BASE)
    except Exception as e:
        log_warn(f"  Spot balances unavailable: {e}")
        return []
    balances = acct.get("balances") if isinstance(acct, dict) else None
    if not isinstance(balances, list):
        return []
    out = []
    for b in balances:
        asset = b.get("asset", "")
        free = float(b.get("free", 0) or 0)
        locked = float(b.get("locked", 0) or 0)
        tot = free + locked
        if tot <= 0:
            continue
        usd = _estimate_asset_usd(asset, tot, aster_price, mark_cache)
        if BALANCE_DUST_USD > 0 and usd < BALANCE_DUST_USD:
            continue
        out.append(
            {
                "asset": asset,
                "free": free,
                "locked": locked,
                "total": tot,
                "est_usd": usd,
            }
        )
    out.sort(key=lambda x: x["est_usd"], reverse=True)
    return out


def _futures_balance_margin_qty(b: dict) -> Tuple[float, float]:
    """
    (wallet_balance, qty_for_margin).

    Prefer availableBalance when > 0 so sizing matches orderable collateral
    (see exchange._balance_portfolio_all_wallet_usd).
    """
    wallet = float(b.get("balance", 0) or 0)
    avail = float(b.get("availableBalance", 0) or 0)
    qty = avail if avail > 1e-12 else wallet
    return wallet, qty


def _effective_usdt_for_margin_asset(
    asset: str,
    qty: float,
    aster_price: float,
) -> float:
    """USDT-equivalent effective margin for ASTER / USDF / USDT (qty = margin-qty)."""
    if qty <= 0:
        return 0.0
    a = (asset or "").upper()
    if a == "ASTER":
        if aster_price <= 0:
            return 0.0
        return qty * aster_price * ASTER_COLLATERAL_RATIO
    if a == "USDF":
        return qty * USDF_COLLATERAL_RATIO
    if a == "USDT":
        return qty
    return 0.0


def _parse_account_float(val) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    return x


def get_collateral_summary() -> dict:
    """
    Fetch balances and compute effective margin for ASTER, USDF, and USDT.

    Always sets ``_live_effective_margin`` from the API sum. In dry run with
    ``DRY_RUN_SIMULATED_MARGIN_USD > 0``, ``_total_effective_margin`` is overridden
    for sizing while ``_live_effective_margin`` stays the real wallet read.

    When ``GET /fapi/v2/account`` ``availableBalance`` (USDT) is below the computed
    effective margin, sizing uses the lower cap. IM/MM from the same response are
    stored for logs and dashboard.
    """
    aster_price = get_aster_price()
    mark_cache: dict = {}
    try:
        data = get("/fapi/v2/balance", signed=True)
    except requests.HTTPError as e:
        body = (e.response.text or "").strip() if e.response is not None else ""
        extra = f" | {body[:800]}" if body else ""
        log_error(f"  Could not fetch balance: {e}{extra}")
        return {}
    except Exception as e:
        log_error(f"  Could not fetch balance: {e}")
        return {}

    summary: dict = {}
    for b in data:
        asset = b.get("asset", "")
        if asset not in ("ASTER", "USDF", "USDT"):
            continue
        wallet, qty = _futures_balance_margin_qty(b)
        if wallet <= 0:
            continue
        eff = _effective_usdt_for_margin_asset(asset, qty, aster_price)
        summary[asset] = {"balance": wallet, "effective_usdt": eff}

    computed_live = sum(
        v["effective_usdt"] for k, v in summary.items() if not k.startswith("_")
    )
    summary["_live_effective_margin"] = computed_live
    summary["_total_effective_margin"] = computed_live
    summary["_margin_capped_by_account"] = False

    try:
        acct = ex.signed_get("/fapi/v2/account", {})
    except Exception as e:
        log_warn(f"  Futures account (margin cap / IM): {e}")
        acct = None
    if isinstance(acct, dict):
        cap = _parse_account_float(acct.get("availableBalance"))
        allow_cap = not (DRY_RUN and DRY_RUN_SIMULATED_MARGIN_USD > 0)
        if (
            allow_cap
            and cap is not None
            and cap > 1e-9
            and cap + 1e-6 < computed_live
        ):
            log_info(
                f"  Effective margin capped by account availableBalance "
                f"${cap:,.2f} < computed ${computed_live:,.2f} — sizing uses cap"
            )
            summary["_total_effective_margin"] = float(cap)
            summary["_margin_capped_by_account"] = True
        summary["_account_available_balance_usdt"] = cap
        tim = _parse_account_float(acct.get("totalInitialMargin"))
        tmm = _parse_account_float(acct.get("totalMaintMargin"))
        if tim is not None:
            summary["_total_initial_margin_usdt"] = tim
        if tmm is not None:
            summary["_total_maint_margin_usdt"] = tmm

    if DRY_RUN and DRY_RUN_SIMULATED_MARGIN_USD > 0:
        summary["_total_effective_margin"] = float(DRY_RUN_SIMULATED_MARGIN_USD)
        summary["_dry_run_simulated_margin"] = True

    if not live_wallet_logs_enabled():
        summary["_futures_detail"] = []
        summary["_spot_detail"] = []
        return summary

    fut_detail = []
    for b in data:
        asset = b.get("asset", "")
        wallet, qty = _futures_balance_margin_qty(b)
        if wallet <= 0:
            continue
        usd = _estimate_asset_usd(asset, qty, aster_price, mark_cache)
        if BALANCE_DUST_USD > 0 and usd < BALANCE_DUST_USD:
            continue
        row = {"asset": asset, "balance": wallet, "est_usd": usd}
        if asset in ("ASTER", "USDF", "USDT"):
            row["eff_margin"] = _effective_usdt_for_margin_asset(asset, qty, aster_price)
        fut_detail.append(row)
    fut_detail.sort(key=lambda x: x["est_usd"], reverse=True)
    if summary.get("_dry_run_simulated_margin"):
        paper_usd = float(DRY_RUN_SIMULATED_MARGIN_USD)
        fut_detail.insert(
            0,
            {
                "asset": "PAPER",
                "balance": paper_usd,
                "est_usd": paper_usd,
                "eff_margin": paper_usd,
            },
        )
    summary["_futures_detail"] = fut_detail
    summary["_spot_detail"] = _fetch_spot_balances_non_dust(aster_price, mark_cache)
    return summary


def get_aster_price() -> float:
    """Current ASTER mark price in USDT."""
    try:
        data = get("/fapi/v1/premiumIndex", {"symbol": "ASTERUSDT"})
        return float(data.get("markPrice", 0))
    except Exception:
        return 0.0


# --- Funding interval + APR (per-symbol; Aster N may differ from 8h) ---------

_next_funding_snap_ms: dict[str, int] = {}
_funding_interval_ms_by_sym: dict[str, int] = {}
_funding_sign_warned: set[str] = set()


def _observe_next_funding_time(symbol: str, next_ms: int) -> None:
    """Infer funding period when ``nextFundingTime`` advances between REST polls."""
    try:
        n = int(next_ms)
    except (TypeError, ValueError):
        return
    prev = _next_funding_snap_ms.get(symbol)
    if prev is not None and n != prev:
        delta = n - prev
        # Typical perp funding: 1h–48h between settlements
        if 3_600_000 <= delta <= 48 * 3_600_000:
            _funding_interval_ms_by_sym[symbol] = int(delta)
    _next_funding_snap_ms[symbol] = n


def fundings_per_day(symbol: str) -> float:
    """Funding events per calendar day; default 3 (8h) until interval is learned."""
    ms = _funding_interval_ms_by_sym.get(symbol)
    if ms and 3_600_000 <= ms <= 48 * 3_600_000:
        return 86_400_000.0 / float(ms)
    return 3.0


def funding_period_hours(symbol: str) -> float:
    fpd = fundings_per_day(symbol)
    return 24.0 / fpd if fpd > 0 else 8.0


def funding_apr_pct_for_symbol(rate: float, symbol: str) -> float:
    """Simple APR% = rate per funding interval × fundings/day × 365 × 100."""
    return float(rate) * fundings_per_day(symbol) * 365.0 * 100.0


def format_funding_pct_label(rate: float, symbol: str) -> str:
    """Human label: % per funding period (shows ~8h or ~4h etc. once learned)."""
    ph = funding_period_hours(symbol)
    if abs(ph - 8.0) < 0.2:
        return f"{rate * 100:+.4f}%/8h"
    if abs(ph - round(ph)) < 0.08:
        return f"{rate * 100:+.4f}%/{int(round(ph))}h"
    return f"{rate * 100:+.4f}%/~{ph:.1f}h"


def _funding_fee_sum_by_symbol_window(hours: int) -> dict[str, float]:
    """Sum signed FUNDING_FEE income (USDT terms) per symbol over the last ``hours``."""
    if DRY_RUN:
        return {}
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(hours) * 3600 * 1000
    rows = fetch_income_window("FUNDING_FEE", start_ms, end_ms, symbol=None)
    by_sym: dict[str, float] = {}
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        by_sym[sym] = by_sym.get(sym, 0.0) + _income_row_usdt_value(r)
    return by_sym


def maybe_log_funding_sign_selfcheck(
    loop_i: int,
    open_symbols: AbstractSet[str],
    rates: list,
) -> None:
    """
    Live-only: compare recent FUNDING_FEE income sign vs lastFundingRate for open longs.

    Per Aster docs, positive rate => longs pay shorts, so a long's FUNDING_FEE income
    rows are often negative when the published rate is positive (you paid funding).
    Mismatches are logged once per symbol (throttled) so API sign vs wallet can be verified.
    """
    if DRY_RUN or not open_symbols or FUNDING_SIGN_SELF_CHECK_CYCLES <= 0:
        return
    if loop_i % FUNDING_SIGN_SELF_CHECK_CYCLES != 0:
        return
    try:
        by_inc = _funding_fee_sum_by_symbol_window(72)
    except Exception as e:
        log_warn(f"  Funding sign self-check: income fetch failed: {e}")
        return
    rate_by = {r["symbol"]: float(r.get("fundingRate") or 0) for r in rates}
    for sym in open_symbols:
        inc = float(by_inc.get(sym, 0.0))
        api = float(rate_by.get(sym, 0.0))
        if abs(inc) < 1e-4 or abs(api) < 1e-12:
            continue
        # Doc-consistent long: (api>0 -> paid funding -> income<0) or inverse
        doc_aligned = (api > 0 and inc < 0) or (api < 0 and inc > 0)
        if doc_aligned:
            continue
        if sym in _funding_sign_warned:
            continue
        _funding_sign_warned.add(sym)
        log_warn(
            f"  [funding sign] {sym}: lastFundingRate={api:+.6g} but sum(FUNDING_FEE "
            f"72h)={inc:+.4f} USDT — same sign for both. Per Aster docs, positive rate "
            f"means longs pay shorts (longs often see negative FUNDING_FEE income). "
            f"Verify convention vs your wallet; adjust MIN/EXIT thresholds if needed."
        )


# --- Market Data --------------------------------------------------------------

def get_all_funding_rates() -> list:
    """All perps sorted by funding rate descending."""
    data = get("/fapi/v1/premiumIndex")
    results = []
    for item in data:
        try:
            sym = item["symbol"]
            next_ms = int(item["nextFundingTime"])
            _observe_next_funding_time(sym, next_ms)
            fpd = fundings_per_day(sym)
            results.append({
                "symbol":          sym,
                "fundingRate":     float(item["lastFundingRate"]),
                "nextFundingTime": next_ms,
                "markPrice":       float(item["markPrice"]),
                "fundingsPerDay":  fpd,
            })
        except (KeyError, ValueError):
            continue
    return sorted(results, key=lambda x: x["fundingRate"], reverse=True)


def get_24h_quote_volumes() -> dict:
    """symbol -> USDT quote volume (last 24h). Empty dict on failure."""
    try:
        data = get("/fapi/v1/ticker/24hr", signed=False)
    except Exception as e:
        log_warn(f"  Could not fetch 24h tickers: {e}")
        return {}
    out: dict = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol")
        if not sym:
            continue
        try:
            out[sym] = float(row.get("quoteVolume", 0) or 0)
        except (TypeError, ValueError):
            continue
    return out


def get_exchange_info() -> dict:
    """Symbol trading rules keyed by symbol."""
    data = get("/fapi/v1/exchangeInfo")
    info = {}
    for s in data.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        filters = {f["filterType"]: f for f in s.get("filters", [])}
        info[s["symbol"]] = {
            "stepSize": filters.get("LOT_SIZE", {}).get("stepSize", "0.001"),
            "minQty":   filters.get("LOT_SIZE", {}).get("minQty", "0.001"),
        }
    return info

def get_mark_price(symbol: str) -> float:
    return float(get("/fapi/v1/premiumIndex", {"symbol": symbol})["markPrice"])


def _income_row_usdt_value(row: dict) -> float:
    """Signed cashflow in USDT terms (income × mark for non-USDT assets)."""
    try:
        amt = float(row.get("income") or 0)
    except (TypeError, ValueError):
        return 0.0
    if amt == 0:
        return 0.0
    asset = (row.get("asset") or "USDT").upper()
    if asset == "USDT":
        return amt
    if asset == "BNB":
        try:
            return amt * float(get_mark_price("BNBUSDT"))
        except Exception:
            log_warn("  income: could not convert BNB to USDT")
            return amt
    pair = f"{asset}USDT"
    try:
        return amt * float(get_mark_price(pair))
    except Exception:
        log_warn(f"  income: unknown asset {asset}; using raw amount as USDT proxy")
        return amt


def fetch_income_window(
    income_type: str,
    start_time_ms: int,
    end_time_ms: int,
    symbol: Optional[str] = None,
) -> list:
    """Paginated GET /fapi/v1/income; rows with time in [start_time_ms, end_time_ms]."""
    collected: list = []
    start = int(start_time_ms)
    end = int(end_time_ms)
    if start > end:
        return collected
    cur = start
    for _ in range(400):
        params: dict = {
            "incomeType": income_type,
            "startTime": cur,
            "endTime": end,
            "limit": 1000,
        }
        if symbol:
            params["symbol"] = symbol
        try:
            chunk = ex.signed_get("/fapi/v1/income", params)
        except Exception as e:
            log_warn(f"  GET /fapi/v1/income ({income_type}): {e}")
            break
        if not isinstance(chunk, list) or not chunk:
            break
        times_in: list = []
        for r in chunk:
            try:
                t = int(r.get("time") or 0)
            except (TypeError, ValueError):
                continue
            if t < start or t > end:
                continue
            collected.append(r)
            times_in.append(t)
        if not times_in:
            break
        mx = max(times_in)
        if mx >= end or len(chunk) < 1000:
            break
        cur = mx + 1
    return collected


def sum_funding_fee_income_usdt(
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
) -> float:
    """Sum FUNDING_FEE income for symbol in [start, end] ms; dry run → 0."""
    if DRY_RUN:
        return 0.0
    rows = fetch_income_window("FUNDING_FEE", start_time_ms, end_time_ms, symbol=symbol)
    return round(sum(_income_row_usdt_value(r) for r in rows), 6)


def sum_funding_fee_income_all_symbols_usdt(start_time_ms: int, end_time_ms: int) -> float:
    """Sum FUNDING_FEE across all symbols in window; dry run → 0."""
    if DRY_RUN:
        return 0.0
    rows = fetch_income_window("FUNDING_FEE", start_time_ms, end_time_ms, symbol=None)
    return round(sum(_income_row_usdt_value(r) for r in rows), 6)


def get_book_ticker(symbol: str) -> dict:
    """Best bid/ask (top of book). Public endpoint."""
    return get("/fapi/v1/ticker/bookTicker", {"symbol": symbol}, signed=False)


def _log_book_prices(symbol: str, mark: Optional[float]) -> None:
    try:
        t = get_book_ticker(symbol)
        bid = float(t["bidPrice"])
        ask = float(t["askPrice"])
        mid = (bid + ask) / 2.0
        m = f"  mark={mark:.6f}" if mark is not None and mark > 0 else ""
        log_info(f"       {symbol}  bid={bid:.6f}  ask={ask:.6f}  mid={mid:.6f}{m}")
    except Exception as e:
        log_warn(f"       {symbol}  book unavailable: {e}")

# --- Account ------------------------------------------------------------------

def get_positions() -> list:
    data = ex.signed_get("/fapi/v2/positionRisk", {})
    return [p for p in data if float(p.get("positionAmt", 0)) != 0]

def set_leverage(symbol: str, leverage: int):
    if DRY_RUN:
        log_info(f"  Leverage -> {leverage}x  [{symbol}]  [PAPER — no API]")
        return
    ex.signed_post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
    log_info(f"  Leverage -> {leverage}x  [{symbol}]")

def set_cross_margin(symbol: str):
    if DRY_RUN:
        log_info(f"  Margin -> CROSS  [{symbol}]  [PAPER — no API]")
        return
    try:
        ex.signed_post("/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSSED"})
        log_info(f"  Margin -> CROSS  [{symbol}]")
    except requests.HTTPError as e:
        resp = e.response
        if resp is not None:
            try:
                j = resp.json()
                if isinstance(j, dict) and int(str(j.get("code", 0))) == -4046:
                    log_info(f"  Margin -> CROSS  [{symbol}]  (already CROSSED)")
                    return
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        raise
    except RuntimeError as e:
        if "No need" not in str(e):
            raise

# --- Orders -------------------------------------------------------------------

def round_step(value: float, step: str) -> str:
    step_d = Decimal(step)
    return str(Decimal(str(value)).quantize(step_d, rounding=ROUND_DOWN))


def perp_qty_meets_min_notional(notional_usdt: float, mark: float, step: str) -> str:
    """Floor qty on step can make notional < target; exchange rejects (-4164). Uses Decimal for steps."""
    step_d = Decimal(step)
    m = Decimal(str(mark))
    need = Decimal(str(notional_usdt))
    q = ((need / m) / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d
    while q * m < need:
        q += step_d
    return str(q.quantize(step_d))


def _wait_order_fill(symbol: str, order_id, initial: dict) -> dict:
    """Poll GET /fapi/v1/order until avg fill is known (market orders can lag)."""
    merged = dict(initial)
    oid = merged.get("orderId", order_id)
    for attempt in range(12):
        avg = float(str(merged.get("avgPrice") or "0").replace(",", ""))
        exq = float(str(merged.get("executedQty") or "0").replace(",", ""))
        if avg > 0 and exq > 0:
            return merged
        time.sleep(0.1 * (attempt + 1))
        try:
            merged = get(
                "/fapi/v1/order",
                {"symbol": symbol, "orderId": oid},
                signed=True,
            )
        except Exception as e:
            log_warn(f"  Order fill poll {attempt + 1}: {e}")
            break
    return merged


def _user_trades_commission_usdt(symbol: str, order_id) -> float:
    """Sum |commission| in USDT for all fills of this order (GET /fapi/v1/userTrades)."""
    try:
        rows = ex.signed_get("/fapi/v1/userTrades", {"symbol": symbol, "orderId": order_id})
    except Exception as e:
        log_warn(f"  userTrades (fees) unavailable: {e}")
        return 0.0
    if not isinstance(rows, list):
        return 0.0
    total = 0.0
    for t in rows:
        asset = (t.get("commissionAsset") or "USDT").upper()
        comm = abs(float(t.get("commission") or 0))
        if comm <= 0:
            continue
        if asset == "USDT":
            total += comm
        elif asset == "BNB":
            try:
                total += comm * float(get_mark_price("BNBUSDT"))
            except Exception:
                log_warn("  Could not convert BNB commission to USDT")
        else:
            pair = f"{asset}USDT"
            try:
                total += comm * float(get_mark_price(pair))
            except Exception:
                log_warn(f"  Unknown commission asset {asset}; fee omitted from sum")
    return round(total, 6)


def resolve_live_fill_and_fees(symbol: str, order: dict) -> Tuple[float, float, float]:
    """
    After a live futures order, return (avg_price, executed_qty, fees_usdt).
    Falls back to zeros if the API does not return fills yet.
    """
    oid = order.get("orderId")
    if oid is None:
        return 0.0, 0.0, 0.0
    merged = _wait_order_fill(symbol, oid, order)
    avg = float(str(merged.get("avgPrice") or "0").replace(",", ""))
    exq = float(str(merged.get("executedQty") or "0").replace(",", ""))
    cq = float(str(merged.get("cumQuote") or "0").replace(",", ""))
    if avg <= 0 and exq > 0 and cq > 0:
        avg = cq / exq
    fees = _user_trades_commission_usdt(symbol, oid)
    return avg, exq, fees


def open_long(symbol: str, notional_usdt: float, exchange_info: dict,
              funding_rate: float = 0.0) -> dict:
    mark    = get_mark_price(symbol)
    step    = exchange_info.get(symbol, {}).get("stepSize", "0.001")
    qty_str = perp_qty_meets_min_notional(notional_usdt, mark, step)
    qty     = float(qty_str)

    if DRY_RUN:
        order_id = _dry_order_id(symbol)
        log_info(
            f"  Opening LONG {symbol}  qty={qty_str}  "
            f"~${notional_usdt:.0f} notional  [PAPER]"
        )
        order = {"orderId": order_id, "status": "DRY_RUN"}
        _dry_positions[symbol] = {
            "positionAmt": qty_str,
            "entryPrice":  str(mark),
            "markPrice":   str(mark),
        }
        log_success(
            f"  [OK] [PAPER] Opened orderId={order.get('orderId')}  "
            f"@ {mark:.4f}  status={order.get('status')}"
        )
        log_trade_open(symbol, order, qty, mark, funding_rate, 0.0)
        return order

    log_info(f"  Opening LONG {symbol}  qty={qty_str}  ~${notional_usdt:.0f} notional")
    order = ex.place_market_order_raw(symbol=symbol, side="BUY", quantity=qty_str)
    log_success(f"  [OK] Opened orderId={order.get('orderId')} status={order.get('status')}")
    avg_px, ex_qty, fee_open = resolve_live_fill_and_fees(symbol, order)
    qty_fill = ex_qty if ex_qty > 0 else qty
    entry_px = avg_px if avg_px > 0 else mark
    log_trade_open(symbol, order, qty_fill, entry_px, funding_rate, fee_open)
    return order

def close_long(symbol: str, exchange_info: dict, close_reason: str = "manual"):
    # In dry run, read from simulated position store instead of live API
    if DRY_RUN:
        pos = _dry_positions.pop(symbol, None)
        if not pos:
            log_warn(f"  No simulated position for {symbol} -- skipping  [PAPER]")
            return
        exit_price = get_mark_price(symbol)
        qty        = float(pos["positionAmt"])
        order_id   = _dry_order_id(symbol)
        log_info(
            f"  Closing LONG {symbol}  qty={qty}  reason={close_reason}  [PAPER]"
        )
        order = {"orderId": order_id, "status": "DRY_RUN"}
        log_success(
            f"  [OK] [PAPER] Closed orderId={order_id}  @ {exit_price:.4f}  "
            f"status={order.get('status')}"
        )
        log_trade_close(symbol, order, qty, exit_price, close_reason)
        return

    positions = get_positions()
    pos = next((p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) > 0), None)
    if not pos:
        log_warn(f"  No open long for {symbol} -- skipping")
        return

    step     = exchange_info.get(symbol, {}).get("stepSize", "0.001")
    qty_str  = round_step(abs(float(pos["positionAmt"])), step)
    qty      = float(qty_str)
    mark_fallback = float(pos.get("markPrice", get_mark_price(symbol)))
    entry_from_pos = float(pos.get("entryPrice", 0) or 0)

    # Verified flatten (poll + chase remainder)
    log_info(f"  Closing LONG {symbol}  qty={qty_str}  reason={close_reason}")
    ok = ex.flatten_position_for_symbol(symbol, reason=close_reason)
    order = {
        "orderId": f"FLATTEN_{symbol}_{int(time.time())}",
        "status": "VERIFIED" if ok else "UNKNOWN",
    }
    if ok:
        log_success(f"  [OK] Closed (verified flat)  [{symbol}]")
    else:
        log_warn(f"  [!!] Close verification failed  [{symbol}] (see logs)")
    exit_avg, exit_qty, fee_close = resolve_live_fill_and_fees(symbol, order)
    qty_close = exit_qty if exit_qty > 0 else qty
    exit_px = exit_avg if exit_avg > 0 else mark_fallback
    log_trade_close(
        symbol,
        order,
        qty_close,
        exit_px,
        close_reason,
        fee_exit_usdt=fee_close,
        entry_price_fallback=entry_from_pos,
    )

# --- Risk ---------------------------------------------------------------------

def stop_loss_entries() -> dict:
    """symbol -> entry price for open longs (live or dry-run) for WS + stop-loss checks."""
    if DRY_RUN:
        return {
            sym: float(p["entryPrice"])
            for sym, p in _dry_positions.items()
            if float(p.get("positionAmt", 0) or 0) > 0
        }
    return {
        p["symbol"]: float(p["entryPrice"])
        for p in get_positions()
        if float(p.get("positionAmt", 0) or 0) > 0
        and float(p.get("entryPrice", 0) or 0) > 0
    }


def has_risk_exposure(open_symbols: set) -> bool:
    """
    True if we hold any long perp exposure — use fast RISK_POLL_INTERVAL_SEC between cycles.

    Covers DRY_RUN simulated positions, bot-tracked symbols, and exchange positions
    (e.g. after restart) so stop loss / funding checks run often enough under leverage.
    """
    if DRY_RUN:
        return bool(_dry_positions)
    if open_symbols:
        return True
    for p in get_positions():
        if float(p.get("positionAmt", 0)) > 0:
            return True
    return False


def check_stop_loss(positions: list) -> list:
    # In dry run, supplement with simulated positions (live API returns nothing)
    if DRY_RUN:
        dry_pos_list = [
            {"symbol": sym, "entryPrice": p["entryPrice"],
             "markPrice": get_mark_price(sym), "positionAmt": p["positionAmt"]}
            for sym, p in _dry_positions.items()
        ]
        positions = dry_pos_list

    to_close = []
    for p in positions:
        entry = float(p.get("entryPrice", 0))
        mark  = float(p.get("markPrice", 0))
        amt   = float(p.get("positionAmt", 0))
        if entry == 0 or amt <= 0:
            continue
        pnl_pct = (mark - entry) / entry
        if pnl_pct <= -STOP_LOSS_PCT:
            log_warn(f"  Stop loss hit: {p['symbol']}  pnl={pnl_pct*100:.2f}%")
            to_close.append(p["symbol"])
    return to_close


def check_take_profit(positions: list) -> list:
    """Close longs when unrealized PnL vs entry >= TAKE_PROFIT_PCT (0 = off)."""
    if TAKE_PROFIT_PCT <= 0:
        return []
    if DRY_RUN:
        dry_pos_list = [
            {"symbol": sym, "entryPrice": p["entryPrice"],
             "markPrice": get_mark_price(sym), "positionAmt": p["positionAmt"]}
            for sym, p in _dry_positions.items()
        ]
        positions = dry_pos_list

    to_close = []
    for p in positions:
        entry = float(p.get("entryPrice", 0))
        mark = float(p.get("markPrice", 0))
        amt = float(p.get("positionAmt", 0))
        if entry == 0 or amt <= 0:
            continue
        pnl_pct = (mark - entry) / entry
        if pnl_pct >= TAKE_PROFIT_PCT:
            log_warn(
                f"  Take profit hit: {p['symbol']}  pnl={pnl_pct*100:.2f}% "
                f"(>= {TAKE_PROFIT_PCT*100:.2f}%)"
            )
            to_close.append(p["symbol"])
    return to_close


# --- Diversification helpers --------------------------------------------------

def rank_weighted_sizes(candidates: list, total_budget: float) -> list:
    """
    Assign notional size to each candidate using rank-weighted allocation.

    Rank 1 gets RANK_TOP_PCT of total_budget.
    Remaining budget is split equally among ranks 2..N.
    Each size is capped at MAX_SINGLE_PCT * total_budget.

    With a single candidate, all deploy budget goes to that symbol (no 25% slice).

    Returns candidates with a 'notional' key added.
    """
    n = len(candidates)
    if n == 0:
        return candidates

    cap = total_budget * MAX_SINGLE_PCT

    if n == 1:
        # One name: deploy full budget (MAX_SINGLE_PCT only limits when splitting across names).
        return [{**candidates[0], "notional": total_budget}]

    top_alloc  = total_budget * RANK_TOP_PCT
    rest_alloc = (total_budget - top_alloc) / (n - 1)
    sized = []
    for i, c in enumerate(candidates):
        raw = top_alloc if i == 0 else rest_alloc
        sized.append({**c, "notional": min(raw, cap)})
    return sized

def is_correlated(symbol: str, open_symbols: set) -> bool:
    """
    Return True if opening this symbol would violate a correlation group rule.
    E.g. if BTCUSDT is already open and WBTCUSDT is in the same group, block it.
    """
    for group in CORR_GROUPS:
        if symbol in group:
            for other in group:
                if other != symbol and other in open_symbols:
                    return True
    return False


def fee_breakeven_funding_intervals(funding_rate: float) -> float:
    """
    Approximate number of funding settlements at |rate| needed to pay round-trip
    taker fees once (open + close), ignoring sign of carry vs long/short economics.

    Uses abs(rate) so the gate is a magnitude floor vs fees; validate real carry with
    FUNDING_FEE income and MIN_FUNDING_RATE.
    """
    fr = abs(float(funding_rate))
    if fr < 1e-18:
        return float("inf")
    rt_fee_frac = 2.0 * ESTIMATED_TAKER_FEE_BPS / 10000.0
    return rt_fee_frac / fr


def funding_passes_fee_breakeven(funding_rate: float) -> bool:
    """True if fee breakeven gate is off, or |rate| is large enough vs ESTIMATED_TAKER_FEE_BPS."""
    if MAX_FEE_BREAKEVEN_FUNDING_INTERVALS <= 0:
        return True
    return fee_breakeven_funding_intervals(funding_rate) <= MAX_FEE_BREAKEVEN_FUNDING_INTERVALS


def log_aster_points_margin_advisory(collateral: dict) -> None:
    """
    Stage 6–style margin mix reminder + KPI pointer (runs once after wallet summary).
    """
    if not collateral:
        return
    aster = collateral.get("ASTER") or {}
    usdf = collateral.get("USDF") or {}
    a_eff = float(aster.get("effective_usdt") or 0)
    u_eff = float(usdf.get("effective_usdt") or 0)
    ok_a = a_eff >= 1.0
    ok_u = u_eff >= 1.0
    if ok_a and ok_u:
        log_success(
            "  [Stage6 margin] USDF + ASTER both in futures wallet — strong Aster Asset Points setup; "
            "profit KPI: sum CLOSE `pnl_net_incl_funding_usdt` (see `python profit_assistant.py kpi`)"
        )
    else:
        hints: List[str] = []
        if not ok_u:
            hints.append("add USDF to futures wallet for 99.99% collateral + asset points")
        if not ok_a:
            hints.append("add ASTER to futures wallet for 80% hair asset points")
        log_info(
            "  [Stage6 margin] " + "; ".join(hints).capitalize()
            + ". Live: verify long-side funding with FUNDING_SIGN_SELF_CHECK_CYCLES vs "
            "GET /fapi/v1/income FUNDING_FEE rows."
        )


def is_pool_symbol_eligible(
    r: dict,
    exchange_info: dict,
    volumes_24h: dict,
    volume_filter_active: bool,
) -> bool:
    """
    Min funding + tradable + blacklist; optional SYMBOL_ALLOWLIST + MIN_QUOTE_VOLUME_24H.
    Does not check open positions or correlation (those apply when actually opening).
    """
    sym = r["symbol"]
    try:
        fr = float(r.get("fundingRate", 0))
    except (TypeError, ValueError):
        return False
    if fr < MIN_FUNDING_RATE:
        return False
    if sym in BLACKLIST or sym not in exchange_info:
        return False
    if SYMBOL_ALLOWLIST is not None and sym.upper() not in SYMBOL_ALLOWLIST:
        return False
    if volume_filter_active:
        qv = volumes_24h.get(sym, 0.0)
        if qv < MIN_QUOTE_VOLUME_24H:
            return False
    return True


def pool_eligibility_rules_label() -> str:
    """Active pool rules for log lines (MIN_FUNDING always; optional allowlist + volume)."""
    parts = ["MIN_FUNDING_RATE"]
    if SYMBOL_ALLOWLIST is not None:
        parts.append("SYMBOL_ALLOWLIST")
    if MIN_QUOTE_VOLUME_24H > 0:
        parts.append("MIN_QUOTE_VOLUME_24H")
    return " + ".join(parts)


def order_rates_with_symbol_boost(
    rates: list,
    boosted: AbstractSet[str],
    min_funding: float,
) -> list:
    """
    Symbols in ``boosted`` with funding >= ``min_funding`` first (descending by rate),
    then all other rows (descending by rate). Used when X posts mention specific perps.
    """
    if not boosted:
        return list(rates)
    bset = set(boosted)
    boosted_rows: list = []
    rest: list = []
    for r in rates:
        sym = r["symbol"]
        try:
            fr = float(r.get("fundingRate", 0))
        except (TypeError, ValueError):
            fr = 0.0
        if sym in bset and fr >= min_funding:
            boosted_rows.append(r)
        else:
            rest.append(r)
    boosted_rows.sort(
        key=lambda x: float(x.get("fundingRate", 0) or 0), reverse=True
    )
    rest.sort(key=lambda x: float(x.get("fundingRate", 0) or 0), reverse=True)
    return boosted_rows + rest


def build_stake_context(
    rates: list, open_symbols: set, position_sizes: dict
) -> Tuple[dict, set]:
    """
    Mark notional (stake) per open symbol; exchange_symbols = symbols with a real position on Aster.
    Simulated-only dry-run legs get stake from _dry_positions and are not in exchange_symbols.
    """
    stake_map: dict = {}
    exchange_symbols: set = set()
    for p in get_positions():
        amt = float(p.get("positionAmt", 0))
        if amt <= 0:
            continue
        sym_p = p["symbol"]
        exchange_symbols.add(sym_p)
        mk = float(p.get("markPrice", 0) or 0)
        if mk <= 0:
            mk = next(
                (float(x["markPrice"]) for x in rates if x["symbol"] == sym_p),
                0.0,
            )
        stake_map[sym_p] = abs(amt) * mk
    if DRY_RUN:
        for sym_p, dp in _dry_positions.items():
            if sym_p in stake_map:
                continue
            qty = float(dp.get("positionAmt", 0) or 0)
            if qty <= 0:
                continue
            mk = float(dp.get("markPrice", 0) or 0)
            if mk <= 0:
                mk = next(
                    (float(x["markPrice"]) for x in rates if x["symbol"] == sym_p),
                    0.0,
                )
            if mk <= 0:
                try:
                    mk = get_mark_price(sym_p)
                except Exception:
                    mk = 0.0
            stake_map[sym_p] = abs(qty) * mk
    for sym_p in open_symbols:
        if sym_p not in stake_map and sym_p in position_sizes:
            stake_map[sym_p] = float(position_sizes[sym_p])
    return stake_map, exchange_symbols


def position_stake_tag(sym: str, exchange_symbols: set) -> str:
    """
    Position source (not the same as dry-run mode):
    [EXCH] = size from GET positionRisk (real position on Aster).
    [SIM]  = only in bot memory (_dry_positions) — no exchange fill while DRY_RUN.
    """
    if sym in exchange_symbols:
        return " [EXCH]"
    if sym in _dry_positions:
        return " [SIM]"
    if not DRY_RUN:
        return " [EXCH]"
    return " [SIM]"


def margin_sizing_tag(collateral: dict) -> str:
    """How deploy budget was computed: live API balances vs DRY_RUN_SIMULATED_MARGIN_USD."""
    if collateral.get("_dry_run_simulated_margin"):
        return " [margin SIM]"
    return " [margin LIVE]"


def stake_detail_in_ledger_below() -> bool:
    """
    True when log_sim_paper_ledger prints per-symbol stake/init/pnl — skip the same
    fields in Portfolio and Top 5 to avoid repeating.
    """
    if DRY_RUN:
        return bool(_dry_positions)
    return any(
        float(p.get("positionAmt", 0) or 0) > 0 for p in get_positions()
    )


def log_sim_paper_ledger(
    position_sizes: dict,
    exchange_symbols: Optional[set] = None,
    rates: Optional[list] = None,
) -> None:
    """
    Stake vs init and pnl% vs entry — same basis as stop / funding exits.
    Dry run: lines from _dry_positions (simulated longs; wallet still from API each cycle).
    Live: lines from exchange longs (get_positions), including opens not in bot memory.
    """
    ex: set = set(exchange_symbols) if exchange_symbols else set()
    if not ex:
        for p in get_positions():
            if float(p.get("positionAmt", 0) or 0) > 0:
                ex.add(p["symbol"])

    if DRY_RUN:
        if not _dry_positions:
            return
        items = [(s, _dry_positions[s]) for s in sorted(_dry_positions.keys())]
        log_section("Sim paper ledger")
        _sizing = (
            "paper DRY_RUN_SIMULATED_MARGIN_USD"
            if DRY_RUN_SIMULATED_MARGIN_USD > 0
            else "live API wallet"
        )
        log_info_styled(
            f"{Style.DIM}  Simulated perps only — sizing margin: {_sizing}. "
            f"PnL vs entry (stop & funding exits){Style.RESET_ALL}"
        )
    else:
        live = [
            p
            for p in get_positions()
            if float(p.get("positionAmt", 0) or 0) > 0
        ]
        if not live:
            return
        items = [(p["symbol"], p) for p in sorted(live, key=lambda x: x["symbol"])]
        log_section("Position ledger")
        log_info_styled(
            f"{Style.DIM}  PnL vs entry (stop & funding exits){Style.RESET_ALL}"
        )

    _neu = Fore.LIGHTBLACK_EX  # flat stake/pnl — gray, not cyan (avoids “all blue”)
    for sym, p in items:
        qty = abs(float(p.get("positionAmt", 0) or 0))
        entry = float(p.get("entryPrice", 0) or 0)
        try:
            mk = get_mark_price(sym)
        except Exception:
            mk = float(p.get("markPrice", 0) or 0)
        budget_usd = float(position_sizes.get(sym, 0) or 0)
        # Open notional = qty×entry (actual position cost basis). Deploy "budget" alone
        # can be lower because qty steps round up — not fees; keeps stake vs init aligned with pnl%.
        open_notional = qty * entry if entry > 0 else 0.0
        init = open_notional if open_notional > 0 else budget_usd
        stake = qty * mk if mk > 0 else 0.0
        pnl_pct = ((mk - entry) / entry * 100.0) if entry > 0 else 0.0
        d_stake = stake - init
        if abs(d_stake) < 0.01:
            stake_col = _neu
        elif d_stake > 0:
            stake_col = Fore.GREEN
        else:
            stake_col = Fore.RED
        if abs(pnl_pct) < 0.0005:
            pnl_col = _neu
        elif pnl_pct > 0:
            pnl_col = Fore.GREEN
        else:
            pnl_col = Fore.RED
        raw_tag = position_stake_tag(sym, ex)
        tag = f"{Style.DIM}{Fore.LIGHTWHITE_EX}{raw_tag}{Style.RESET_ALL}"
        fr_part = ""
        if rates:
            ri = next((x for x in rates if x["symbol"] == sym), None)
            if ri is not None:
                frv = float(ri.get("fundingRate") or 0)
                apr_v = funding_apr_pct_for_symbol(frv, sym)
                fr_sig = Fore.GREEN if frv >= 0 else Fore.RED
                fr_part = (
                    f"  {fr_sig}{format_funding_pct_label(frv, sym)} "
                    f"({apr_v:.0f}% APR){Style.RESET_ALL}"
                )
        book_muted = Fore.LIGHTBLACK_EX
        log_info_styled(
            f"    {Fore.WHITE}{Style.BRIGHT}{sym:<12}{Style.RESET_ALL} "
            f"{Style.DIM}{Fore.WHITE}init≈${init:,.2f}{Style.RESET_ALL}  "
            f"{stake_col}stake≈${stake:,.2f}{Style.RESET_ALL}  "
            f"{pnl_col}pnl≈{pnl_pct:+.2f}% vs entry{Style.RESET_ALL}"
            f"{tag}{fr_part}"
        )
        bid_ask_mid = None
        try:
            t = get_book_ticker(sym)
            b_ = float(t["bidPrice"])
            a_ = float(t["askPrice"])
            bid_ask_mid = (b_, a_, (b_ + a_) / 2.0)
        except Exception:
            pass
        book_bits: list = []
        if bid_ask_mid is not None:
            b_, a_, mid_ = bid_ask_mid
            book_bits.extend(
                [f"bid={b_:.6f}", f"ask={a_:.6f}", f"mid={mid_:.6f}"]
            )
        if mk > 0:
            book_bits.append(f"mark={mk:.6f}")
        if entry > 0:
            book_bits.append(f"entry={entry:.6f}")
        if book_bits:
            log_info_styled(
                f"      {book_muted}{'  '.join(book_bits)}{Style.RESET_ALL}"
            )


def portfolio_summary(
    open_symbols: set,
    rates: list,
    sizes: dict,
    stake_map: Optional[dict] = None,
    exchange_symbols: Optional[set] = None,
    omit_stake_lines: bool = False,
) -> str:
    """Build a compact portfolio summary; optional stake/init + [SIM]/[EXCH] per line."""
    lines = []
    total_notional = 0.0
    weighted_apr   = 0.0
    for sym in sorted(open_symbols):
        info    = next((r for r in rates if r["symbol"] == sym), {})
        rate    = info.get("fundingRate", 0)
        notional = sizes.get(sym, 0)
        apr      = funding_apr_pct_for_symbol(float(rate or 0), sym)
        total_notional += notional
        weighted_apr   += apr * notional
        stake_part = ""
        if (
            not omit_stake_lines
            and stake_map is not None
            and exchange_symbols is not None
        ):
            st = stake_map.get(sym)
            init_sz = sizes.get(sym)
            if st is not None and st > 0:
                if init_sz is not None and init_sz > 0:
                    stake_part = (
                        f"  stake≈${st:,.0f}  init≈${init_sz:,.0f}"
                        f"{position_stake_tag(sym, exchange_symbols)}"
                    )
                else:
                    stake_part = (
                        f"  stake≈${st:,.0f}{position_stake_tag(sym, exchange_symbols)}"
                    )
        lines.append(
            f"    {sym:<14} {format_funding_pct_label(float(rate or 0), sym)}  "
            f"${notional:.0f}{stake_part}"
        )
    avg_apr = weighted_apr / total_notional if total_notional else 0
    header  = (f"  Portfolio: {len(open_symbols)} positions  "
               f"~${total_notional:.0f} deployed  "
               f"blended APR={avg_apr:.1f}%")
    return header + "\n" + "\n".join(lines)

def compute_deploy_budget(collateral: dict) -> float:
    """
    Total notional budget (USDT) for opening perps — same formula live & dry-run.

    Cross-margin style: deploy notional ≈ effective_collateral_usd * WALLET_DEPLOY_PCT
    * LEVERAGE (collateral * deploy fraction * leverage). DRY_RUN with
    DRY_RUN_SIMULATED_MARGIN_USD > 0 substitutes that USD for effective margin so
    paper sizing matches what live would use with the same wallet settings.
    """
    total_margin = collateral.get("_total_effective_margin", 0.0)
    lev = max(1, int(LEVERAGE))
    budget = total_margin * WALLET_DEPLOY_PCT * float(lev)
    if WALLET_MAX_USD > 0:
        budget = min(budget, WALLET_MAX_USD)
    budget = max(budget, 0.0)
    return budget

def available_budget(total_budget: float, position_sizes: dict) -> float:
    """Budget remaining after accounting for already-open positions."""
    deployed = sum(position_sizes.values())
    return max(total_budget - deployed, 0.0)


def effective_deploy_cap(max_notional_budget: float) -> float:
    """
    Max total notional that may be allocated across positions (sum of position_sizes).
    A fraction RESERVE_DEPLOY_PCT of max_notional_budget is never allocated, leaving
    dry powder for new pool-eligible symbols when they appear.
    """
    return max_notional_budget * (1.0 - RESERVE_DEPLOY_PCT)


def _iter_open_long_positions():
    """Yield (symbol, position_dict) for longs — dry-run store or exchange."""
    if DRY_RUN:
        for sym, p in _dry_positions.items():
            if float(p.get("positionAmt", 0) or 0) > 0:
                yield sym, p
        return
    for p in get_positions():
        if float(p.get("positionAmt", 0) or 0) > 0:
            yield p["symbol"], p


def compute_portfolio_aggregate_stats(
    rates: list,
    position_sizes: dict,
) -> dict:
    """
    Totals across open longs: sizing sum, mark notional, cost basis, unrealized USD,
    blended funding APR (mark-weighted).
    """
    mark_nv = 0.0
    cost_basis = 0.0
    unrealized_usd = 0.0
    sizing_sum = 0.0
    n = 0
    w_apr_num = 0.0
    for sym, p in _iter_open_long_positions():
        qty = abs(float(p.get("positionAmt", 0) or 0))
        entry = float(p.get("entryPrice", 0) or 0)
        try:
            mk = get_mark_price(sym)
        except Exception:
            mk = float(p.get("markPrice", 0) or 0)
        if qty <= 0:
            continue
        leg = qty * mk
        mark_nv += leg
        cost_basis += qty * entry
        unrealized_usd += qty * (mk - entry)
        sizing_sum += float(position_sizes.get(sym, 0) or 0)
        n += 1
        info = next((r for r in rates if r["symbol"] == sym), None)
        fr = float(info.get("fundingRate", 0)) if info else 0.0
        w_apr_num += funding_apr_pct_for_symbol(fr, sym) * leg
    blended_apr = w_apr_num / mark_nv if mark_nv > 0 else 0.0
    pnl_pct = (unrealized_usd / cost_basis * 100.0) if cost_basis > 0 else 0.0
    return {
        "n": n,
        "mark_notional": mark_nv,
        "cost_basis": cost_basis,
        "unrealized_usd": unrealized_usd,
        "pnl_pct_cost": pnl_pct,
        "blended_apr_pct": blended_apr,
        "sizing_sum": sizing_sum,
    }


def log_portfolio_totals_line(
    rates: list,
    position_sizes: dict,
    *,
    total_budget: float,
    deploy_cap: float,
    avail_budget: float,
    margin_total: float,
    margin_tag: str = "",
    collateral: Optional[dict] = None,
) -> None:
    """
    Sectioned: margin & deploy, allocation cap vs deployed vs pool dry powder (with at-cap hint),
    then holdings (slots, mark, uPnL, blended APR). All styled — avoids cyan vs colored clash.
    """
    st = compute_portfolio_aggregate_stats(rates, position_sizes)
    n = st["n"]
    deployed = st["sizing_sum"]
    res_s = (
        f"  (reserve {RESERVE_DEPLOY_PCT*100:.1f}% dry powder)"
        if RESERVE_DEPLOY_PCT > 0
        else ""
    )
    eps = max(50.0, 0.01 * deploy_cap) if deploy_cap > 0 else 0.0
    at_cap = (
        deploy_cap > 0
        and deployed >= deploy_cap - eps
        and avail_budget <= 1e-6
    )
    if avail_budget >= WALLET_MIN_USD:
        avail_col = Fore.GREEN
        pool_note = ""
    elif avail_budget > 1e-6:
        avail_col = Fore.YELLOW
        pool_note = " (below min open size)"
    else:
        if at_cap and deploy_cap > 0:
            avail_col = Fore.YELLOW
            pool_note = (
                f" — at allocation cap (${deployed:,.0f} / ${deploy_cap:,.0f})"
            )
        else:
            avail_col = Fore.LIGHTBLACK_EX
            pool_note = ""

    log_section("Margin & budget")
    log_info_styled(
        f"  {Style.DIM}Max deploy{Style.RESET_ALL} ${total_budget:,.0f}  "
        f"| {Style.DIM}effective margin{Style.RESET_ALL} ~${margin_total:,.0f}"
        f"{margin_tag}"
    )
    if collateral:
        tim = collateral.get("_total_initial_margin_usdt")
        tmm = collateral.get("_total_maint_margin_usdt")
        if tim is not None or tmm is not None:
            log_info_styled(
                f"  {Style.DIM}Account IM / MM{Style.RESET_ALL}  "
                f"initial≈${float(tim or 0):,.0f}  "
                f"maint≈${float(tmm or 0):,.0f}"
            )
    if (
        collateral
        and DRY_RUN
        and collateral.get("_dry_run_simulated_margin")
        and collateral.get("_live_effective_margin") is not None
    ):
        lm = float(collateral["_live_effective_margin"])
        log_info_styled(
            f"  {Style.DIM}Live wallet (API read, not used for deploy sizing here)"
            f"{Style.RESET_ALL} ~${lm:,.0f}"
        )
    log_info_styled(
        f"  {Style.DIM}Allocation cap{Style.RESET_ALL} ${deploy_cap:,.0f}  "
        f"| {Style.DIM}deployed (sizing){Style.RESET_ALL} ${deployed:,.0f}  "
        f"| {Style.DIM}pool dry powder{Style.RESET_ALL} {avail_col}"
        f"${avail_budget:,.0f}{Style.RESET_ALL}{pool_note}{res_s}"
    )

    log_section("Holdings")
    if n == 0:
        log_info_styled(
            f"  {Style.DIM}No open legs{Style.RESET_ALL}  "
            f"0/{MAX_POSITIONS} slots  "
            f"| {Style.DIM}margin{Style.RESET_ALL} ~${margin_total:,.0f}"
        )
        return
    u = st["unrealized_usd"]
    pnl_col = (
        Fore.GREEN
        if u > 1e-6
        else (Fore.RED if u < -1e-6 else Fore.LIGHTBLACK_EX)
    )
    log_info_styled(
        f"  {n}/{MAX_POSITIONS} slots  "
        f"| {Style.DIM}deployed (sizing){Style.RESET_ALL} ${st['sizing_sum']:,.0f}  "
        f"| {Style.DIM}at mark{Style.RESET_ALL} ${st['mark_notional']:,.0f}  "
        f"| {Style.DIM}uPnL{Style.RESET_ALL} {pnl_col}${u:+,.2f}{Style.RESET_ALL} "
        f"({st['pnl_pct_cost']:+.2f}% vs entry)  "
        f"| {Style.DIM}blended funding{Style.RESET_ALL} ~{st['blended_apr_pct']:.1f}% APR  "
        f"| {Style.DIM}margin{Style.RESET_ALL} ~${margin_total:,.0f}"
    )

# --- Main ---------------------------------------------------------------------

def _emit_futures_spot_balance_tables(collateral: dict, log_fn) -> None:
    """Log futures margin + spot rows (dust filtered). log_fn = log_success or log_info."""
    dust_lbl = f" (hiding <${BALANCE_DUST_USD:.0f} est.)" if BALANCE_DUST_USD > 0 else ""
    log_fn(f"  Futures margin wallet{dust_lbl}:")
    fut = collateral.get("_futures_detail")
    if fut is None:
        log_fn("    (balance fetch unavailable)")
    elif not fut:
        log_fn("    (no non-dust assets — lower BALANCE_DUST_USD to show small balances)")
    else:
        for r in fut:
            asset = r["asset"]
            bal = r["balance"]
            usd = r["est_usd"]
            if "eff_margin" in r:
                em = r["eff_margin"]
                log_fn(
                    f"    {asset:<8} balance={bal:.8g}  ~${usd:.0f}  "
                    f"eff_margin≈${em:.0f}"
                )
            else:
                log_fn(f"    {asset:<8} balance={bal:.8g}  ~${usd:.0f}")

    log_fn(f"  Spot wallet{dust_lbl}:")
    spot = collateral.get("_spot_detail")
    if spot is None:
        log_fn("    (spot unavailable)")
    elif not spot:
        log_fn("    (no non-dust balances — lower BALANCE_DUST_USD to show small balances)")
    else:
        for r in spot:
            log_fn(
                f"    {r['asset']:<8} total={r['total']:.8g}  ~${r['est_usd']:.0f}  "
                f"free={r['free']:.8g}  locked={r['locked']:.8g}"
            )


def print_startup_banner(collateral: dict, aster_price: float):
    log_success("=" * 62)
    if DRY_RUN:
        log_warn("  ⚠️  DRY RUN MODE — NO REAL ORDERS WILL BE PLACED  ⚠️")
        log_warn(
            "  Reads live markets, funding, and wallet balances; simulates perp "
            "opens/closes in memory only (hide wallet tables with "
            "DRY_RUN_SHOW_LIVE_WALLET_DETAILS=false)."
        )
        log_warn("  Set DRY_RUN=false in .env when ready to go live.")
        if DRY_RUN_SIMULATED_MARGIN_USD > 0:
            log_warn(
                f"  Position sizing uses simulated margin ${DRY_RUN_SIMULATED_MARGIN_USD:.0f} "
                f"(DRY_RUN_SIMULATED_MARGIN_USD). Set to 0 to size from live API wallet margin instead."
            )
        else:
            log_success(
                "  Dry run sizing: live wallet effective margin (DRY_RUN_SIMULATED_MARGIN_USD=0); "
                "perp orders simulated only."
            )
        log_success("=" * 62)
    log_success("  Aster Funding Rate Farmer  --  Multi-Asset Margin Mode")
    log_success("=" * 62)

    if live_wallet_logs_enabled():
        if BALANCE_DUST_USD > 0:
            log_success(
                f"  Balance display: hide rows below ~${BALANCE_DUST_USD:.0f} est. "
                f"(BALANCE_DUST_USD)"
            )
        _emit_futures_spot_balance_tables(collateral, log_success)
        if not collateral.get("_dry_run_simulated_margin"):
            if not collateral.get("ASTER"):
                log_warn(
                    "  ASTER  not found in futures wallet -- deposit ASTER tokens for extra points"
                )
            if not collateral.get("USDF"):
                log_warn(
                    "  USDF   not found in futures wallet -- mint/buy USDF for best margin efficiency"
                )
    else:
        log_info(
            "  Dry run: live futures + spot wallet lines hidden "
            "(DRY_RUN_SHOW_LIVE_WALLET_DETAILS=false). "
            "Sizing still uses live API margin unless DRY_RUN_SIMULATED_MARGIN_USD > 0."
        )

    total = collateral.get("_total_effective_margin", 0)
    log_success(f"  Total effective margin (for sizing):  ~${total:.0f}")
    if DRY_RUN and collateral.get("_dry_run_simulated_margin"):
        lm = collateral.get("_live_effective_margin")
        if lm is not None:
            log_info(
                f"  Live wallet effective margin (API read): ~${float(lm):,.0f} "
                "(not used for sizing while DRY_RUN_SIMULATED_MARGIN_USD > 0)"
            )
        if live_wallet_logs_enabled():
            log_info(
                "    ^ PAPER row = sizing collateral (DRY_RUN_SIMULATED_MARGIN_USD); "
                "other futures rows are live account reads"
            )
        else:
            log_info("    ^ simulated margin for sizing — wallet tables hidden in logs")
    elif DRY_RUN and not collateral.get("_dry_run_simulated_margin"):
        log_info(
            "  Dry run: sizing matches this live API margin; perp fills are simulated only."
        )
    deploy_budget = compute_deploy_budget(collateral)
    _cap = effective_deploy_cap(deploy_budget)
    log_success(
        f"  Deploy budget (max notional):  ${deploy_budget:.0f}  "
        f"(margin × {WALLET_DEPLOY_PCT*100:.0f}% × {LEVERAGE}x)"
    )
    if RESERVE_DEPLOY_PCT > 0:
        log_success(
            f"  Allocation cap (max in positions):  ${_cap:.0f}  "
            f"({RESERVE_DEPLOY_PCT*100:.1f}% reserve"
            + (
                f" = 1/{MAX_POSITIONS} slot"
                if RESERVE_SLOT_FOR_NEW_POOLS
                else ""
            )
            + " — dry powder for new pool names)"
        )
    if WALLET_MAX_USD > 0:
        log_success(f"  Budget ceiling: ${WALLET_MAX_USD:.0f}")
    tim = collateral.get("_total_initial_margin_usdt")
    tmm = collateral.get("_total_maint_margin_usdt")
    if tim is not None or tmm is not None:
        log_success(
            f"  Account margin (GET /fapi/v2/account):  "
            f"initial≈${float(tim or 0):,.0f}  maint≈${float(tmm or 0):,.0f}"
        )
    if not DRY_RUN and credentials_ok():
        try:
            now_ms = int(time.time() * 1000)
            ms = now_ms - INCOME_LOOKBACK_DAYS * 86400000
            fu = sum_funding_fee_income_all_symbols_usdt(ms, now_ms)
            log_success(
                f"  Funding income (GET /fapi/v1/income, last {INCOME_LOOKBACK_DAYS}d):  "
                f"${fu:+,.2f} USDT"
            )
        except Exception as e:
            log_warn(f"  Funding income summary: {e}")
    log_success(
        f"  Min funding:  {MIN_FUNDING_RATE*100:.4f}% per interval "
        f"(same units as API lastFundingRate; ~{MIN_FUNDING_RATE*3*365*100:.1f}% APR "
        f"if 8h interval)"
    )
    if MAX_FEE_BREAKEVEN_FUNDING_INTERVALS > 0:
        log_success(
            f"  Fee-aware opens: skip if breakeven > {MAX_FEE_BREAKEVEN_FUNDING_INTERVALS:.0f} "
            f"funding intervals (ESTIMATED_TAKER_FEE_BPS={ESTIMATED_TAKER_FEE_BPS:.0f})"
        )
    if MIN_QUOTE_VOLUME_24H > 0:
        log_success(
            f"  Pool quality: 24h quote volume ≥ ${MIN_QUOTE_VOLUME_24H:,.0f} USDT "
            f"(MIN_QUOTE_VOLUME_24H)"
        )
    if SYMBOL_ALLOWLIST is not None:
        log_success(
            f"  Symbol allowlist: {len(SYMBOL_ALLOWLIST)} symbols "
            f"(SYMBOL_ALLOWLIST — only these names considered)"
        )
    log_success(f"  Max positions: {MAX_POSITIONS}")
    log_success(f"  Poll: {POLL_INTERVAL_SEC}s idle  |  {RISK_POLL_INTERVAL_SEC}s when positions open (risk)")
    log_success(f"  MARK_PRICE_WS:  {MARK_PRICE_WS}")
    log_success(f"  SHOW_BOOK_IN_LOGS:  {SHOW_BOOK_IN_LOGS}")
    dn_status = "ENABLED (set DELTA_NEUTRAL=false to disable)" if DELTA_NEUTRAL else "disabled (set DELTA_NEUTRAL=true to enable HL hedge)"
    log_success(f"  Delta-neutral: {dn_status}")
    log_aster_points_margin_advisory(collateral)
    log_success("=" * 62)

def run(max_cycles: int = 0) -> None:
    """
    Main loop. max_cycles > 0 exits after that many completed iterations (after sleep),
    without unwinding positions — for staging smoke tests (e.g. paper + --max-cycles 1).
    """
    if not credentials_ok():
        log_error(
            "Aster API credentials not set. Pro API V3: ASTER_USER, ASTER_SIGNER, "
            "ASTER_SIGNER_PRIVATE_KEY — or legacy: ASTER_API_KEY + ASTER_SECRET_KEY. "
            "See .env.example and https://github.com/asterdex/api-docs"
        )
        return

    log_info("Configuring account...")
    enable_multi_asset_mode()

    # Delta-neutral: conditionally import and initialise Hyperliquid
    hl_info = hl_exchange = hl_address = None
    if DELTA_NEUTRAL:
        try:
            from delta_neutral import hl_setup, hl_open_short, hl_close_short, hl_get_funding_rate
            hl_info, hl_exchange, hl_address = hl_setup()
            log_success("  [HL] Hedge leg ready")
        except Exception as e:
            log_error(f"  [HL] Failed to initialise hedge leg: {e}")
            log_warn("  Continuing in Aster-only mode (DELTA_NEUTRAL disabled)")
            hl_info = hl_exchange = hl_address = None

    aster_price = get_aster_price()
    collateral  = get_collateral_summary()
    print_startup_banner(collateral, aster_price)

    exchange_info  = get_exchange_info()
    open_symbols:   set  = set()   # currently open symbols
    position_sizes: dict = {}        # symbol -> notional deployed

    for p in get_positions():
        if float(p["positionAmt"]) > 0:
            sym = p["symbol"]
            open_symbols.add(sym)
            amt = abs(float(p["positionAmt"]))
            ep = float(p.get("entryPrice", 0) or 0)
            mp = float(p.get("markPrice", 0) or 0)
            if ep > 0:
                position_sizes[sym] = amt * ep
            elif mp > 0:
                position_sizes[sym] = amt * mp
            log_info(
                f"  Recovered: {sym}  amt={p['positionAmt']}  "
                f"init_stake≈${position_sizes.get(sym, 0):,.0f}"
            )

    mark_watcher = None
    if MARK_PRICE_WS:
        try:
            from aster_ws import MarkPriceWatcher, websocket_available

            if websocket_available():
                ws_base = os.getenv(
                    "FSTREAM_WS_URL", "wss://fstream.asterdex.com/stream"
                ).strip()
                mark_watcher = MarkPriceWatcher(STOP_LOSS_PCT, base_url=ws_base)
                mark_watcher.start()
                log_success(
                    "  [WS] Mark price combined stream — faster stop vs entry (REST still runs)"
                )
            else:
                log_warn("  [WS] Install websocket-client for mark-price stream: pip install websocket-client")
        except Exception as e:
            log_warn(f"  [WS] Mark price stream not started: {e}")
            mark_watcher = None

    news_symbol_expiry: dict[str, float] = {}
    last_x_news_poll_monotonic = 0.0
    _x_boost_logged_sig: Optional[Tuple[str, ...]] = None
    _x_boost_no_creds_logged = False

    completed_cycles = 0
    main_loop_i = 0
    while True:
        try:
            main_loop_i += 1
            _cycle_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            log_info_styled(
                f"\n{Style.DIM}{Fore.LIGHTBLACK_EX}  === cycle {main_loop_i} · {_cycle_ts} ==="
                f"{Style.RESET_ALL}"
            )
            log_info("  Scanning funding rates...")
            rates = get_all_funding_rates()
            maybe_log_funding_sign_selfcheck(main_loop_i, open_symbols, rates)

            news_active: set[str] = set()
            if NEWS_SYMBOL_BOOST_ENABLED:
                wall = time.time()
                for _sym, _exp in list(news_symbol_expiry.items()):
                    if _exp <= wall:
                        del news_symbol_expiry[_sym]
                now_m = time.monotonic()
                if now_m - last_x_news_poll_monotonic >= NEWS_POLL_SEC:
                    last_x_news_poll_monotonic = now_m
                    if not (X_BEARER_TOKEN or (X_API_KEY and X_API_SECRET)):
                        if not _x_boost_no_creds_logged:
                            log_warn(
                                "  [X boost] NEWS_SYMBOL_BOOST_ENABLED but no "
                                "X_BEARER_TOKEN or X_API_KEY+X_API_SECRET — boost inactive"
                            )
                            _x_boost_no_creds_logged = True
                    else:
                        try:
                            import news as _news_x

                            xt_lines = _news_x.fetch_x_recent_lines()
                            syms = _news_x.extract_usdt_perp_symbols_from_xt(
                                xt_lines, valid_symbols=set(exchange_info.keys())
                            )
                            deadline = wall + NEWS_SYMBOL_BOOST_TTL_SEC
                            for s in syms:
                                prev = news_symbol_expiry.get(s, 0.0)
                                news_symbol_expiry[s] = max(prev, deadline)
                        except Exception as e:
                            log_warn(f"  [X boost] poll failed: {e}")
                news_active = {
                    s for s, e in news_symbol_expiry.items() if e > wall
                }
                sig = tuple(sorted(news_active))
                if sig != _x_boost_logged_sig:
                    _x_boost_logged_sig = sig
                    log_info(
                        "  [X boost] active symbols: "
                        f"{', '.join(sig) if sig else '(none)'}"
                    )
            rates_ordered = order_rates_with_symbol_boost(
                rates, news_active, MIN_FUNDING_RATE
            )

            volumes_24h = get_24h_quote_volumes()
            volume_filter_active = MIN_QUOTE_VOLUME_24H > 0
            if volume_filter_active and not volumes_24h:
                log_warn(
                    "  MIN_QUOTE_VOLUME_24H is set but 24h ticker failed — "
                    "skipping volume filter this cycle"
                )
                volume_filter_active = False

            # Live wallet sizing — recompute every cycle so growth is captured
            collateral    = get_collateral_summary()
            aster_price   = get_aster_price()
            total_budget  = compute_deploy_budget(collateral)
            deploy_cap    = effective_deploy_cap(total_budget)
            avail_budget  = available_budget(deploy_cap, position_sizes)
            margin_total  = collateral.get("_total_effective_margin", 0)

            halted, halt_reason = farming_halt_active()

            # Mark-price WebSocket (push) — drain before REST stop-loss
            if mark_watcher is not None:
                mark_watcher.sync(stop_loss_entries())
                for sym in mark_watcher.drain_stop_signals():
                    log_warn(f"  [WS] Stop loss triggered (mark): {sym}")
                    close_long(sym, exchange_info, close_reason="stop_loss_ws")
                    if DELTA_NEUTRAL and hl_info:
                        hl_close_short(
                            hl_info,
                            hl_exchange,
                            hl_address,
                            sym.replace("USDT", ""),
                            "stop_loss_ws",
                        )
                    open_symbols.discard(sym)
                    position_sizes.pop(sym, None)

            # Stop loss (REST / poll)
            for sym in check_stop_loss(get_positions()):
                close_long(sym, exchange_info, close_reason="stop_loss")
                if DELTA_NEUTRAL and hl_info:
                    hl_close_short(hl_info, hl_exchange, hl_address,
                                   sym.replace("USDT",""), "stop_loss")
                open_symbols.discard(sym)
                position_sizes.pop(sym, None)

            for sym in check_take_profit(get_positions()):
                close_long(sym, exchange_info, close_reason="take_profit")
                if DELTA_NEUTRAL and hl_info:
                    hl_close_short(hl_info, hl_exchange, hl_address,
                                   sym.replace("USDT",""), "take_profit")
                open_symbols.discard(sym)
                position_sizes.pop(sym, None)

            # Exit on funding flip
            for sym in list(open_symbols):
                info = next((r for r in rates if r["symbol"] == sym), None)
                rate_rest: Optional[float] = None
                if info is not None:
                    raw_fr = info.get("fundingRate")
                    if raw_fr is not None:
                        try:
                            rate_rest = float(raw_fr)
                        except (TypeError, ValueError):
                            rate_rest = None
                rate_ws: Optional[float] = None
                if FUNDING_EXIT_USE_WS_ESTIMATED and mark_watcher is not None:
                    rate_ws = mark_watcher.get_estimated_funding(sym)
                if (
                    FUNDING_EXIT_USE_WS_ESTIMATED
                    and rate_ws is not None
                ):
                    rate = rate_ws
                    rate_src = "est_ws"
                else:
                    rate = rate_rest
                    rate_src = "last_rest"
                if rate is None or rate < EXIT_FUNDING_RATE:
                    if rate is None:
                        rate_str = "N/A"
                    else:
                        rate_str = (
                            f"{format_funding_pct_label(rate, sym)} "
                            f"({funding_apr_pct_for_symbol(rate, sym):.0f}% APR)"
                        )
                    log_warn(
                        f"  Funding dropped [{sym}] ({rate_src}) -> {rate_str}"
                    )
                    close_long(sym, exchange_info, close_reason="funding_dropped")
                    if DELTA_NEUTRAL and hl_info:
                        hl_close_short(hl_info, hl_exchange, hl_address,
                                       sym.replace("USDT",""), "funding_dropped")
                    open_symbols.discard(sym)
                    position_sizes.pop(sym, None)

            # Open new positions (rank-weighted diversification)
            slots = MAX_POSITIONS - len(open_symbols)
            if slots > 0 and halted:
                log_warn(
                    f"  [FARMING_HALT] {halt_reason} — skipping new opens; "
                    "stop-loss / take-profit / funding exits still run."
                )
            elif slots > 0:
                # Build candidate list incrementally so correlation guard
                # sees symbols selected earlier in the same cycle
                pending: set = set()
                raw_candidates = []
                for r in rates_ordered:
                    if len(raw_candidates) >= slots:
                        break
                    sym = r["symbol"]
                    if not is_pool_symbol_eligible(
                        r, exchange_info, volumes_24h, volume_filter_active
                    ):
                        continue
                    if sym in open_symbols or is_correlated(sym, open_symbols | pending):
                        continue
                    raw_candidates.append(r)
                    pending.add(sym)

                # Rank-weighted sizing from live wallet budget
                if avail_budget < WALLET_MIN_USD:
                    _deployed_sum = sum(position_sizes.values())
                    _cap_eps = max(50.0, 0.01 * deploy_cap) if deploy_cap > 0 else 0.0
                    at_alloc_cap = (
                        deploy_cap > 0
                        and _deployed_sum >= deploy_cap - _cap_eps
                    )
                    # At cap: Margin & budget section already shows deployed/cap/pool $0.
                    if not at_alloc_cap:
                        log_warn(
                            f"  Available budget ${avail_budget:.0f} below "
                            f"minimum ${WALLET_MIN_USD:.0f} -- skipping new opens"
                        )
                    raw_candidates = []  # prevent opening below min

                candidates = rank_weighted_sizes(raw_candidates, avail_budget)

                # Small wallet: split sizes are all under WALLET_MIN_USD — one concentrated leg.
                if (
                    candidates
                    and avail_budget >= WALLET_MIN_USD
                    and all(c.get("notional", 0) < WALLET_MIN_USD for c in candidates)
                ):
                    top = candidates[0]
                    log_warn(
                        f"  Split sizes each < WALLET_MIN_USD (${WALLET_MIN_USD:.0f}) — "
                        f"concentrating full ${avail_budget:.0f} deploy on {top['symbol']}"
                    )
                    candidates = [{**top, "notional": avail_budget}]

                for c in candidates:
                    sym      = c["symbol"]
                    rate     = c["fundingRate"]
                    notional = c["notional"]
                    if notional < WALLET_MIN_USD:
                        log_warn(f"  {sym} notional ${notional:.0f} below min "
                                 f"${WALLET_MIN_USD:.0f} -- skipping")
                        continue
                    if not funding_passes_fee_breakeven(rate):
                        be = fee_breakeven_funding_intervals(rate)
                        log_warn(
                            f"  {sym} skip fee breakeven: ~{be:.1f} funding intervals to cover "
                            f"est. RT fees ({ESTIMATED_TAKER_FEE_BPS:.0f} bps/side) vs |rate|="
                            f"{abs(float(rate)):.6g} — raise MAX_FEE_BREAKEVEN_FUNDING_INTERVALS "
                            "or lower ESTIMATED_TAKER_FEE_BPS to allow"
                        )
                        continue
                    apr      = funding_apr_pct_for_symbol(rate, sym)
                    mins     = max(0, (c["nextFundingTime"] - int(time.time()*1000)) // 60000)
                    log_success(
                        f"\n  Target: {sym}  {format_funding_pct_label(rate, sym)}  "
                        f"({apr:.1f}% APR)  ${notional:.0f} notional  "
                        f"next funding {mins}m"
                    )
                    try:
                        set_cross_margin(sym)
                        set_leverage(sym, LEVERAGE)
                        # Delta-neutral: open HL short first; skip if it fails
                        if DELTA_NEUTRAL and hl_info:
                            hl_ok = hl_open_short(hl_info, hl_exchange, hl_address,
                                                   sym.replace("USDT",""), notional,
                                                   rate, hl_get_funding_rate(hl_info, sym.replace("USDT","")))
                            if not hl_ok:
                                log_warn(f"  HL short failed for {sym} -- skipping")
                                continue
                        open_long(sym, notional, exchange_info, funding_rate=rate)
                        open_symbols.add(sym)
                        position_sizes[sym] = notional
                    except Exception as e:
                        log_error(f"  Failed to open {sym}: {e}")

                if not candidates:
                    if not raw_candidates and not rates:
                        log_info("  No qualifying opportunities (no funding data)")
                    elif not raw_candidates and rates:
                        if avail_budget < WALLET_MIN_USD:
                            pass  # already warned: budget below WALLET_MIN_USD
                        else:
                            br = rates_ordered[0]
                            bf = float(br["fundingRate"])
                            if bf < MIN_FUNDING_RATE:
                                log_warn(
                                    "  No qualifying opportunities — best funding is "
                                    f"{br['symbol']} at "
                                    f"{format_funding_pct_label(bf, br['symbol'])}, "
                                    "below MIN_FUNDING_RATE "
                                    f"{MIN_FUNDING_RATE*100:.4f}%/interval "
                                    "(lower MIN_FUNDING_RATE in .env to enter)"
                                )
                            elif (
                                SYMBOL_ALLOWLIST is not None or MIN_QUOTE_VOLUME_24H > 0
                            ):
                                # Do not cite global #1 (often illiquid alts) — summarize pool-qualified set.
                                eligible = [
                                    r
                                    for r in rates_ordered
                                    if is_pool_symbol_eligible(
                                        r,
                                        exchange_info,
                                        volumes_24h,
                                        volume_filter_active,
                                    )
                                ]
                                elig_names = [x["symbol"] for x in eligible]
                                not_held = [
                                    r
                                    for r in eligible
                                    if r["symbol"] not in open_symbols
                                    and not is_correlated(r["symbol"], open_symbols)
                                ]
                                if not eligible:
                                    log_warn(
                                        "  No qualifying opportunities — no symbol passes "
                                        f"{pool_eligibility_rules_label()} together."
                                    )
                                elif not not_held:
                                    extra = (
                                        "add symbols to SYMBOL_ALLOWLIST or loosen MIN_QUOTE_VOLUME_24H."
                                        if SYMBOL_ALLOWLIST is not None
                                        else "loosen MIN_QUOTE_VOLUME_24H or MIN_FUNDING_RATE."
                                    )
                                    log_info(
                                        f"  No new pool to open: all {len(eligible)} pool-eligible "
                                        f"symbol(s) already held ({', '.join(elig_names)}). "
                                        f"Other perps may be negative funding or below min volume — {extra}"
                                    )
                                else:
                                    log_warn(
                                        "  No qualifying opportunities — "
                                        f"{len(not_held)} pool-eligible not held "
                                        f"({', '.join(r['symbol'] for r in not_held[:6])}"
                                        f"{'…' if len(not_held) > 6 else ''}) "
                                        f"— check correlation, WALLET_MIN_USD splits, or errors above."
                                    )
                            else:
                                top_sym = br["symbol"]
                                extra = []
                                if volume_filter_active and volumes_24h:
                                    qv = volumes_24h.get(top_sym, 0.0)
                                    if qv < MIN_QUOTE_VOLUME_24H:
                                        extra.append(
                                            f"best {top_sym} 24h vol ${qv:,.0f} "
                                            f"< MIN_QUOTE_VOLUME_24H ${MIN_QUOTE_VOLUME_24H:,.0f}"
                                        )
                                if SYMBOL_ALLOWLIST is not None and top_sym.upper() not in SYMBOL_ALLOWLIST:
                                    extra.append("best symbol not in SYMBOL_ALLOWLIST")
                                if extra:
                                    log_warn(
                                        "  No qualifying opportunities — "
                                        + "; ".join(extra)
                                    )
                                else:
                                    log_warn(
                                        "  No qualifying opportunities — top rates filtered "
                                        "(BLACKLIST, not tradable in exchangeInfo, or correlation)"
                                    )
                    else:
                        log_info("  No qualifying opportunities this cycle")
            else:
                log_info(f"  At max positions ({MAX_POSITIONS}) -- holding")

            stake_map: dict = {}
            exchange_symbols: set = set()
            if open_symbols:
                stake_map, exchange_symbols = build_stake_context(
                    rates, open_symbols, position_sizes
                )

            _omit_stake_dupes = stake_detail_in_ledger_below()

            log_portfolio_totals_line(
                rates,
                position_sizes,
                total_budget=total_budget,
                deploy_cap=deploy_cap,
                avail_budget=avail_budget,
                margin_total=margin_total,
                margin_tag=margin_sizing_tag(collateral),
                collateral=collateral,
            )

            # Status (skip Portfolio block when position ledger below has full per-symbol lines)
            if open_symbols and not _omit_stake_dupes:
                log_section("Per-symbol funding (portfolio)")
                log_info_styled(
                    portfolio_summary(
                        open_symbols,
                        rates,
                        position_sizes,
                        stake_map,
                        exchange_symbols,
                        omit_stake_lines=_omit_stake_dupes,
                    )
                )
            elif not open_symbols:
                if DRY_RUN:
                    log_info_styled(
                        f"{Style.DIM}  (Dry run: simulated longs only after a ‘Target:’ open; "
                        f"funding ≥ MIN_FUNDING_RATE {MIN_FUNDING_RATE*100:.4f}%/interval, "
                        f"budget ≥ WALLET_MIN_USD ${WALLET_MIN_USD:.0f}){Style.RESET_ALL}"
                    )
            pool_filters_on = SYMBOL_ALLOWLIST is not None or MIN_QUOTE_VOLUME_24H > 0
            if pool_filters_on:
                rows_top5 = []
                for r in rates_ordered:
                    if len(rows_top5) >= 5:
                        break
                    if is_pool_symbol_eligible(
                        r, exchange_info, volumes_24h, volume_filter_active
                    ):
                        rows_top5.append(r)
                log_section("Market — top funding")
                _sub = pool_eligibility_rules_label()
                if _omit_stake_dupes and open_symbols:
                    log_info_styled(
                        f"{Style.DIM}  {_sub}; held symbols omitted (see ledger below)"
                        f"{Style.RESET_ALL}"
                    )
                else:
                    log_info_styled(f"{Style.DIM}  {_sub}{Style.RESET_ALL}")
                if not rows_top5:
                    _hint = "lower MIN_QUOTE_VOLUME_24H or MIN_FUNDING_RATE"
                    if SYMBOL_ALLOWLIST is not None:
                        _hint += ", or widen SYMBOL_ALLOWLIST"
                    log_warn(
                        f"  No symbols pass your pool filters this cycle — {_hint}"
                    )
            else:
                rows_top5 = rates_ordered[:5]
                log_section("Market — top funding")
                if _omit_stake_dupes and open_symbols:
                    log_info_styled(
                        f"{Style.DIM}  All symbols (no pool filters); held omitted (ledger below)"
                        f"{Style.RESET_ALL}"
                    )
                else:
                    log_info_styled(
                        f"{Style.DIM}  All symbols (no pool filters){Style.RESET_ALL}"
                    )

            if _omit_stake_dupes and open_symbols:
                rows_for_log = []
                for r in rates_ordered:
                    if len(rows_for_log) >= 5:
                        break
                    if r["symbol"] in open_symbols:
                        continue
                    if pool_filters_on and not is_pool_symbol_eligible(
                        r, exchange_info, volumes_24h, volume_filter_active
                    ):
                        continue
                    rows_for_log.append(r)
            else:
                rows_for_log = rows_top5

            # Top 5 [IN] lines reuse stake_map / exchange_symbols from build_stake_context above
            for r in rows_for_log:
                sym = r["symbol"]
                flag = "[IN]" if sym in open_symbols else "    "
                fr = r["fundingRate"]
                mark = float(r.get("markPrice") or 0)
                extra = ""
                # Open positions: stake/PnL live in the ledger below — no book/stake repeat here.
                in_pos = sym in open_symbols
                if SHOW_BOOK_IN_LOGS and not (in_pos and _omit_stake_dupes):
                    try:
                        t = get_book_ticker(sym)
                        bid = float(t["bidPrice"])
                        ask = float(t["askPrice"])
                        mid = (bid + ask) / 2.0
                        extra = (
                            f"   bid={bid:.6f} ask={ask:.6f} mid={mid:.6f} mark={mark:.6f}"
                        )
                    except Exception:
                        extra = f"   mark={mark:.6f}" if mark > 0 else ""
                if in_pos and not _omit_stake_dupes:
                    st = stake_map.get(sym)
                    init = position_sizes.get(sym)
                    if st is not None and st > 0:
                        if init is not None and init > 0:
                            if st < init - 1e-6:
                                col = Fore.RED
                            elif st > init + 1e-6:
                                col = Fore.GREEN
                            else:
                                col = Fore.CYAN
                            tag = position_stake_tag(sym, exchange_symbols)
                            stake_s = (
                                f"{col}   stake≈${st:,.0f}  init ${init:,.0f}"
                                f"{Style.RESET_ALL}{tag}"
                            )
                        else:
                            stake_s = (
                                f"   stake≈${st:,.0f}"
                                f"{position_stake_tag(sym, exchange_symbols)}"
                            )
                        extra = (extra + stake_s) if extra else stake_s
                # log_info_styled: log_info’s cyan prefix would wash out stake green/red
                log_info_styled(
                    f"  {flag} {sym:<14} "
                    f"{format_funding_pct_label(float(fr), sym)}  "
                    f"({funding_apr_pct_for_symbol(float(fr), sym):.0f}% APR){extra}"
                )

            if SHOW_BOOK_IN_LOGS and open_symbols and not _omit_stake_dupes:
                in_top5 = {r["symbol"] for r in rows_top5}
                book_only = sorted(open_symbols - in_top5)
                if book_only:
                    log_info_styled(
                        f"{Style.DIM}  Book — open legs not shown in top-5 rows above:"
                        f"{Style.RESET_ALL}"
                    )
                    for sym in book_only:
                        mp = next(
                            (float(x["markPrice"]) for x in rates if x["symbol"] == sym),
                            None,
                        )
                        if mp is None or mp <= 0:
                            try:
                                mp = get_mark_price(sym)
                            except Exception:
                                mp = None
                        _log_book_prices(sym, mp)

            # Wallet snapshot (already fetched at top of cycle)
            if live_wallet_logs_enabled():
                _emit_futures_spot_balance_tables(collateral, log_info)
            log_sim_paper_ledger(position_sizes, exchange_symbols, rates)

            append_cycle_snapshot(
                open_symbols=open_symbols,
                position_sizes=position_sizes,
                avail_budget=avail_budget,
                total_budget=total_budget,
                margin_effective=margin_total,
                halted=halted,
                halt_reason=halt_reason,
                deploy_cap=deploy_cap,
            )

            if has_risk_exposure(open_symbols):
                sleep_sec = RISK_POLL_INTERVAL_SEC
                _risk_line = f"stop loss -{STOP_LOSS_PCT*100:.1f}% vs entry"
                if TAKE_PROFIT_PCT > 0:
                    _risk_line += (
                        f"  |  take profit +{TAKE_PROFIT_PCT*100:.1f}% vs entry"
                    )
                log_info(
                    f"\n  Sleeping {sleep_sec}s (risk poll — open position(s), "
                    f"{_risk_line})..."
                )
            else:
                sleep_sec = POLL_INTERVAL_SEC
                log_info(f"\n  Sleeping {sleep_sec}s (scan interval, no positions)...")
            time.sleep(sleep_sec)
            completed_cycles += 1
            if max_cycles > 0 and completed_cycles >= max_cycles:
                if DRY_RUN:
                    log_success(
                        f"  Staging exit: completed {completed_cycles} cycle(s) "
                        f"(--max-cycles={max_cycles}). Exiting; paper positions remain in memory "
                        "and in the trade log only (no orders were sent)."
                    )
                else:
                    log_warn(
                        f"  Staging exit: completed {completed_cycles} cycle(s) "
                        f"(--max-cycles={max_cycles}). Process exiting without closing real "
                        "positions — they stay open on the exchange. For normal shutdown with "
                        "flatten, use Ctrl+C; for continuous operation omit --max-cycles."
                    )
                if mark_watcher is not None:
                    mark_watcher.stop()
                return

        except KeyboardInterrupt:
            log_warn("\n  Shutting down -- closing all positions...")
            if mark_watcher is not None:
                mark_watcher.stop()
            for sym in list(open_symbols):
                try:
                    close_long(sym, exchange_info, close_reason="shutdown")
                    if DELTA_NEUTRAL and hl_info:
                        hl_close_short(hl_info, hl_exchange, hl_address,
                                       sym.replace("USDT",""), "shutdown")
                    position_sizes.pop(sym, None)
                except Exception as e:
                    log_error(f"  Error closing {sym}: {e}")
            log_success("  All positions closed. Goodbye.")
            break

        except requests.exceptions.RequestException as e:
            log_error(f"  Network error: {e} -- retrying in 60s")
            time.sleep(60)

        except Exception as e:
            log_error(f"  Unexpected error: {e}")
            import traceback; traceback.print_exc()
            time.sleep(30)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Aster DEX funding-rate farmer (perp longs, optional HL hedge)."
    )
    ap.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Exit after N completed poll cycles (after interval sleep). "
            "0 = run until interrupted (default). Use 1–3 for paper/live staging checks."
        ),
    )
    cli = ap.parse_args()
    if cli.max_cycles < 0:
        ap.error("--max-cycles must be >= 0")
    run(max_cycles=cli.max_cycles)
