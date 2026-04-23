#!/usr/bin/env python3
"""
Report how many perp "pools" pass the same gates as `funding_farmer.py` (public REST),
and how deploy / slots relate to your env. Use with `.env` or Fly `fly secrets`.

Public endpoints only: GET /fapi/v1/premiumIndex, /fapi/v1/exchangeInfo, /fapi/v1/ticker/24hr
Optional: `--wallet` to load live margin + allocation cap (same code paths as the bot; needs API creds).

Examples:
  python3 scripts/pool_selection_tuner.py
  python3 scripts/pool_selection_tuner.py --wallet
  python3 scripts/pool_selection_tuner.py --sweep 0.0001,0.0002,0.0003,0.0005
  python3 scripts/pool_selection_tuner.py --fly-hints
  FUNDING_HISTORY_LOOKBACK_H=0 python3 scripts/pool_selection_tuner.py
  # Note: this script's .env with override may ignore shell; use:
  python3 scripts/pool_selection_tuner.py --funding-history-off

Also runs get_all_funding_rates + funding_farmer.enrich_rates_with_funding_history to compare
eligibility with your FUNDING_HISTORY_LOOKBACK_H vs 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import copy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

# Project root (parent of `scripts/`) — allow `python3 scripts/...` from any cwd
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
for _name in (".env", "env"):
    _p = _ROOT / _name
    if _p.is_file():
        load_dotenv(_p, override=True)
if not any((_ROOT / n).is_file() for n in (".env", "env")):
    load_dotenv()

from aster_client import get  # noqa: E402


@dataclass
class EnvSnapshot:
    min_funding_rate: float
    min_quote_volume_24h: float
    max_positions: int
    max_positions_auto: bool
    wallet_min_usd: float
    min_slot_usd: float
    wallet_deploy_pct: float
    leverage: int
    wallet_max_usd: float
    reserve_deploy_pct: float
    reserve_slot_for_new_pools: bool
    symbol_allowlist: Optional[Set[str]]
    blacklist: Set[str]
    funding_history_lookback_h: int
    # fee breakeven gate (opens only) — listed for completeness
    max_fee_breakeven_intervals: float
    est_taker_fee_bps: float


def _f(name: str, default: str) -> str:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def read_env() -> EnvSnapshot:
    mfr = float(_f("MIN_FUNDING_RATE", "0.0005"))
    mqv = float(_f("MIN_QUOTE_VOLUME_24H", "0") or 0)
    mpos = int(_f("MAX_POSITIONS", "7"))
    mauto = _f("MAX_POSITIONS_AUTO", "false").lower() in ("1", "true", "yes", "on")
    wmin = float(_f("WALLET_MIN_USD", "20"))
    mslot = _f("MIN_SLOT_USD", "").strip()
    try:
        mslot_f = float(mslot) if mslot else wmin
    except ValueError:
        mslot_f = wmin
    wdep = float(_f("WALLET_DEPLOY_PCT", "0.80"))
    lev = max(1, int(float(_f("LEVERAGE", "3"))))
    wmax = float(_f("WALLET_MAX_USD", "0") or 0)
    rsv_raw = _f("RESERVE_DEPLOY_PCT", "").strip()
    rsv_slot = _f("RESERVE_SLOT_FOR_NEW_POOLS", "false").lower() in ("1", "true", "yes", "on")
    if rsv_raw:
        rsv = min(0.95, max(0.0, float(rsv_raw)))
    elif rsv_slot:
        rsv = 1.0 / max(1, mpos)
    else:
        rsv = 0.0
    al = _f("SYMBOL_ALLOWLIST", "").strip()
    allow: Optional[Set[str]] = None
    if al:
        allow = {x.strip().upper() for x in al.split(",") if x.strip()}
    bl = {x.strip().upper() for x in _f("BLACKLIST", "").split(",") if x.strip()}
    fhh = 0
    try:
        fhh = max(0, int(_f("FUNDING_HISTORY_LOOKBACK_H", "0")))
    except ValueError:
        fhh = 0
    try:
        mfi = max(0.0, float(_f("MAX_FEE_BREAKEVEN_FUNDING_INTERVALS", "0") or 0))
    except ValueError:
        mfi = 0.0
    try:
        fee_bps = max(0.0, float(_f("ESTIMATED_TAKER_FEE_BPS", "5") or 5))
    except ValueError:
        fee_bps = 5.0
    return EnvSnapshot(
        min_funding_rate=mfr,
        min_quote_volume_24h=mqv,
        max_positions=mpos,
        max_positions_auto=mauto,
        wallet_min_usd=wmin,
        min_slot_usd=mslot_f,
        wallet_deploy_pct=wdep,
        leverage=lev,
        wallet_max_usd=wmax,
        reserve_deploy_pct=rsv,
        reserve_slot_for_new_pools=rsv_slot,
        symbol_allowlist=allow,
        blacklist=bl,
        funding_history_lookback_h=fhh,
        max_fee_breakeven_intervals=mfi,
        est_taker_fee_bps=fee_bps,
    )


def _tradable_usdt_symbols(exchange_data: dict) -> Set[str]:
    out: Set[str] = set()
    for s in exchange_data.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        sym = s.get("symbol")
        if sym and str(sym).endswith("USDT"):
            out.add(str(sym).upper())
    return out


def _quote_volumes_24h() -> Dict[str, float]:
    try:
        data = get("/fapi/v1/ticker/24hr", signed=False)
    except Exception as e:
        print(f"Warning: 24h ticker failed ({e}) — volume filter not applied in counts", file=sys.stderr)
        return {}
    out: Dict[str, float] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol")
        if not sym:
            continue
        try:
            out[str(sym).upper()] = float(row.get("quoteVolume", 0) or 0)
        except (TypeError, ValueError):
            continue
    return out


def _premium_rates(exchange_data: dict) -> List[Tuple[str, float]]:
    trad = _tradable_usdt_symbols(exchange_data)
    data = get("/fapi/v1/premiumIndex", signed=False)
    out: List[Tuple[str, float]] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        sym = item.get("symbol")
        if not sym or str(sym).upper() not in trad:
            continue
        try:
            fr = float(item.get("lastFundingRate", 0) or 0)
        except (TypeError, ValueError):
            fr = 0.0
        out.append((str(sym).upper(), fr))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _passes(
    sym: str,
    fr: float,
    min_f: float,
    env: EnvSnapshot,
    vols: Dict[str, float],
) -> bool:
    vol_on = env.min_quote_volume_24h > 0 and bool(vols)
    if fr < min_f:
        return False
    if sym in env.blacklist:
        return False
    if env.symbol_allowlist is not None and sym not in env.symbol_allowlist:
        return False
    if vol_on:
        if vols.get(sym, 0.0) < env.min_quote_volume_24h:
            return False
    return True


def count_eligible(
    rates: List[Tuple[str, float]],
    *,
    min_f: float,
    env: EnvSnapshot,
    vols: Dict[str, float],
) -> int:
    return sum(1 for sym, fr in rates if _passes(sym, fr, min_f, env, vols))


def list_eligible_pools(
    rates: List[Tuple[str, float]],
    *,
    min_f: float,
    env: EnvSnapshot,
    vols: Dict[str, float],
    limit: int,
) -> List[dict]:
    """Perps that pass the same pre-history gates as the farmer, high funding first."""
    out: List[dict] = []
    for sym, fr in rates:
        if not _passes(sym, fr, min_f, env, vols):
            continue
        qv = vols.get(sym)
        out.append(
            {
                "symbol": sym,
                "last_funding_rate": fr,
                "funding_rate_pct_8h_eq": fr * 100,  # label matches farmer's stored scale
                "quote_volume_24h_usd": qv,
            }
        )
        if len(out) >= limit:
            break
    return out


def _passes_enriched_row(
    r: dict,
    min_f: float,
    env: EnvSnapshot,
    vols: Dict[str, float],
) -> bool:
    """
    Like funding_farmer _pool_symbol_eligible_core, including funding_hist_pool_ok
    when FUNDING_HISTORY_LOOKBACK_H > 0 in env.
    """
    sym = str(r.get("symbol") or "")
    try:
        fr = float(r.get("fundingRate", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not _passes(sym, fr, min_f, env, vols):
        return False
    if (
        env.funding_history_lookback_h > 0
        and r.get("funding_hist_pool_ok") is False
    ):
        return False
    return True


def _count_list_enriched(
    rates_enriched: List[dict],
    *,
    min_f: float,
    env: EnvSnapshot,
    vols: Dict[str, float],
) -> int:
    return sum(
        1
        for r in rates_enriched
        if _passes_enriched_row(r, min_f, env, vols)
    )


def _list_eligible_enriched(
    rates_enriched: List[dict],
    *,
    min_f: float,
    env: EnvSnapshot,
    vols: Dict[str, float],
    limit: int,
) -> List[dict]:
    out: List[dict] = []
    for r in rates_enriched:
        if not _passes_enriched_row(r, min_f, env, vols):
            continue
        sym = str(r.get("symbol", ""))
        fr = float(r.get("fundingRate", 0) or 0)
        out.append(
            {
                "symbol": sym,
                "last_funding_rate": fr,
                "funding_rate_pct_8h_eq": fr * 100,
                "quote_volume_24h_usd": vols.get(sym),
                "funding_hist_pool_ok": r.get("funding_hist_pool_ok"),
            }
        )
        if len(out) >= limit:
            break
    return out


def run_funding_history_enrich_compare(
    env: EnvSnapshot,
    vols: Dict[str, float],
    *,
    list_n: int,
    min_f: float,
) -> Optional[dict]:
    """
    Uses funding_farmer.get_all_funding_rates + enrich_rates_with_funding_history.
    Returns counts/lists for current LOOKBACK_H and for 0, or None on import/API failure.
    """
    try:
        import funding_farmer as ff  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not import funding_farmer: {e}"}
    try:
        base: List[dict] = ff.get_all_funding_rates()
    except Exception as e:  # noqa: BLE001
        return {"error": f"get_all_funding_rates: {e}"}
    if not base:
        return {"error": "No rates from get_all_funding_rates()"}
    orig_lb = ff.FUNDING_HISTORY_LOOKBACK_H
    try:
        h_rates: List[dict] = copy.deepcopy(base)
        ff.FUNDING_HISTORY_LOOKBACK_H = env.funding_history_lookback_h
        ff.enrich_rates_with_funding_history(h_rates)

        z_rates: List[dict] = copy.deepcopy(base)
        ff.FUNDING_HISTORY_LOOKBACK_H = 0
        ff.enrich_rates_with_funding_history(z_rates)
    finally:
        ff.FUNDING_HISTORY_LOOKBACK_H = orig_lb

    env0 = replace(env, funding_history_lookback_h=0)
    rows_sweep: List[dict] = []
    for mf in sorted(
        {0.0, min_f, min_f * 0.5, min_f * 0.25, max(0.0, min_f * 2.0), min_f * 4.0}
    ):
        c_h = _count_list_enriched(h_rates, min_f=mf, env=env, vols=vols)
        c_z = _count_list_enriched(z_rates, min_f=mf, env=env0, vols=vols)
        rows_sweep.append(
            {
                "min_f": mf,
                "count_with_env_funding_history_lookback": c_h,
                "count_with_lookback_zero": c_z,
            }
        )

    top_h = _list_eligible_enriched(
        h_rates, min_f=min_f, env=env, vols=vols, limit=list_n
    )
    top_z = _list_eligible_enriched(
        z_rates, min_f=min_f, env=env0, vols=vols, limit=list_n
    )
    c_at_min_h = _count_list_enriched(h_rates, min_f=min_f, env=env, vols=vols)
    c_at_min_z = _count_list_enriched(
        z_rates, min_f=min_f, env=env0, vols=vols
    )
    return {
        "funding_history_lookback_h_in_env": env.funding_history_lookback_h,
        "at_min_funding": {
            "with_env_lookback": c_at_min_h,
            "with_lookback_zero": c_at_min_z,
        },
        "sweep": rows_sweep,
        "top_with_env_lookback": top_h,
        "top_with_lookback_zero": top_z,
    }


def _effective_max_slots(deploy_cap: float, env: EnvSnapshot) -> int:
    if not env.max_positions_auto:
        return env.max_positions
    fl = max(env.wallet_min_usd, env.min_slot_usd)
    if fl <= 1e-12:
        fl = env.wallet_min_usd
    if deploy_cap <= 0:
        return 1
    n_budget = max(1, int(deploy_cap // fl))
    return max(1, min(env.max_positions, n_budget))


def run_wallet_block(env: EnvSnapshot) -> Optional[dict]:
    from aster_client import credentials_ok  # noqa: WPS433

    if not credentials_ok():
        return {"error": "aster_client credentials not configured; cannot load wallet (set ASTER_USER / ASTER_SIGNER / ASTER_SIGNER_PRIVATE_KEY on Fly or .env)."}

    import funding_farmer as ff  # noqa: WPS433

    collateral = ff.get_collateral_summary()
    if not collateral or collateral.get("_total_effective_margin") in (None, 0, 0.0):
        return {
            "error": "get_collateral_summary() empty or zero margin; check /fapi/v2/balance and account read.",
        }
    total_budget = ff.compute_deploy_budget(collateral)
    deploy_cap = ff.effective_deploy_cap(total_budget)
    eff_margin = float(collateral.get("_total_effective_margin", 0) or 0)
    open_syms: set = set()
    sizes: dict = {}
    ff.sync_open_long_state_from_exchange(open_syms, sizes, log_each=False)
    deployed = float(sum(sizes.values()))
    avail = ff.available_budget(deploy_cap, sizes)
    eff_max = _effective_max_slots(deploy_cap, env)
    n_open = len(open_syms)
    slots = max(eff_max - n_open, 0)
    return {
        "effective_margin_usd": eff_margin,
        "total_max_deploy_notional": total_budget,
        "allocation_cap_after_reserve": deploy_cap,
        "reserve_deploy_fraction": env.reserve_deploy_pct,
        "deployed_notional_sizing": deployed,
        "pool_dry_powder": avail,
        "max_concurrent_longs": eff_max,
        "open_longs": n_open,
        "open_symbols": sorted(open_syms),
        "free_slots": slots,
        "capped_by_wallet_max": env.wallet_max_usd > 0 and total_budget <= env.wallet_max_usd - 1e-6,
        "margin_capped_by_account": bool(collateral.get("_margin_capped_by_account")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Tuning report for pool filters vs live Aster market (same public inputs as the farmer).",
    )
    ap.add_argument(
        "--sweep",
        default="",
        help="Comma-separated MIN_FUNDING_RATE values to compare (e.g. 0.0001,0.0003,0.0005). Default: auto from your MIN_FUNDING_RATE.",
    )
    ap.add_argument(
        "--wallet",
        action="store_true",
        help="Import funding_farmer and show margin, deploy cap, and slot headroom (needs API creds).",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable output (stdout).",
    )
    ap.add_argument(
        "--fly-hints",
        action="store_true",
        help="Print example `fly secrets set` lines for the main knobs (review before applying).",
    )
    ap.add_argument(
        "--list",
        type=int,
        metavar="N",
        default=40,
        dest="list_n",
        help="Print top N eligible perps (MIN_FUNDING from env) with rate and 24h vol. 0=skip. Default: 40.",
    )
    ap.add_argument(
        "--funding-history-off",
        action="store_true",
        help="Use eligibility as if FUNDING_HISTORY_LOOKBACK_H=0 (enrich+pool_ok like the bot; preview Fly secret=0).",
    )
    ap.add_argument(
        "--no-farmer-enrich",
        action="store_true",
        help="Skip get_all_funding_rates + enrich (no history compare; tuple-based list only).",
    )
    args = ap.parse_args()
    env = read_env()
    env_effective: EnvSnapshot = (
        replace(env, funding_history_lookback_h=0)
        if args.funding_history_off
        else env
    )

    ex = get("/fapi/v1/exchangeInfo", signed=False)
    rates = _premium_rates(ex)
    vols = _quote_volumes_24h()

    if args.sweep.strip():
        try:
            sweep = [float(x.strip()) for x in args.sweep.split(",") if x.strip()]
        except ValueError:
            print("Invalid --sweep: use numbers like 0.0001,0.0002", file=sys.stderr)
            return 1
    else:
        m = env.min_funding_rate
        sweep = sorted(
            {max(0.0, m), max(0.0, m * 0.5), max(0.0, m * 0.25), 0.0, m * 1.0}
        )
        sweep = sorted({round(x, 8) for x in sweep if x >= 0})

    rows: List[dict] = []
    for mf in sorted(sweep):
        c = count_eligible(
            rates,
            min_f=mf,
            env=env_effective,
            vols=vols,
        )
        rows.append(
            {
                "min_funding_rate": mf,
                "count_eligible_sym": c,
            }
        )

    vol_note = "on" if (env.min_quote_volume_24h > 0 and bool(vols)) else "off (0 or ticker failed)"
    al_note = "set" if env.symbol_allowlist else "none"
    hist_note = (
        f">0 ({env.funding_history_lookback_h}h) (enrich compare in script if enabled)"
        if env.funding_history_lookback_h > 0
        else "0 (off)"
    )
    enrich_cmp: Any = None
    if not args.no_farmer_enrich:
        enrich_cmp = run_funding_history_enrich_compare(
            env, vols, list_n=max(0, int(args.list_n)), min_f=env.min_funding_rate
        )

    def _env_for_json() -> dict:
        d = asdict(env)
        d["symbol_allowlist"] = (
            sorted(env.symbol_allowlist) if env.symbol_allowlist is not None else None
        )
        d["blacklist"] = sorted(env.blacklist)
        return d

    out: dict = {
        "usdt_tradable_in_exchange_info": len(_tradable_usdt_symbols(ex)),
        "premium_index_rows_matched": len(rates),
        "env": _env_for_json(),
        "volume_filter": vol_note,
        "min_quote_volume_24h": env.min_quote_volume_24h,
        "symbol_allowlist": al_note,
        "funding_history_lookback_h": hist_note,
        "counts_by_min_funding": rows,
        "funding_history_enrich_compare": enrich_cmp,
    }

    wallet: Optional[dict] = None
    if args.wallet:
        wallet = run_wallet_block(env)
        out["wallet"] = wallet
    else:
        wallet = None

    if (
        not args.no_farmer_enrich
        and isinstance(enrich_cmp, dict)
        and "error" not in enrich_cmp
    ):
        if args.funding_history_off or env.funding_history_lookback_h == 0:
            at_min = enrich_cmp.get("top_with_lookback_zero") or []
        else:
            at_min = enrich_cmp.get("top_with_env_lookback") or []
    else:
        at_min = list_eligible_pools(
            rates,
            min_f=env_effective.min_funding_rate,
            env=env_effective,
            vols=vols,
            limit=max(0, int(args.list_n)),
        )
    if at_min:
        out["eligible_pools_at_current_min_funding"] = at_min
    else:
        out["eligible_pools_at_current_min_funding"] = []

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print("=" * 72)
        print("Pool selection tuning (read-only) — public routes + your env")
        print("=" * 72)
        print(f"  USDT+TRADING symbols in exchangeInfo:  {out['usdt_tradable_in_exchange_info']}")
        print(f"  premiumIndex rows matched:             {out['premium_index_rows_matched']}")
        print(f"  24h volume filter:                    {out['volume_filter']}")
        print(
            f"  MIN_QUOTE_VOLUME_24H:                 {env.min_quote_volume_24h:,.0f} USDT"
        )
        if env.symbol_allowlist:
            print(
                f"  SYMBOL_ALLOWLIST:                     {al_note}  "
                f"({len(env.symbol_allowlist)} sym)"
            )
        else:
            print("  SYMBOL_ALLOWLIST:                     none")
        print(
            f"  BLACKLIST entries:                    {len(env.blacklist)}"
        )
        print(
            f"  FUNDING_HISTORY_LOOKBACK_H:            {env.funding_history_lookback_h}  "
            f"({'enrich compare below' if env.funding_history_lookback_h > 0 else 'off'})"
        )
        print()
        print(
            f"  MIN_FUNDING_RATE (env):               {env.min_funding_rate:.6g}  "
            f"({env.min_funding_rate * 100:.4f}% per 8h-style quote scale)"
        )
        print("  Count of pools (tuple snapshot, no FUNDING_HISTORY gating):")
        for r in rows:
            print(
                f"    min_f={r['min_funding_rate']:.6g}  ->  {r['count_eligible_sym']} symbol(s)"
            )
        if not args.json and isinstance(enrich_cmp, dict):
            if enrich_cmp.get("error"):
                print()
                print(
                    f"  Farmer enrich compare:  {enrich_cmp['error']}"
                )
            else:
                a = enrich_cmp["at_min_funding"]["with_env_lookback"]
                b = enrich_cmp["at_min_funding"]["with_lookback_zero"]
                hlb = enrich_cmp.get("funding_history_lookback_h_in_env", 0)
                print()
                print("  Farmer enrich (same as bot: get_all_funding_rates + funding history):")
                print(
                    f"    At MIN_FUNDING with FUNDING_HISTORY_LOOKBACK_H={hlb}:  {a} pool(s) eligible"
                )
                print(
                    f"    At MIN_FUNDING with LOOKBACK_H=0:                   {b} pool(s) eligible  "
                    f"(delta: {b - a:+d} vs current lookback)"
                )
                for row in enrich_cmp.get("sweep", [])[:12]:
                    print(
                        f"    sweep min_f={row['min_f']:.6g}  with_lookback={row['count_with_env_funding_history_lookback']}  "
                        f"lookback_0={row['count_with_lookback_zero']}"
                    )
        if args.funding_history_off and not args.json:
            print()
            print("  (Preview: list below uses FUNDING_HISTORY_LOOKBACK_H=0. Set in Fly: fly secrets set FUNDING_HISTORY_LOOKBACK_H=0)")
        open_set: Set[str] = set()
        if wallet and "error" not in wallet and wallet.get("open_symbols"):
            open_set = set(wallet["open_symbols"])
        if at_min and args.list_n > 0:
            print()
            if args.no_farmer_enrich or enrich_cmp is None or enrich_cmp.get("error"):
                _mode = "tuple snapshot (no funding history in row)"
            elif args.funding_history_off or env.funding_history_lookback_h == 0:
                _mode = "enrich + LOOKBACK_H=0"
            else:
                _mode = f"enrich + LOOKBACK_H={env.funding_history_lookback_h}"
            print(
                f"  Eligible perps (top {len(at_min)}; {_mode}) — notional / fees / corr not applied"
            )
            for row in at_min:
                sym = row["symbol"]
                fr = float(row["last_funding_rate"])
                qv = row.get("quote_volume_24h_usd")
                tag = "  [OPEN]" if sym in open_set else ""
                qvs = f"{qv:,.0f}" if qv is not None else "n/a"
                print(
                    f"    {sym:<16}  rate={fr:+.6f}  ({fr * 100:+.4f}%)  24h quote $ {qvs}{tag}"
                )
        if wallet and wallet.get("error"):
            print()
            print("  [wallet]", wallet["error"])
        elif wallet and "error" not in wallet:
            w = wallet
            print()
            print("  Wallet / deploy (same math as live bot):")
            print(
                f"    Effective margin (sizing) ~ ${w['effective_margin_usd']:,.0f}  "
                f"(cap_by_account={w.get('margin_capped_by_account')})"
            )
            print(
                f"    Max deploy (notional cap)  ${w['total_max_deploy_notional']:,.0f}  "
                f"  allocation cap (after reserve) ${w['allocation_cap_after_reserve']:,.0f}"
            )
            print(
                f"    Deployed (sizing)           ${w['deployed_notional_sizing']:,.0f}  "
                f"  pool dry powder ${w['pool_dry_powder']:,.0f}"
            )
            print(
                f"    Slots: {w['open_longs']} open, max {w['max_concurrent_longs']}, "
                f"free {w['free_slots']}"
            )
            if w.get("open_symbols"):
                syms = ", ".join(w["open_symbols"][:32])
                more = len(w["open_symbols"]) - 32
                ex = f"  (+{more} more)" if more > 0 else ""
                print(f"    Open longs:  {syms}{ex}")
        print()
        if args.fly_hints:
            app = _f("FLY_APP", "aster-funding-farmer")
            print("  Example Fly adjustments (unquoted values — set your app name):")
            print(
                f"    fly secrets set -a {app} MIN_FUNDING_RATE=0.0002"
            )
            print(
                f"    fly secrets set -a {app} MIN_QUOTE_VOLUME_24H=0"
            )
            print(
                f"    fly secrets set -a {app} MAX_POSITIONS=10"
            )
            print(
                f"    fly secrets set -a {app} RESERVE_DEPLOY_PCT=0.05"
            )
            print(
                f"    fly secrets set -a {app} RESERVE_SLOT_FOR_NEW_POOLS=false"
            )
            print(
                f"    fly secrets set -a {app} WALLET_MAX_USD=0"
            )
            print(
                f"    fly secrets set -a {app} WALLET_DEPLOY_PCT=0.9"
            )
            print(
                f"    fly secrets set -a {app} FUNDING_HISTORY_LOOKBACK_H=0  # or unset: disable history gate"
            )
            print("  (Lower MIN = more names qualify; 0 = no volume filter; more MAX_POSITIONS = more")
            print("  concurrent longs. RESERVE_* controls idle buffer; LOOKBACK 0 = no min/median history filter.)")
            print()
        print("  To sweep custom floors:  --sweep 0.0001,0.0002,0.0003")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
