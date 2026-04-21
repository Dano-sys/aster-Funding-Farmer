#!/usr/bin/env python3
"""
Preflight for a local small live run (no secret values printed).

  cd repo && python3 scripts/verify_live_smoke_prereqs.py
  cd repo && python3 scripts/verify_live_smoke_prereqs.py --claude

Loads .env from repo root when present.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _clean(s: str | None) -> str:
    if not s:
        return ""
    return s.strip().strip("\ufeff").replace("\r", "").replace("\n", "")


def _not_placeholder(s: str) -> bool:
    sl = s.lower()
    return bool(sl.strip()) and "your_" not in sl and "placeholder" not in sl


_KNOWN_FAKE_ANTHROPIC_PREFIX = "sk-ant-api03-dkgdwcktsg2sws5kyuekw0xrt9bzlovhm6xvqhlpfhslew"


def main() -> int:
    ap = argparse.ArgumentParser(description="Preflight checks for local live smoke run.")
    ap.add_argument(
        "--claude",
        action="store_true",
        help="Also require ANTHROPIC_API_KEY for claude_advisor / code review.",
    )
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    if (root / ".env").is_file():
        from dotenv import load_dotenv

        load_dotenv(root / ".env", override=True)

    sys.path.insert(0, str(root))
    from aster_client import credentials_ok

    errors: list[str] = []

    user = _clean(os.getenv("ASTER_USER"))
    signer = _clean(os.getenv("ASTER_SIGNER"))
    pk = _clean(os.getenv("ASTER_SIGNER_PRIVATE_KEY"))
    if not _not_placeholder(user):
        errors.append("ASTER_USER missing or placeholder")
    if not _not_placeholder(signer):
        errors.append("ASTER_SIGNER missing or placeholder")
    if not _not_placeholder(pk):
        errors.append("ASTER_SIGNER_PRIVATE_KEY missing or placeholder")

    if not credentials_ok():
        errors.append("Aster credentials_ok() is false (check V3 or legacy env)")

    if args.claude:
        ak = _clean(os.getenv("ANTHROPIC_API_KEY"))
        if not ak:
            errors.append("ANTHROPIC_API_KEY unset (--claude)")
        elif ak.lower().startswith(_KNOWN_FAKE_ANTHROPIC_PREFIX):
            errors.append(
                "ANTHROPIC_API_KEY matches .env.example placeholder; set a real key (--claude)"
            )

    if errors:
        print("Issues:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("OK: Aster Pro API V3 credentials present.")
    if args.claude:
        print("OK: ANTHROPIC_API_KEY set (value not shown).")
    print()
    print("Reminders:")
    print("  - Live exit: use Ctrl+C to flatten; --max-cycles with DRY_RUN=false does NOT close perps.")
    print("  - run_small_staged default clean slate = flatten ALL Aster perps before start; use --no-clean-slate to skip.")
    print("  - Optional: CYCLE_SNAPSHOT_ENABLE=true for richer claude_advisor payloads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
