"""
Aster DEX - Funding Rate Farmer
================================
Strategy:
  - Scans all perp symbols for the highest positive funding rate
  - Goes LONG on the best opportunity (collects funding every 8h)
  - Uses USDF + ASTER tokens as margin (Multi-Asset Mode) for max Stage 6 points
  - Monitors position health, closes if funding flips negative
  - Automatically rotates to better opportunities

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

import os
import csv
import time
from typing import Optional
import logging
import requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from dotenv import load_dotenv
from colorama import init, Fore, Style

load_dotenv()

from aster_client import credentials_ok, get, post
init(autoreset=True)

# --- Logging ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("funding_farmer.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

def log_info(msg):    log.info(Fore.CYAN    + msg + Style.RESET_ALL)
def log_success(msg): log.info(Fore.GREEN   + msg + Style.RESET_ALL)
def log_warn(msg):    log.warning(Fore.YELLOW + msg + Style.RESET_ALL)
def log_error(msg):   log.error(Fore.RED    + msg + Style.RESET_ALL)

# --- Config -------------------------------------------------------------------

# Multi-Asset Mode collateral ratios (BNB Chain, from Aster docs)
# 2000 ASTER @ ~$0.70 = $1,400 * 80% = ~$1,120 effective margin
# USDF is 99.99% -- essentially 1:1, best stablecoin on the platform
ASTER_COLLATERAL_RATIO = 0.80
USDF_COLLATERAL_RATIO  = 0.9999

# Risk params - tune in .env
LEVERAGE          = int(os.getenv("LEVERAGE", "3"))
MIN_FUNDING_RATE  = float(os.getenv("MIN_FUNDING_RATE", "0.0005"))
EXIT_FUNDING_RATE = float(os.getenv("EXIT_FUNDING_RATE", "0.0001"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))
# When any perp long is open, sleep this long between cycles (stop loss + funding exit).
# Much shorter than POLL_INTERVAL_SEC so leveraged moves are re-checked frequently.
RISK_POLL_INTERVAL_SEC = int(os.getenv("RISK_POLL_INTERVAL_SEC", "15"))
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT", "0.05"))
BLACKLIST         = [s for s in os.getenv("BLACKLIST", "").split(",") if s]
TRADE_LOG_FILE    = os.getenv("TRADE_LOG_FILE", "trades.csv")

# Wallet-based sizing
# Bot reads live effective margin each cycle and deploys WALLET_DEPLOY_PCT of it.
# As funding carry grows your balance, every new position automatically scales up.
# e.g. WALLET_DEPLOY_PCT=0.80 -> deploy 80% of your effective margin
WALLET_DEPLOY_PCT = float(os.getenv("WALLET_DEPLOY_PCT", "0.80"))
# Never deploy more than this absolute cap (safety ceiling, 0 = no cap)
WALLET_MAX_USD    = float(os.getenv("WALLET_MAX_USD", "0"))
# Never deploy less than this (avoids tiny below-minimum positions)
WALLET_MIN_USD    = float(os.getenv("WALLET_MIN_USD", "20"))

# Dry run mode
# true  = live API reads (rates, wallet, positions) but NO orders are placed
#         Simulated positions are tracked in memory so the full open->hold->close
#         cycle runs exactly as it would live. Safe to run with real API keys.
# false = live trading (default)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
# Dry run: override effective margin for sizing (default 2000 USDT when DRY_RUN if unset). Set to 0 for live wallet totals.
_drs = os.getenv("DRY_RUN_SIMULATED_MARGIN_USD")
if _drs is None or str(_drs).strip() == "":
    DRY_RUN_SIMULATED_MARGIN_USD = float(2000 if DRY_RUN else 0)
else:
    DRY_RUN_SIMULATED_MARGIN_USD = float(str(_drs).strip())

# Delta-neutral mode
# Set to true to enable the Hyperliquid hedge leg (requires HL_PRIVATE_KEY etc. in .env)
# When false (default), bot runs Aster-only funding farm with no HL connection
DELTA_NEUTRAL = os.getenv("DELTA_NEUTRAL", "false").lower() == "true"

# Mark-price WebSocket: push-based stop vs entry (faster than REST alone; requires websocket-client)
MARK_PRICE_WS = os.getenv("MARK_PRICE_WS", "true").lower() == "true"
# Log best bid / ask / mid (+ mark) for open positions and top funding symbols (extra REST calls)
SHOW_BOOK_IN_LOGS = os.getenv("SHOW_BOOK_IN_LOGS", "true").lower() == "true"

# Diversification params
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

# --- Trade Logger -------------------------------------------------------------

TRADE_CSV_HEADERS = [
    "timestamp_utc", "action", "symbol", "order_id",
    "quantity", "price", "notional_usdt",
    "funding_rate_8h", "funding_apr_pct",
    "entry_price", "exit_price", "pnl_usdt", "pnl_pct",
    "hold_duration_min", "close_reason",
]

# In-memory store of open trades for PnL on close
# { symbol: { entry_price, quantity, open_time, funding_rate } }
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

def _ensure_csv():
    """Create trades.csv with headers if it does not exist yet."""
    if not os.path.exists(TRADE_LOG_FILE):
        with open(TRADE_LOG_FILE, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=TRADE_CSV_HEADERS).writeheader()

def _append_csv(row: dict):
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_CSV_HEADERS)
        writer.writerow({h: row.get(h, "") for h in TRADE_CSV_HEADERS})

def log_trade_open(symbol: str, order: dict, quantity: float,
                   entry_price: float, funding_rate: float):
    """Record an opening trade and cache entry data for PnL on close."""
    _ensure_csv()
    notional = quantity * entry_price
    apr      = funding_rate * 3 * 365 * 100
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
    })
    _open_trades[symbol] = {
        "entry_price":  entry_price,
        "quantity":     quantity,
        "open_time":    time.time(),
        "funding_rate": funding_rate,
    }
    log_success(f"  [TRADE LOG] OPEN  {symbol}  qty={quantity:.6f}"
                f"  @ {entry_price:.4f}  funding={funding_rate*100:.4f}%/8h")

def log_trade_close(symbol: str, order: dict, quantity: float,
                    exit_price: float, close_reason: str):
    """Record a closing trade with full PnL calculation."""
    _ensure_csv()
    entry_data   = _open_trades.pop(symbol, {})
    entry_price  = entry_data.get("entry_price", 0.0)
    open_time    = entry_data.get("open_time", time.time())
    funding_rate = entry_data.get("funding_rate", 0.0)

    notional   = quantity * exit_price
    hold_mins  = round((time.time() - open_time) / 60, 1)
    pnl_usdt   = round((exit_price - entry_price) * quantity, 4) if entry_price else ""
    pnl_pct    = round((exit_price - entry_price) / entry_price * 100, 4) if entry_price else ""
    apr        = funding_rate * 3 * 365 * 100

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
        "pnl_usdt":          pnl_usdt,
        "pnl_pct":           pnl_pct,
        "hold_duration_min": hold_mins,
        "close_reason":      close_reason,
    })
    pnl_str = f"${pnl_usdt:+.4f} ({pnl_pct:+.2f}%)" if entry_price else "n/a"
    log_success(f"  [TRADE LOG] CLOSE {symbol}  qty={quantity:.6f}"
                f"  @ {exit_price:.4f}  pnl={pnl_str}"
                f"  held={hold_mins}m  reason={close_reason}")

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
        result = get("/fapi/v1/multiAssetsMargin", signed=True)
        if result.get("multiAssetsMargin") is True:
            log_success("  [OK] Multi-Asset Mode already active")
            return
    except Exception:
        pass

    try:
        post("/fapi/v1/multiAssetsMargin", {"multiAssetsMargin": "true"})
        log_success("  [OK] Multi-Asset Mode ENABLED -- ASTER + USDF both count as margin")
    except RuntimeError as e:
        if "No need" in str(e):
            log_success("  [OK] Multi-Asset Mode already active")
        else:
            log_warn(f"  [!!] Could not enable Multi-Asset Mode: {e}")
            log_warn("       Deposit ASTER/USDF to your Aster account on BNB Chain first")

def get_collateral_summary() -> dict:
    """Fetch balances and compute effective margin for ASTER, USDF, and USDT."""
    try:
        data = get("/fapi/v2/balance", signed=True)
    except Exception as e:
        log_error(f"  Could not fetch balance: {e}")
        return {}

    summary = {}
    for b in data:
        asset = b["asset"]
        bal   = float(b.get("balance", 0))
        if bal <= 0:
            continue
        if asset == "ASTER":
            summary["ASTER"] = {"balance": bal, "effective_usdt": bal * ASTER_COLLATERAL_RATIO}
        elif asset == "USDF":
            summary["USDF"]  = {"balance": bal, "effective_usdt": bal * USDF_COLLATERAL_RATIO}
        elif asset == "USDT":
            summary["USDT"]  = {"balance": bal, "effective_usdt": bal}

    summary["_total_effective_margin"] = sum(
        v["effective_usdt"] for k, v in summary.items() if not k.startswith("_")
    )
    if DRY_RUN and DRY_RUN_SIMULATED_MARGIN_USD > 0:
        summary["_total_effective_margin"] = float(DRY_RUN_SIMULATED_MARGIN_USD)
        summary["_dry_run_simulated_margin"] = True
    return summary

def get_aster_price() -> float:
    """Current ASTER mark price in USDT."""
    try:
        data = get("/fapi/v1/premiumIndex", {"symbol": "ASTERUSDT"})
        return float(data.get("markPrice", 0))
    except Exception:
        return 0.0

# --- Market Data --------------------------------------------------------------

def get_all_funding_rates() -> list:
    """All perps sorted by funding rate descending."""
    data = get("/fapi/v1/premiumIndex")
    results = []
    for item in data:
        try:
            results.append({
                "symbol":          item["symbol"],
                "fundingRate":     float(item["lastFundingRate"]),
                "nextFundingTime": item["nextFundingTime"],
                "markPrice":       float(item["markPrice"]),
            })
        except (KeyError, ValueError):
            continue
    return sorted(results, key=lambda x: x["fundingRate"], reverse=True)

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
    data = get("/fapi/v2/positionRisk", signed=True)
    return [p for p in data if float(p.get("positionAmt", 0)) != 0]

def set_leverage(symbol: str, leverage: int):
    if DRY_RUN:
        log_warn(f"  [DRY RUN] Would set leverage {leverage}x  [{symbol}]")
        return
    post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
    log_info(f"  Leverage -> {leverage}x  [{symbol}]")

def set_cross_margin(symbol: str):
    if DRY_RUN:
        log_warn(f"  [DRY RUN] Would set CROSS margin  [{symbol}]")
        return
    try:
        post("/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSSED"})
        log_info(f"  Margin -> CROSS  [{symbol}]")
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


def open_long(symbol: str, notional_usdt: float, exchange_info: dict,
              funding_rate: float = 0.0) -> dict:
    mark    = get_mark_price(symbol)
    step    = exchange_info.get(symbol, {}).get("stepSize", "0.001")
    qty_str = perp_qty_meets_min_notional(notional_usdt, mark, step)
    qty     = float(qty_str)

    if DRY_RUN:
        order_id = _dry_order_id(symbol)
        log_warn(f"  [DRY RUN] LONG {symbol}  qty={qty_str}  ~${notional_usdt:.0f}"
                 f"  @ {mark:.4f}  orderId={order_id}")
        order = {"orderId": order_id, "status": "DRY_RUN"}
        _dry_positions[symbol] = {
            "positionAmt": qty_str,
            "entryPrice":  str(mark),
            "markPrice":   str(mark),
        }
        log_trade_open(symbol, order, qty, mark, funding_rate)
        return order

    log_info(f"  Opening LONG {symbol}  qty={qty_str}  ~${notional_usdt:.0f} notional")
    order = post("/fapi/v1/order", {
        "symbol": symbol, "side": "BUY", "type": "MARKET", "quantity": qty_str,
    })
    log_success(f"  [OK] Opened orderId={order.get('orderId')} status={order.get('status')}")
    log_trade_open(symbol, order, qty, mark, funding_rate)
    return order

def close_long(symbol: str, exchange_info: dict, close_reason: str = "manual"):
    # In dry run, read from simulated position store instead of live API
    if DRY_RUN:
        pos = _dry_positions.pop(symbol, None)
        if not pos:
            log_warn(f"  [DRY RUN] No simulated position for {symbol} -- skipping")
            return
        exit_price = get_mark_price(symbol)
        qty        = float(pos["positionAmt"])
        order_id   = _dry_order_id(symbol)
        log_warn(f"  [DRY RUN] CLOSE {symbol}  qty={qty}  @ {exit_price:.4f}"
                 f"  reason={close_reason}  orderId={order_id}")
        order = {"orderId": order_id, "status": "DRY_RUN"}
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
    exit_price = float(pos.get("markPrice", get_mark_price(symbol)))

    log_info(f"  Closing LONG {symbol}  qty={qty_str}  reason={close_reason}")
    order = post("/fapi/v1/order", {
        "symbol": symbol, "side": "SELL", "type": "MARKET",
        "quantity": qty_str, "reduceOnly": "true",
    })
    log_success(f"  [OK] Closed orderId={order.get('orderId')} status={order.get('status')}")
    log_trade_close(symbol, order, qty, exit_price, close_reason)

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

def portfolio_summary(open_symbols: set, rates: list, sizes: dict) -> str:
    """Build a compact one-line portfolio summary for the status display."""
    lines = []
    total_notional = 0.0
    weighted_apr   = 0.0
    for sym in sorted(open_symbols):
        info    = next((r for r in rates if r["symbol"] == sym), {})
        rate    = info.get("fundingRate", 0)
        notional = sizes.get(sym, 0)
        apr      = rate * 3 * 365 * 100
        total_notional += notional
        weighted_apr   += apr * notional
        lines.append(f"    {sym:<14} {rate*100:+.4f}%/8h  ${notional:.0f}")
    avg_apr = weighted_apr / total_notional if total_notional else 0
    header  = (f"  Portfolio: {len(open_symbols)} positions  "
               f"~${total_notional:.0f} deployed  "
               f"blended APR={avg_apr:.1f}%")
    return header + "\n" + "\n".join(lines)

def compute_deploy_budget(collateral: dict) -> float:
    """
    Compute total notional budget for this cycle based on live wallet balance.

    budget = effective_margin * WALLET_DEPLOY_PCT
    Clamped to [WALLET_MIN_USD, WALLET_MAX_USD] if limits are set.
    Already-deployed capital (open position_sizes) is subtracted so we only
    size NEW positions from the remaining available budget.
    """
    total_margin = collateral.get("_total_effective_margin", 0.0)
    budget = total_margin * WALLET_DEPLOY_PCT
    if WALLET_MAX_USD > 0:
        budget = min(budget, WALLET_MAX_USD)
    budget = max(budget, 0.0)
    return budget

def available_budget(total_budget: float, position_sizes: dict) -> float:
    """Budget remaining after accounting for already-open positions."""
    deployed = sum(position_sizes.values())
    return max(total_budget - deployed, 0.0)

# --- Main ---------------------------------------------------------------------

def print_startup_banner(collateral: dict, aster_price: float):
    log_success("=" * 62)
    if DRY_RUN:
        log_warn("  ⚠️  DRY RUN MODE — NO REAL ORDERS WILL BE PLACED  ⚠️")
        log_warn("  Reads live rates + wallet. Simulates opens/closes in memory.")
        log_warn("  Set DRY_RUN=false in .env when ready to go live.")
        if DRY_RUN_SIMULATED_MARGIN_USD > 0:
            log_warn(
                f"  Position sizing uses simulated margin ${DRY_RUN_SIMULATED_MARGIN_USD:.0f} "
                f"(DRY_RUN_SIMULATED_MARGIN_USD). Set to 0 to size from live API margin instead."
            )
        log_success("=" * 62)
    log_success("  Aster Funding Rate Farmer  --  Multi-Asset Margin Mode")
    log_success("=" * 62)

    a = collateral.get("ASTER", {})
    u = collateral.get("USDF", {})

    if a:
        aster_usd = a["balance"] * aster_price if aster_price else 0
        log_success(f"  ASTER  {a['balance']:.0f} tokens  ~${aster_usd:.0f} value"
                    f"  ->  ${a['effective_usdt']:.0f} effective margin (80%)")
    else:
        log_warn("  ASTER  not found -- deposit ASTER tokens for extra points")

    if u:
        log_success(f"  USDF   ${u['balance']:.2f}  ->  ${u['effective_usdt']:.2f} effective (99.99%)")
    else:
        log_warn("  USDF   not found -- mint/buy USDF for best margin efficiency")

    total = collateral.get("_total_effective_margin", 0)
    log_success(f"  Total effective margin (for sizing):  ~${total:.0f}")
    if DRY_RUN and collateral.get("_dry_run_simulated_margin"):
        log_info("    ^ simulated for dry run — ASTER/USDF lines above are live account reads")
    deploy_budget = compute_deploy_budget(collateral)
    log_success(f"  Deploy budget:  ${deploy_budget:.0f}  "
                f"({WALLET_DEPLOY_PCT*100:.0f}% of margin  x{LEVERAGE} leverage)")
    if WALLET_MAX_USD > 0:
        log_success(f"  Budget ceiling: ${WALLET_MAX_USD:.0f}")
    log_success(f"  Min funding:  {MIN_FUNDING_RATE*100:.4f}%/8h  "
                f"({MIN_FUNDING_RATE*3*365*100:.1f}% APR floor)")
    log_success(f"  Max positions: {MAX_POSITIONS}")
    log_success(f"  Poll: {POLL_INTERVAL_SEC}s idle  |  {RISK_POLL_INTERVAL_SEC}s when positions open (risk)")
    log_success(f"  MARK_PRICE_WS:  {MARK_PRICE_WS}")
    log_success(f"  SHOW_BOOK_IN_LOGS:  {SHOW_BOOK_IN_LOGS}")
    dn_status = "ENABLED (set DELTA_NEUTRAL=false to disable)" if DELTA_NEUTRAL else "disabled (set DELTA_NEUTRAL=true to enable HL hedge)"
    log_success(f"  Delta-neutral: {dn_status}")
    log_success("=" * 62)

def run():
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
            open_symbols.add(p["symbol"])
            log_info(f"  Recovered: {p['symbol']}  amt={p['positionAmt']}")

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

    while True:
        try:
            log_info("\n Scanning funding rates...")
            rates = get_all_funding_rates()

            # Live wallet sizing — recompute every cycle so growth is captured
            collateral    = get_collateral_summary()
            aster_price   = get_aster_price()
            total_budget  = compute_deploy_budget(collateral)
            avail_budget  = available_budget(total_budget, position_sizes)
            margin_total  = collateral.get("_total_effective_margin", 0)
            log_info(f"  Wallet: ${margin_total:.0f} effective margin  "
                     f"-> ${total_budget:.0f} deploy budget  "
                     f"(${avail_budget:.0f} available)")

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

            # Exit on funding flip
            for sym in list(open_symbols):
                info = next((r for r in rates if r["symbol"] == sym), None)
                rate = info["fundingRate"] if info else None
                if rate is None or rate < EXIT_FUNDING_RATE:
                    rate_str = f"{rate*100:.4f}%/8h" if rate is not None else "N/A"
                    log_warn(f"  Funding dropped [{sym}] -> {rate_str}")
                    close_long(sym, exchange_info, close_reason="funding_dropped")
                    if DELTA_NEUTRAL and hl_info:
                        hl_close_short(hl_info, hl_exchange, hl_address,
                                       sym.replace("USDT",""), "funding_dropped")
                    open_symbols.discard(sym)
                    position_sizes.pop(sym, None)

            # Open new positions (rank-weighted diversification)
            slots = MAX_POSITIONS - len(open_symbols)
            if slots > 0:
                # Build candidate list incrementally so correlation guard
                # sees symbols selected earlier in the same cycle
                pending: set = set()
                raw_candidates = []
                for r in rates:
                    if len(raw_candidates) >= slots:
                        break
                    sym = r["symbol"]
                    if (r["fundingRate"] < MIN_FUNDING_RATE
                            or sym in open_symbols
                            or sym in BLACKLIST
                            or sym not in exchange_info
                            or is_correlated(sym, open_symbols | pending)):
                        continue
                    raw_candidates.append(r)
                    pending.add(sym)

                # Rank-weighted sizing from live wallet budget
                if avail_budget < WALLET_MIN_USD:
                    log_warn(f"  Available budget ${avail_budget:.0f} below "
                             f"minimum ${WALLET_MIN_USD:.0f} -- skipping new opens")
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
                    apr      = rate * 3 * 365 * 100
                    mins     = max(0, (c["nextFundingTime"] - int(time.time()*1000)) // 60000)
                    log_success(f"\n  Target: {sym}  {rate*100:.4f}%/8h  "
                                f"({apr:.1f}% APR)  ${notional:.0f} notional  "
                                f"next funding {mins}m")
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
                            br = rates[0]
                            bf = float(br["fundingRate"])
                            if bf < MIN_FUNDING_RATE:
                                log_warn(
                                    f"  No qualifying opportunities — best funding is "
                                    f"{br['symbol']} at {bf*100:.4f}%/8h, below MIN_FUNDING_RATE "
                                    f"{MIN_FUNDING_RATE*100:.4f}%/8h (lower MIN_FUNDING_RATE in .env to enter)"
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

            # Status
            if open_symbols:
                log_info("\n" + portfolio_summary(open_symbols, rates, position_sizes))
                if SHOW_BOOK_IN_LOGS:
                    log_info("  Book (bid / ask / mid, mark from index):")
                    for sym in sorted(open_symbols):
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
            else:
                log_info("  No open positions")
                if DRY_RUN:
                    log_info(
                        "  (Dry run: simulated longs only appear after a ‘Target:’ open; "
                        f"same rules as live — funding ≥ MIN_FUNDING_RATE {MIN_FUNDING_RATE*100:.4f}%/8h "
                        f"and budget ≥ WALLET_MIN_USD ${WALLET_MIN_USD:.0f})"
                    )
            log_info("  Top 5 rates (all symbols):")
            for r in rates[:5]:
                sym = r["symbol"]
                flag = "[IN]" if sym in open_symbols else "    "
                fr = r["fundingRate"]
                mark = float(r.get("markPrice") or 0)
                extra = ""
                if SHOW_BOOK_IN_LOGS:
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
                log_info(
                    f"  {flag} {sym:<14} "
                    f"{fr*100:+.4f}%/8h  "
                    f"({fr*3*365*100:.0f}% APR){extra}"
                )

            # Wallet snapshot (already fetched at top of cycle)
            a = collateral.get("ASTER", {})
            u = collateral.get("USDF", {})
            if a and aster_price:
                log_info(f"\n  ASTER: {a['balance']:.0f} tokens  "
                         f"~${a['balance']*aster_price:.0f}  "
                         f"(${a['effective_usdt']:.0f} margin)")
            if u:
                log_info(f"  USDF:  ${u['balance']:.2f}")
            log_info(f"  Wallet: ${margin_total:.0f} effective  "
                     f"-> ${total_budget:.0f} budget  "
                     f"({WALLET_DEPLOY_PCT*100:.0f}% deployed)")

            if has_risk_exposure(open_symbols):
                sleep_sec = RISK_POLL_INTERVAL_SEC
                log_info(
                    f"\n  Sleeping {sleep_sec}s (risk poll — open position(s), "
                    f"stop loss {STOP_LOSS_PCT*100:.1f}% vs entry)..."
                )
            else:
                sleep_sec = POLL_INTERVAL_SEC
                log_info(f"\n  Sleeping {sleep_sec}s (scan interval, no positions)...")
            time.sleep(sleep_sec)

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
    run()
