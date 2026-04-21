#!/usr/bin/env python3
"""
Profit & points helper — watch trades.csv as rows arrive, inspect risk/funding "levers",
and read static tips on balancing PnL vs Aster Stage 6–style points.

Does not place trades. Optional: disable advise with PROFIT_ASSISTANT_ENABLED=false in .env
(watch still works).

  python profit_assistant.py watch          # tail trades.csv (TRADE_LOG_FILE)
  python profit_assistant.py levers         # show key .env knobs + what they do
  python profit_assistant.py tips           # profit vs points tradeoffs
  python profit_assistant.py kpi            # primary KPIs (profit vs points proxies)
  python profit_assistant.py summary        # aggregate CLOSE rows from trades.csv
  python profit_assistant.py watch --from-start   # replay whole file then follow

  kpi and summary run even when PROFIT_ASSISTANT_ENABLED=false (same as watch).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

TRADE_LOG_FILE = os.getenv("TRADE_LOG_FILE", "trades.csv")
PROFIT_ASSISTANT_ENABLED = os.getenv("PROFIT_ASSISTANT_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)


def _guard_assist() -> bool:
    if PROFIT_ASSISTANT_ENABLED:
        return True
    print(
        "PROFIT_ASSISTANT_ENABLED=false — levers/tips disabled. "
        "Set PROFIT_ASSISTANT_ENABLED=true in .env or use: watch | kpi | summary",
        file=sys.stderr,
    )
    return False


# (env key, default if missing, one-line effect)
LEVERS: List[Tuple[str, str, str]] = [
    ("DRY_RUN", "false", "Paper fills only; same paths & sizing as live (live marks/rates)"),
    ("DRY_RUN_SIMULATED_MARGIN_USD", "0", "Fake collateral USD for sizing when >0; 0 = live API margin"),
    ("DRY_RUN_SHOW_LIVE_WALLET_DETAILS", "true", "Dry run: log live futures/spot balances (default true)"),
    ("LEVERAGE", "3", "Higher = more PnL per move + faster liquidation risk"),
    ("WALLET_DEPLOY_PCT", "0.80", "Deploy fraction × collateral; budget × LEVERAGE = max notional"),
    ("WALLET_MAX_USD", "0", "Hard cap on deploy budget (0 = none)"),
    ("WALLET_MIN_USD", "20", "Minimum per new position (exchange + slippage)"),
    ("BALANCE_DUST_USD", "5", "Hide spot/futures balance lines below this USD estimate"),
    ("MAX_POSITIONS", "7", "More names = diversification; splits budget per leg"),
    ("RESERVE_SLOT_FOR_NEW_POOLS", "false", "true = reserve 1/MAX_POSITIONS deploy for new pools"),
    ("RESERVE_DEPLOY_PCT", "", "Optional fraction 0–0.95 reserve; overrides slot mode if set"),
    ("RANK_TOP_PCT", "0.25", "Top funding symbol’s share of deploy budget"),
    ("MAX_SINGLE_PCT", "0.30", "Max % of budget in one symbol"),
    ("MIN_FUNDING_RATE", "0.0005", "Floor to open (higher = pickier, safer carry)"),
    ("EXIT_FUNDING_RATE", "0.0001", "Close long if funding falls below this (same units as API lastFundingRate)"),
    ("STOP_LOSS_PCT", "0.05", "Close if mark vs entry adverse by this fraction"),
    ("TAKE_PROFIT_PCT", "0", "Close if mark vs entry up by this fraction (0=off)"),
    ("FUNDING_EXIT_USE_WS_ESTIMATED", "false", "Use markPrice WS field r for funding_dropped when set"),
    ("FUNDING_SIGN_SELF_CHECK_CYCLES", "36", "Live: compare FUNDING_FEE income vs rate every N loops (0=off)"),
    ("FUNDING_OPEN_BLOCK_LAST_SEC", "0", "Skip new opens if secs to next funding < this (0=off)"),
    ("FUNDING_OPEN_PAUSE_AFTER_SETTLE_SEC", "0", "After funding-time step, pause new opens on that symbol (0=off)"),
    ("FUNDING_SYNC_IDLE_SLEEP", "false", "When flat, cap idle sleep toward next funding (top-N REST times)"),
    ("FUNDING_SYNC_IDLE_TOP_N", "20", "Symbols considered for idle sleep cap"),
    ("FUNDING_SYNC_BUFFER_SEC", "10", "Seconds before next funding to wake early"),
    ("FUNDING_HISTORY_LOOKBACK_H", "0", "Hours of GET /fapi/v1/fundingRate for top-N (0=off)"),
    ("FUNDING_HISTORY_TOP_N", "20", "How many snapshot leaders fetch history per cycle"),
    ("FUNDING_HISTORY_LIMIT", "50", "Max fundingRate rows per symbol (API cap 1000)"),
    ("FUNDING_HISTORY_CACHE_TTL_SEC", "900", "Cache TTL for history fetches"),
    ("FUNDING_RANK_BLEND_WEIGHT", "0", "0–1 blend snapshot vs hist mean for sort"),
    ("FUNDING_HISTORY_REQUIRE", "", "Pool filter: min|median vs MIN_FUNDING_RATE (empty=off)"),
    ("FUNDING_HISTORY_SPIKE_RATIO", "0", "Skip pool if |snap|/|mean| exceeds this (0=off)"),
    ("POLL_INTERVAL_SEC", "60", "Scan interval when flat (new opportunities)"),
    ("RISK_POLL_INTERVAL_SEC", "15", "Scan interval while a long is open (risk)"),
    ("MARK_PRICE_WS", "true", "Faster mark-based stop vs REST-only"),
    ("DELTA_NEUTRAL", "false", "HL short hedge: less delta, different HL fees/slippage"),
    ("BLACKLIST", "", "Never trade these symbols"),
    ("MIN_QUOTE_VOLUME_24H", "0", "Min 24h USDT volume on a perp; 0 = off; cuts illiquid names"),
    ("SYMBOL_ALLOWLIST", "", "If set, only these symbols (comma-sep) can be opened"),
    ("FARMING_HALT", "false", "Skip new opens only; stop-loss / take-profit / funding exits still run"),
    ("FARMING_HALT_FILE", "", "If this path exists, same as halt (touch file to stop new opens)"),
    ("CYCLE_SNAPSHOT_ENABLE", "false", "Append one JSON line per farmer cycle for alerts/Claude"),
    ("CYCLE_SNAPSHOT_FILE", "farmer_cycle.jsonl", "Path for cycle snapshot JSONL ring buffer"),
    ("CLAUDE_ADVISOR_LOOP_ON_FLY", "false", "Docker/Fly: background claude_advisor loop in entrypoint"),
    ("CLAUDE_ADVISOR_LOOP_SLEEP_SEC", "180", "Seconds between claude_advisor.py run when loop on Fly"),
    ("ESTIMATED_TAKER_FEE_BPS", "5", "Assumed fee each side for fee-breakeven gate (see farmer)"),
    ("MAX_FEE_BREAKEVEN_FUNDING_INTERVALS", "0", "Skip opens if RT fees need more intervals (0=off)"),
]


def cmd_levers() -> int:
    if not _guard_assist():
        return 1
    print("Key knobs (from your environment — same names as .env)\n")
    print(f"{'Variable':<30} {'Value':<38} Notes")
    print("-" * 100)
    for key, default, note in LEVERS:
        val = os.getenv(key)
        shown = (
            val
            if val is not None and str(val).strip() != ""
            else f"<unset, default {default}>"
        )
        if len(shown) > 36:
            shown = shown[:35] + "…"
        print(f"{key:<30} {shown:<38} {note}")
    print("\nEdit .env, restart funding_farmer.py. Validate in DRY_RUN first.")
    return 0


def cmd_tips() -> int:
    if not _guard_assist():
        return 1
    print(
        """\
Aster-style incentives (conceptual — check official rules for Stage 6):
  • Trading points: fees on open/close — larger size + more turns = more, but costs cap.
  • Position points: size × hold time — bigger notional and longer holds score more.
  • Aster Asset points: USDF + ASTER as margin (multi-asset mode) add bonus without selling.

Profit (funding carry) tradeoffs:
  • Higher MIN_FUNDING_RATE → fewer opens, stronger carry per slot, may miss short spikes.
  • More MAX_POSITIONS / lower RANK_TOP_PCT → diversified funding exposure, smaller per-symbol size.
  • Higher LEVERAGE → same notional uses less margin but liquidation closer — use with tight STOP_LOSS_PCT & RISK_POLL_INTERVAL_SEC.
  • Lower STOP_LOSS_PCT → exit faster on dips, more churn (fees) and missed recovery.
  • MARK_PRICE_WS + low RISK_POLL_INTERVAL_SEC → faster reaction to marks vs periodic funding settlements.

Suggested workflow:
  1) DRY_RUN=true, tune levers, use:  python profit_assistant.py watch
  2) After sessions:  python profit_assistant.py summary  (totals on CLOSE rows)
  3) Review trades.csv: primary $ KPI = pnl_net_incl_funding_usdt; pnl_usdt = price PnL net fees only.
  4) Go live with small WALLET_MAX_USD, then widen.
"""
    )
    return 0


def cmd_kpi() -> int:
    """Documented primary metrics for profit vs Stage-6-style points (no on-chain scoring in-repo)."""
    print(
        """\
Primary KPIs (this repo — choose what you optimize for):

  1) Dollar profit (recommended for “still in profit”)
     • Sum CLOSE rows:  pnl_net_incl_funding_usdt  (= mark PnL − fees + realized FUNDING_FEE in hold window)
     • Do not use pnl_usdt alone for funding farms — it excludes funding accrual until close aggregates it.
     • Command:  python profit_assistant.py summary

  2) Drawdown / risk (constraint, not a CSV column)
     • Tune STOP_LOSS_PCT, LEVERAGE, WALLET_MAX_USD, MARK_PRICE_WS + RISK_POLL_INTERVAL_SEC
     • Track worst peak-to-trough on margin or per-close pnl_net_incl_funding_usdt in your own sheet.

  3) Points (qualitative — official Stage 6 rules are on docs.asterdex.com)
     • Trading points proxy: fees_usdt on closes (more churn → more fees → more trading points, less $ net).
     • Position points proxy: notional_usdt × hold_duration_min on closes (bigger + longer → more).
     • Aster Asset points: USDF + ASTER futures margin + multi-asset mode (farmer logs [Stage6 margin] at startup).
     • The bot does not maximize points; it ranks by lastFundingRate. Align .env levers after picking (1) vs (3).

Entry signal note: new opens still use REST lastFundingRate only; optional WS field ``r`` is for funding_dropped exits when enabled — not used for ranking today.
"""
    )
    return 0


def _parse_float_cell(raw: str) -> Optional[float]:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def cmd_summary(trade_path: Path) -> int:
    """Aggregate CLOSE rows from TRADE_LOG_FILE for tuning MIN_FUNDING_RATE / exits / sizing."""
    path = trade_path.resolve()
    if not path.is_file():
        print(f"No file: {path}", file=sys.stderr)
        return 1
    closes: List[Dict[str, str]] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or ()
        for row in reader:
            if (row.get("action") or "").strip().upper() != "CLOSE":
                continue
            closes.append(row)

    n = len(closes)
    print(f"Trade log: {path}\nCLOSE rows: {n}\n")

    if n == 0:
        print("No closes yet — run DRY_RUN or live, then re-run summary.")
        return 0

    def col_sum(key: str) -> Tuple[float, int]:
        s = 0.0
        c = 0
        for r in closes:
            v = _parse_float_cell(r.get(key, "") or "")
            if v is not None:
                s += v
                c += 1
        return s, c

    pnl_net, _ = col_sum("pnl_usdt")
    fees, _ = col_sum("fees_usdt")
    gross, _ = col_sum("pnl_gross_usdt")
    fund, nf = col_sum("funding_income_usdt")
    pnl_all, na = col_sum("pnl_net_incl_funding_usdt")

    print(f"  Sum pnl_usdt (price, net fees, no funding in col):     ${pnl_net:+,.4f}")
    print(f"  Sum pnl_gross_usdt (price only, before fees):          ${gross:+,.4f}")
    print(f"  Sum fees_usdt (entry+exit commissions on closes):      ${fees:+,.4f}")
    if nf:
        print(f"  Sum funding_income_usdt (window on close):             ${fund:+,.4f}  ({nf} rows)")
    else:
        print("  Sum funding_income_usdt:                              (column missing or empty)")
    if na:
        print(
            f"  Sum pnl_net_incl_funding_usdt **primary $ KPI**:      ${pnl_all:+,.4f}  ({na} rows)"
        )
    else:
        print(
            "  Sum pnl_net_incl_funding_usdt:                        (column missing — upgrade farmer / migrate CSV)"
        )

    by_reason: Dict[str, int] = {}
    for r in closes:
        reason = (r.get("close_reason") or "").strip() or "(empty)"
        by_reason[reason] = by_reason.get(reason, 0) + 1
    print("\nClose reasons:")
    for reason, cnt in sorted(by_reason.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {cnt:4d}  {reason}")

    hold_sum, nh = col_sum("hold_duration_min")
    if nh:
        print(f"\nMean hold (min): {hold_sum / nh:.1f}  (over {nh} closes with hold_duration_min)")

    print("\nTune loop: adjust MIN_FUNDING_RATE, EXIT_FUNDING_RATE, STOP_LOSS_PCT, MAX_POSITIONS, "
          "RANK_TOP_PCT in .env → DRY_RUN → summary again.")
    return 0


def _fmt_row(row: Dict[str, Any]) -> str:
    action = row.get("action", "")
    sym = row.get("symbol", "")
    reason = row.get("close_reason", "")
    pid = row.get("order_id", "")
    ts = row.get("timestamp_utc", "")
    base = f"[{ts}] {action:5} {sym:14} id={pid}"
    if action == "CLOSE":
        pnl = row.get("pnl_usdt", "")
        pctp = row.get("pnl_pct", "")
        fees = (row.get("fees_usdt") or "").strip()
        fee_part = f"  fees={fees}" if fees else ""
        p_all = (row.get("pnl_net_incl_funding_usdt") or "").strip()
        fund = (row.get("funding_income_usdt") or "").strip()
        inc_part = ""
        if p_all or fund:
            inc_part = f"  pnl+funding={p_all}" + (f"  fund={fund}" if fund else "")
        base += f"  pnl_net={pnl} ({pctp}%){fee_part}{inc_part}  reason={reason}"
    else:
        apr = row.get("funding_apr_pct", "")
        n = row.get("notional_usdt", "")
        fee_e = (row.get("fee_entry_usdt") or "").strip()
        fee_part = f"  fee≈{fee_e}" if fee_e else ""
        base += (
            f"  notional≈${n}  APR~{apr}%  fund/8h={row.get('funding_rate_8h','')}"
            f"{fee_part}"
        )
    return base


def cmd_watch(trade_path: Path, from_start: bool) -> int:
    path = trade_path.resolve()
    print(f"Watching {path}  (Ctrl+C to stop)\n", file=sys.stderr)
    seen = 0
    if path.exists():
        with open(path, newline="") as f:
            r = csv.DictReader(f)
            rows = list(r)
            if from_start:
                for row in rows:
                    print(_fmt_row(row))
                seen = len(rows)
            else:
                seen = len(rows)

    try:
        while True:
            if not path.exists():
                time.sleep(0.5)
                continue
            with open(path, newline="") as f:
                r = csv.DictReader(f)
                rows = list(r)
            if len(rows) > seen:
                for row in rows[seen:]:
                    print(_fmt_row(row), flush=True)
                seen = len(rows)
            time.sleep(0.8)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Profit assistant — watch trades, inspect levers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_w = sub.add_parser("watch", help="Follow TRADE_LOG_FILE / trades.csv as new rows append")
    p_w.add_argument(
        "--file",
        type=Path,
        default=None,
        help=f"CSV path (default: env TRADE_LOG_FILE or {TRADE_LOG_FILE})",
    )
    p_w.add_argument(
        "--from-start",
        action="store_true",
        help="Print existing rows first, then follow",
    )

    sub.add_parser("levers", help="List main .env levers and what they affect")
    sub.add_parser("tips", help="Notes on profit vs points and risk knobs")
    sub.add_parser("kpi", help="Primary KPIs: $ net incl. funding vs points proxies")

    p_s = sub.add_parser("summary", help="Aggregate CLOSE rows (pnl, fees, funding) from CSV")
    p_s.add_argument(
        "--file",
        type=Path,
        default=None,
        help=f"CSV path (default: env TRADE_LOG_FILE or {TRADE_LOG_FILE})",
    )

    args = ap.parse_args()

    if args.cmd == "watch":
        fpath = args.file or Path(TRADE_LOG_FILE)
        return cmd_watch(fpath, args.from_start)
    if args.cmd == "levers":
        return cmd_levers()
    if args.cmd == "tips":
        return cmd_tips()
    if args.cmd == "kpi":
        return cmd_kpi()
    if args.cmd == "summary":
        fpath = args.file or Path(TRADE_LOG_FILE)
        return cmd_summary(fpath)
    return 1


if __name__ == "__main__":
    sys.exit(main())
