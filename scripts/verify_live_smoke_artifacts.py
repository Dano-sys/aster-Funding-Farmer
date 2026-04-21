#!/usr/bin/env python3
"""
Quick post-run checks for logs / trade log / optional Claude JSONL.

  cd repo && python3 scripts/verify_live_smoke_artifacts.py

Loads .env from repo root when present (for TRADE_LOG_FILE, FUNDING_FARMER_LOG, CLAUDE_ADVISOR_OUT_JSONL).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _tail_lines(path: Path, n: int) -> list[str]:
    if not path.is_file():
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return lines[-n:] if len(lines) > n else lines


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    if (root / ".env").is_file():
        from dotenv import load_dotenv

        load_dotenv(root / ".env", override=True)

    log_path = Path(os.getenv("FUNDING_FARMER_LOG", "funding_farmer.log").strip() or "funding_farmer.log")
    trade_path = Path(os.getenv("TRADE_LOG_FILE", "trades.csv").strip() or "trades.csv")
    advisor_path = Path(os.getenv("CLAUDE_ADVISOR_OUT_JSONL", "claude_advisor_out.jsonl").strip() or "claude_advisor_out.jsonl")

    ok = True
    for label, p in (("Farmer log", log_path), ("Trade log", trade_path)):
        if p.is_file():
            sz = p.stat().st_size
            print(f"{label}: {p} ({sz} bytes)")
        else:
            print(f"{label}: {p} — missing")
            ok = False

    if advisor_path.is_file():
        print(f"Claude advisor JSONL: {advisor_path} ({advisor_path.stat().st_size} bytes)")
    else:
        print(f"Claude advisor JSONL: {advisor_path} — missing (optional if advisor not run)")

    if log_path.is_file():
        tail = _tail_lines(log_path, 8)
        if tail:
            print("\n--- Last lines of farmer log ---")
            for ln in tail:
                sys.stdout.write(ln if ln.endswith("\n") else ln + "\n")

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
