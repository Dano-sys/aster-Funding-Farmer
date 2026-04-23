#!/usr/bin/env python3
"""
Convert `claude_advisor.py` JSON stdout into a small Markdown report.

Usage:
  python3 scripts/claude_advisor_json_to_md.py --in advisor.json --out reports/out.md
  python3 claude_advisor.py run | python3 scripts/claude_advisor_json_to_md.py --out reports/out.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def _load_json(path: Path | None) -> Dict[str, Any]:
    if path is None:
        raw = sys.stdin.read()
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
    obj = json.loads(raw)
    return obj if isinstance(obj, dict) else {"_raw": obj}


def _bullets(items: List[str]) -> str:
    if not items:
        return "- (none)\n"
    return "".join(f"- {x}\n" for x in items)


def _env_changes(items: List[Dict[str, str]]) -> str:
    if not items:
        return "- (none)\n"
    out = []
    for it in items:
        k = (it.get("key") or "").strip()
        v = (it.get("value") or "").strip()
        r = (it.get("rationale") or "").strip()
        line = f"- **{k}** → `{v}`"
        if r:
            line += f" — {r}"
        out.append(line + "\n")
    return "".join(out)


def _code_changes(items: List[Dict[str, str]]) -> str:
    if not items:
        return "- (none)\n"
    out = []
    for it in items:
        fn = (it.get("file") or "").strip()
        hint = (it.get("hint") or "").strip()
        left = f"`{fn}`" if fn else "(file unspecified)"
        out.append(f"- {left}: {hint}\n" if hint else f"- {left}\n")
    return "".join(out)


def render_md(advisor_json: Dict[str, Any], model: str = "", source: str = "") -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    summary = (advisor_json.get("summary") or "").strip()
    debug_notes = advisor_json.get("debug_notes") or []
    risk_flags = advisor_json.get("risk_flags") or []
    bl = advisor_json.get("suggested_blacklist_add") or []
    env_changes = advisor_json.get("suggested_env_changes") or []
    code_changes = advisor_json.get("suggested_code_changes") or []
    points = (advisor_json.get("points_vs_carry_notes") or "").strip()

    if not isinstance(debug_notes, list):
        debug_notes = [str(debug_notes)]
    if not isinstance(risk_flags, list):
        risk_flags = [str(risk_flags)]
    if not isinstance(bl, list):
        bl = [str(bl)]
    if not isinstance(env_changes, list):
        env_changes = []
    if not isinstance(code_changes, list):
        code_changes = []

    header_bits = [f"Generated: {now}"]
    if model:
        header_bits.append(f"Model: {model}")
    if source:
        header_bits.append(f"Source: `{source}`")

    md = []
    md.append("# Claude advisor report\n\n")
    md.append("> " + " | ".join(header_bits) + "\n\n")

    md.append("## Summary\n\n")
    md.append((summary + "\n\n") if summary else "(empty)\n\n")

    md.append("## Debug notes\n\n")
    md.append(_bullets([str(x).strip() for x in debug_notes if str(x).strip()]))
    md.append("\n")

    md.append("## Risk flags\n\n")
    md.append(_bullets([str(x).strip() for x in risk_flags if str(x).strip()]))
    md.append("\n")

    md.append("## Suggested blacklist additions\n\n")
    md.append(_bullets([str(x).strip().upper() for x in bl if str(x).strip()]))
    md.append("\n")

    md.append("## Suggested env changes\n\n")
    md.append(_env_changes([{str(k): str(v) for k, v in it.items()} for it in env_changes if isinstance(it, dict)]))
    md.append("\n")

    md.append("## Suggested code change hints\n\n")
    md.append(_code_changes([{str(k): str(v) for k, v in it.items()} for it in code_changes if isinstance(it, dict)]))
    md.append("\n")

    md.append("## Points vs carry notes\n\n")
    md.append((points + "\n") if points else "(empty)\n")

    return "".join(md)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", type=Path, default=None, help="Input JSON file (default: stdin)")
    ap.add_argument("--out", dest="out_path", type=Path, required=True, help="Output markdown file")
    ap.add_argument("--model", type=str, default="", help="Optional model name for header")
    ap.add_argument("--source", type=str, default="", help="Optional source identifier (e.g., pulled log file path)")
    args = ap.parse_args()

    advisor_json = _load_json(args.in_path)
    md = render_md(advisor_json, model=args.model.strip(), source=args.source.strip())
    args.out_path.parent.mkdir(parents=True, exist_ok=True)
    args.out_path.write_text(md, encoding="utf-8")
    print(str(args.out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

