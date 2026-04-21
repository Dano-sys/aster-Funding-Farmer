#!/usr/bin/env python3
"""
Simple local web UI for Aster Funding Farmer: config, margin, spot/perp detail,
open positions, funding leaderboard, bot-eligible symbols, recent trades, and
recent Claude advisor JSONL runs (if present).

  python3 web_dashboard.py
  Open http://127.0.0.1:8765

Environment:
  DASHBOARD_HOST   default 127.0.0.1 (use 0.0.0.0 only on trusted networks)
  DASHBOARD_PORT   default 8765
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

# Import after load_dotenv so funding_farmer sees the same env as the bot
import funding_farmer as ff  # noqa: E402
from aster_client import FAPI_BASE, SAPI_BASE, USE_V3, credentials_ok  # noqa: E402

CONFIG_KEYS = [
    "DRY_RUN",
    "DRY_RUN_SIMULATED_MARGIN_USD",
    "DRY_RUN_SHOW_LIVE_WALLET_DETAILS",
    "LEVERAGE",
    "MIN_FUNDING_RATE",
    "EXIT_FUNDING_RATE",
    "POLL_INTERVAL_SEC",
    "RISK_POLL_INTERVAL_SEC",
    "STOP_LOSS_PCT",
    "TAKE_PROFIT_PCT",
    "BLACKLIST",
    "MAX_POSITIONS",
    "RANK_TOP_PCT",
    "MAX_SINGLE_PCT",
    "CORR_GROUPS",
    "WALLET_DEPLOY_PCT",
    "WALLET_MAX_USD",
    "WALLET_MIN_USD",
    "MIN_QUOTE_VOLUME_24H",
    "SYMBOL_ALLOWLIST",
    "TRADE_LOG_FILE",
    "DELTA_NEUTRAL",
    "MARK_PRICE_WS",
    "SHOW_BOOK_IN_LOGS",
    "BALANCE_DUST_USD",
    "INCOME_LOOKBACK_DAYS",
    "ASTER_FAPI_BASE",
    "ASTER_SAPI_BASE",
    "FARMING_HALT",
    "FARMING_HALT_FILE",
    "CYCLE_SNAPSHOT_ENABLE",
    "CYCLE_SNAPSHOT_FILE",
    "CLAUDE_ADVISOR_ENABLED",
    "CLAUDE_ADVISOR_MIN_INTERVAL_SEC",
    "CLAUDE_AUTO_APPLY",
]


def _json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if obj == obj else None  # NaN -> null
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    return str(obj)


def _read_recent_trades(path: str, limit: int = 80) -> List[Dict[str, str]]:
    if not path or not os.path.isfile(path):
        return []
    rows: List[Dict[str, str]] = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                rows.append({k: (v or "") for k, v in row.items()})
    except OSError:
        return []
    return rows[-limit:]


def _read_claude_advisor_jsonl(path: str, limit: int = 12) -> List[dict]:
    """Last N lines from claude_advisor_out.jsonl ({ts_unix, model, advisor_json})."""
    if not path or not os.path.isfile(path):
        return []
    lines: List[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(line)
    except OSError:
        return []
    out: List[dict] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _read_last_jsonl_object(path: str) -> Optional[dict]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])
    except (OSError, json.JSONDecodeError):
        return None


def _parse_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _position_unrealized(p: dict) -> Optional[float]:
    for k in ("unRealizedProfit", "unrealizedProfit", "unrealized_pnl"):
        if k in p:
            return _parse_float(p.get(k))
    return None


def _sum_realized_pnl_from_csv(path: str) -> Optional[float]:
    """Sum pnl_usdt on CLOSE rows (net of trading fees). None if file missing/unreadable."""
    if not path or not os.path.isfile(path):
        return None
    total = 0.0
    n = 0
    try:
        with open(path, newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                act = (row.get("action") or "").strip().upper()
                if act != "CLOSE":
                    continue
                pv = _parse_float(row.get("pnl_usdt"))
                if pv is None:
                    continue
                total += pv
                n += 1
    except OSError:
        return None
    return total if n else 0.0


def _sum_close_column_csv(path: str, column: str) -> Optional[float]:
    """Sum numeric *column* on CLOSE rows. None if file missing or column absent."""
    if not path or not os.path.isfile(path):
        return None
    total = 0.0
    n = 0
    try:
        with open(path, newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            fields = r.fieldnames or []
            if column not in fields:
                return None
            for row in r:
                act = (row.get("action") or "").strip().upper()
                if act != "CLOSE":
                    continue
                pv = _parse_float(row.get(column))
                if pv is None:
                    continue
                total += pv
                n += 1
    except OSError:
        return None
    return total if n else 0.0


def _build_summary(collateral: dict, positions: List[dict], trade_log_path: str) -> dict:
    eff = _parse_float(collateral.get("_total_effective_margin"))
    if eff is None:
        eff = 0.0

    fut_sum = 0.0
    fut_eff = 0.0
    for row in collateral.get("_futures_detail") or []:
        eu = _parse_float(row.get("est_usd"))
        if eu is not None:
            fut_sum += eu
        em = _parse_float(row.get("eff_margin"))
        if em is not None:
            fut_eff += em

    spot_sum = 0.0
    for row in collateral.get("_spot_detail") or []:
        eu = _parse_float(row.get("est_usd"))
        if eu is not None:
            spot_sum += eu

    open_notional = 0.0
    unreal = 0.0
    for p in positions:
        amt = abs(_parse_float(p.get("positionAmt")) or 0.0)
        mk = _parse_float(p.get("markPrice")) or 0.0
        if amt and mk:
            open_notional += amt * mk
        u = _position_unrealized(p)
        if u is not None:
            unreal += u

    realized = _sum_realized_pnl_from_csv(trade_log_path)
    realized_incl = _sum_close_column_csv(trade_log_path, "pnl_net_incl_funding_usdt")

    tim = _parse_float(collateral.get("_total_initial_margin_usdt"))
    tmm = _parse_float(collateral.get("_total_maint_margin_usdt"))
    acct_avail = _parse_float(collateral.get("_account_available_balance_usdt"))

    funding_lb: Optional[float] = None
    if credentials_ok():
        try:
            now_ms = int(time.time() * 1000)
            ms = now_ms - ff.INCOME_LOOKBACK_DAYS * 86400000
            funding_lb = ff.sum_funding_fee_income_all_symbols_usdt(ms, now_ms)
        except Exception:
            funding_lb = None

    return {
        "effective_margin_usdt": round(eff, 2),
        "futures_wallet_est_usdt": round(fut_sum, 2),
        "futures_eff_margin_components_usdt": round(fut_eff, 2),
        "spot_wallet_est_usdt": round(spot_sum, 2),
        "combined_wallet_est_usdt": round(fut_sum + spot_sum, 2),
        "open_notional_usdt": round(open_notional, 2),
        "unrealized_pnl_usdt": round(unreal, 4),
        "realized_pnl_trades_usdt": None if realized is None else round(realized, 4),
        "realized_pnl_incl_funding_csv_usdt": (
            None if realized_incl is None else round(realized_incl, 4)
        ),
        "total_initial_margin_usdt": None if tim is None else round(tim, 2),
        "total_maint_margin_usdt": None if tmm is None else round(tmm, 2),
        "account_available_balance_usdt": None if acct_avail is None else round(acct_avail, 2),
        "funding_income_lookback_usdt": (
            None if funding_lb is None else round(funding_lb, 4)
        ),
        "income_lookback_days": ff.INCOME_LOOKBACK_DAYS,
        "carry_components": {
            "unrealized_pnl_usdt": round(unreal, 4),
            "realized_pnl_trades_net_fees_usdt": None
            if realized is None
            else round(realized, 4),
            "realized_pnl_incl_funding_from_csv_usdt": None
            if realized_incl is None
            else round(realized_incl, 4),
            "funding_income_exchange_usdt": None
            if funding_lb is None
            else round(funding_lb, 4),
            "note": "Open positions still accrue funding; CSV net+funding is per closed rows only.",
        },
    }


def build_snapshot() -> dict:
    out: dict = {
        "generated_at_utc": ff._now_utc(),
        "api": {"fapi": FAPI_BASE, "sapi": SAPI_BASE},
        "auth": {
            "credentials_ok": credentials_ok(),
            "signing": "v3_eip712" if USE_V3 else ("legacy_hmac" if credentials_ok() else "none"),
        },
        "config": {},
        "errors": [],
    }

    for k in CONFIG_KEYS:
        if k == "CORR_GROUPS":
            out["config"][k] = ff.CORR_GROUPS_RAW
            continue
        if k == "SYMBOL_ALLOWLIST":
            raw = os.getenv("SYMBOL_ALLOWLIST", "").strip()
            out["config"][k] = raw or "(none)"
            continue
        if k == "FARMING_HALT_FILE":
            raw = os.getenv("FARMING_HALT_FILE", "").strip()
            exists = bool(raw) and os.path.isfile(raw)
            out["config"][k] = (
                f"{raw}  (file exists → halt)" if exists else (raw or "(unset)")
            )
            continue
        v = os.getenv(k)
        out["config"][k] = v if v is not None and v != "" else "(default)"

    if not credentials_ok():
        out["errors"].append(
            "API credentials missing. Set Pro V3 or legacy keys in .env (see .env.example)."
        )
        return _json_safe(out)

    try:
        out["aster_usdt"] = round(ff.get_aster_price(), 6)
    except Exception as e:
        out["errors"].append(f"ASTER price: {e}")
        out["aster_usdt"] = 0.0

    try:
        out["collateral"] = ff.get_collateral_summary()
    except Exception as e:
        out["errors"].append(f"Collateral: {e}")
        out["collateral"] = {}

    raw_pos: List[dict] = []
    try:
        raw_pos = ff.get_positions()
        out["positions"] = [_json_safe(p) for p in raw_pos]
    except Exception as e:
        out["errors"].append(f"Positions: {e}")
        out["positions"] = []

    try:
        rates = ff.get_all_funding_rates()
        out["funding_top"] = rates[:60]
    except Exception as e:
        out["errors"].append(f"Funding rates: {e}")
        out["funding_top"] = []

    vol: dict = {}
    ex: dict = {}
    try:
        vol = ff.get_24h_quote_volumes()
    except Exception as e:
        out["errors"].append(f"24h volumes: {e}")
    try:
        ex = ff.get_exchange_info()
    except Exception as e:
        out["errors"].append(f"Exchange info: {e}")

    eligible: List[dict] = []
    volume_active = ff.MIN_QUOTE_VOLUME_24H > 0
    try:
        for r in ff.get_all_funding_rates():
            if ff.is_pool_symbol_eligible(r, ex, vol, volume_active):
                qsym = r["symbol"]
                fr = float(r.get("fundingRate", 0))
                fpd = float(r.get("fundingsPerDay") or ff.fundings_per_day(qsym))
                eligible.append(
                    {
                        "symbol": qsym,
                        "fundingRate": fr,
                        "fundingsPerDay": round(fpd, 6),
                        "funding_apr_pct": round(
                            ff.funding_apr_pct_for_symbol(fr, qsym), 4
                        ),
                        "markPrice": r.get("markPrice"),
                        "quoteVolume24h": vol.get(qsym, 0.0),
                    }
                )
            if len(eligible) >= 25:
                break
    except Exception as e:
        out["errors"].append(f"Eligible pool: {e}")

    out["funding_eligible"] = eligible
    out["pool_rules"] = ff.pool_eligibility_rules_label()

    out["recent_trades"] = _read_recent_trades(ff.TRADE_LOG_FILE, 80)

    advisor_path = os.getenv("CLAUDE_ADVISOR_OUT_JSONL", "claude_advisor_out.jsonl").strip() or "claude_advisor_out.jsonl"
    out["claude_advisor_file"] = advisor_path
    out["claude_advisor_recent"] = _read_claude_advisor_jsonl(advisor_path, 12)

    snap_path = os.getenv("CYCLE_SNAPSHOT_FILE", "farmer_cycle.jsonl").strip() or "farmer_cycle.jsonl"
    out["cycle_snapshot_file"] = snap_path
    out["cycle_snapshot_last"] = _read_last_jsonl_object(snap_path)

    out["summary"] = _build_summary(
        out.get("collateral") or {}, raw_pos, ff.TRADE_LOG_FILE
    )

    return _json_safe(out)


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Aster Funding Farmer — Dashboard</title>
  <style>
    :root {
      --bg: #0f1419;
      --panel: #1a2332;
      --text: #e7ecf3;
      --muted: #8b9cb3;
      --accent: #3d8bfd;
      --good: #3ecf8e;
      --warn: #e6c35c;
    }
    * { box-sizing: border-box; }
    body {
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      line-height: 1.45;
      font-size: 14px;
    }
    header {
      padding: 1rem 1.25rem;
      border-bottom: 1px solid #2a3544;
    }
    .hdr-top {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0.75rem 1.5rem;
    }
    header h1 { font-size: 1.15rem; font-weight: 600; margin: 0; }
    header .sub { color: var(--muted); font-size: 0.85rem; }
    .hdr-stats {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
      gap: 0.5rem 0.75rem;
      margin-top: 0.85rem;
      padding-top: 0.85rem;
      border-top: 1px solid #2a3544;
    }
    .stat {
      background: var(--panel);
      border: 1px solid #243044;
      border-radius: 8px;
      padding: 0.45rem 0.55rem;
    }
    .stat .lbl {
      font-size: 0.68rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 0.2rem;
    }
    .stat .val {
      font-size: 0.95rem;
      font-variant-numeric: tabular-nums;
      font-weight: 500;
    }
    .stat .val.pos { color: var(--good); }
    .stat .val.neg { color: #ff8b8b; }
    tfoot td { font-weight: 600; color: var(--text); border-top: 1px solid #3a4a60; }
    tfoot .num { color: var(--accent); }
    tfoot td.num.pos { color: var(--good); }
    tfoot td.num.neg { color: #ff8b8b; }
    main { padding: 1rem 1.25rem 2rem; max-width: 1200px; margin: 0 auto; }
    section {
      background: var(--panel);
      border-radius: 10px;
      padding: 1rem 1.1rem;
      margin-bottom: 1rem;
      border: 1px solid #243044;
    }
    section h2 {
      font-size: 0.95rem;
      margin: 0 0 0.75rem;
      color: var(--accent);
      font-weight: 600;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.8rem;
    }
    th, td {
      text-align: left;
      padding: 0.35rem 0.5rem;
      border-bottom: 1px solid #243044;
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 500; white-space: nowrap; }
    tr:last-child td { border-bottom: none; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .err { color: #ff8b8b; margin: 0.25rem 0; font-size: 0.85rem; }
    .kv { display: grid; grid-template-columns: 14rem 1fr; gap: 0.25rem 1rem; font-size: 0.82rem; }
    .kv div:nth-child(odd) { color: var(--muted); }
    @media (max-width: 640px) {
      .kv { grid-template-columns: 1fr; }
      table { font-size: 0.72rem; }
    }
    .refresh { color: var(--muted); font-size: 0.8rem; }
    a { color: var(--accent); }
  </style>
</head>
<body>
  <header>
    <div class="hdr-top">
      <h1>Aster Funding Farmer</h1>
      <span class="sub" id="stamp">Loading…</span>
      <span class="refresh">Auto-refresh every 20s · <a href="/api/snapshot">JSON</a></span>
    </div>
    <div class="hdr-stats" id="hdrStats"></div>
  </header>
  <main id="app">
    <p class="muted">Loading snapshot…</p>
  </main>
  <script>
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  function fmtNum(x, d) {
    if (x === null || x === undefined || x === '') return '—';
    const n = Number(x);
    if (Number.isNaN(n)) return esc(x);
    return n.toFixed(d);
  }
  function fmtSigned(x, d) {
    if (x === null || x === undefined || x === '') return '—';
    const n = Number(x);
    if (Number.isNaN(n)) return esc(x);
    const s = n >= 0 ? '+' : '';
    return s + n.toFixed(d);
  }
  function pnlClass(n) {
    if (n === null || n === undefined || n === '') return '';
    const x = Number(n);
    if (Number.isNaN(x)) return '';
    return x >= 0 ? 'pos' : 'neg';
  }
  function renderHdrStats(s) {
    const el = document.getElementById('hdrStats');
    if (!el) return;
    if (!s || typeof s !== 'object') {
      el.innerHTML = '';
      return;
    }
    const rp = s.realized_pnl_trades_usdt;
    const rpShow = (rp === null || rp === undefined);
    el.innerHTML =
      '<div class="stat"><div class="lbl">Effective margin</div><div class="val">'
        + fmtNum(s.effective_margin_usdt, 2) + ' USDT</div></div>'
      + '<div class="stat"><div class="lbl">Futures (est.)</div><div class="val">'
        + fmtNum(s.futures_wallet_est_usdt, 2) + ' USDT</div></div>'
      + '<div class="stat"><div class="lbl">Spot (est.)</div><div class="val">'
        + fmtNum(s.spot_wallet_est_usdt, 2) + ' USDT</div></div>'
      + '<div class="stat"><div class="lbl">Wallet total</div><div class="val">'
        + fmtNum(s.combined_wallet_est_usdt, 2) + ' USDT</div></div>'
      + '<div class="stat"><div class="lbl">Open notional</div><div class="val">'
        + fmtNum(s.open_notional_usdt, 2) + ' USDT</div></div>'
      + '<div class="stat"><div class="lbl">Unrealized PnL</div><div class="val ' + pnlClass(s.unrealized_pnl_usdt) + '">'
        + fmtSigned(s.unrealized_pnl_usdt, 4) + ' USDT</div></div>'
      + '<div class="stat"><div class="lbl">Realized PnL (log, net fees)</div><div class="val '
        + (rpShow ? '' : pnlClass(rp)) + '">'
        + (rpShow ? '—' : fmtSigned(rp, 4)) + ' USDT</div></div>'
      + '<div class="stat"><div class="lbl">Realized+Funding (CSV)</div><div class="val '
        + (s.realized_pnl_incl_funding_csv_usdt === null || s.realized_pnl_incl_funding_csv_usdt === undefined ? '' : pnlClass(s.realized_pnl_incl_funding_csv_usdt)) + '">'
        + (s.realized_pnl_incl_funding_csv_usdt === null || s.realized_pnl_incl_funding_csv_usdt === undefined ? '—' : fmtSigned(s.realized_pnl_incl_funding_csv_usdt, 4))
        + ' USDT</div></div>'
      + '<div class="stat"><div class="lbl">Funding (income, '
        + (s.income_lookback_days != null ? esc(String(s.income_lookback_days)) : '?')
        + 'd)</div><div class="val '
        + (s.funding_income_lookback_usdt === null || s.funding_income_lookback_usdt === undefined ? '' : pnlClass(s.funding_income_lookback_usdt)) + '">'
        + (s.funding_income_lookback_usdt === null || s.funding_income_lookback_usdt === undefined ? '—' : fmtSigned(s.funding_income_lookback_usdt, 4))
        + ' USDT</div></div>'
      + '<div class="stat"><div class="lbl">Initial margin</div><div class="val">'
        + (s.total_initial_margin_usdt === null || s.total_initial_margin_usdt === undefined ? '—' : fmtNum(s.total_initial_margin_usdt, 2))
        + ' USDT</div></div>'
      + '<div class="stat"><div class="lbl">Maint. margin</div><div class="val">'
        + (s.total_maint_margin_usdt === null || s.total_maint_margin_usdt === undefined ? '—' : fmtNum(s.total_maint_margin_usdt, 2))
        + ' USDT</div></div>';
  }
  function render(data) {
    const app = document.getElementById('app');
    const stamp = document.getElementById('stamp');
    stamp.textContent = 'Updated ' + (data.generated_at_utc || '?') + ' · '
      + (data.api && data.api.fapi ? data.api.fapi : '');
    renderHdrStats(data.summary);

    let html = '';

    if (data.errors && data.errors.length) {
      html += '<section><h2>Notices</h2>';
      for (const e of data.errors) {
        html += '<p class="err">' + esc(e) + '</p>';
      }
      html += '</section>';
    }

    html += '<section><h2>Auth</h2><div class="kv">';
    html += '<div>Status</div><div>' + esc(data.auth && data.auth.signing) + '</div>';
    html += '<div>Credentials</div><div>' + (data.auth && data.auth.credentials_ok ? 'OK' : 'Missing') + '</div>';
    html += '</div></section>';

    html += '<section><h2>Bot configuration</h2><div class="kv">';
    if (data.config) {
      for (const [k, v] of Object.entries(data.config)) {
        html += '<div>' + esc(k) + '</div><div>' + esc(v) + '</div>';
      }
    }
    html += '</div></section>';

    html += '<section><h2>Margin & balances</h2>';
    const c = data.collateral || {};
    const te = c._total_effective_margin;
    const sum = data.summary || {};
    html += '<p style="margin:0 0 0.5rem;color:var(--muted);font-size:0.85rem;">';
    html += 'ASTER mark ≈ ' + fmtNum(data.aster_usdt, 4) + ' USDT';
    if (c._dry_run_simulated_margin) {
      html += ' · <span style="color:var(--warn)">dry-run simulated margin active</span>';
    }
    html += '</p>';
    html += '<p><strong>Effective margin (USDT)</strong>: ' + fmtNum(te, 2) + '</p>';
    if (sum.total_initial_margin_usdt !== null && sum.total_initial_margin_usdt !== undefined) {
      html += '<p style="margin:0.35rem 0;font-size:0.85rem"><strong>Account margin</strong> (GET /fapi/v2/account): initial '
        + fmtNum(sum.total_initial_margin_usdt, 2) + ' · maintenance '
        + fmtNum(sum.total_maint_margin_usdt, 2) + ' USDT</p>';
    }
    if (sum.carry_components && sum.carry_components.note) {
      html += '<p style="margin:0.25rem 0 0.5rem;color:var(--muted);font-size:0.8rem">'
        + esc(sum.carry_components.note) + '</p>';
    }

    const fd = c._futures_detail || [];
    if (fd.length) {
      html += '<h3 style="font-size:0.85rem;margin:0.75rem 0 0.35rem;color:var(--muted)">Futures (non-dust)</h3>';
      html += '<table><thead><tr><th>Asset</th><th class="num">Balance</th><th class="num">Est USD</th><th class="num">Eff. margin</th></tr></thead><tbody>';
      for (const row of fd) {
        html += '<tr><td>' + esc(row.asset) + '</td><td class="num">' + fmtNum(row.balance, 6)
          + '</td><td class="num">' + fmtNum(row.est_usd, 2) + '</td><td class="num">' + fmtNum(row.eff_margin, 2) + '</td></tr>';
      }
      html += '</tbody><tfoot><tr><td>Total</td><td class="num">—</td><td class="num">'
        + fmtNum(sum.futures_wallet_est_usdt, 2) + '</td><td class="num">'
        + fmtNum(sum.futures_eff_margin_components_usdt, 2) + '</td></tr></tfoot></table>';
    }

    const sd = c._spot_detail || [];
    if (sd.length) {
      html += '<h3 style="font-size:0.85rem;margin:0.75rem 0 0.35rem;color:var(--muted)">Spot (non-dust)</h3>';
      html += '<table><thead><tr><th>Asset</th><th class="num">Free</th><th class="num">Locked</th><th class="num">Total</th><th class="num">Est USD</th></tr></thead><tbody>';
      for (const row of sd) {
        html += '<tr><td>' + esc(row.asset) + '</td><td class="num">' + fmtNum(row.free, 6)
          + '</td><td class="num">' + fmtNum(row.locked, 6) + '</td><td class="num">' + fmtNum(row.total, 6)
          + '</td><td class="num">' + fmtNum(row.est_usd, 2) + '</td></tr>';
      }
      html += '</tbody><tfoot><tr><td>Total</td><td class="num">—</td><td class="num">—</td><td class="num">—</td><td class="num">'
        + fmtNum(sum.spot_wallet_est_usdt, 2) + '</td></tr></tfoot></table>';
    }
    html += '</section>';

    const pos = data.positions || [];
    html += '<section><h2>Open positions (' + pos.length + ')</h2>';
    if (!pos.length) {
      html += '<p style="color:var(--muted);margin:0">No open perp positions.</p>';
    } else {
      html += '<table><thead><tr><th>Symbol</th><th class="num">Qty</th><th class="num">Entry</th><th class="num">Mark</th><th class="num">uPnL</th><th>Side</th></tr></thead><tbody>';
      for (const p of pos) {
        const u = p.unRealizedProfit !== undefined ? p.unRealizedProfit : p.unrealizedProfit;
        html += '<tr><td>' + esc(p.symbol) + '</td><td class="num">' + fmtNum(p.positionAmt, 6)
          + '</td><td class="num">' + fmtNum(p.entryPrice, 6) + '</td><td class="num">' + fmtNum(p.markPrice, 6)
          + '</td><td class="num">' + fmtNum(u, 4) + '</td><td>' + esc(p.positionSide || '—') + '</td></tr>';
      }
      html += '</tbody><tfoot><tr><td>Total</td><td class="num">—</td><td class="num">—</td><td class="num">—</td><td class="num '
        + pnlClass(sum.unrealized_pnl_usdt) + '">'
        + fmtSigned(sum.unrealized_pnl_usdt, 4) + '</td><td>—</td></tr></tfoot></table>';
    }
    html += '</section>';

    html += '<section><h2>Bot-eligible funding (next entries)</h2>';
    html += '<p style="margin:0 0 0.5rem;color:var(--muted);font-size:0.85rem">Rules: ' + esc(data.pool_rules || '') + '</p>';
    const el = data.funding_eligible || [];
    if (!el.length) {
      html += '<p style="color:var(--muted);margin:0">No symbols pass current filters (or data error).</p>';
    } else {
      html += '<table><thead><tr><th>Symbol</th><th class="num">Funding / interval</th><th class="num">APR %</th><th class="num">Mark</th><th class="num">24h quote vol</th></tr></thead><tbody>';
      for (const r of el) {
        html += '<tr><td>' + esc(r.symbol) + '</td><td class="num">' + fmtNum(r.fundingRate, 6)
          + '</td><td class="num">' + fmtNum(r.funding_apr_pct, 2) + '</td><td class="num">' + fmtNum(r.markPrice, 6)
          + '</td><td class="num">' + fmtNum(r.quoteVolume24h, 0) + '</td></tr>';
      }
      html += '</tbody></table>';
    }
    html += '</section>';

    html += '<section><h2>Top funding rates (all perps)</h2>';
    const top = data.funding_top || [];
    html += '<table><thead><tr><th>Symbol</th><th class="num">Funding / interval</th><th class="num">APR %</th><th class="num">Mark</th></tr></thead><tbody>';
    for (const r of top) {
      const fr = Number(r.fundingRate);
      const fpd = Number(r.fundingsPerDay) || 3;
      const apr = fr * fpd * 365 * 100;
      html += '<tr><td>' + esc(r.symbol) + '</td><td class="num">' + fmtNum(r.fundingRate, 6)
        + '</td><td class="num">' + fmtNum(apr, 2) + '</td><td class="num">' + fmtNum(r.markPrice, 6) + '</td></tr>';
    }
    html += '</tbody></table></section>';

    const ca = (data.claude_advisor_recent || []).slice().reverse();
    const caPath = data.claude_advisor_file || 'claude_advisor_out.jsonl';
    html += '<section><h2>Claude advisor</h2>';
    html += '<p style="margin:0 0 0.75rem;color:var(--muted);font-size:0.85rem">'
      + 'Latest runs from <code>' + esc(caPath) + '</code> (newest first). Suggestions only — does not trade.</p>';
    if (!ca.length) {
      html += '<p style="color:var(--muted);margin:0">No runs yet. Run: <code>CLAUDE_ADVISOR_ENABLED=true python claude_advisor.py run</code></p>';
    } else {
      for (const entry of ca) {
        const inner = entry.advisor_json || {};
        const ts = entry.ts_unix ? new Date(entry.ts_unix * 1000).toISOString() : '';
        html += '<article style="border:1px solid #243044;border-radius:8px;padding:0.65rem 0.75rem;margin-bottom:0.6rem">';
        html += '<div style="font-size:0.72rem;color:var(--muted);margin-bottom:0.35rem">'
          + esc(ts) + ' · model ' + esc(entry.model || '') + '</div>';
        html += '<p style="margin:0 0 0.35rem"><strong>Summary</strong> ' + esc(inner.summary || '') + '</p>';
        const dn = inner.debug_notes;
        if (dn) {
          if (Array.isArray(dn) && dn.length) {
            html += '<p style="margin:0.35rem 0 0;font-size:0.82rem"><strong>Debug</strong></p><ul style="margin:0.2rem 0 0;padding-left:1.2rem;font-size:0.82rem">';
            for (const line of dn) {
              html += '<li>' + esc(String(line)) + '</li>';
            }
            html += '</ul>';
          } else if (typeof dn === 'string' && dn.trim()) {
            html += '<p style="margin:0.35rem 0 0;font-size:0.82rem"><strong>Debug</strong> ' + esc(dn) + '</p>';
          }
        }
        const rf = inner.risk_flags || [];
        if (rf.length) {
          html += '<p style="margin:0.35rem 0 0;font-size:0.82rem"><strong>Risk</strong> '
            + esc(rf.join('; ')) + '</p>';
        }
        const bl = inner.suggested_blacklist_add || [];
        if (bl.length) {
          html += '<p style="margin:0.35rem 0 0;font-size:0.82rem"><strong>Blacklist +</strong> '
            + esc(bl.join(', ')) + '</p>';
        }
        const envc = inner.suggested_env_changes || [];
        if (envc.length) {
          html += '<details style="margin-top:0.35rem"><summary style="cursor:pointer;color:var(--accent)">'
            + 'Suggested env (' + envc.length + ')</summary>';
          html += '<ul style="margin:0.35rem 0 0;padding-left:1.2rem;font-size:0.82rem">';
          for (const ch of envc) {
            html += '<li><code>' + esc(ch.key) + '</code> → ' + esc(ch.value) + ' — '
              + esc(ch.rationale || '') + '</li>';
          }
          html += '</ul></details>';
        }
        const scc = inner.suggested_code_changes || [];
        if (scc.length) {
          html += '<details style="margin-top:0.35rem"><summary style="cursor:pointer;color:var(--accent)">'
            + 'Code hints (' + scc.length + ')</summary>';
          html += '<ul style="margin:0.35rem 0 0;padding-left:1.2rem;font-size:0.82rem">';
          for (const ch of scc) {
            const fn = ch && ch.file != null ? String(ch.file) : '';
            const hint = ch && ch.hint != null ? String(ch.hint) : '';
            html += '<li><code>' + esc(fn) + '</code> — ' + esc(hint) + '</li>';
          }
          html += '</ul></details>';
        }
        if (inner.points_vs_carry_notes) {
          html += '<p style="margin:0.35rem 0 0;font-size:0.82rem;color:var(--muted)">'
            + esc(inner.points_vs_carry_notes) + '</p>';
        }
        html += '</article>';
      }
    }
    const csl = data.cycle_snapshot_last;
    if (csl && typeof csl === 'object') {
      html += '<p style="margin:0.75rem 0 0;font-size:0.8rem;color:var(--muted)">Last cycle snapshot (<code>'
        + esc(data.cycle_snapshot_file || '') + '</code>): open '
        + esc(JSON.stringify(csl.open_symbols || [])) + ', halted='
        + esc(String(csl.farming_halted)) + '</p>';
    }
    html += '</section>';

    const tr = data.recent_trades || [];
    html += '<section><h2>Recent trades (' + tr.length + ' rows)</h2>';
    if (!tr.length) {
      html += '<p style="color:var(--muted);margin:0">No rows in trade log yet.</p>';
    } else {
      const keys = Object.keys(tr[0]);
      html += '<div style="overflow-x:auto"><table><thead><tr>';
      for (const k of keys) {
        html += '<th>' + esc(k) + '</th>';
      }
      html += '</tr></thead><tbody>';
      for (const row of tr) {
        html += '<tr>';
        for (const k of keys) {
          html += '<td>' + esc(row[k]) + '</td>';
        }
        html += '</tr>';
      }
      html += '</tbody></table></div>';
    }
    html += '</section>';

    app.innerHTML = html;
  }

  async function load() {
    try {
      const r = await fetch('/api/snapshot');
      const data = await r.json();
      render(data);
    } catch (e) {
      document.getElementById('app').innerHTML = '<section><p class="err">Failed to load: ' + esc(e) + '</p></section>';
    }
  }
  load();
  setInterval(load, 20000);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/snapshot":
            data = json.dumps(build_snapshot(), indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404, "Not found")


def main() -> int:
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("DASHBOARD_PORT", "8765"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard: http://{host}:{port}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
