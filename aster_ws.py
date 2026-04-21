"""
Futures mark-price WebSocket (Aster / Binance-compatible streams).

Subscribes to <symbol>@markPrice on wss://fstream.asterdex.com/stream — pushes faster than
REST polling for stop-loss vs entry price. Does not replace exchange liquidation.

Payloads may include Binance-style fields: ``r`` (estimated funding for the current interval)
and ``T`` (nextFundingTime in ms). Aster REST ``GET /fapi/v1/premiumIndex`` exposes
``lastFundingRate`` only; when ``r`` / ``T`` are present, this watcher exposes them for
optional exit logic and settlement countdown (see ``funding_farmer``).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Dict, Optional, Set

log = logging.getLogger(__name__)

try:
    import websocket

    _WS_OK = True
except ImportError:
    websocket = None  # type: ignore
    _WS_OK = False

DEFAULT_WS_BASE = "wss://fstream.asterdex.com/stream"


def websocket_available() -> bool:
    return _WS_OK


class MarkPriceWatcher:
    """
    Background thread: combined markPrice stream -> compare to entry prices -> stop queue.

    Call sync(entries) each main-loop iteration with symbol -> entry price for open longs.
    """

    def __init__(
        self,
        stop_loss_pct: float,
        base_url: str = DEFAULT_WS_BASE,
    ) -> None:
        self.stop_loss_pct = float(stop_loss_pct)
        self.base_url = base_url.rstrip("/") or DEFAULT_WS_BASE
        self._entries: Dict[str, float] = {}
        self._entry_lock = threading.Lock()
        self._symbols: Set[str] = set()
        self._last_url: Optional[str] = None
        self._url_lock = threading.Lock()
        self._stop_queue: "queue.Queue[str]" = queue.Queue()
        self._ws = None
        self._ws_ref_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._est_funding: Dict[str, float] = {}
        self._est_funding_lock = threading.Lock()
        self._next_funding_ms: Dict[str, int] = {}
        self._next_funding_lock = threading.Lock()

    @staticmethod
    def _streams_query(symbols: Set[str]) -> str:
        return "/".join(f"{s.lower()}@markPrice" for s in sorted(symbols))

    def _compute_url(self, symbols: Set[str]) -> Optional[str]:
        if not symbols:
            return None
        return f"{self.base_url}?streams={self._streams_query(symbols)}"

    def sync(self, entries: Dict[str, float]) -> None:
        """Update entry prices and reconnect WebSocket if subscribed symbols change."""
        symbols = {s for s, ep in entries.items() if ep and ep > 0}
        with self._entry_lock:
            self._entries = dict(entries)
        new_url = self._compute_url(symbols)
        with self._url_lock:
            old_url = self._last_url
            self._symbols = set(symbols)
            self._last_url = new_url
        if old_url != new_url:
            with self._ws_ref_lock:
                w = self._ws
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
        with self._est_funding_lock:
            for k in list(self._est_funding.keys()):
                if k not in symbols:
                    self._est_funding.pop(k, None)
        with self._next_funding_lock:
            for k in list(self._next_funding_ms.keys()):
                if k not in symbols:
                    self._next_funding_ms.pop(k, None)

    def get_estimated_funding(self, symbol: str) -> Optional[float]:
        """Latest ``r`` from markPrice stream for ``symbol``, if any."""
        with self._est_funding_lock:
            v = self._est_funding.get(symbol)
        return v if v is not None else None

    def get_next_funding_time_ms(self, symbol: str) -> Optional[int]:
        """Latest ``T`` (next funding time, ms) from markPrice stream for ``symbol``, if any."""
        with self._next_funding_lock:
            v = self._next_funding_ms.get(symbol)
        return int(v) if v is not None else None

    def drain_stop_signals(self) -> list[str]:
        out: list[str] = []
        while True:
            try:
                out.append(self._stop_queue.get_nowait())
            except queue.Empty:
                break
        seen: Set[str] = set()
        deduped: list[str] = []
        for s in out:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        return deduped

    def _handle_message(self, message: str) -> None:
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        if isinstance(msg, dict) and isinstance(msg.get("data"), dict):
            data = msg["data"]
        elif isinstance(msg, dict):
            data = msg
        else:
            return
        if data.get("e") != "markPriceUpdate":
            return
        sym = data.get("s")
        p = data.get("p")
        if not sym or p is None:
            return
        with self._url_lock:
            subscribed = sym in self._symbols
        if subscribed:
            r_raw = data.get("r")
            if r_raw is not None:
                try:
                    rf = float(r_raw)
                    with self._est_funding_lock:
                        self._est_funding[sym] = rf
                except (TypeError, ValueError):
                    pass
            t_raw = data.get("T")
            if t_raw is not None:
                try:
                    ti = int(t_raw)
                    with self._next_funding_lock:
                        self._next_funding_ms[sym] = ti
                except (TypeError, ValueError):
                    pass
        try:
            mark = float(p)
        except (TypeError, ValueError):
            return
        with self._entry_lock:
            entry = self._entries.get(sym)
        if entry is None or entry <= 0:
            return
        pnl_pct = (mark - entry) / entry
        if pnl_pct <= -self.stop_loss_pct:
            self._stop_queue.put(sym)
            log.warning(
                "Mark WS stop signal %s  mark=%.6f  entry=%.6f  pnl=%.2f%%",
                sym,
                mark,
                entry,
                pnl_pct * 100,
            )

    def _run(self) -> None:
        while self._running:
            with self._url_lock:
                url = self._last_url
            if not url:
                time.sleep(0.4)
                continue
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_message=lambda _, m: self._handle_message(m),
                    on_open=lambda _: log.info("Mark price WebSocket connected"),
                    on_error=lambda _, err: log.warning("Mark price WebSocket error: %s", err),
                    on_close=lambda _w, c, _m: log.info(
                        "Mark price WebSocket closed (code=%s)", c
                    ),
                )
                with self._ws_ref_lock:
                    self._ws = ws
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log.warning("Mark price WebSocket run error: %s", e)
            finally:
                with self._ws_ref_lock:
                    self._ws = None
            if not self._running:
                break
            time.sleep(1.0)

    def start(self) -> None:
        if not _WS_OK:
            raise RuntimeError("websocket-client not installed")
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="aster-mark-ws", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._ws_ref_lock:
            w = self._ws
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
