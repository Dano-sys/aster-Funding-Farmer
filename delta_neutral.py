"""
Hyperliquid short hedge leg for Aster funding_farmer.

When DELTA_NEUTRAL=true in .env, funding_farmer opens an HL short before each
Aster long so price exposure is hedged while collecting net funding spread.

Requires: HL_PRIVATE_KEY, HL_WALLET_ADDRESS (optional if key controls that address).
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

import eth_account
from dotenv import load_dotenv
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()

log = logging.getLogger(__name__)

HL_PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "").strip()
HL_WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS", "").strip()
LEVERAGE_HL = int(os.getenv("LEVERAGE_HL", "3"))
HEDGE_RATIO = float(os.getenv("HEDGE_RATIO", "1.0"))
MIN_NET_FUNDING = float(os.getenv("MIN_NET_FUNDING", "0.0002"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Simulated HL short size (coin units) when DRY_RUN=true
_dry_hl_short: dict[str, float] = {}


def _wallet() -> LocalAccount:
    if not HL_PRIVATE_KEY or "your_hl" in HL_PRIVATE_KEY.lower():
        raise ValueError("HL_PRIVATE_KEY is not set or is still a placeholder")
    key = HL_PRIVATE_KEY if HL_PRIVATE_KEY.startswith("0x") else "0x" + HL_PRIVATE_KEY
    return eth_account.Account.from_key(key)


def _account_address(wallet: LocalAccount) -> str:
    if HL_WALLET_ADDRESS and "your_hl" not in HL_WALLET_ADDRESS.lower():
        return HL_WALLET_ADDRESS
    return wallet.address


def hl_setup() -> tuple[Info, Exchange, str]:
    """
    Initialise Hyperliquid Info + Exchange on mainnet.
    Returns (info, exchange, address) as expected by funding_farmer.run().
    """
    wallet = _wallet()
    address = _account_address(wallet)
    base_url = constants.MAINNET_API_URL
    exchange = Exchange(wallet, base_url, account_address=address)
    return exchange.info, exchange, address


def _coin_in_universe(hl_info: Info, coin: str) -> bool:
    try:
        return coin in hl_info.name_to_coin
    except Exception:
        return False


def _sz_decimals(hl_info: Info, coin: str) -> int:
    for u in hl_info.meta()["universe"]:
        if u["name"] == coin:
            return int(u["szDecimals"])
    return 4


def _round_sz(sz: float, decimals: int) -> float:
    if decimals <= 0:
        return float(int(sz))
    q = Decimal("1").scaleb(-decimals)
    return float(Decimal(str(sz)).quantize(q, rounding=ROUND_DOWN))


def hl_get_funding_rate(hl_info: Info, coin: str) -> float:
    """Latest perp funding rate (same units as Aster: per funding interval, often ~8h)."""
    try:
        meta, ctxs = hl_info.meta_and_asset_ctxs()
        for i, u in enumerate(meta["universe"]):
            if u["name"] == coin:
                return float(ctxs[i]["funding"])
    except Exception as e:
        log.warning("Could not get HL funding for %s: %s", coin, e)
    return 0.0


def _mid_px(hl_info: Info, coin: str) -> Optional[float]:
    try:
        mids = hl_info.all_mids()
        if coin not in mids:
            return None
        return float(mids[coin])
    except Exception as e:
        log.warning("Could not get mid for %s: %s", coin, e)
        return None


def hl_open_short(
    hl_info: Info,
    hl_exchange: Exchange,
    hl_address: str,
    coin: str,
    notional: float,
    aster_rate: float,
    hl_rate: float,
) -> bool:
    """
    Open a short on HL sized to hedge `notional` USD (after HEDGE_RATIO).
    Returns False if net funding is below MIN_NET_FUNDING, coin missing, or order fails.
    """
    if not _coin_in_universe(hl_info, coin):
        log.warning("  [HL] Coin %s not listed on Hyperliquid — skipping hedge", coin)
        return False

    net = aster_rate - hl_rate
    if net < MIN_NET_FUNDING:
        log.warning(
            "  [HL] Net funding %.6f below MIN_NET_FUNDING %.6f — skip %s",
            net,
            MIN_NET_FUNDING,
            coin,
        )
        return False

    mid = _mid_px(hl_info, coin)
    if not mid or mid <= 0:
        log.warning("  [HL] Could not get price for %s", coin)
        return False

    raw_sz = (notional * HEDGE_RATIO) / mid
    dec = _sz_decimals(hl_info, coin)
    sz = _round_sz(raw_sz, dec)
    if sz <= 0:
        log.warning("  [HL] Size rounded to zero for %s", coin)
        return False

    if DRY_RUN:
        log.warning(
            "  [HL DRY RUN] SHORT %s  sz=%s  ~$%.0f  net_rate=%.6f",
            coin,
            sz,
            notional * HEDGE_RATIO,
            net,
        )
        _dry_hl_short[coin] = sz
        return True

    try:
        hl_exchange.update_leverage(LEVERAGE_HL, coin, is_cross=True)
    except Exception as e:
        log.warning("  [HL] update_leverage: %s (continuing)", e)

    try:
        res = hl_exchange.market_open(coin, False, sz, None, 0.05)
    except Exception as e:
        log.error("  [HL] market_open failed: %s", e)
        return False

    if not _order_ok(res):
        log.error("  [HL] market_open bad response: %s", res)
        return False

    log.info("  [HL] Opened short %s  sz=%s", coin, sz)
    return True


def _order_ok(res: Any) -> bool:
    if res is None:
        return False
    if isinstance(res, dict):
        st = res.get("status")
        if st == "ok":
            return True
        if st == "err":
            return False
        # Some responses nest status
        r = res.get("response")
        if isinstance(r, dict) and r.get("type") == "order":
            return True
    return True


def hl_close_short(
    hl_info: Info,
    hl_exchange: Exchange,
    hl_address: str,
    coin: str,
    reason: str,
) -> None:
    """Close HL short for coin (reduce-only market close)."""
    if DRY_RUN:
        had = _dry_hl_short.pop(coin, None)
        if had is not None:
            log.warning(
                "  [HL DRY RUN] CLOSE short %s  sz=%s  reason=%s", coin, had, reason
            )
        else:
            log.warning("  [HL DRY RUN] No simulated HL position for %s", coin)
        return

    if not _coin_in_universe(hl_info, coin):
        return

    try:
        res = hl_exchange.market_close(coin)
        if not _order_ok(res):
            log.error("  [HL] market_close bad response: %s", res)
        else:
            log.info("  [HL] Closed short %s  reason=%s", coin, reason)
    except Exception as e:
        log.error("  [HL] market_close failed: %s", e)
