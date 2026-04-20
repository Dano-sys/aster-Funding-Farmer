#!/usr/bin/env python3
"""
Lightweight 24/7-style alerts: new trades.csv rows + log error patterns.
Requires WEBHOOK_URL and/or Telegram. Default off (ALERT_WATCHER_ENABLED=false).

  python alert_watcher.py

Env: TRADE_LOG_FILE, FUNDING_FARMER_LOG, ALERT_ON_CLOSE_REASONS, ALERT_DEBOUNCE_SEC,
     ALERT_HEARTBEAT_OPEN, WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
     ALERT_WATCHER_STATE_FILE
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

TRADE_LOG_FILE = os.getenv("TRADE_LOG_FILE", "trades.csv")
FUNDING_FARMER_LOG = os.getenv("FUNDING_FARMER_LOG", "funding_farmer.log").strip() or "funding_farmer.log"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ALERT_WATCHER_ENABLED = os.getenv("ALERT_WATCHER_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
_reasons_raw = os.getenv(
    "ALERT_ON_CLOSE_REASONS",
    "stop_loss,stop_loss_ws,take_profit,funding_dropped",
).strip()
ALERT_ON_CLOSE_REASONS = {
    x.strip().lower()
    for x in _reasons_raw.split(",")
    if x.strip()
}
ALERT_DEBOUNCE_SEC = int(os.getenv("ALERT_DEBOUNCE_SEC", "300") or "300")
ALERT_HEARTBEAT_OPEN = os.getenv("ALERT_HEARTBEAT_OPEN", "false").lower() in (
    "1",
    "true",
    "yes",
)
ALERT_LOG_REGEX = os.getenv(
    "ALERT_LOG_REGEX",
    r"(ERROR|Traceback|RuntimeError|Exception:|-[0-9]{4}\))",
).strip() or r"(ERROR|Traceback)"
ALERT_WATCHER_STATE_FILE = os.getenv(
    "ALERT_WATCHER_STATE_FILE", ".alert_watcher_state.json"
).strip() or ".alert_watcher_state.json"

_log_pat: Optional[re.Pattern[str]] = None


def _pattern() -> re.Pattern[str]:
    global _log_pat
    if _log_pat is None:
        _log_pat = re.compile(ALERT_LOG_REGEX)
    return _log_pat


def _load_state() -> Dict[str, Any]:
    p = Path(ALERT_WATCHER_STATE_FILE)
    if not p.is_file():
        return {"csv_rows": 0, "log_pos": 0, "debounce": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"csv_rows": 0, "log_pos": 0, "debounce": {}}


def _save_state(st: Dict[str, Any]) -> None:
    try:
        Path(ALERT_WATCHER_STATE_FILE).write_text(
            json.dumps(st, indent=0), encoding="utf-8"
        )
    except OSError as e:
        print(f"state write failed: {e}", file=sys.stderr)


def _debounce_ok(st: Dict[str, Any], key: str) -> bool:
    db = st.setdefault("debounce", {})
    now = time.time()
    last = float(db.get(key, 0))
    if now - last < ALERT_DEBOUNCE_SEC:
        return False
    db[key] = now
    return True


def _send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]},
        timeout=15,
    )


def _send_webhook(payload: Dict[str, Any]) -> None:
    if not WEBHOOK_URL:
        return
    requests.post(WEBHOOK_URL, json=payload, timeout=15)


def _notify(title: str, body: str, payload: Dict[str, Any]) -> None:
    text = f"{title}\n{body}"
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _send_telegram(text)
    if WEBHOOK_URL:
        _send_webhook({"title": title, "body": body, **payload})


def _read_all_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        r = csv.DictReader(f)
        return list(r)


def poll_csv(st: Dict[str, Any], path: Path) -> None:
    rows = _read_all_csv_rows(path)
    total_data = len(rows)
    prev = int(st.get("csv_rows", 0))
    if total_data < prev:
        prev = 0
        st["csv_rows"] = 0
    if total_data <= prev:
        return
    for idx in range(prev, total_data):
        row = rows[idx]
        if not row:
            continue
        action = (row.get("action") or "").upper()
        sym = row.get("symbol") or ""
        if action == "OPEN":
            if not ALERT_HEARTBEAT_OPEN:
                continue
            key = f"open:{sym}:{row.get('timestamp_utc','')}"
            if not _debounce_ok(st, key):
                continue
            _notify(
                "Funding farmer OPEN",
                f"{sym} notional≈{row.get('notional_usdt','')}",
                {"type": "open", "row": row},
            )
        elif action == "CLOSE":
            reason = (row.get("close_reason") or "").strip().lower()
            if reason not in ALERT_ON_CLOSE_REASONS:
                continue
            key = f"close:{reason}:{sym}:{row.get('timestamp_utc','')}"
            if not _debounce_ok(st, key):
                continue
            fees = (row.get("fees_usdt") or "").strip()
            fee_txt = f" fees={fees}" if fees else ""
            _notify(
                f"Funding farmer CLOSE ({reason})",
                f"{sym} pnl_net={row.get('pnl_usdt','')} ({row.get('pnl_pct','')}%)"
                f"{fee_txt}",
                {"type": "close", "row": row},
            )
    st["csv_rows"] = total_data


def poll_log(st: Dict[str, Any], path: Path) -> None:
    if not path.is_file():
        return
    pos = int(st.get("log_pos", 0))
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            size = f.tell()
            if pos > size:
                pos = 0
            f.seek(pos)
            chunk = f.read()
            new_pos = f.tell()
    except OSError:
        return
    if not chunk:
        st["log_pos"] = new_pos
        return
    pat = _pattern()
    for line in chunk.splitlines():
        if pat.search(line):
            key = f"log:{line[:120]}"
            if not _debounce_ok(st, key):
                continue
            _notify("Funding farmer log alert", line[:3500], {"type": "log", "line": line[:2000]})
    st["log_pos"] = new_pos


def main() -> int:
    if not ALERT_WATCHER_ENABLED:
        print(
            "ALERT_WATCHER_ENABLED is not true — exiting. Set true and configure "
            "WEBHOOK_URL and/or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.",
            file=sys.stderr,
        )
        return 0
    if not WEBHOOK_URL and (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID):
        print(
            "Set WEBHOOK_URL and/or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.",
            file=sys.stderr,
        )
        return 1
    trade_path = Path(TRADE_LOG_FILE)
    log_path = Path(FUNDING_FARMER_LOG)
    print(
        f"Watching CSV={trade_path.resolve()} log={log_path.resolve()} "
        f"(debounce {ALERT_DEBOUNCE_SEC}s). Ctrl+C to stop.",
        file=sys.stderr,
    )
    st = _load_state()
    if trade_path.is_file():
        all_rows = _read_all_csv_rows(trade_path)
        total = len(all_rows)
        prev = int(st.get("csv_rows", 0))
        catchup = os.getenv("ALERT_CATCHUP_ON_START", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        if prev == 0 and total > 0 and not catchup:
            st["csv_rows"] = total
        else:
            st["csv_rows"] = min(prev, total)
    else:
        st["csv_rows"] = 0
    if log_path.is_file():
        try:
            st["log_pos"] = min(int(st.get("log_pos", 0)), log_path.stat().st_size)
        except OSError:
            st["log_pos"] = 0
    else:
        st["log_pos"] = 0

    try:
        while True:
            poll_csv(st, trade_path)
            poll_log(st, log_path)
            _save_state(st)
            time.sleep(5.0)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        _save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
