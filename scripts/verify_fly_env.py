#!/usr/bin/env python3
"""
Sanity-check environment before / after Fly deploy (no secret values printed).

  python3 scripts/verify_fly_env.py
  python3 scripts/verify_fly_env.py --strict

Uses os.environ only (matches Fly Machines: secrets + fly.toml [env]).
"""

from __future__ import annotations

import argparse
import os
import sys


def _clean(s: str | None) -> str:
    if not s:
        return ""
    return s.strip().strip("\ufeff").replace("\r", "").replace("\n", "")


def _not_placeholder(s: str) -> bool:
    sl = s.lower()
    return bool(sl.strip()) and "your_" not in sl and "placeholder" not in sl


# First segment of the documented example key in .env.example (never use in prod).
_KNOWN_FAKE_ANTHROPIC_PREFIX = "sk-ant-api03-dkgdwcktsg2sws5kyuekw0xrt9bzlovhm6xvqhlpfhslew"


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify Fly / runtime env for funding_farmer.")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings (e.g. alert watcher misconfig) as errors.",
    )
    args = ap.parse_args()
    errors: list[str] = []
    warnings: list[str] = []

    user = _clean(os.getenv("ASTER_USER"))
    signer = _clean(os.getenv("ASTER_SIGNER"))
    pk = _clean(os.getenv("ASTER_SIGNER_PRIVATE_KEY"))

    if not _not_placeholder(user):
        errors.append("ASTER_USER missing or placeholder")
    if not _not_placeholder(signer):
        errors.append("ASTER_SIGNER missing or placeholder")
    if not _not_placeholder(pk):
        errors.append("ASTER_SIGNER_PRIVATE_KEY missing or placeholder")

    dry = _truthy("DRY_RUN", "false")

    if _truthy("DELTA_NEUTRAL", "false"):
        hl_pk = _clean(os.getenv("HL_PRIVATE_KEY"))
        hl_addr = _clean(os.getenv("HL_WALLET_ADDRESS"))
        if not _not_placeholder(hl_pk) or "your_" in hl_pk.lower():
            errors.append("DELTA_NEUTRAL=true but HL_PRIVATE_KEY missing or placeholder")
        if not _not_placeholder(hl_addr) or "your_" in hl_addr.lower():
            errors.append("DELTA_NEUTRAL=true but HL_WALLET_ADDRESS missing or placeholder")

    ak = _clean(os.getenv("ANTHROPIC_API_KEY"))
    if ak and ak.lower().startswith(_KNOWN_FAKE_ANTHROPIC_PREFIX):
        errors.append(
            "ANTHROPIC_API_KEY matches the .env.example placeholder; replace with a real key or unset."
        )

    if _truthy("ALERT_WATCHER_ENABLED", "false"):
        wh = _clean(os.getenv("WEBHOOK_URL"))
        tok = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
        chat = _clean(os.getenv("TELEGRAM_CHAT_ID"))
        if not wh and (not tok or not chat):
            warnings.append(
                "ALERT_WATCHER_ENABLED=true but neither WEBHOOK_URL nor "
                "(TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) is set."
            )

    if dry:
        dsim = _clean(os.getenv("DRY_RUN_SIMULATED_MARGIN_USD", "0"))
        dsv: float | None
        try:
            dsv = float(dsim) if dsim else 0.0
        except ValueError:
            warnings.append(
                f"DRY_RUN_SIMULATED_MARGIN_USD={dsim!r} is not a number (wallet-sized paper uses 0)."
            )
            dsv = None
        if dsv is not None and dsv > 0:
            warnings.append(
                "DRY_RUN=true and DRY_RUN_SIMULATED_MARGIN_USD>0: fixed paper margin, not live wallet sizing "
                "(use 0 for wallet-sized dry run on Fly)."
            )

    if _truthy("CLAUDE_ADVISOR_LOOP_ON_FLY", "false"):
        if not _truthy("CLAUDE_ADVISOR_ENABLED", "false"):
            errors.append(
                "CLAUDE_ADVISOR_LOOP_ON_FLY=true requires CLAUDE_ADVISOR_ENABLED=true (docker-entrypoint)."
            )
        if not _not_placeholder(ak):
            errors.append(
                "CLAUDE_ADVISOR_LOOP_ON_FLY=true requires ANTHROPIC_API_KEY for claude_advisor.py run."
            )

    if os.getenv("FLY_APP_NAME") or os.getenv("FLY_APP"):
        print("Fly: ensure a single trading machine: fly scale count 1 -a <app>")

    if not dry and not errors:
        wmax = _clean(os.getenv("WALLET_MAX_USD", "0"))
        try:
            wv = float(wmax) if wmax else 0.0
        except ValueError:
            wv = -1.0
            warnings.append(f"WALLET_MAX_USD={wmax!r} is not a number (defaults may apply in app)")
        if wv == 0.0:
            print("Live mode: WALLET_MAX_USD=0 (no deploy cap — full wallet sizing).")
        elif wv > 0:
            print(f"Live mode: WALLET_MAX_USD={wv:g} (hard cap on deploy budget).")

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)

    if errors:
        return 1
    if args.strict and warnings:
        print("ERROR: --strict: warnings above treated as failures.", file=sys.stderr)
        return 1

    print("OK: required Aster (and HL if delta-neutral) env checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
