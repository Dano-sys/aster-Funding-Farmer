"""
Aster Futures REST client — supports Pro API V3 (EIP-712) and legacy HMAC (V1/V2 paths).

V3 (recommended): set ASTER_USER, ASTER_SIGNER, ASTER_SIGNER_PRIVATE_KEY
  See https://github.com/asterdex/api-docs/blob/master/V3(Recommended)/EN/aster-finance-futures-api-v3.md

Legacy: set ASTER_API_KEY and ASTER_SECRET_KEY (HMAC, v1/v2 paths).
"""

import hashlib
import hmac
import os
import threading
import time
from urllib.parse import urlencode

import requests
from typing import Optional

from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address

load_dotenv()

# Public / signing base (default matches Aster docs; fapi3 may 403 from some networks — use fapi)
FAPI_BASE = os.getenv("ASTER_FAPI_BASE", "https://fapi.asterdex.com").rstrip("/")
# Spot API V3 — https://github.com/asterdex/api-docs (aster-finance-spot-api-v3.md)
SAPI_BASE = os.getenv("ASTER_SAPI_BASE", "https://sapi.asterdex.com").rstrip("/")

# --- Legacy HMAC ----------------------------------------------------------------
API_KEY = os.getenv("ASTER_API_KEY", "").strip()
SECRET_KEY = os.getenv("ASTER_SECRET_KEY", "").strip()

# --- Pro API V3 (EIP-712) -------------------------------------------------------
ASTER_USER = os.getenv("ASTER_USER", "").strip()
ASTER_SIGNER = os.getenv("ASTER_SIGNER", "").strip()
ASTER_SIGNER_PRIVATE_KEY = os.getenv("ASTER_SIGNER_PRIVATE_KEY", "").strip()

def _not_placeholder(s: str) -> bool:
    s = s.lower()
    return bool(s) and "your_" not in s and "placeholder" not in s


USE_V3 = bool(
    _not_placeholder(ASTER_USER)
    and _not_placeholder(ASTER_SIGNER)
    and _not_placeholder(ASTER_SIGNER_PRIVATE_KEY)
)
USE_LEGACY = bool(
    _not_placeholder(API_KEY) and _not_placeholder(SECRET_KEY)
)

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
_last_ms = 0
_nonce_i = 0


def _v3_addr(addr: str) -> str:
    """EIP-55 checksum; Aster may reject mixed-case addresses on some routes."""
    try:
        return to_checksum_address(addr.strip())
    except Exception:
        return addr.strip()


def _micro_nonce() -> int:
    """Microsecond nonce; increments within same second (V3 spec)."""
    global _last_ms, _nonce_i
    with _nonce_lock:
        now_ms = int(time.time())
        if now_ms == _last_ms:
            _nonce_i += 1
        else:
            _last_ms = now_ms
            _nonce_i = 0
        return now_ms * 1_000_000 + _nonce_i


def _normalize_path(path: str) -> str:
    if USE_V3:
        return path.replace("/fapi/v1/", "/fapi/v3/").replace("/fapi/v2/", "/fapi/v3/")
    return path


def _sign_v3_payload(param_str: str) -> str:
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
    """Aster examples use User-Agent PythonApp/1.0; some routes (spot) may 500 without it."""
    h = {"User-Agent": os.getenv("ASTER_HTTP_USER_AGENT", "PythonApp/1.0")}
    if extra:
        h.update(extra)
    return h


def _headers_legacy() -> dict:
    return {**_http_headers(), "X-MBX-APIKEY": API_KEY}


def _sign_legacy(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _timestamp_ms() -> int:
    return int(time.time() * 1000)


def credentials_ok() -> bool:
    return USE_V3 or USE_LEGACY


def get(
    path: str,
    params: Optional[dict] = None,
    signed: bool = False,
    base_url: Optional[str] = None,
):
    """REST GET. Futures paths under FAPI_BASE get /fapi/v1|v2 → /fapi/v3 normalization when using V3."""
    base = (base_url or FAPI_BASE).rstrip("/")
    if base == FAPI_BASE:
        path = _normalize_path(path)
    params = params or {}
    if not signed:
        r = requests.get(
            base + path, params=params, headers=_http_headers(), timeout=15
        )
        r.raise_for_status()
        return r.json()

    if USE_V3:
        body = dict(params)
        body["nonce"] = str(_micro_nonce())
        body["user"] = _v3_addr(ASTER_USER)
        body["signer"] = _v3_addr(ASTER_SIGNER)
        param_str = urlencode(body)
        sig = _sign_v3_payload(param_str)
        url = f"{base}{path}?{param_str}&signature={sig}"
        # Do not set Content-Type on GET; some gateways return 500 if it is set.
        r = requests.get(url, headers=_http_headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    p = dict(params)
    p["timestamp"] = _timestamp_ms()
    p["signature"] = _sign_legacy(p)
    r = requests.get(
        base + path, params=p, headers=_headers_legacy(), timeout=15
    )
    r.raise_for_status()
    return r.json()


def post(path: str, params: dict, base_url: Optional[str] = None) -> dict:
    base = (base_url or FAPI_BASE).rstrip("/")
    if base == FAPI_BASE:
        path = _normalize_path(path)
    params = dict(params)

    if USE_V3:
        body = dict(params)
        body["nonce"] = str(_micro_nonce())
        body["user"] = _v3_addr(ASTER_USER)
        body["signer"] = _v3_addr(ASTER_SIGNER)
        param_str = urlencode(body)
        body["signature"] = _sign_v3_payload(param_str)
        r = requests.post(
            base + path,
            data=body,
            headers=_http_headers(
                {"Content-Type": "application/x-www-form-urlencoded"}
            ),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    else:
        params["timestamp"] = _timestamp_ms()
        params["signature"] = _sign_legacy(params)
        r = requests.post(
            base + path,
            data=params,
            headers=_headers_legacy(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

    if isinstance(data, dict) and "code" in data:
        c = data["code"]
        if c not in (200, 0, "200", "0"):
            raise RuntimeError(f"API error {data.get('code')}: {data.get('msg', '')}")
    return data
