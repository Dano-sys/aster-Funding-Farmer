"""
exchange.py — Aster futures REST (/fapi)

Signed requests use **Pro API V3 (EIP-712)** only (`ASTER_USER`, `ASTER_SIGNER`, `ASTER_SIGNER_PRIVATE_KEY`).
Per Aster docs, V3 is the supported path for new integrations: https://github.com/asterdex/api-docs/blob/master/README.md

Futures V3 spec: https://github.com/asterdex/api-docs/blob/master/V3(Recommended)/EN/aster-finance-futures-api-v3.md
"""

import json
import logging
import math
import os
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from config import (
    ALERT_WEBHOOK_URL,
    ASTER_SIGNER,
    ASTER_SIGNER_PRIVATE_KEY,
    ASTER_USER,
    BALANCE_LOG_DUST_MIN_USD,
    BALANCE_SIZING_SCOPE,
    BASE_URL,
    CLOSE_VERIFY_POLL_SEC,
    CLOSE_VERIFY_TIMEOUT_SEC,
    COLLATERAL_ASSET,
    COLLATERAL_ASSETS,
    COLLATERAL_PRICE_SYMBOL,
    FUTURES_WS_ENABLED,
    FUTURES_WS_FALLBACK_AFTER_SEC,
    FUTURES_WS_URL,
    LADDER_IM_HEADROOM_PCT,
    LADDER_RUNGS,
    LEVERAGE,
    MIN_ZONE1_PRICE,
    ORDER_COUNT,
    SYMBOL,
    TRADING_HALTED,
    TRADING_HALT_FILE,
    USE_V3,
    WALLET_PCT,
    MARGIN_BUFFER_PCT,
    ZONE1_BELOW_SPOT_PCT,
    ZONE1_BELOW_SPOT_USD,
    ZONE1_PRICES,
    ZONE1_SPOT_ANCHOR,
    ZONE2_SPREADS,
)

try:
    import websocket as _websocket  # websocket-client
except ImportError:
    _websocket = None  # type: ignore[misc, assignment]

log = logging.getLogger(__name__)


def trading_halted_now() -> bool:
    """True when TRADING_HALTED env is set, or TRADING_HALT_FILE exists (re-checked each call)."""
    if TRADING_HALTED:
        return True
    if TRADING_HALT_FILE:
        try:
            if Path(TRADING_HALT_FILE).is_file():
                return True
        except OSError:
            pass
    return False


def notify_alert(message: str) -> None:
    """POST JSON `{"text": ...}` to ALERT_WEBHOOK_URL when set (Slack-compatible)."""
    if not ALERT_WEBHOOK_URL:
        return
    try:
        r = requests.post(
            ALERT_WEBHOOK_URL,
            json={"text": message},
            headers={"Content-Type": "application/json"},
            timeout=8,
        )
        if r.status_code >= 400:
            log.warning("Alert webhook HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Alert webhook failed: %s", e)


def _require_v3_credentials() -> None:
    if not USE_V3:
        raise RuntimeError(
            "Pro API V3 is required: set ASTER_USER, ASTER_SIGNER, and "
            "ASTER_SIGNER_PRIVATE_KEY in env (see env.example). "
            "https://github.com/asterdex/api-docs/blob/master/README.md"
        )


# ── Pro API V3 (EIP-712) ─────────────────────────────────────────────────────

EIP712_TYPED_DATA = {
    "types": {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "Message": [{"name": "msg", "type": "string"}],
    },
    "primaryType": "Message",
    "domain": {
        "name": "AsterSignTransaction",
        "version": "1",
        "chainId": 1666,
        "verifyingContract": "0x0000000000000000000000000000000000000000",
    },
    "message": {"msg": ""},
}

_nonce_lock = threading.Lock()
_last_nonce_us = 0
_nonce_i = 0


def _micro_nonce() -> int:
    """Microseconds since epoch (+ tiny counter); must stay within ~10s of server time (V3 spec)."""
    global _last_nonce_us, _nonce_i
    with _nonce_lock:
        now_us = int(time.time() * 1_000_000)
        if now_us == _last_nonce_us:
            _nonce_i += 1
        else:
            _last_nonce_us = now_us
            _nonce_i = 0
        return now_us + _nonce_i


def _raise_for_aster(r: requests.Response) -> None:
    try:
        r.raise_for_status()
    except requests.HTTPError:
        log.error("Aster HTTP %s %s — %s", r.status_code, r.url.split("?", 1)[0], r.text[:2000])
        raise


def _v3_addr(addr: str) -> str:
    from eth_utils import to_checksum_address

    return to_checksum_address(addr.strip())


def _sign_v3_payload(param_str: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    data = {
        "types": EIP712_TYPED_DATA["types"],
        "primaryType": EIP712_TYPED_DATA["primaryType"],
        "domain": EIP712_TYPED_DATA["domain"],
        "message": {"msg": param_str},
    }
    signable = encode_typed_data(full_message=data)
    pk = (
        ASTER_SIGNER_PRIVATE_KEY
        if ASTER_SIGNER_PRIVATE_KEY.startswith("0x")
        else "0x" + ASTER_SIGNER_PRIVATE_KEY
    )
    acct = Account.from_key(pk)
    return acct.sign_message(signable).signature.hex()


def _http_headers(extra: Optional[dict] = None) -> dict:
    h = {"User-Agent": os.getenv("ASTER_HTTP_USER_AGENT", "PythonApp/1.0")}
    if extra:
        h.update(extra)
    return h


def _fapi_signed_path(path: str) -> str:
    """Logical /fapi/v1|v2 paths from docs → /fapi/v3 for signed Pro API requests."""
    return path.replace("/fapi/v1/", "/fapi/v3/").replace("/fapi/v2/", "/fapi/v3/")


def _signed_get(path: str, params: Optional[dict] = None) -> dict:
    _require_v3_credentials()
    params = params or {}
    pth = _fapi_signed_path(path)
    url = f"{BASE_URL.rstrip('/')}{pth}"
    body = dict(params)
    body["nonce"] = str(_micro_nonce())
    body["user"] = _v3_addr(ASTER_USER)
    body["signer"] = _v3_addr(ASTER_SIGNER)
    param_str = urlencode(body)
    sig = _sign_v3_payload(param_str)
    r = requests.get(
        f"{url}?{param_str}&signature={sig}",
        headers=_http_headers(),
        timeout=10,
    )
    _raise_for_aster(r)
    return r.json()


def _signed_post(path: str, params: dict) -> dict:
    _require_v3_credentials()
    pth = _fapi_signed_path(path)
    url = f"{BASE_URL.rstrip('/')}{pth}"
    body = dict(params)
    body["nonce"] = str(_micro_nonce())
    body["user"] = _v3_addr(ASTER_USER)
    body["signer"] = _v3_addr(ASTER_SIGNER)
    param_str = urlencode(body)
    body["signature"] = _sign_v3_payload(param_str)
    r = requests.post(
        url,
        data=body,
        headers=_http_headers({"Content-Type": "application/x-www-form-urlencoded"}),
        timeout=10,
    )
    _raise_for_aster(r)
    return r.json()


def _signed_delete(path: str, params: dict) -> dict:
    _require_v3_credentials()
    pth = _fapi_signed_path(path)
    url = f"{BASE_URL.rstrip('/')}{pth}"
    body = dict(params)
    body["nonce"] = str(_micro_nonce())
    body["user"] = _v3_addr(ASTER_USER)
    body["signer"] = _v3_addr(ASTER_SIGNER)
    param_str = urlencode(body)
    sig = _sign_v3_payload(param_str)
    r = requests.delete(
        f"{url}?{param_str}&signature={sig}",
        headers=_http_headers(),
        timeout=10,
    )
    _raise_for_aster(r)
    return r.json() if r.text else {}


# ── Public signed request helpers (multi-symbol) ──────────────────────────────

def signed_get(path: str, params: Optional[dict] = None) -> Any:
    """
    Signed GET for arbitrary futures path (V3 EIP-712).
    Useful for multi-symbol bots that don't want the single-SYMBOL helpers below.
    """
    return _signed_get(path, params or {})


def signed_post(path: str, params: dict) -> Any:
    """Signed POST for arbitrary futures path (V3 EIP-712)."""
    return _signed_post(path, params)


def signed_delete(path: str, params: dict) -> Any:
    """Signed DELETE for arbitrary futures path (V3 EIP-712)."""
    return _signed_delete(path, params)


def place_market_order_raw(
    *,
    symbol: str,
    side: str,
    quantity: str,
    reduce_only: bool = False,
) -> dict:
    """
    Place a MARKET order with quantity already formatted (string).
    Does not consult exchangeInfo filters — caller must ensure step/min notional.
    """
    p: dict = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
        "positionSide": "BOTH",
    }
    if reduce_only:
        p["reduceOnly"] = "true"
    j = _signed_post("/fapi/v1/order", p)
    return j if isinstance(j, dict) else {"raw": j}


def get_position_for_symbol(symbol: str) -> tuple[float, float, float, float]:
    """
    positionRisk row for symbol → (positionAmt, entryPrice, markPrice, unrealizedProfit).
    Best-effort across Binance-style field variants.
    """
    raw = _signed_get("/fapi/v2/positionRisk", {"symbol": symbol})
    if not isinstance(raw, list):
        return 0.0, 0.0, 0.0, 0.0
    for pos in raw:
        if str(pos.get("symbol")) != symbol:
            continue
        amt = float(pos.get("positionAmt") or 0)
        entry = float(pos.get("entryPrice") or 0)
        mark = float(pos.get("markPrice") or 0)
        upnl = float(pos.get("unRealizedProfit") or pos.get("unrealizedProfit") or 0)
        return amt, entry, mark, upnl
    return 0.0, 0.0, 0.0, 0.0


def flatten_position_for_symbol(symbol: str, reason: str) -> bool:
    """
    Verified flatten for an arbitrary symbol (long→SELL reduceOnly, short→BUY).
    Uses the same poll/chase pattern as single-SYMBOL `flatten_position()`.
    """
    amt, _, _, _ = get_position_for_symbol(symbol)
    if abs(float(amt)) <= 1e-12:
        log.info("flatten_position_for_symbol: already flat %s (%s)", symbol, reason)
        return True

    def _poll_rem() -> float:
        a2, _, _, _ = get_position_for_symbol(symbol)
        return float(a2)

    legs: List[Tuple[float, Any]] = []
    deadline = time.time() + max(5, CLOSE_VERIFY_TIMEOUT_SEC)
    eps = 1e-8

    if amt > 0:
        side = "SELL"
        reduce_only = True
    else:
        side = "BUY"
        reduce_only = False

    rem0 = abs(float(amt))
    try:
        res = place_market_order_raw(
            symbol=symbol,
            side=side,
            quantity=_format_order_qty(rem0),
            reduce_only=reduce_only,
        )
        legs.append((rem0, res.get("orderId")))
    except Exception as e:
        log.error("flatten_position_for_symbol initial order failed: %s", e, exc_info=True)
        notify_alert(f"aster: flatten failed {symbol} ({reason}): {e}")
        return False

    while time.time() < deadline:
        time.sleep(max(0.3, CLOSE_VERIFY_POLL_SEC))
        rem = _poll_rem()
        r = abs(float(rem))
        if r <= eps:
            log.info(
                "CLOSED %s — %s (verified flat) · legs=%s",
                symbol,
                reason,
                _fmt_market_legs(legs),
            )
            return True
        try:
            log.warning(
                "flatten_position_for_symbol: chasing %s remainder %.6g (%s)",
                symbol,
                r,
                reason,
            )
            res = place_market_order_raw(
                symbol=symbol,
                side=side,
                quantity=_format_order_qty(r),
                reduce_only=reduce_only,
            )
            legs.append((r, res.get("orderId")))
        except Exception as e:
            log.error("flatten_position_for_symbol chase failed: %s", e, exc_info=True)
            notify_alert(f"aster: flatten chase failed {symbol} ({reason}): {e}")
            return False

    rem = _poll_rem()
    if abs(float(rem)) <= eps:
        log.info(
            "CLOSED %s — %s (verified flat after final check) · legs=%s",
            symbol,
            reason,
            _fmt_market_legs(legs),
        )
        return True
    log.error(
        "CLOSE VERIFY FAILED — %s still size=%.6g after %ss — %s",
        symbol,
        rem,
        CLOSE_VERIFY_TIMEOUT_SEC,
        reason,
    )
    notify_alert(
        f"aster: CLOSE VERIFY FAILED {symbol} size={rem} after {CLOSE_VERIFY_TIMEOUT_SEC}s ({reason})"
    )
    return False


# ── MARKET DATA ───────────────────────────────────────────────────────────────

# Futures wallet rows treated as ~1 USD per unit (no {ASSET}USDT ticker needed).
_STABLE_COLLATERAL = frozenset(
    {
        "USDT",
        "USDF",
        "USDC",
        "BUSD",
        "FDUSD",
        "TUSD",
        "USDP",
        "PYUSD",
        "DAI",
    }
)
# When falling back to “any stable in wallet”, prefer this order (first hit wins).
_STABLE_BALANCE_FALLBACK_ORDER = (
    "USDT",
    "USDF",
    "USDC",
    "BUSD",
    "FDUSD",
    "TUSD",
    "USDP",
    "PYUSD",
    "DAI",
)


def _balance_log_use_color() -> bool:
    if os.getenv("NO_COLOR", "").strip():
        return False
    # Logging may be attached to stdout or stderr depending on handler.
    return sys.stdout.isatty() or sys.stderr.isatty()


def _fmt_upnl_colored(upnl: float) -> str:
    """Green only for clearly positive uPnL, red only for clearly negative; ~0 is uncolored."""
    s = f"{upnl:+.2f}"
    if not _balance_log_use_color():
        return f"${s}"
    if upnl > 1e-6:
        return f"\033[92m${s}\033[0m"
    if upnl < -1e-6:
        return f"\033[91m${s}\033[0m"
    return f"${s}"


def format_colored_upnl(upnl: float) -> str:
    """Green/red for clearly +/- (e.g. realized legs). Prefer ``format_unrealized_upnl`` for open-position mark PnL."""
    return _fmt_upnl_colored(upnl)


def format_unrealized_upnl(upnl: float) -> str:
    """
    Open-position mark-to-market (long: (mark − entry) × size). Not realized until you sell/reduce;
    shown without profit-green framing so it is not confused with locked-in gains.
    """
    return f"${float(upnl):+.2f}"


# ── Futures market WebSocket (unsigned) + REST fallback ───────────────────────

_ws_lock = threading.Lock()
_ws_last_px: Optional[float] = None  # from aggTrade "p"
_ws_book: Optional[dict] = None  # normalized like get_book_ticker REST
_ws_last_event_ts = 0.0
_ws_connected = False
_ws_thread: Optional[threading.Thread] = None
_ws_should_run = False
_ws_sub_id = 1
_ws_log_rest_once_ts = 0.0


def _ws_events_from_raw(message: str) -> List[dict]:
    try:
        j = json.loads(message)
    except json.JSONDecodeError:
        return []
    if isinstance(j, dict) and isinstance(j.get("data"), dict):
        return [j["data"]]
    if isinstance(j, dict) and j.get("e"):
        return [j]
    return []


def _ws_apply_event(d: dict) -> None:
    global _ws_last_px, _ws_book, _ws_last_event_ts
    ev = d.get("e")
    if ev == "aggTrade":
        try:
            px = float(d["p"])
        except (KeyError, TypeError, ValueError):
            return
        with _ws_lock:
            _ws_last_px = px
            _ws_last_event_ts = time.time()
        return
    if ev == "bookTicker":
        try:
            sym = str(d.get("s", SYMBOL))
            bid = float(d["b"])
            ask = float(d["a"])
            bid_qty = float(d["B"])
            ask_qty = float(d["A"])
        except (KeyError, TypeError, ValueError):
            return
        row = {
            "symbol": sym,
            "bid": bid,
            "ask": ask,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "spread": ask - bid,
            "mid": (bid + ask) / 2.0,
        }
        with _ws_lock:
            _ws_book = row
            _ws_last_event_ts = time.time()


def _ws_on_message(_ws: Any, message: str) -> None:
    if isinstance(message, (bytes, bytearray)):
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError:
            return
    for d in _ws_events_from_raw(message):
        _ws_apply_event(d)


def _ws_on_error(_ws: Any, error: Any) -> None:
    if error:
        log.debug("Futures WS error: %s", error)


def _ws_on_close(_ws: Any, *_args: Any) -> None:
    global _ws_connected
    _ws_connected = False
    log.warning("Futures WS disconnected — using REST until reconnected")


def _ws_on_open(ws: Any) -> None:
    global _ws_connected, _ws_sub_id
    sym = SYMBOL.lower()
    _ws_sub_id += 1
    payload = {
        "method": "SUBSCRIBE",
        "params": [f"{sym}@bookTicker", f"{sym}@aggTrade"],
        "id": _ws_sub_id,
    }
    try:
        ws.send(json.dumps(payload))
        _ws_connected = True
        log.info("Futures WS connected (%s) — subscribed %s@bookTicker + @aggTrade", FUTURES_WS_URL, sym)
    except Exception as e:
        log.warning("Futures WS SUBSCRIBE send failed: %s", e)
        _ws_connected = False


def _ws_thread_main() -> None:
    global _ws_connected
    backoff = 1.0
    while _ws_should_run:
        if _websocket is None:
            log.warning("websocket-client not installed — futures WS disabled (REST only)")
            return
        try:
            app = _websocket.WebSocketApp(
                FUTURES_WS_URL,
                on_message=_ws_on_message,
                on_error=_ws_on_error,
                on_close=_ws_on_close,
                on_open=_ws_on_open,
            )
            app.run_forever(ping_interval=20, ping_timeout=15)
        except Exception as e:
            log.warning("Futures WS run_forever: %s", e)
        _ws_connected = False
        if not _ws_should_run:
            break
        time.sleep(min(30.0, backoff))
        backoff = min(30.0, backoff * 1.5)


def _maybe_start_futures_ws() -> None:
    global _ws_thread, _ws_should_run
    if not FUTURES_WS_ENABLED or _websocket is None:
        return
    if _ws_thread is not None and _ws_thread.is_alive():
        return
    _ws_should_run = True
    _ws_thread = threading.Thread(target=_ws_thread_main, name="futures-ws", daemon=True)
    _ws_thread.start()


def _ws_force_rest_fallback() -> bool:
    """True → callers should use REST for this read."""
    global _ws_log_rest_once_ts
    if not FUTURES_WS_ENABLED or _websocket is None:
        return True
    if not _ws_connected:
        return True
    if FUTURES_WS_FALLBACK_AFTER_SEC > 0:
        if time.time() - _ws_last_event_ts > FUTURES_WS_FALLBACK_AFTER_SEC:
            now = time.time()
            if now - _ws_log_rest_once_ts > 120.0:
                log.warning(
                    "Futures WS quiet >%.0fs — REST fallback (set FUTURES_WS_FALLBACK_AFTER_SEC=0 to disable)",
                    FUTURES_WS_FALLBACK_AFTER_SEC,
                )
                _ws_log_rest_once_ts = now
            return True
    return False


def _ticker_price(symbol: str) -> float:
    """GET /fapi/v1/ticker/price for any futures symbol."""
    r = requests.get(
        f"{BASE_URL.rstrip('/')}/fapi/v1/ticker/price",
        params={"symbol": symbol},
        headers=_http_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return float(r.json()["price"])


def get_price() -> float:
    """
    Last price: WebSocket ``aggTrade`` ``p`` when available; else book mid from ``bookTicker``;
    if WS unavailable use GET ``/fapi/v1/ticker/price``.
    """
    _maybe_start_futures_ws()
    if not _ws_force_rest_fallback():
        with _ws_lock:
            if _ws_last_px is not None:
                return float(_ws_last_px)
            if _ws_book is not None:
                return float(_ws_book["mid"])
    return _ticker_price(SYMBOL)


def _book_ticker_rest(symbol: str) -> dict:
    r = requests.get(
        f"{BASE_URL.rstrip('/')}/fapi/v1/ticker/bookTicker",
        params={"symbol": symbol},
        headers=_http_headers(),
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    bid = float(j["bidPrice"])
    ask = float(j["askPrice"])
    bid_qty = float(j["bidQty"])
    ask_qty = float(j["askQty"])
    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "spread": ask - bid,
        "mid": (bid + ask) / 2.0,
    }


def get_book_ticker(symbol: Optional[str] = None) -> dict:
    """
    Best bid/ask: WebSocket ``bookTicker`` for configured ``SYMBOL`` when connected, else REST.
    Other symbols always use REST.
    """
    sym = symbol or SYMBOL
    if sym != SYMBOL:
        return _book_ticker_rest(sym)
    _maybe_start_futures_ws()
    if not _ws_force_rest_fallback():
        with _ws_lock:
            if _ws_book is not None and _ws_book.get("symbol") == sym:
                return dict(_ws_book)
    return _book_ticker_rest(sym)


def format_book_line(book: dict) -> str:
    """One-line bid / ask / mid / spread / top qty from a bookTicker dict."""
    return (
        f"{book['symbol']} bid ${book['bid']:.4f}×{book['bid_qty']:.4g} | "
        f"ask ${book['ask']:.4f}×{book['ask_qty']:.4g} | "
        f"mid ${book['mid']:.4f} | spread ${book['spread']:.4f}"
    )


def mark_for_pnl(book: Optional[dict], last_px: float) -> float:
    """
    Mark for uPnL display: book mid when bookTicker is available, else last trade price.
    Live uPnL still comes from positionRisk; this is the reference price shown next to it.
    Paper uPnL uses this mark so position / paper orders / balance lines agree.
    """
    if book is not None and book.get("mid") is not None:
        return float(book["mid"])
    return float(last_px)


def book_ticker_line(symbol: Optional[str] = None) -> str:
    """One-line bid / ask / mid / spread / top qty for logs."""
    try:
        return format_book_line(get_book_ticker(symbol))
    except Exception as e:
        return f"bookTicker unavailable ({e})"


def _balance_row_to_usd(
    asset: str,
    available: float,
    *,
    quiet_ticker_miss: bool = False,
) -> float:
    """
    Pegged stables (_STABLE_COLLATERAL): 1 unit ≈ 1 USD for sizing.
    Other assets: multiply by mark from COLLATERAL_PRICE_SYMBOL or {ASSET}USDT futures ticker.
    ``quiet_ticker_miss``: used for ``all_wallet`` sums — no per-asset warning (see debug + summary).
    """
    if asset in _STABLE_COLLATERAL:
        return available
    px_sym = COLLATERAL_PRICE_SYMBOL or f"{asset}USDT"
    try:
        px = _ticker_price(px_sym)
    except Exception as e:
        msg = (
            "Cannot convert %s balance to USD (ticker %s): %s — set COLLATERAL_PRICE_SYMBOL if needed"
        )
        if quiet_ticker_miss:
            log.debug(msg, asset, px_sym, e)
        else:
            log.warning(msg, asset, px_sym, e)
        return 0.0
    return available * px


def _symbol_base_quote(symbol: str) -> tuple[str, str]:
    """e.g. AAVEUSDT -> (AAVE, USDT). Longest quote suffix wins."""
    s = (symbol or "").strip().upper()
    quotes = ("USDT", "USDC", "USDF", "BUSD", "FDUSD", "TUSD", "USD")
    for q in sorted(quotes, key=len, reverse=True):
        if len(s) > len(q) and s.endswith(q):
            return s[: -len(q)], q
    return "", ""


def _futures_balance_log_target_assets() -> set[str]:
    """Futures rows to show at startup: trade pair + configured collateral."""
    out: set[str] = set()
    base, quote = _symbol_base_quote(SYMBOL)
    if base:
        out.add(base)
    if quote:
        out.add(quote)
    if COLLATERAL_ASSETS:
        out.update(x for x in COLLATERAL_ASSETS if x)
    if COLLATERAL_ASSET:
        out.add(COLLATERAL_ASSET)
    if not out:
        out.update({"USDT", "USDF"})
    return out


# ── ACCOUNT ───────────────────────────────────────────────────────────────────

def log_futures_startup_balances(
    *,
    unrealized_pnl: Optional[float] = None,
    position_size: Optional[float] = None,
    entry_price: Optional[float] = None,
    mark_px: Optional[float] = None,
    mark_basis: str = "last",
) -> None:
    """
    Log GET /fapi/v2/balance rows for SYMBOL base/quote and collateral env only.
    Omits rows whose USD estimate is below BALANCE_LOG_DUST_MIN_USD (default $5; set 0 to show all).
    Used at startup and on every Aster poll with open orders (not the full multi-asset dump).
    When ``unrealized_pnl`` is passed (typically from positionRisk or paper sim), appends one
    summary line with **unsettled** mark PnL (plain $, not profit-green) next to the balance block.
    ``mark_basis`` labels ``mark_px``
    (e.g. ``\"book mid\"`` from bookTicker, or ``\"last\"`` from ticker/price).
    Spot wallet is not queried (futures-only bot).
    """
    rows = _signed_get("/fapi/v2/balance", {})
    if not isinstance(rows, list):
        log.error("Futures balances: unexpected response %s", rows)
        return
    targets = _futures_balance_log_target_assets()
    dust = BALANCE_LOG_DUST_MIN_USD
    any_line = False
    for a in sorted(rows, key=lambda x: str(x.get("asset", ""))):
        asset = str(a.get("asset", "") or "")
        if asset not in targets:
            continue
        try:
            avail = float(a.get("availableBalance", 0) or 0)
            wall_raw = a.get("walletBalance", a.get("balance", 0))
            wallet = float(wall_raw or 0)
        except (TypeError, ValueError):
            continue
        usd_avail = _balance_row_to_usd(asset, avail)
        usd_wallet = _balance_row_to_usd(asset, wallet)
        usd_max = max(usd_avail, usd_wallet)
        if dust > 0 and usd_max < dust:
            continue
        any_line = True
        log.info(
            "  %s │ available=%.8g wallet=%.8g (~max $%.2f USD est.)",
            asset,
            avail,
            wallet,
            usd_max,
        )
    if not any_line:
        log.info(
            "  (no non-dust rows for %s — dust min $%.2f; set BALANCE_LOG_DUST_MIN_USD=0 to show tiny balances)",
            ", ".join(sorted(targets)),
            dust,
        )

    if unrealized_pnl is not None:
        up = float(unrealized_pnl)
        pnl_s = format_unrealized_upnl(up)
        sz = float(position_size) if position_size is not None else 0.0
        en = float(entry_price) if entry_price is not None else 0.0
        mk = float(mark_px) if mark_px is not None else None
        if abs(sz) > 1e-12:
            tail = f" │ size={sz:.6g} entry=${en:.4f}"
            if mk is not None:
                tail += f" │ {mark_basis} ${mk:.4f}"
            log.info("  %s perp unsettled (mark) %s%s", SYMBOL, pnl_s, tail)
        else:
            tail = ""
            if mk is not None:
                tail = f" │ {mark_basis} ${mk:.4f}"
            log.info("  %s perp unsettled (mark) %s (flat)%s", SYMBOL, pnl_s, tail)


def log_all_balances() -> None:
    """Log every asset row from GET /fapi/v2/balance (V3 when using Pro API)."""
    rows = _signed_get("/fapi/v2/balance", {})
    if not isinstance(rows, list):
        log.error("All balances: unexpected response %s", rows)
        return
    log.info("%d asset row(s):", len(rows))
    for a in sorted(rows, key=lambda x: str(x.get("asset", ""))):
        asset = a.get("asset", "?")
        avail = a.get("availableBalance", "0")
        wall = a.get("walletBalance", a.get("balance", ""))
        margin = a.get("marginBalance", "")
        cross = a.get("crossWalletBalance", "")
        log.info(
            "  %s | available=%s wallet=%s margin=%s crossWallet=%s",
            asset,
            avail,
            wall if wall != "" else "—",
            margin if margin != "" else "—",
            cross if cross != "" else "—",
        )


def get_leverage_bracket(symbol: Optional[str] = None) -> Any:
    """
    GET /fapi/v1/leverageBracket — notional caps per leverage tier (signed).
    Response is usually a list of {symbol, brackets: [{bracket, initialLeverage, notionalCap, ...}]}.
    """
    sym = symbol or SYMBOL
    return _signed_get("/fapi/v1/leverageBracket", {"symbol": sym})


def get_open_orders(symbol: Optional[str] = None) -> list:
    """GET /fapi/v1/openOrders for symbol (signed)."""
    sym = symbol or SYMBOL
    raw = _signed_get("/fapi/v1/openOrders", {"symbol": sym})
    if isinstance(raw, list):
        return raw
    log.error("openOrders unexpected: %s", raw)
    return []


def log_open_orders(symbol: Optional[str] = None, *, max_lines: int = 40) -> None:
    """Log resting orders for the symbol (newest / most relevant first if API orders them)."""
    sym = symbol or SYMBOL
    try:
        orders = get_open_orders(sym)
    except Exception as e:
        log.warning("%s open orders: could not fetch (%s)", sym, e)
        return
    if not orders:
        log.info("  %s open orders: (none)", sym)
        return

    def _num(x: object) -> float:
        try:
            if x is None or x == "":
                return 0.0
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    parsed: list[tuple[dict, float, float, float, float]] = []
    sum_rem = 0.0
    sum_amt = 0.0
    for o in orders:
        orig = _num(o.get("origQty")) or _num(o.get("quantity"))
        filled = _num(o.get("executedQty")) or _num(o.get("cumQty"))
        rem = max(0.0, orig - filled)
        pr = _num(o.get("price"))
        amt = rem * pr if pr > 0 else 0.0
        sum_rem += rem
        sum_amt += amt
        parsed.append((o, orig, rem, pr, amt))

    log.info(
        "  %s open orders: %d · Σrem_qty=%.6g · Σamt≈$%.2f",
        sym,
        len(orders),
        sum_rem,
        sum_amt,
    )
    for o, orig, rem, _, amt in parsed[:max_lines]:
        amt_s = f"amt≈${amt:.2f}" if amt > 0 else "amt=—"
        log.info(
            "    id=%s %s %s price=%s qty=%.6g rem=%.6g %s filled=%s status=%s",
            o.get("orderId"),
            o.get("side"),
            o.get("type"),
            o.get("price"),
            orig,
            rem,
            amt_s,
            o.get("executedQty"),
            o.get("status"),
        )
    if len(orders) > max_lines:
        log.info("    … %d more not shown (Σ above is all %d orders)", len(orders) - max_lines, len(orders))


def _balance_portfolio_all_wallet_usd(rows: list) -> float:
    """
    Sum USD estimate of futures assets for **ladder sizing** (not open-position notional alone).

    Uses **availableBalance** per row when > 0 — that is what can back new orders; **walletBalance**
    can include margin locked in positions or open orders, so ``max(wallet, avail)`` overstates
    deployable funds and triggers Aster **-2019 Margin is insufficient**. Falls back to
    **walletBalance** only when ``availableBalance`` is zero (API quirks / edge rows).
    """
    dust = float(BALANCE_LOG_DUST_MIN_USD or 0.0)
    total = 0.0
    skipped_nonzero: list[str] = []
    for a in rows:
        asset = str(a.get("asset") or "")
        if not asset:
            continue
        try:
            wall = float(a.get("walletBalance", a.get("balance", 0)) or 0)
            avail = float(a.get("availableBalance", 0) or 0)
        except (TypeError, ValueError):
            continue
        qty = avail if avail > 1e-12 else wall
        if qty <= 0:
            continue
        usd = _balance_row_to_usd(asset, qty, quiet_ticker_miss=True)
        if usd <= 0 and qty > 0 and asset not in _STABLE_COLLATERAL:
            skipped_nonzero.append(asset)
        if dust > 0 and usd < dust:
            continue
        total += usd
    log.info(
        "Balance (all_wallet scope — Σ availableBalance→USD per asset, dust≥$%.2f; "
        "walletBalance if avail=0): $%.2f",
        dust,
        total,
    )
    if skipped_nonzero:
        uniq = sorted(set(skipped_nonzero))
        tail = ", ".join(uniq[:25])
        if len(uniq) > 25:
            tail += ", …"
        log.info(
            "all_wallet: %d asset(s) had wallet balance but no priced USDT ticker (counted $0 in sum); "
            "set COLLATERAL_PRICE_SYMBOL for a single non-stable override or use collateral scope: %s",
            len(uniq),
            tail,
        )
    return total


def _um_account_available_balance_usd() -> Optional[float]:
    """
    GET /fapi/v2/account (→ v3 when using Pro API V3) — Binance-style **availableBalance**
    (USDT terms, cross-margin wallet actually free for **new** orders).

    Per-asset ``/fapi/v2/balance`` sums can still exceed this when the venue applies portfolio
    rules, brackets, or haircuts — then Aster returns **-2019** on the first order. When this
    call succeeds, ``get_balance()`` uses ``min(computed, availableBalance)`` for sizing.
    """
    try:
        j = _signed_get("/fapi/v2/account", {})
    except Exception as e:
        log.debug("GET /fapi/v2/account for sizing cap: %s", e)
        return None
    if not isinstance(j, dict):
        return None
    raw = j.get("availableBalance")
    if raw is None or raw == "":
        return None
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return None
    if x <= 1e-9:
        return None
    return x


def _apply_um_available_cap(primary: float) -> float:
    """If UM account ``availableBalance`` is lower than *primary*, size the ladder to the cap."""
    if primary <= 1e-9:
        return primary
    cap = _um_account_available_balance_usd()
    if cap is None:
        return primary
    if cap + 1e-6 < primary:
        log.info(
            "Balance sizing cap: UM account availableBalance $%.2f < computed $%.2f — using cap",
            cap,
            primary,
        )
        return float(cap)
    return primary


def get_balance() -> float:
    """GET /fapi/v2/balance (→ v3 when using Pro API V3). Returns USD-equivalent for ladder sizing."""
    rows = _signed_get("/fapi/v2/balance", {})
    if not isinstance(rows, list):
        log.error("Unexpected balance response: %s", rows)
        return 0.0
    if BALANCE_SIZING_SCOPE == "all_wallet":
        return _apply_um_available_cap(_balance_portfolio_all_wallet_usd(rows))
    if COLLATERAL_ASSETS:
        by_asset = {a.get("asset"): a for a in rows if a.get("asset")}
        total = 0.0
        for asset in COLLATERAL_ASSETS:
            row = by_asset.get(asset)
            if not row:
                log.warning("COLLATERAL_ASSETS: no balance row for %s", asset)
                continue
            raw = float(row["availableBalance"])
            usd = _balance_row_to_usd(asset, raw)
            total += usd
            if asset in _STABLE_COLLATERAL:
                log.info("Collateral %s: $%.2f", asset, usd)
            else:
                log.info(
                    "Collateral %s: %.6g tokens → ~$%.2f USD (via %s)",
                    asset,
                    raw,
                    usd,
                    COLLATERAL_PRICE_SYMBOL or f"{asset}USDT",
                )
        log.info(
            "Balance (combined %s): ~$%.2f USD for sizing",
            "+".join(COLLATERAL_ASSETS),
            total,
        )
        return _apply_um_available_cap(total)
    if COLLATERAL_ASSET:
        for a in rows:
            if a.get("asset") == COLLATERAL_ASSET:
                raw = float(a["availableBalance"])
                usd = _balance_row_to_usd(COLLATERAL_ASSET, raw)
                if COLLATERAL_ASSET in _STABLE_COLLATERAL:
                    log.info("Balance (%s): $%.2f", COLLATERAL_ASSET, usd)
                else:
                    log.info(
                        "Balance (%s): %.6g tokens → ~$%.2f USD for sizing (via %s)",
                        COLLATERAL_ASSET,
                        raw,
                        usd,
                        COLLATERAL_PRICE_SYMBOL or f"{COLLATERAL_ASSET}USDT",
                    )
                return _apply_um_available_cap(usd)
        log.warning(
            "COLLATERAL_ASSET=%s not in balance response, falling back",
            COLLATERAL_ASSET,
        )
    for pref in _STABLE_BALANCE_FALLBACK_ORDER:
        for a in rows:
            if a.get("asset") == pref:
                bal = float(a["availableBalance"])
                log.info("Balance (%s): $%.2f", a["asset"], bal)
                return _apply_um_available_cap(bal)
    for a in rows:
        if a.get("asset") == "ASTER":
            raw = float(a["availableBalance"])
            usd = _balance_row_to_usd("ASTER", raw)
            log.info("Balance (ASTER fallback): %.6g tokens → ~$%.2f USD", raw, usd)
            return _apply_um_available_cap(usd)
    return 0.0


def get_balance_rows() -> list:
    """GET /fapi/v2/balance — raw asset rows (signed)."""
    raw = _signed_get("/fapi/v2/balance", {})
    return raw if isinstance(raw, list) else []


def get_position_risk_list(symbol: Optional[str] = None) -> list:
    """
    GET /fapi/v2/positionRisk (signed).
    ``symbol`` set: one symbol. ``symbol=None``: all symbols (if the venue supports empty filter).
    """
    params: dict = {}
    if symbol is not None:
        params["symbol"] = symbol
    raw = _signed_get("/fapi/v2/positionRisk", params)
    if isinstance(raw, list):
        return raw
    log.warning("positionRisk unexpected: %s", raw)
    return []


def get_position_risk_row(symbol: Optional[str] = None) -> Optional[dict]:
    """Single symbol row from positionRisk (best-effort)."""
    sym = symbol or SYMBOL
    rows = get_position_risk_list(sym)
    for row in rows:
        if isinstance(row, dict) and row.get("symbol") == sym:
            return row
    if len(rows) == 1 and isinstance(rows[0], dict):
        return rows[0]
    return None


def get_position() -> tuple[float, float, float]:
    """GET /fapi/v2/positionRisk — (size, entry_price, unrealised_pnl)."""
    raw = _signed_get("/fapi/v2/positionRisk", {"symbol": SYMBOL})
    if not isinstance(raw, list):
        log.error("Unexpected positionRisk response: %s", raw)
        return 0.0, 0.0, 0.0
    for pos in raw:
        if pos["symbol"] == SYMBOL:
            return (
                float(pos["positionAmt"]),
                float(pos["entryPrice"]),
                float(pos["unRealizedProfit"]),
            )
    return 0.0, 0.0, 0.0


# ── SETUP ─────────────────────────────────────────────────────────────────────

def _v3_margin_body() -> dict:
    body = {"symbol": SYMBOL, "marginType": "CROSSED"}
    body["nonce"] = str(_micro_nonce())
    body["user"] = _v3_addr(ASTER_USER)
    body["signer"] = _v3_addr(ASTER_SIGNER)
    param_str = urlencode(body)
    body["signature"] = _sign_v3_payload(param_str)
    return body


def set_leverage() -> None:
    """POST /fapi/v1/leverage"""
    _signed_post(
        "/fapi/v1/leverage",
        {"symbol": SYMBOL, "leverage": int(LEVERAGE)},
    )
    log.info(f"Leverage set to {LEVERAGE}x")


def set_margin_cross() -> None:
    """POST /fapi/v1/marginType — cross margin. Ignores 'already set' error (-4046)."""
    _require_v3_credentials()
    try:
        r = requests.post(
            f"{BASE_URL.rstrip('/')}{_fapi_signed_path('/fapi/v1/marginType')}",
            data=_v3_margin_body(),
            headers=_http_headers({"Content-Type": "application/x-www-form-urlencoded"}),
            timeout=10,
        )
        if r.status_code == 200:
            try:
                j = r.json()
            except Exception:
                log.info("Margin: CROSS")
                return
            if isinstance(j, dict) and j.get("code") == -4046:
                log.info("Margin: CROSS (already set)")
            elif isinstance(j, dict) and j.get("code") not in (None, 200, 0, "200", "0"):
                log.warning("marginType: %s", j)
            else:
                log.info("Margin: CROSS")
        else:
            try:
                j = r.json()
                if isinstance(j, dict) and j.get("code") == -4046:
                    log.info("Margin: CROSS (already set)")
                else:
                    log.warning("marginType: %s", j)
            except Exception:
                log.warning("marginType: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("marginType: %s", e)


def get_multi_assets_margin_enabled() -> Optional[bool]:
    """
    GET /fapi/v1/multiAssetsMargin — whether this API account uses multi-asset collateral.

    The Aster **web UI** can show multi-asset on while the **futures API** account is still
    single-asset; then USDT-margined orders fail until this mode is enabled via API.
    """
    try:
        j = _signed_get("/fapi/v1/multiAssetsMargin", {})
        if not isinstance(j, dict):
            return None
        v = j.get("multiAssetsMargin")
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() == "true"
        return None
    except Exception as e:
        log.warning("multiAssetsMargin GET: %s", e)
        return None


def set_multi_assets_margin(enabled: bool) -> None:
    """POST /fapi/v1/multiAssetsMargin — turn multi-asset collateral on/off for the API account."""
    j = _signed_post(
        "/fapi/v1/multiAssetsMargin",
        {"multiAssetsMargin": "true" if enabled else "false"},
    )
    if isinstance(j, dict) and j.get("code") not in (None, 200, 0, "200", "0") and j.get("msg") != "success":
        log.warning("multiAssetsMargin POST response: %s", j)
    log.info("multiAssetsMargin → %s", "true" if enabled else "false")


def ensure_multi_assets_margin_enabled() -> None:
    """If the API reports single-asset mode, enable multi-asset (matches typical UI default)."""
    cur = get_multi_assets_margin_enabled()
    if cur is None:
        log.warning(
            "multiAssetsMargin: could not read mode — if orders fail with -5018, "
            "confirm Aster supports this endpoint for V3 and that UI/API use the same login (ASTER_USER)."
        )
        return
    log.info("Futures API multiAssetsMargin (multi-asset collateral): %s", cur)
    if not cur:
        log.info("Enabling multi-assets margin via API (UI was on; API was off)")
        try:
            set_multi_assets_margin(True)
        except Exception as e:
            log.warning("set_multi_assets_margin: %s", e)


# ── ORDERS ────────────────────────────────────────────────────────────────────

def _place_order(params: dict) -> dict:
    return _signed_post("/fapi/v1/order", params)


def place_limit_buy(qty: float, price: float) -> dict:
    """POST /fapi/v1/order — GTC limit buy."""
    _ensure_trade_limits()
    qty_f = _floor_qty_to_step(float(qty))
    price_f = _round_price_to_tick(float(price))
    assert _trade_limits is not None
    if qty_f + 1e-12 < float(_trade_limits["qty_min"]):
        raise ValueError(
            f"place_limit_buy: qty {qty_f} below minQty {_trade_limits['qty_min']} for {SYMBOL}"
        )
    result = _place_order(
        {
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": _format_order_qty(qty_f),
            "price": _format_order_price(price_f),
            "positionSide": "BOTH",
        }
    )
    log.info("Limit BUY %s @ $%s: %s", _format_order_qty(qty_f), _format_order_price(price_f), result.get("orderId"))
    return result


def _market_resp_extras(result: dict) -> str:
    """Short tail for logs (Aster/Binance-style fields when present)."""
    if not isinstance(result, dict):
        return ""
    parts: List[str] = []
    for k in ("executedQty", "cumQty", "avgPrice", "price"):
        v = result.get(k)
        if v is not None and str(v).strip() != "":
            parts.append(f"{k}={v}")
    return (" · " + " ".join(parts)) if parts else ""


def place_market_buy(qty: float, *, reason: Optional[str] = None) -> dict:
    """POST /fapi/v1/order — market buy (opens long or covers short)."""
    _ensure_trade_limits()
    qty_f = _floor_qty_to_step(float(qty))
    result = _place_order(
        {
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "MARKET",
            "quantity": _format_order_qty(qty_f),
            "positionSide": "BOTH",
        }
    )
    rs = f" · reason={reason}" if reason else ""
    ext = _market_resp_extras(result) if isinstance(result, dict) else ""
    log.info(
        "Market BUY %s%s · orderId=%s%s",
        _format_order_qty(qty_f),
        ext,
        result.get("orderId") if isinstance(result, dict) else None,
        rs,
    )
    return result


def place_market_sell(qty: float, *, reason: Optional[str] = None) -> dict:
    """POST /fapi/v1/order — market sell (closes long)."""
    _ensure_trade_limits()
    qty_f = _floor_qty_to_step(float(qty))
    result = _place_order(
        {
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "MARKET",
            "quantity": _format_order_qty(qty_f),
            "positionSide": "BOTH",
        }
    )
    rs = f" · reason={reason}" if reason else ""
    ext = _market_resp_extras(result) if isinstance(result, dict) else ""
    log.info(
        "Market SELL %s%s · orderId=%s%s",
        _format_order_qty(qty_f),
        ext,
        result.get("orderId") if isinstance(result, dict) else None,
        rs,
    )
    return result


def cancel_all_orders() -> None:
    """DELETE /fapi/v1/allOpenOrders"""
    _signed_delete("/fapi/v1/allOpenOrders", {"symbol": SYMBOL})
    log.info("All open orders cancelled")


def _fmt_market_legs(legs: List[Tuple[float, Any]]) -> str:
    """Human-readable list of (qty, orderId) legs for close summaries."""
    if not legs:
        return "(none)"
    bits = []
    for q, oid in legs:
        bits.append(f"qty={_format_order_qty(float(q))} orderId={oid}")
    return "; ".join(bits)


def close_position(size: float, reason: str) -> bool:
    """
    Market-sell to flat; poll until |position| negligible or timeout.
    Returns True if verified flat (or nothing to close). On failure logs error and notify_alert.
    """
    if size <= 1e-12:
        return True
    qty = abs(float(size))
    legs: List[Tuple[float, Any]] = []
    try:
        res = place_market_sell(qty, reason=reason)
        legs.append((qty, res.get("orderId") if isinstance(res, dict) else None))
    except Exception as e:
        log.error("close_position initial market sell failed: %s", e, exc_info=True)
        notify_alert(f"aster-aave-exploit: close failed ({reason}): {e}")
        return False

    deadline = time.time() + max(5, CLOSE_VERIFY_TIMEOUT_SEC)
    eps = 1e-8
    while time.time() < deadline:
        time.sleep(max(0.3, CLOSE_VERIFY_POLL_SEC))
        rem, _, _ = get_position()
        r = abs(float(rem))
        if r <= eps:
            log.info(
                "CLOSED — %s (verified flat) · market sells (%d leg(s)): %s",
                reason,
                len(legs),
                _fmt_market_legs(legs),
            )
            return True
        if float(rem) < -eps:
            log.error("close_position: unexpected short size %.6g — %s", rem, reason)
            notify_alert(
                f"aster-aave-exploit: unexpected short {rem} after close ({reason})"
            )
            return False
        try:
            log.warning("close_position: chasing remainder %.6g (reason=%s)", r, reason)
            res = place_market_sell(r, reason=reason)
            legs.append((r, res.get("orderId") if isinstance(res, dict) else None))
        except Exception as e:
            log.error("close_position chase sell failed: %s", e, exc_info=True)
            notify_alert(f"aster-aave-exploit: chase sell failed ({reason}): {e}")
            return False

    rem, _, _ = get_position()
    if abs(float(rem)) <= eps:
        log.info(
            "CLOSED — %s (verified flat after final check) · market sells (%d leg(s)): %s",
            reason,
            len(legs),
            _fmt_market_legs(legs),
        )
        return True
    log.error(
        "CLOSE VERIFY FAILED — still size=%.6g after %ss — %s",
        rem,
        CLOSE_VERIFY_TIMEOUT_SEC,
        reason,
    )
    notify_alert(
        f"aster-aave-exploit: CLOSE VERIFY FAILED size={rem} after {CLOSE_VERIFY_TIMEOUT_SEC}s ({reason})"
    )
    return False


def _close_short_position(qty: float, reason: str) -> bool:
    """``qty`` = abs(short size); market buy to flat with same verify/chase pattern as ``close_position``."""
    if qty <= 1e-12:
        return True
    legs: List[Tuple[float, Any]] = []
    try:
        res = place_market_buy(qty, reason=reason)
        legs.append((qty, res.get("orderId") if isinstance(res, dict) else None))
    except Exception as e:
        log.error("_close_short_position initial market buy failed: %s", e, exc_info=True)
        notify_alert(f"aster-aave-exploit: short close failed ({reason}): {e}")
        return False

    deadline = time.time() + max(5, CLOSE_VERIFY_TIMEOUT_SEC)
    eps = 1e-8
    while time.time() < deadline:
        time.sleep(max(0.3, CLOSE_VERIFY_POLL_SEC))
        rem, _, _ = get_position()
        r = abs(float(rem))
        if r <= eps:
            log.info(
                "CLOSED SHORT — %s (verified flat) · market buys (%d leg(s)): %s",
                reason,
                len(legs),
                _fmt_market_legs(legs),
            )
            return True
        if float(rem) > eps:
            log.error(
                "close short: unexpected long size %.6g after buy — %s",
                rem,
                reason,
            )
            notify_alert(
                f"aster-aave-exploit: unexpected long {rem} after short close ({reason})"
            )
            return False
        try:
            log.warning("_close_short_position: chasing remainder %.6g (reason=%s)", r, reason)
            res = place_market_buy(r, reason=reason)
            legs.append((r, res.get("orderId") if isinstance(res, dict) else None))
        except Exception as e:
            log.error("_close_short_position chase buy failed: %s", e, exc_info=True)
            notify_alert(f"aster-aave-exploit: short chase buy failed ({reason}): {e}")
            return False

    rem, _, _ = get_position()
    if abs(float(rem)) <= eps:
        log.info(
            "CLOSED SHORT — %s (verified flat after final check) · market buys (%d leg(s)): %s",
            reason,
            len(legs),
            _fmt_market_legs(legs),
        )
        return True
    log.error(
        "CLOSE SHORT VERIFY FAILED — still size=%.6g after %ss — %s",
        rem,
        CLOSE_VERIFY_TIMEOUT_SEC,
        reason,
    )
    notify_alert(
        f"aster-aave-exploit: CLOSE SHORT VERIFY FAILED size={rem} after {CLOSE_VERIFY_TIMEOUT_SEC}s ({reason})"
    )
    return False


def flatten_position(reason: str) -> bool:
    """
    Market-flat ``SYMBOL`` (long→sell, short→buy), poll until flat or timeout.
    Returns True if already flat or verified flat.
    """
    sz, _, _ = get_position()
    if abs(float(sz)) <= 1e-12:
        log.info("flatten_position: already flat (%s)", reason)
        return True
    if float(sz) > 1e-12:
        log.info("flatten_position: closing long size=%.6g (%s)", sz, reason)
        return close_position(float(sz), reason)
    log.info("flatten_position: closing short size=%.6g (%s)", sz, reason)
    return _close_short_position(abs(float(sz)), reason)


# ── LADDER BUILDER ────────────────────────────────────────────────────────────

def _warn_order_count() -> None:
    if LADDER_RUNGS != ORDER_COUNT:
        z1n = (
            len(ZONE1_BELOW_SPOT_USD)
            if ZONE1_BELOW_SPOT_USD
            else len(ZONE1_BELOW_SPOT_PCT)
            if ZONE1_BELOW_SPOT_PCT
            else len(ZONE1_PRICES)
        )
        log.warning(
            "ORDER_COUNT=%s but ladder has %s rungs (Zone1=%s dynamic/absolute + Zone2=%s); "
            "sizing still uses ORDER_COUNT — fix .env to match.",
            ORDER_COUNT,
            LADDER_RUNGS,
            z1n,
            len(ZONE2_SPREADS),
        )


def _zone1_prices_at_spot(live_price: float) -> List[float]:
    """
    Zone 1 rungs: dynamic below spot (USD or % at ladder build) or absolute ZONE1_PRICES.
    Dynamic rungs are sorted ascending; invalid (<= MIN_ZONE1_PRICE) are skipped with a warning.
    """
    if ZONE1_BELOW_SPOT_USD:
        raw: List[float] = []
        for off in ZONE1_BELOW_SPOT_USD:
            if off < 0:
                log.warning("ZONE1_BELOW_SPOT_USD entry %.4g is negative (ignored)", off)
                continue
            p = round(live_price - off, 1)
            if p <= MIN_ZONE1_PRICE:
                log.warning(
                    "Zone1 skip: reference %.4f - %.4f = %.4f (<= MIN_ZONE1_PRICE %.4f)",
                    live_price,
                    off,
                    p,
                    MIN_ZONE1_PRICE,
                )
                continue
            raw.append(p)
        out = sorted(set(raw))
        if len(out) < len(ZONE1_BELOW_SPOT_USD):
            log.warning(
                "Zone1: only %d/%d USD-below-spot rungs valid (others skipped as invalid price)",
                len(out),
                len(ZONE1_BELOW_SPOT_USD),
            )
        log.info(
            "Zone 1 (USD below reference %s @ $%.4f): %s",
            ZONE1_BELOW_SPOT_USD,
            live_price,
            out,
        )
        return out
    if ZONE1_BELOW_SPOT_PCT:
        raw = []
        for pct in ZONE1_BELOW_SPOT_PCT:
            if pct < 0 or pct >= 100:
                log.warning("ZONE1_BELOW_SPOT_PCT entry %.4g out of range 0..100 (ignored)", pct)
                continue
            p = round(live_price * (1.0 - pct / 100.0), 1)
            if p <= MIN_ZONE1_PRICE:
                log.warning(
                    "Zone1 skip: reference * (1 - %.4f%%) = %.4f (<= MIN_ZONE1_PRICE %.4f)",
                    pct,
                    p,
                    MIN_ZONE1_PRICE,
                )
                continue
            raw.append(p)
        out = sorted(set(raw))
        if len(out) < len(ZONE1_BELOW_SPOT_PCT):
            log.warning(
                "Zone1: only %d/%d pct-below-spot rungs valid (others skipped as invalid price)",
                len(out),
                len(ZONE1_BELOW_SPOT_PCT),
            )
        log.info(
            "Zone 1 (pct below reference %s @ $%.4f): %s",
            ZONE1_BELOW_SPOT_PCT,
            live_price,
            out,
        )
        return out
    if ZONE1_PRICES:
        log.info("Zone 1 (absolute from env): %s", ZONE1_PRICES)
    return list(ZONE1_PRICES)


# Cached LOT_SIZE / PRICE_FILTER for SYMBOL (public exchangeInfo).
_trade_limits: Optional[Dict[str, Any]] = None


def _filter_decimals(step_or_tick: str) -> int:
    d = Decimal(str(step_or_tick).strip())
    e = int(d.as_tuple().exponent)
    return max(0, -e)


def _ensure_trade_limits() -> None:
    """Load LOT_SIZE + PRICE_FILTER once (fixes Aster -1111 precision vs arbitrary float/round)."""
    global _trade_limits
    if _trade_limits is not None:
        return
    url = f"{BASE_URL.rstrip('/')}/fapi/v1/exchangeInfo"
    r = requests.get(url, params={"symbol": SYMBOL}, timeout=20)
    _raise_for_aster(r)
    j = r.json()
    sym: Optional[dict] = None
    for s in j.get("symbols") or []:
        if s.get("symbol") == SYMBOL:
            sym = s
            break
    if not sym:
        raise RuntimeError(f"exchangeInfo: symbol {SYMBOL!r} not found")
    fm: dict = {}
    for f in sym.get("filters") or []:
        ft = f.get("filterType")
        if isinstance(ft, str):
            fm[ft] = f
    lot = fm.get("LOT_SIZE") or {}
    pf = fm.get("PRICE_FILTER") or {}
    pp = fm.get("PERCENT_PRICE") or {}
    step_s = str(lot.get("stepSize", "0.001"))
    tick_s = str(pf.get("tickSize", "0.01"))
    mn: Optional[float] = None
    for name in ("MIN_NOTIONAL", "NOTIONAL"):
        f = fm.get(name)
        if not f:
            continue
        for k in ("notional", "minNotional"):
            v = f.get(k)
            if v is not None and str(v).strip() != "":
                mn = float(v)
                break
        if mn is not None:
            break

    def _pp_mult(key: str) -> Optional[float]:
        v = pp.get(key)
        if v is None:
            return None
        s = str(v).strip()
        if not s or s in ("0", "0.0", "0.0000"):
            return None
        try:
            x = float(s)
        except (TypeError, ValueError):
            return None
        return x if x > 0 else None

    pct_up = _pp_mult("multiplierUp")
    pct_down = _pp_mult("multiplierDown")
    pct_lt_up = _pp_mult("ltMultiplierUp")
    pct_lt_down = _pp_mult("ltMultiplierDown")

    _trade_limits = {
        "qty_step": float(step_s),
        "qty_step_str": step_s,
        "qty_min": float(lot.get("minQty", 0) or 0),
        "qty_max": float(lot.get("maxQty", 1e18) or 1e18),
        "price_tick": float(tick_s),
        "price_tick_str": tick_s,
        "min_notional": mn,
        "percent_mult_up": pct_up,
        "percent_mult_down": pct_down,
        "percent_lt_mult_up": pct_lt_up,
        "percent_lt_mult_down": pct_lt_down,
    }
    log.info(
        "exchangeInfo %s: LOT_SIZE step=%s min=%s | PRICE_FILTER tick=%s",
        SYMBOL,
        step_s,
        _trade_limits["qty_min"],
        tick_s,
    )
    if pct_up is not None or pct_down is not None:
        log.info(
            "exchangeInfo %s: PERCENT_PRICE mult_up=%s mult_down=%s lt_up=%s lt_down=%s",
            SYMBOL,
            pct_up,
            pct_down,
            pct_lt_up,
            pct_lt_down,
        )


def _premium_index() -> dict:
    """Public GET /fapi/v1/premiumIndex — mark and index (PERCENT_PRICE baseline)."""
    url = f"{BASE_URL.rstrip('/')}/fapi/v1/premiumIndex"
    r = requests.get(url, params={"symbol": SYMBOL}, timeout=15)
    _raise_for_aster(r)
    j = r.json()
    if not isinstance(j, dict):
        raise RuntimeError(f"premiumIndex: expected object, got {type(j).__name__}")
    return j


def _percent_price_max_buy() -> Optional[float]:
    """
    Highest limit BUY price allowed vs PERCENT_PRICE (Binance-style: buy <= mark * multiplierUp).
    Uses min(mark,index) as baseline so a slightly stale mark does not exceed the venue cap.
    Returns None when the filter is absent or unusable.
    """
    _ensure_trade_limits()
    assert _trade_limits is not None
    mup = _trade_limits.get("percent_mult_up")
    if mup is None or mup <= 0:
        return None
    tick = float(_trade_limits["price_tick"])
    if tick <= 0:
        return None
    try:
        pi = _premium_index()
    except Exception as e:
        log.warning("PERCENT_PRICE cap skipped: premiumIndex failed: %s", e)
        return None
    try:
        mark = float(pi["markPrice"])
    except (KeyError, TypeError, ValueError) as e:
        log.warning("PERCENT_PRICE cap skipped: markPrice missing/invalid: %s", e)
        return None
    idx_raw = pi.get("indexPrice")
    try:
        idx = float(idx_raw) if idx_raw is not None else mark
    except (TypeError, ValueError):
        idx = mark
    base = min(mark, idx)
    raw_cap = base * float(mup)
    lt_up = _trade_limits.get("percent_lt_mult_up")
    if lt_up is not None and lt_up > 0:
        raw_cap = min(raw_cap, base * float(lt_up))
    # Largest tick-aligned price still <= raw_cap (avoid 93.28 when venue max is 93.268).
    cap = math.floor(float(raw_cap) / tick + 1e-12) * tick
    log.info(
        "PERCENT_PRICE max buy: mark=%.6g index=%.6g base=%.6g × mult_up=%.6g → $%.4f (tick=%s)",
        mark,
        idx,
        base,
        float(mup),
        cap,
        _trade_limits["price_tick_str"],
    )
    return float(cap)


def _floor_qty_to_step(qty: float) -> float:
    """Floor quantity to LOT_SIZE step; before exchangeInfo use legacy 3dp round (tests)."""
    if qty <= 0:
        return 0.0
    if _trade_limits is None:
        return round(float(qty), 3)
    st = float(_trade_limits["qty_step"])
    if st <= 0:
        return round(float(qty), 8)
    q = math.floor(float(qty) / st + 1e-12) * st
    qmin = float(_trade_limits["qty_min"])
    if q + 1e-12 < qmin:
        if float(qty) + 1e-12 >= qmin:
            q = qmin
        else:
            return 0.0
    qmax = float(_trade_limits["qty_max"])
    if q > qmax:
        q = math.floor(qmax / st + 1e-12) * st
    return float(q)


def _round_price_to_tick(price: float) -> float:
    if _trade_limits is None:
        return float(price)
    tick = float(_trade_limits["price_tick"])
    if tick <= 0:
        return float(price)
    n = round(float(price) / tick)
    return float(Decimal(str(n)) * Decimal(str(tick)))


def _format_order_qty(qty: float) -> str:
    if _trade_limits is None:
        return f"{float(qty):.8f}".rstrip("0").rstrip(".") or "0"
    d = _filter_decimals(str(_trade_limits["qty_step_str"]))
    return f"{float(qty):.{d}f}"


def _format_order_price(price: float) -> str:
    if _trade_limits is None:
        return f"{float(price):.8f}".rstrip("0").rstrip(".") or "0"
    px = _round_price_to_tick(float(price))
    d = _filter_decimals(str(_trade_limits["price_tick_str"]))
    return f"{px:.{d}f}"


def _usd_per_order() -> float:
    balance = get_balance()
    deployable = balance * WALLET_PCT
    buffer = balance * MARGIN_BUFFER_PCT
    safe_total = deployable - buffer
    hr = float(LADDER_IM_HEADROOM_PCT)
    safe_ladder = max(0.0, safe_total * (1.0 - hr))
    usd_each = safe_ladder / max(1, ORDER_COUNT)

    log.info(
        f"Balance: ${balance:.2f} | "
        f"Deploy {WALLET_PCT*100:.0f}%: ${deployable:.2f} | "
        f"Buffer {MARGIN_BUFFER_PCT*100:.0f}%: ${buffer:.2f} | "
        f"Safe total: ${safe_total:.2f} | "
        f"Ladder IM headroom {hr*100:.0f}% → ${safe_ladder:.2f} split /{ORDER_COUNT} | "
        f"Per order: ${usd_each:.2f}"
    )
    return usd_each


def build_zone2(live_price: float) -> list:
    """Auto-build Zone 2 prices just above current price."""
    zone2 = [round(live_price + s, 1) for s in ZONE2_SPREADS]
    log.info(f"Zone 2 auto @ ${live_price:.2f}: {zone2}")
    return zone2


def _resolve_ladder_reference() -> Tuple[float, Optional[dict]]:
    """
    Single price for Zone1 dynamic math and Zone2 spreads (see ZONE1_SPOT_ANCHOR).
    Returns (reference_price, book_ticker_or_none). When anchor is mid|bid|ask and book succeeds,
    the same dict is returned for logging / DRY_RUN without a second GET.
    """
    if ZONE1_SPOT_ANCHOR == "last":
        px = get_price()
        log.info("Ladder reference: last (GET ticker/price) $%.4f", px)
        return px, None
    try:
        book = get_book_ticker()
        k = ZONE1_SPOT_ANCHOR
        px = float(book[k])
        log.info("Ladder reference: %s (bookTicker) $%.4f", k, px)
        return px, book
    except Exception as e:
        log.warning(
            "ZONE1_SPOT_ANCHOR=%s: bookTicker failed (%s); using ticker price",
            ZONE1_SPOT_ANCHOR,
            e,
        )
        px = get_price()
        log.info("Ladder reference: last (fallback ticker/price) $%.4f", px)
        return px, None


def ladder_rows_totals(
    rows: List[tuple[float, float, float]],
) -> tuple[float, float, float]:
    """Sum (qty, notional quote, margin USD) over ladder rows (price, usd_each, qty)."""
    sq = sum(float(q) for _, _, q in rows)
    sn = sum(float(p) * float(q) for p, _, q in rows)
    sm = sum(float(u) for _, u, _ in rows)
    return sq, sn, sm


def ladder_pairs_totals(pairs: List[tuple[float, float]]) -> tuple[float, float, float]:
    """
    Same totals as ladder_rows_totals, from [(price, usd_each), ...] using the ladder qty rule.
    """
    sq = sn = sm = 0.0
    for price, usd in pairs:
        p, u = float(price), float(usd)
        q = _floor_qty_to_step((u * LEVERAGE) / p)
        sq += q
        sn += q * p
        sm += u
    return sq, sn, sm


def _build_ladder_rows() -> Tuple[List[tuple[float, float, float]], Optional[dict], float]:
    """Returns ([(price, usd_each, qty), ...], book_ticker or None, ladder reference price)."""
    _warn_order_count()
    _ensure_trade_limits()
    usd_each = _usd_per_order()
    ref_px, book = _resolve_ladder_reference()
    zone1 = _zone1_prices_at_spot(ref_px)
    zone2 = build_zone2(ref_px)
    all_prices = zone1 + zone2
    rows: List[tuple[float, float, float]] = []
    assert _trade_limits is not None
    mn = _trade_limits.get("min_notional")
    pct_buy_max = _percent_price_max_buy()
    for price in all_prices:
        p_adj = _round_price_to_tick(float(price))
        if pct_buy_max is not None and p_adj > float(pct_buy_max) + 1e-9:
            log.warning(
                "Ladder skip: buy $%.4f above PERCENT_PRICE max $%.4f",
                p_adj,
                float(pct_buy_max),
            )
            continue
        qty = _floor_qty_to_step((usd_each * LEVERAGE) / p_adj)
        if qty <= 0:
            log.warning("Ladder skip: qty rounds to 0 after LOT_SIZE @ $%.4f", p_adj)
            continue
        if mn is not None and qty * p_adj + 1e-9 < float(mn):
            log.warning(
                "Ladder skip: notional %.4f < MIN_NOTIONAL %.4f @ $%.4f",
                qty * p_adj,
                float(mn),
                p_adj,
            )
            continue
        rows.append((p_adj, usd_each, qty))
    if book is None:
        try:
            book = get_book_ticker()
        except Exception as e:
            log.warning("bookTicker: %s", e)
    if book:
        b = book
        log.info(
            "%s top of book (fills): bid $%.4f qty %.4g | ask $%.4f qty %.4g | spread $%.4f | mid $%.4f",
            b["symbol"],
            b["bid"],
            b["bid_qty"],
            b["ask"],
            b["ask_qty"],
            b["spread"],
            b["mid"],
        )
    return rows, book, ref_px


def log_ladder_preview() -> Tuple[List[tuple[float, float, float]], float]:
    """
    DRY_RUN: same reads and sizing as live, log each rung — no POSTs.
    Returns (rows, ref_px) where rows are (price, usd_each, qty) and ref_px is the ladder anchor.
    """
    rows, book, ref_px = _build_ladder_rows()
    ask_ref = float(book["ask"]) if book else None
    log.info(f"[DRY_RUN] Would place {len(rows)} limit buys (no orders sent):")
    for price, usd, qty in rows:
        notional = usd * LEVERAGE
        vs_ask = ""
        if ask_ref is not None:
            vs_ask = f" | Δ to ask ${ask_ref - price:+.3f} (resting buy unless limit ≥ ask)"
        log.info(
            f"[DRY_RUN]   LIMIT BUY qty={qty} @ ${price} "
            f"(margin ${usd:.2f} × {LEVERAGE}x ≈ ${notional:.2f} notional){vs_ask}"
        )
    tq, tn, tm = ladder_rows_totals(rows)
    log.info(
        "[DRY_RUN] Ladder totals · %d rungs · Σqty=%.6g · Σnotional≈$%.2f · Σmargin≈$%.2f",
        len(rows),
        tq,
        tn,
        tm,
    )
    return rows, ref_px


def place_full_ladder() -> list[tuple[float, float]]:
    rows, _, _ = _build_ladder_rows()
    tq, tn, tm = ladder_rows_totals(rows)
    usd_each = float(rows[0][1]) if rows else 0.0
    log.info(
        "Placing %d orders @ $%.2f margin each · Σqty=%.6g · Σnotional≈$%.2f · Σmargin≈$%.2f",
        len(rows),
        usd_each,
        tq,
        tn,
        tm,
    )
    for price, usd, qty in rows:
        place_limit_buy(qty, price)
        time.sleep(0.3)
    return [(p, u) for p, u, _ in rows]
