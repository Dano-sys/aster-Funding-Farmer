"""config.py — loads settings from .env. Zone 2 is built at runtime."""

import logging
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

_cfg_log = logging.getLogger(__name__)

_root = Path(__file__).resolve().parent
# Load both `.env` and `env` when present; later files override (so `env` can win over `.env`).
for _name in (".env", "env"):
    _p = _root / _name
    if _p.is_file():
        load_dotenv(_p, override=True)
if not any((_root / n).is_file() for n in (".env", "env")):
    load_dotenv()

# Account — [Futures API V3](https://github.com/asterdex/api-docs/blob/master/README.md) (EIP-712) only; legacy V1 API keys are not supported.
# This repo is mainnet-only. Use DRY_RUN=true to validate safely (no live trading actions).
BASE_URL = os.getenv("ASTER_FAPI_BASE", "https://fapi.asterdex.com").rstrip("/")

# Unsigned public market WebSocket (book + last trade).
_futures_ws_url_raw = os.getenv("FUTURES_WS_URL", "").strip()
if _futures_ws_url_raw:
    FUTURES_WS_URL = _futures_ws_url_raw.rstrip("/")
else:
    FUTURES_WS_URL = "wss://fstream.asterdex.com/ws"
FUTURES_WS_ENABLED = os.getenv("FUTURES_WS_ENABLED", "true").lower() == "true"
try:
    FUTURES_WS_FALLBACK_AFTER_SEC = float(os.getenv("FUTURES_WS_FALLBACK_AFTER_SEC", "0"))
except ValueError:
    FUTURES_WS_FALLBACK_AFTER_SEC = 0.0


def _clean_addr(raw: str) -> str:
    s = (raw or "").strip().strip("\ufeff").replace("\r", "").replace("\n", "")
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


def _not_placeholder(s: str) -> bool:
    sl = (s or "").lower()
    return bool(sl.strip()) and "your_" not in sl and "placeholder" not in sl


# Pro API V3 (EIP-712) — paths in code use /fapi/v1|v2 logical names; exchange layer rewrites signed routes to /fapi/v3/.
ASTER_USER = _clean_addr(os.getenv("ASTER_USER", ""))
ASTER_SIGNER = _clean_addr(os.getenv("ASTER_SIGNER", ""))
_pk = _clean_addr(os.getenv("ASTER_SIGNER_PRIVATE_KEY", ""))
if os.getenv("STRIP_0X_PREFIX_FROM_KEYS", "false").lower() == "true" and _pk.startswith(("0x", "0X")):
    _pk = _pk[2:].strip()
ASTER_SIGNER_PRIVATE_KEY = _pk

USE_V3 = (
    _not_placeholder(ASTER_USER)
    and _not_placeholder(ASTER_SIGNER)
    and _not_placeholder(ASTER_SIGNER_PRIVATE_KEY)
)

# Trade (Aster futures /fapi)
SYMBOL      = os.getenv("SYMBOL", "AAVEUSDT")
LEVERAGE    = float(os.getenv("LEVERAGE", 1.5))
WALLET_PCT         = float(os.getenv("WALLET_PCT", 80)) / 100
MARGIN_BUFFER_PCT  = float(os.getenv("MARGIN_BUFFER_PCT", 5)) / 100
ORDER_COUNT        = int(os.getenv("ORDER_COUNT", 6))
# Withhold this fraction of (deployable − buffer) so simultaneous limit IM stays under UM cap (Aster -2019).
try:
    _limhr = float(os.getenv("LADDER_IM_HEADROOM_PCT", "15"))
except ValueError:
    _limhr = 15.0
LADDER_IM_HEADROOM_PCT = max(0.0, min(85.0, _limhr)) / 100.0

# Single balance row (ignored if COLLATERAL_ASSETS is set). Empty = auto fallback USDT → USDF → ASTER.
_collateral = os.getenv("COLLATERAL_ASSET", "").strip().upper()
COLLATERAL_ASSET = _collateral if _collateral else None
# Combine several assets for sizing (USD sum). Easiest for USDF + ASTER: COLLATERAL_ASSETS=USDF,ASTER
_raw_multi = os.getenv("COLLATERAL_ASSETS", "").strip()
if _raw_multi:
    COLLATERAL_ASSETS = list(
        dict.fromkeys(x.strip().upper() for x in _raw_multi.split(",") if x.strip())
    )
else:
    COLLATERAL_ASSETS = None
# For non-stable collateral (e.g. ASTER): futures symbol for ticker → USD (default {ASSET}USDT).
_cps = os.getenv("COLLATERAL_PRICE_SYMBOL", "").strip().upper()
COLLATERAL_PRICE_SYMBOL = _cps if _cps else None

# Ladder sizing: what ``get_balance()`` sums before ``WALLET_PCT`` / buffer.
#   collateral (default) — COLLATERAL_ASSETS / COLLATERAL_ASSET / USDT|USDF|ASTER fallback (see README).
#   all_wallet — sum USD estimate of each asset's wallet balance from /fapi/v2/balance (entire futures wallet).
_bss = os.getenv("BALANCE_SIZING_SCOPE", "collateral").strip().lower()
if _bss in ("all_wallet", "all", "portfolio", "total"):
    BALANCE_SIZING_SCOPE = "all_wallet"
else:
    if _bss not in ("", "collateral"):
        _cfg_log.warning(
            "BALANCE_SIZING_SCOPE=%r invalid (use collateral|all_wallet); defaulting to collateral",
            os.getenv("BALANCE_SIZING_SCOPE", ""),
        )
    BALANCE_SIZING_SCOPE = "collateral"

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
# When DRY_RUN: simulate limit fills vs last price (no POSTs). Set false to only preview ladder.
DRY_RUN_PAPER_FILLS = DRY_RUN and os.getenv("DRY_RUN_PAPER_FILLS", "true").lower() == "true"

# Exits
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", 112.0))
STOP_LOSS   = float(os.getenv("STOP_LOSS", 75.0))

# Drop-then-pump: ratchet stop upward (long). Set arm to 0 to disable each rule.
BREAK_EVEN_ARM_PCT = float(os.getenv("BREAK_EVEN_ARM_PCT", "2"))
BREAK_EVEN_LOCK_PCT = float(os.getenv("BREAK_EVEN_LOCK_PCT", "0.05"))
TRAIL_ARM_PCT = float(os.getenv("TRAIL_ARM_PCT", "2"))
TRAIL_PULLBACK_PCT = float(os.getenv("TRAIL_PULLBACK_PCT", "2"))

# After BAD news: optional full ladder reload to catch a dip (see README).
_bad_mode = os.getenv("BAD_NEWS_DIP_MODE", "off").strip().lower()
BAD_NEWS_DIP_MODE = _bad_mode if _bad_mode in ("off", "reload_ladder") else "off"
BAD_NEWS_RELOAD_COOLDOWN_SEC = int(os.getenv("BAD_NEWS_RELOAD_COOLDOWN_SEC", "900"))
BAD_NEWS_MAX_RELOADS = int(os.getenv("BAD_NEWS_MAX_RELOADS", "3"))
BAD_NEWS_ONLY_IF_FLAT = os.getenv("BAD_NEWS_ONLY_IF_FLAT", "true").lower() == "true"

# After GOOD news (TP/stop already bumped): optional cancel + full ladder reload.
_good_mode = os.getenv("GOOD_NEWS_LADDER_MODE", "off").strip().lower()
GOOD_NEWS_LADDER_MODE = _good_mode if _good_mode in ("off", "reload_ladder") else "off"
GOOD_NEWS_RELOAD_COOLDOWN_SEC = int(os.getenv("GOOD_NEWS_RELOAD_COOLDOWN_SEC", "900"))
GOOD_NEWS_MAX_RELOADS = int(os.getenv("GOOD_NEWS_MAX_RELOADS", "3"))
GOOD_NEWS_ONLY_IF_FLAT = os.getenv("GOOD_NEWS_ONLY_IF_FLAT", "true").lower() == "true"

# Zone 1 — absolute $ from ZONE1_*_PRICE, OR dynamic below spot at ladder-build time (see exchange).


def _comma_floats(s: str) -> list[float]:
    out: list[float] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out


ZONE1_PRICES = []
for i in range(1, 10):
    price = os.getenv(f"ZONE1_{i}_PRICE")
    if price:
        ZONE1_PRICES.append(float(price))

_usd1_raw = os.getenv("ZONE1_BELOW_SPOT_USD", "").strip()
_pct1_raw = os.getenv("ZONE1_BELOW_SPOT_PCT", "").strip()
ZONE1_BELOW_SPOT_USD = _comma_floats(_usd1_raw)
# Precedence: USD list wins; PCT used only when USD is empty (after parse).
ZONE1_BELOW_SPOT_PCT = [] if ZONE1_BELOW_SPOT_USD else _comma_floats(_pct1_raw)
if _usd1_raw and _pct1_raw and ZONE1_BELOW_SPOT_USD:
    _cfg_log.info(
        "ZONE1_BELOW_SPOT_PCT ignored — ZONE1_BELOW_SPOT_USD takes precedence when both are set."
    )
if (ZONE1_BELOW_SPOT_USD or ZONE1_BELOW_SPOT_PCT) and ZONE1_PRICES:
    _cfg_log.warning(
        "ZONE1_*_PRICE is set but dynamic Zone1 (ZONE1_BELOW_SPOT_USD or ZONE1_BELOW_SPOT_PCT) "
        "takes precedence; absolute Zone 1 prices are ignored."
    )

# Zone 2 — spreads above ladder reference at build time (sizes at runtime).
# ZONE2_SPREAD_1..3 default to 5,3,1 if unset (backward compatible). ZONE2_SPREAD_4+ optional; first gap after index 3 stops.
_zone2_defaults = (5.0, 3.0, 1.0)
ZONE2_SPREADS: list[float] = []
for _zi in range(1, 21):
    _raw = os.getenv(f"ZONE2_SPREAD_{_zi}")
    if _raw is None or not _raw.strip():
        if _zi <= 3:
            ZONE2_SPREADS.append(_zone2_defaults[_zi - 1])
        else:
            break
        continue
    try:
        ZONE2_SPREADS.append(float(_raw))
    except ValueError:
        _cfg_log.warning(
            "ZONE2_SPREAD_%d=%r invalid (using default %.4g for i<=3, else stop)",
            _zi,
            _raw,
            _zone2_defaults[_zi - 1] if _zi <= 3 else 0.0,
        )
        if _zi <= 3:
            ZONE2_SPREADS.append(_zone2_defaults[_zi - 1])
        else:
            break

# Points
MIN_HOLD_MINUTES = int(os.getenv("MIN_HOLD_MINUTES", 60))

# Timing — Aster poll is the main loop; news uses NEWS_POLL_SEC (typically much slower).
PRICE_POLL_SEC = int(os.getenv("PRICE_POLL_SEC", 10))
# When false: no RSS/X/Reddit fetches, no startup snapshot, no handle_news (GOOD/BAD/VERY_BAD paths).
NEWS_ENABLED = os.getenv("NEWS_ENABLED", "true").lower() == "true"
NEWS_POLL_SEC = int(os.getenv("NEWS_POLL_SEC", 300))
# When a poll finds zero *new* headlines (see news.fresh_headlines), wait longer before the next poll.
# Set NEWS_POLL_IDLE_MULT=1 to always use NEWS_POLL_SEC (legacy behavior).
try:
    NEWS_POLL_IDLE_MULT = float(os.getenv("NEWS_POLL_IDLE_MULT", "3"))
except ValueError:
    NEWS_POLL_IDLE_MULT = 3.0
NEWS_POLL_IDLE_MULT = max(1.0, NEWS_POLL_IDLE_MULT)
try:
    NEWS_POLL_IDLE_MAX_SEC = int(os.getenv("NEWS_POLL_IDLE_MAX_SEC", "3600"))
except ValueError:
    NEWS_POLL_IDLE_MAX_SEC = 3600
NEWS_POLL_IDLE_MAX_SEC = max(NEWS_POLL_SEC, NEWS_POLL_IDLE_MAX_SEC)

# Live close: poll position until flat or timeout (then log + optional webhook).
CLOSE_VERIFY_TIMEOUT_SEC = int(os.getenv("CLOSE_VERIFY_TIMEOUT_SEC", "30"))
CLOSE_VERIFY_POLL_SEC = float(os.getenv("CLOSE_VERIFY_POLL_SEC", "2"))

# Optional JSON POST (e.g. Slack incoming webhook body {"text": "..."}).
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()

# Main loop: after this many consecutive poll failures, POST alert webhook once (then reset counter).
LOOP_ALERT_AFTER_FAILURES = int(os.getenv("LOOP_ALERT_AFTER_FAILURES", "5"))
# Sleep cap when backing off after errors (base is PRICE_POLL_SEC * 2^min(failures,5)).
LOOP_BACKOFF_MAX_SEC = int(os.getenv("LOOP_BACKOFF_MAX_SEC", "120"))

# After TP/SL (or paper flat), place a new full ladder when flat + no resting SYMBOL orders (live) / no paper rests.
AUTO_RELADDER_ON_FLAT = os.getenv("AUTO_RELADDER_ON_FLAT", "true").lower() == "true"
RELADDER_COOLDOWN_SEC = int(os.getenv("RELADDER_COOLDOWN_SEC", "15"))

# Periodic full ladder replace (live): cancel SYMBOL opens + place_full_ladder so rungs stay near the market.
# 0 = disabled. When |position|>0, only runs if unrealized uPnL (USD) **>** STALE_LADDER_REFRESH_MIN_UPNL_USD (green first).
try:
    STALE_LADDER_REFRESH_SEC = max(0, int(os.getenv("STALE_LADDER_REFRESH_SEC", "0")))
except ValueError:
    STALE_LADDER_REFRESH_SEC = 0
try:
    STALE_LADDER_REFRESH_MIN_UPNL_USD = float(os.getenv("STALE_LADDER_REFRESH_MIN_UPNL_USD", "0"))
except ValueError:
    STALE_LADDER_REFRESH_MIN_UPNL_USD = 0.0

# Skip cancel / close / ladder placement / reload when true (also if TRADING_HALT_FILE exists).
TRADING_HALTED = os.getenv("TRADING_HALTED", "false").lower() == "true"
TRADING_HALT_FILE = os.getenv("TRADING_HALT_FILE", "").strip()

# Live startup: cancel all open orders on SYMBOL before margin/leverage + fresh ladder (no overlap after redeploy).
CANCEL_OPEN_ORDERS_ON_STARTUP = (
    os.getenv("CANCEL_OPEN_ORDERS_ON_STARTUP", "true").lower() == "true"
)
# Live startup: market-flat any open SYMBOL position after cancels (long→sell, short→buy). Destructive — default off locally.
CLOSE_POSITION_ON_STARTUP = (
    os.getenv("CLOSE_POSITION_ON_STARTUP", "false").lower() == "true"
)

# Persist Rh / trail / TP / stop across restarts (live only). Empty = disabled.
BOT_STATE_PATH = os.getenv("BOT_STATE_PATH", "").strip()

# Futures balance logs + all_wallet sum: skip rows below this USD estimate (0 = show/count all).
BALANCE_LOG_DUST_MIN_USD = float(os.getenv("BALANCE_LOG_DUST_MIN_USD", "5.0"))

# News poll log ANSI (good / bad / very bad). Respects https://no-color.org/
_nlc = os.getenv("NEWS_LOG_COLORS", "auto").strip().lower()
if os.getenv("NO_COLOR", "").strip():
    NEWS_LOG_COLORS = False
elif _nlc in ("0", "false", "no", "off"):
    NEWS_LOG_COLORS = False
elif _nlc in ("1", "true", "yes", "on"):
    NEWS_LOG_COLORS = True
else:
    NEWS_LOG_COLORS = sys.stderr.isatty()

# Optional X (Twitter) API v2 recent search — official API only (see README → X).
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()
X_API_KEY = os.getenv("X_API_KEY", "").strip()
X_API_SECRET = os.getenv("X_API_SECRET", "").strip()
# Empty = use built-in default in news.py (Aave official @ + narrative keywords).
X_SEARCH_QUERY = os.getenv("X_SEARCH_QUERY", "").strip()

# Skip Reddit fetches (JSON/RSS) when true — useful if Reddit returns 403 on your network.
NEWS_SKIP_REDDIT = os.getenv("NEWS_SKIP_REDDIT", "false").lower() == "true"
# Optional: full User-Agent string for Reddit only (JSON + Atom RSS). Reddit often 403s
# non-browser UAs from Python; leave unset to use a Mozilla-compatible default with app id.
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "").strip()
try:
    X_MAX_RESULTS = max(10, min(100, int(os.getenv("X_MAX_RESULTS", "10"))))
except ValueError:
    X_MAX_RESULTS = 10

# funding_farmer: prioritize USDT perp symbols seen in recent X posts (requires X credentials + query).
NEWS_SYMBOL_BOOST_ENABLED = (
    os.getenv("NEWS_SYMBOL_BOOST_ENABLED", "false").lower() == "true"
)
try:
    NEWS_SYMBOL_BOOST_TTL_SEC = max(
        60, int(os.getenv("NEWS_SYMBOL_BOOST_TTL_SEC", "21600"))
    )
except ValueError:
    NEWS_SYMBOL_BOOST_TTL_SEC = 21600

_zone1_rung_count = (
    len(ZONE1_BELOW_SPOT_USD)
    if ZONE1_BELOW_SPOT_USD
    else len(ZONE1_BELOW_SPOT_PCT)
    if ZONE1_BELOW_SPOT_PCT
    else len(ZONE1_PRICES)
)
LADDER_RUNGS = _zone1_rung_count + len(ZONE2_SPREADS)

# Dynamic Zone 1: skip rungs with computed price <= this (see exchange._zone1_prices_at_spot).
MIN_ZONE1_PRICE = float(os.getenv("MIN_ZONE1_PRICE", "0.01"))

# Ladder reference for Zone1 offsets + Zone2 spreads: last=ticker price; mid/bid/ask=bookTicker (fallback last).
_zone1_anchor = os.getenv("ZONE1_SPOT_ANCHOR", "last").strip().lower()
if _zone1_anchor in ("last", "mid", "bid", "ask"):
    ZONE1_SPOT_ANCHOR = _zone1_anchor
else:
    _cfg_log.warning(
        "ZONE1_SPOT_ANCHOR=%r invalid (use last|mid|bid|ask); defaulting to last",
        os.getenv("ZONE1_SPOT_ANCHOR", ""),
    )
    ZONE1_SPOT_ANCHOR = "last"
