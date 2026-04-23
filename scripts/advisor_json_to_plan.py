#!/usr/bin/env python3
"""
Render a lightweight *human-review* plan markdown from a single Claude advisor JSON file.

Input:  JSON file created by scripts/claude_fly_log_report.sh (one object).
Output: A plan markdown file (no side effects beyond writing the output path).
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class AdvisorPaths:
    advisor_json: Path
    report_md: Optional[Path]
    pulled_log: Optional[Path]


def _read_json(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError(f"advisor json must be an object, got {type(data).__name__}")
    return data


def _infer_paths(advisor_json: Path, repo_root: Path) -> AdvisorPaths:
    """
    Best-effort inference based on the filename timestamp used by claude_fly_log_report.sh:
      fly-logs/claude-advisor-<APP>-<YYYYMMDD-HHMMSS>.json
    """
    name = advisor_json.name
    m = re.search(r"-(\d{8}-\d{6})\.json$", name)
    ts = m.group(1) if m else ""
    report_md = None
    pulled_log = None
    if ts:
        # script uses:
        #   md_out="reports/claude-${APP}-${ts}.md"
        #   pulled="fly-logs/funding_farmer-${APP}-${ts}.log"
        # We don't need APP specifically if we scan for the timestamp suffix.
        reps = list((repo_root / "reports").glob(f"**/*{ts}.md"))
        if reps:
            report_md = reps[0]
        logs = list((repo_root / "fly-logs").glob(f"**/*{ts}.log"))
        if logs:
            pulled_log = logs[0]
    return AdvisorPaths(advisor_json=advisor_json, report_md=report_md, pulled_log=pulled_log)


def _bullets(items: List[str]) -> str:
    items = [str(x).strip() for x in items if str(x).strip()]
    if not items:
        return "- (none)\n"
    return "".join(f"- {x}\n" for x in items)


def _render_plan_md(advisor: Dict[str, Any], *, paths: AdvisorPaths) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    summary = str(advisor.get("summary") or "").strip()
    risk_flags = advisor.get("risk_flags") or []
    env_changes = advisor.get("suggested_env_changes") or []
    bl_add = advisor.get("suggested_blacklist_add") or []
    code_changes = advisor.get("suggested_code_changes") or []

    if not isinstance(risk_flags, list):
        risk_flags = [str(risk_flags)]
    if not isinstance(bl_add, list):
        bl_add = [str(bl_add)]
    if not isinstance(env_changes, list):
        env_changes = []
    if not isinstance(code_changes, list):
        code_changes = []

    md: List[str] = []
    md.append(f"# Plan (from Claude advisor) — {now}\n\n")

    md.append("## Links\n\n")
    md.append(f"- Advisor JSON: `{paths.advisor_json.as_posix()}`\n")
    if paths.report_md:
        md.append(f"- Report Markdown: `{paths.report_md.as_posix()}`\n")
    if paths.pulled_log:
        md.append(f"- Pulled Fly log snapshot: `{paths.pulled_log.as_posix()}`\n")
    md.append("\n")

    md.append("## Summary\n\n")
    md.append((summary + "\n\n") if summary else "(empty)\n\n")

    md.append("## Risk flags (from advisor)\n\n")
    md.append(_bullets([str(x) for x in risk_flags]))
    md.append("\n")

    md.append("## Proposed config changes (proposal only)\n\n")
    md.append("### Suggested blacklist additions\n\n")
    md.append(_bullets([str(x).strip().upper() for x in bl_add]))
    md.append("\n")

    md.append("### Suggested env changes\n\n")
    if not env_changes:
        md.append("- (none)\n\n")
    else:
        for it in env_changes:
            if not isinstance(it, dict):
                continue
            k = str(it.get("key") or "").strip()
            v = str(it.get("value") or "").strip()
            r = str(it.get("rationale") or "").strip()
            if not k or not v:
                continue
            line = f"- **{k}** → `{v}`"
            if r:
                line += f" — {r}"
            md.append(line + "\n")
        md.append("\n")

    md.append("## Proposed code changes (proposal only)\n\n")
    if not code_changes:
        md.append("- (none)\n\n")
    else:
        for it in code_changes:
            if not isinstance(it, dict):
                continue
            fn = str(it.get("file") or "").strip()
            hint = str(it.get("hint") or "").strip()
            left = f"`{fn}`" if fn else "(file unspecified)"
            md.append(f"- {left}: {hint}\n" if hint else f"- {left}\n")
        md.append("\n")

    md.append("## Verification checklist\n\n")
    md.append("- Confirm Fly runtime `Resolved env:` matches expected (no secret overrides).\n")
    md.append("- Run one-cycle dry run locally (`DRY_RUN=true`, `--max-cycles 1`) to validate gates/logging.\n")
    md.append("- Re-run report after changes; ensure the same issues stop appearing.\n")

    return "".join(md)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a plan markdown from advisor JSON.")
    ap.add_argument("--in", dest="inp", required=True, help="Advisor JSON path")
    ap.add_argument("--out", dest="out", required=True, help="Plan markdown output path")
    ap.add_argument(
        "--repo-root",
        default=".",
        help="Repo root (used to infer report/log paths); default '.'",
    )
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    inp = Path(args.inp).resolve()
    out = Path(args.out).resolve()

    advisor = _read_json(inp)
    paths = _infer_paths(inp, repo_root)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render_plan_md(advisor, paths=paths), encoding="utf-8")
    print(out.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

