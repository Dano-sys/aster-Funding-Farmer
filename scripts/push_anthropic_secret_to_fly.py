#!/usr/bin/env python3
"""Read ANTHROPIC_API_KEY from repo .env and set it on Fly (never prints the key)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def _app_name() -> str:
    if os.environ.get("FLY_APP"):
        return os.environ["FLY_APP"].strip()
    fly_toml = Path(__file__).resolve().parent.parent / "fly.toml"
    if fly_toml.is_file():
        m = re.search(r'^app\s*=\s*"([^"]+)"', fly_toml.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1).strip()
    return "aster-funding-farmer"


def _read_anthropic_key(env_path: Path) -> str:
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("ANTHROPIC_API_KEY="):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if not env_path.is_file():
        print("No .env at repo root.", file=sys.stderr)
        return 1
    key = _read_anthropic_key(env_path)
    if not key:
        print(
            "ANTHROPIC_API_KEY is empty in .env — paste your key after ANTHROPIC_API_KEY= then run again.",
            file=sys.stderr,
        )
        return 1
    app = _app_name()
    r = subprocess.run(
        ["fly", "secrets", "set", f"ANTHROPIC_API_KEY={key}", "--app", app],
        cwd=str(root),
    )
    if r.returncode == 0:
        print(f"Fly secret ANTHROPIC_API_KEY set for app {app!r} (value not shown).")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
