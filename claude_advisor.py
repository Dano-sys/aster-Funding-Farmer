#!/usr/bin/env python3
"""
Claude (Anthropic) advisor — cheap on-demand review of pool/trade context.
Does not place orders. Default off; use CLAUDE_ADVISOR_ENABLED=true for API runs.

  python claude_advisor.py dry-run    # print context size + prompt preview (no API)
  python claude_advisor.py run         # call Anthropic, append JSONL result

Future auto-apply is gated: CLAUDE_AUTO_APPLY is not implemented; enabling it exits with an error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

load_dotenv()

# Optional: reuse lever key list from profit_assistant (no API side effects on import)
try:
    from profit_assistant import LEVERS as _PA_LEVERS
except ImportError:
    _PA_LEVERS = []

_EXTRA_LEVERS = [
    ("FARMING_HALT", "false", "Skip new opens only; exits still run"),
    ("FARMING_HALT_FILE", "", "If path exists on disk, same as halt (touch to panic-stop opens)"),
    ("CYCLE_SNAPSHOT_ENABLE", "false", "Append one JSON line per farmer cycle to CYCLE_SNAPSHOT_FILE"),
    ("CYCLE_SNAPSHOT_FILE", "farmer_cycle.jsonl", "Ring-buffer path for cycle snapshots"),
    ("CLAUDE_ADVISOR_ENABLED", "false", "Allow run subcommand to call Anthropic"),
    ("CLAUDE_AUTO_APPLY", "false", "Reserved — must stay false until implemented with kill-switch checks"),
]

TRADE_LOG_FILE = os.getenv("TRADE_LOG_FILE", "trades.csv")
FUNDING_FARMER_LOG = os.getenv("FUNDING_FARMER_LOG", "funding_farmer.log").strip() or "funding_farmer.log"
CYCLE_SNAPSHOT_FILE = os.getenv("CYCLE_SNAPSHOT_FILE", "farmer_cycle.jsonl").strip() or "farmer_cycle.jsonl"
CLAUDE_ADVISOR_MAX_CSV_ROWS = int(os.getenv("CLAUDE_ADVISOR_MAX_CSV_ROWS", "60") or "60")
CLAUDE_ADVISOR_MAX_LOG_LINES = int(os.getenv("CLAUDE_ADVISOR_MAX_LOG_LINES", "250") or "250")
CLAUDE_ADVISOR_MAX_SNAPSHOT_ROWS = int(os.getenv("CLAUDE_ADVISOR_MAX_SNAPSHOT_ROWS", "30") or "30")
CLAUDE_ADVISOR_MIN_INTERVAL_SEC = int(os.getenv("CLAUDE_ADVISOR_MIN_INTERVAL_SEC", "0") or "0")
CLAUDE_ADVISOR_LAST_RUN_FILE = os.getenv(
    "CLAUDE_ADVISOR_LAST_RUN_FILE", ".claude_advisor_last_run"
).strip() or ".claude_advisor_last_run"
CLAUDE_ADVISOR_OUT_JSONL = os.getenv(
    "CLAUDE_ADVISOR_OUT_JSONL", "claude_advisor_out.jsonl"
).strip() or "claude_advisor_out.jsonl"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-3-5-haiku-20241022").strip()

SYSTEM_PROMPT = """You review an Aster DEX perpetual funding-rate farming bot (long carry, optional HL hedge).
Output ONLY valid JSON (no markdown fences), one object, with exactly these keys:
- summary: string, <= 400 chars
- risk_flags: array of short strings
- suggested_blacklist_add: array of symbol strings like BTCUSDT (may be empty)
- suggested_env_changes: array of objects {"key": str, "value": str, "rationale": str} — only keys that exist in .env for this bot (no secrets)
- points_vs_carry_notes: string, <= 500 chars
Be conservative: illiquidity -> suggest MIN_QUOTE_VOLUME_24H or SYMBOL_ALLOWLIST, not leverage increases.
"""


def _farming_halted() -> Tuple[bool, str]:
    if os.getenv("FARMING_HALT", "").strip().lower() in ("1", "true", "yes"):
        return True, "FARMING_HALT=true"
    p = os.getenv("FARMING_HALT_FILE", "").strip()
    if p and os.path.isfile(p):
        return True, f"halt file: {p}"
    return False, ""


def _lever_snapshot() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for row in list(_PA_LEVERS) + _EXTRA_LEVERS:
        key = row[0]
        v = os.getenv(key)
        if v is None or str(v).strip() == "":
            continue
        s = str(v).strip()
        if any(x in key.upper() for x in ("KEY", "SECRET", "PRIVATE", "PASSWORD", "TOKEN")):
            out[key] = "<set>"
        else:
            out[key] = s[:500] + ("…" if len(s) > 500 else "")
    return out


def _tail_csv_rows(path: Path, max_rows: int) -> List[Dict[str, str]]:
    if not path.is_file() or max_rows <= 0:
        return []
    import csv

    rows: List[Dict[str, str]] = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append({k: (row.get(k) or "") for k in r.fieldnames or []})
    return rows[-max_rows:]


def _tail_text_lines(path: Path, max_lines: int) -> str:
    if not path.is_file() or max_lines <= 0:
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    chunk = lines[-max_lines:]
    return "".join(chunk)


def _tail_jsonl_objects(path: Path, max_rows: int) -> List[Dict[str, Any]]:
    if not path.is_file() or max_rows <= 0:
        return []
    lines: List[str] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(line)
    out: List[Dict[str, Any]] = []
    for line in lines[-max_rows:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("No JSON object in model response")
    return json.loads(m.group(0))


def _interval_ok() -> bool:
    if CLAUDE_ADVISOR_MIN_INTERVAL_SEC <= 0:
        return True
    p = Path(CLAUDE_ADVISOR_LAST_RUN_FILE)
    if not p.is_file():
        return True
    try:
        age = time.time() - p.stat().st_mtime
        return age >= CLAUDE_ADVISOR_MIN_INTERVAL_SEC
    except OSError:
        return True


def _touch_last_run() -> None:
    try:
        Path(CLAUDE_ADVISOR_LAST_RUN_FILE).write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def build_user_message(trade_path: Path) -> str:
    csv_rows = _tail_csv_rows(trade_path, CLAUDE_ADVISOR_MAX_CSV_ROWS)
    log_tail = _tail_text_lines(Path(FUNDING_FARMER_LOG), CLAUDE_ADVISOR_MAX_LOG_LINES)
    snaps = _tail_jsonl_objects(Path(CYCLE_SNAPSHOT_FILE), CLAUDE_ADVISOR_MAX_SNAPSHOT_ROWS)
    payload = {
        "recent_trades_csv_rows": csv_rows,
        "funding_farmer_log_tail": log_tail,
        "recent_cycle_snapshots": snaps,
        "env_levers_non_secret": _lever_snapshot(),
    }
    return json.dumps(payload, indent=0)[:120_000]


def cmd_dry_run(trade_path: Path) -> int:
    body = build_user_message(trade_path)
    print(f"User message length: {len(body)} chars", file=sys.stderr)
    print(f"Model would be: {CLAUDE_MODEL}", file=sys.stderr)
    preview = body[:2400] + ("\n…" if len(body) > 2400 else "")
    print(preview)
    return 0


def cmd_run(trade_path: Path) -> int:
    if os.getenv("CLAUDE_AUTO_APPLY", "").strip().lower() in ("1", "true", "yes"):
        print(
            "CLAUDE_AUTO_APPLY is not implemented. Set it to false. "
            "Any future auto-apply must respect FARMING_HALT / halt file.",
            file=sys.stderr,
        )
        return 2
    halted, why = _farming_halted()
    if halted:
        print(
            f"Note: farming halt active ({why}) — still generating suggestions; "
            "any future CLAUDE_AUTO_APPLY must refuse while halted.",
            file=sys.stderr,
        )
    enabled = os.getenv("CLAUDE_ADVISOR_ENABLED", "").strip().lower() in ("1", "true", "yes")
    if not enabled:
        print(
            "Set CLAUDE_ADVISOR_ENABLED=true in .env to call the API, or use: dry-run",
            file=sys.stderr,
        )
        return 1
    if not _interval_ok():
        print(
            f"Min interval not elapsed ({CLAUDE_ADVISOR_MIN_INTERVAL_SEC}s) — "
            f"see {CLAUDE_ADVISOR_LAST_RUN_FILE}",
            file=sys.stderr,
        )
        return 4
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    try:
        import anthropic
    except ImportError:
        print("Install: pip install anthropic", file=sys.stderr)
        return 1

    user_msg = build_user_message(trade_path)
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = ""
    for block in msg.content:
        if hasattr(block, "text"):
            text += block.text
    try:
        parsed = _extract_json_object(text)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Failed to parse model JSON: {e}\nRaw:\n{text[:2000]}", file=sys.stderr)
        return 1

    record = {
        "ts_unix": int(time.time()),
        "model": CLAUDE_MODEL,
        "advisor_json": parsed,
    }
    with open(CLAUDE_ADVISOR_OUT_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    _touch_last_run()
    print(json.dumps(parsed, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude advisor for funding farmer context")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_dr = sub.add_parser("dry-run", help="Build prompt preview without API")
    p_dr.add_argument("--file", type=Path, default=None, help="trades CSV (default TRADE_LOG_FILE)")
    p_run = sub.add_parser("run", help="Call Anthropic and append claude_advisor_out.jsonl")
    p_run.add_argument("--file", type=Path, default=None, help="trades CSV (default TRADE_LOG_FILE)")
    args = ap.parse_args()
    tpath = args.file or Path(TRADE_LOG_FILE)
    if args.cmd == "dry-run":
        return cmd_dry_run(tpath)
    if args.cmd == "run":
        return cmd_run(tpath)
    return 1


if __name__ == "__main__":
    sys.exit(main())
