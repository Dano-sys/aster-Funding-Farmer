#!/usr/bin/env python3
"""
Small staged run: optional sizing defaults, clean slate, then funding_farmer.run().

  Default mode (no --live-small):
  - Does not modify your .env file.
  - os.environ.setdefault only: WALLET_MAX_USD=150, MAX_POSITIONS=2, LEVERAGE=2,
    RESERVE_SLOT_FOR_NEW_POOLS=false (skipped for keys already set, e.g. Fly secrets).

  --live-small  (minimal real-money for THIS PROCESS ONLY):
  - Forces DRY_RUN=false and tight caps (overrides .env and Fly secrets for this process).
  - Still does not write your .env file.
  - Use --live-small-pools N for concurrent symbols (default 3). Budget floor is at least N×$20.
  - Omit --max-cycles for continuous run (e.g. Fly worker). Use --no-clean-slate on Fly so
    restarts do not market-close all positions.

  Clean slate (unless --no-clean-slate):
  - DRY_RUN: clears in-memory paper positions.
  - Live: market-flattens every non-zero Aster perp (longs via close_long, shorts via exchange).
  - Does not cancel unrelated open limits; does not close Hyperliquid hedges.

  Live + --max-cycles N: the process exits after N cycles without flattening real positions
  (they stay on the exchange). For graceful flatten, run with max_cycles=0 and stop with Ctrl+C.

  Optional Claude (this process only; requires ANTHROPIC_API_KEY in .env):
  - --with-claude-advisor: background loop calling claude_advisor.py run (JSONL output).
  - --with-code-review: enables funding_farmer's CODE_REVIEW daemon (Markdown reviews).
  - --with-claude: both of the above.
  - Tune: --claude-advisor-interval-sec (default 180), --code-review-interval-sec (default 3600, min 60).

  Examples:
    python3 run_small_staged.py --max-cycles 1
    python3 run_small_staged.py --live-small --live-small-budget 100 --max-cycles 2
    python3 run_small_staged.py --live-small --no-clean-slate --max-cycles 1
    python3 run_small_staged.py --live-small --with-claude
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional


def _load_dotenv_repo_root() -> None:
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parent
    for name in (".env", "env"):
        p = root / name
        if p.is_file():
            load_dotenv(p, override=True)
    if not any((root / n).is_file() for n in (".env", "env")):
        load_dotenv()


def _apply_staging_defaults() -> None:
    os.environ.setdefault("WALLET_MAX_USD", "150")
    os.environ.setdefault("MAX_POSITIONS", "2")
    os.environ.setdefault("LEVERAGE", "2")
    os.environ.setdefault("RESERVE_SLOT_FOR_NEW_POOLS", "false")


def _apply_min_live_profile(budget_usd: int, pools: int) -> None:
    """Force smallest practical live profile for one process (overrides existing env)."""
    p = max(1, min(20, int(pools)))
    min_per = 20
    os.environ.setdefault("WALLET_MIN_USD", str(min_per))
    floor = max(30, p * min_per)
    b = max(floor, int(budget_usd))
    os.environ["DRY_RUN"] = "false"
    os.environ["WALLET_MAX_USD"] = str(b)
    os.environ["MAX_POSITIONS"] = str(p)
    os.environ["LEVERAGE"] = "2"
    os.environ["RESERVE_SLOT_FOR_NEW_POOLS"] = "false"


def _apply_code_review_staging_env(interval_sec: int) -> None:
    """In-process periodic reviews when funding_farmer.run() starts (see code_review_scheduler)."""
    sec = max(60, int(interval_sec))
    os.environ["CODE_REVIEW_ENABLED"] = "true"
    os.environ["CODE_REVIEW_INTERVAL_SEC"] = str(sec)
    os.environ["CODE_REVIEW_RUN_ONCE_ON_START"] = "true"


def _apply_claude_advisor_staging_env() -> None:
    os.environ["CLAUDE_ADVISOR_ENABLED"] = "true"
    os.environ["CLAUDE_ADVISOR_MIN_INTERVAL_SEC"] = "0"


class _ClaudeAdvisorLoop:
    """Background loop: subprocess claude_advisor.py run until stop()."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()

    def _loop(self, repo_root: Path, interval_sec: int) -> None:
        advisor = repo_root / "claude_advisor.py"
        first = True
        while not self._stop.is_set():
            if not first:
                if self._stop.wait(timeout=interval_sec):
                    return
            first = False
            if self._stop.is_set():
                return
            proc = subprocess.Popen(
                [sys.executable, str(advisor), "run"],
                cwd=str(repo_root),
                env=os.environ.copy(),
            )
            with self._proc_lock:
                self._proc = proc
            try:
                while proc.poll() is None:
                    if self._stop.wait(timeout=0.5):
                        proc.terminate()
                        try:
                            proc.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        return
            finally:
                with self._proc_lock:
                    self._proc = None
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                rc = proc.returncode
                if rc not in (0, None) and not self._stop.is_set():
                    print(
                        f"[run_small_staged] claude_advisor.py run exited {rc} "
                        "(pip install anthropic? CLAUDE_ADVISOR_ENABLED / ANTHROPIC_API_KEY?)",
                        file=sys.stderr,
                    )

    def start(self, repo_root: Path, interval_sec: int) -> None:
        self._thread = threading.Thread(
            target=self._loop,
            args=(repo_root, interval_sec),
            name="claude_advisor_loop",
            daemon=False,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._proc_lock:
            p = self._proc
            if p is not None and p.poll() is None:
                p.terminate()
        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                with self._proc_lock:
                    p = self._proc
                    if p is not None and p.poll() is None:
                        p.kill()


def _staging_clean_slate(ff) -> None:
    import exchange as ex

    if ff.DRY_RUN:
        n = len(ff._dry_positions)
        ff._dry_positions.clear()
        ff.log_warn(f"[run_small_staged] cleared {n} simulated paper position(s)")
        return

    ff.log_warn("[run_small_staged] flattening ALL Aster perp positions (live orders)")
    ei = ff.get_exchange_info()
    legs: list[tuple[str, float]] = []
    for p in ff.get_positions():
        amt = float(p.get("positionAmt", 0) or 0)
        if abs(amt) <= 1e-12:
            continue
        legs.append((str(p.get("symbol", "")), amt))
    for sym, amt in legs:
        if not sym:
            continue
        if amt > 0:
            ff.close_long(sym, ei, "staging_clean_slate")
        else:
            ok = ex.flatten_position_for_symbol(sym, reason="staging_clean_slate")
            if not ok:
                ff.log_warn(f"  [run_small_staged] flatten may be incomplete for short {sym}")
    if ff.DELTA_NEUTRAL:
        print(
            "[run_small_staged] DELTA_NEUTRAL=true: only Aster was flattened; "
            "Hyperliquid hedge was not closed.",
            file=sys.stderr,
        )


def main() -> None:
    _load_dotenv_repo_root()
    repo_root = Path(__file__).resolve().parent

    ap = argparse.ArgumentParser(
        description=(
            "Staging runner: small caps, optional clean slate, then funding farmer loop. "
            "Use --live-small for minimal real-money test (overrides env for this process)."
        )
    )
    ap.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        metavar="N",
        help="Exit after N completed poll cycles (0 = run until interrupted).",
    )
    ap.add_argument(
        "--no-clean-slate",
        action="store_true",
        help="Skip paper clear / live flatten before starting the bot.",
    )
    ap.add_argument(
        "--live-small",
        action="store_true",
        help=(
            "Minimal LIVE run: DRY_RUN=false and small caps for this process only "
            "(overrides Fly secrets / .env for WALLET_MAX_USD, MAX_POSITIONS, LEVERAGE, reserve)."
        ),
    )
    ap.add_argument(
        "--live-small-budget",
        type=int,
        default=120,
        metavar="USD",
        help=(
            "With --live-small: total deploy cap WALLET_MAX_USD (default 120). "
            "Raised automatically to at least pools×$20 so each slot can meet WALLET_MIN_USD."
        ),
    )
    ap.add_argument(
        "--live-small-pools",
        type=int,
        default=3,
        metavar="N",
        help="With --live-small: MAX_POSITIONS / concurrent symbols (default 3, max 20).",
    )
    ap.add_argument(
        "--with-claude-advisor",
        action="store_true",
        help="Background loop: claude_advisor.py run on an interval (needs ANTHROPIC_API_KEY).",
    )
    ap.add_argument(
        "--with-code-review",
        action="store_true",
        help="Enable in-process CODE_REVIEW daemon inside funding_farmer (Markdown reviews).",
    )
    ap.add_argument(
        "--with-claude",
        action="store_true",
        help="Shorthand for --with-claude-advisor and --with-code-review.",
    )
    ap.add_argument(
        "--claude-advisor-interval-sec",
        type=int,
        default=180,
        metavar="N",
        help="Seconds between claude_advisor.py runs (default 180).",
    )
    ap.add_argument(
        "--code-review-interval-sec",
        type=int,
        default=3600,
        metavar="N",
        help="CODE_REVIEW_INTERVAL_SEC for this process (default 3600, min 60).",
    )
    args = ap.parse_args()
    if args.max_cycles < 0:
        ap.error("--max-cycles must be >= 0")
    if args.live_small_pools < 1 or args.live_small_pools > 20:
        ap.error("--live-small-pools must be 1–20")
    if args.live_small_budget < 30:
        ap.error("--live-small-budget must be >= 30")
    if args.claude_advisor_interval_sec < 10:
        ap.error("--claude-advisor-interval-sec must be >= 10")
    if args.code_review_interval_sec < 60:
        ap.error("--code-review-interval-sec must be >= 60")

    want_advisor = args.with_claude_advisor or args.with_claude
    want_code_review = args.with_code_review or args.with_claude

    if args.live_small:
        _apply_min_live_profile(args.live_small_budget, args.live_small_pools)
        print(
            "\n>>> run_small_staged: LIVE SMALL — real orders, "
            f"WALLET_MAX_USD={os.environ['WALLET_MAX_USD']}, "
            f"MAX_POSITIONS={os.environ['MAX_POSITIONS']}, LEVERAGE=2, DRY_RUN=false "
            "(this process only)\n",
            file=sys.stderr,
        )
    else:
        _apply_staging_defaults()

    from aster_client import credentials_ok

    if not credentials_ok():
        print(
            "Aster API credentials missing (ASTER_USER, ASTER_SIGNER, ASTER_SIGNER_PRIVATE_KEY). "
            "See .env.example.",
            file=sys.stderr,
        )
        sys.exit(1)

    if want_code_review:
        _apply_code_review_staging_env(args.code_review_interval_sec)
    if want_advisor:
        _apply_claude_advisor_staging_env()

    if want_advisor or want_code_review:
        bits = []
        if want_advisor:
            bits.append(
                f"claude_advisor loop every {args.claude_advisor_interval_sec}s"
            )
        if want_code_review:
            bits.append(
                f"code_review interval {max(60, args.code_review_interval_sec)}s "
                "(run once on start)"
            )
        print(
            f">>> run_small_staged: Claude — {'; '.join(bits)}\n",
            file=sys.stderr,
        )

    import funding_farmer as ff

    if args.live_small and ff.DELTA_NEUTRAL:
        print(
            "[run_small_staged] DELTA_NEUTRAL is enabled — Aster is capped small; "
            "HL leg is not auto-scaled here. Prefer DELTA_NEUTRAL=false for a pure tiny Aster test.",
            file=sys.stderr,
        )

    advisor_loop: Optional[_ClaudeAdvisorLoop] = None
    if want_advisor:
        advisor_loop = _ClaudeAdvisorLoop()
        advisor_loop.start(repo_root, args.claude_advisor_interval_sec)

    try:
        if not args.no_clean_slate:
            _staging_clean_slate(ff)
        ff.run(max_cycles=args.max_cycles)
    finally:
        if advisor_loop is not None:
            advisor_loop.stop()


if __name__ == "__main__":
    main()
