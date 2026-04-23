"""
Optional daemon: periodic Claude advisor JSON -> Markdown report.

Started from funding_farmer.run(); does not place trades.
Uses the same inputs as claude_advisor.py (TRADE_LOG_FILE, FUNDING_FARMER_LOG, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("advisor_report")

_REPO_ROOT = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except ValueError:
        return default


def _repo_root() -> Path:
    return _REPO_ROOT


def _interval_ok(last_file: Path, min_sec: int) -> bool:
    if min_sec <= 0:
        return True
    if not last_file.is_file():
        return True
    try:
        return (time.time() - last_file.stat().st_mtime) >= min_sec
    except OSError:
        return True


def _touch(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def _normalize_model() -> None:
    # Avoid a known-dead default some envs still carry.
    if os.getenv("CLAUDE_MODEL", "").strip() == "claude-3-5-haiku-20241022":
        os.environ["CLAUDE_MODEL"] = "claude-haiku-4-5"


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


def render_advisor_markdown(advisor_json: Dict[str, Any], model: str = "", source: str = "") -> str:
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

    md: List[str] = []
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
    md.append(
        _env_changes([{str(k): str(v) for k, v in it.items()} for it in env_changes if isinstance(it, dict)])
    )
    md.append("\n")

    md.append("## Suggested code change hints\n\n")
    md.append(
        _code_changes([{str(k): str(v) for k, v in it.items()} for it in code_changes if isinstance(it, dict)])
    )
    md.append("\n")

    md.append("## Points vs carry notes\n\n")
    md.append((points + "\n") if points else "(empty)\n")

    return "".join(md)


def _report_paths() -> tuple[Path, Path]:
    root = _repo_root()
    out_dir = os.getenv("CLAUDE_ADVISOR_DAILY_REPORT_DIR", "reports").strip() or "reports"
    p = Path(out_dir)
    if not p.is_absolute():
        p = root / p

    stem = os.getenv("CLAUDE_ADVISOR_DAILY_REPORT_STEM", "claude-daily").strip() or "claude-daily"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    md_path = p / f"{stem}-{day}.md"

    json_dir = os.getenv("CLAUDE_ADVISOR_DAILY_REPORT_JSON_DIR", "").strip()
    if json_dir:
        jp = Path(json_dir)
        if not jp.is_absolute():
            jp = root / jp
    else:
        jp = p
    json_path = jp / f"{stem}-{day}.json"
    return md_path, json_path


_run_lock = threading.Lock()


def run_one_advisor_daily_report() -> bool:
    """Run claude_advisor.py once and write daily md+json. Returns True if md written."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.warning("[advisor_report] ANTHROPIC_API_KEY not set — skipping")
        return False

    enabled = _env_bool("CLAUDE_ADVISOR_ENABLED", False)
    if not enabled:
        log.warning("[advisor_report] CLAUDE_ADVISOR_ENABLED not true — skipping")
        return False

    last_run = Path(
        os.getenv("CLAUDE_ADVISOR_DAILY_REPORT_LAST_RUN_FILE", ".claude_advisor_daily_report_last_run").strip()
        or ".claude_advisor_daily_report_last_run"
    )
    if not last_run.is_absolute():
        last_run = _repo_root() / last_run

    interval_sec = max(60, _env_int("CLAUDE_ADVISOR_DAILY_REPORT_INTERVAL_SEC", 86_400))
    min_sec = os.getenv("CLAUDE_ADVISOR_DAILY_REPORT_MIN_INTERVAL_SEC", "").strip()
    min_interval = int(min_sec) if min_sec.isdigit() else interval_sec

    with _run_lock:
        if not _interval_ok(last_run, min_interval):
            log.info("[advisor_report] Min interval not elapsed — skip")
            return False

        _normalize_model()
        if not os.getenv("CLAUDE_ADVISOR_MAX_TOKENS", "").strip():
            # claude_advisor defaults can be too small for complete JSON objects.
            os.environ["CLAUDE_ADVISOR_MAX_TOKENS"] = "2048"

        md_path, json_path = _report_paths()
        md_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [sys.executable, str(_repo_root() / "claude_advisor.py"), "run"]
        try:
            env = os.environ.copy()
            # claude_advisor.py has its own min-interval gate; the daemon already enforces spacing.
            env.setdefault("CLAUDE_ADVISOR_MIN_INTERVAL_SEC", "0")
            r = subprocess.run(
                cmd,
                cwd=str(_repo_root()),
                capture_output=True,
                text=True,
                timeout=max(60, _env_int("CLAUDE_ADVISOR_DAILY_REPORT_TIMEOUT_SEC", 300)),
                env=env,
            )
        except subprocess.TimeoutExpired:
            log.error("[advisor_report] claude_advisor.py timed out")
            return False
        except OSError as e:
            log.error("[advisor_report] failed to spawn claude_advisor.py: %s", e)
            return False

        if r.returncode != 0:
            err = (r.stderr or "").strip()
            log.error("[advisor_report] claude_advisor.py failed rc=%s: %s", r.returncode, err[:2000])
            return False

        stdout = (r.stdout or "").strip()
        if not stdout:
            log.error("[advisor_report] empty stdout from claude_advisor.py")
            return False

        try:
            advisor = json.loads(stdout)
        except json.JSONDecodeError as e:
            log.error("[advisor_report] stdout was not JSON: %s\n%s", e, stdout[:2000])
            return False

        if not isinstance(advisor, dict):
            log.error("[advisor_report] unexpected JSON type: %s", type(advisor).__name__)
            return False

        try:
            json_path.write_text(stdout + "\n", encoding="utf-8")
        except OSError:
            pass

        model = os.getenv("CLAUDE_MODEL", "").strip()
        log_src = os.getenv("FUNDING_FARMER_LOG", "funding_farmer.log").strip() or "funding_farmer.log"
        md = render_advisor_markdown(advisor, model=model, source=log_src)
        try:
            md_path.write_text(md, encoding="utf-8")
        except OSError as e:
            log.error("[advisor_report] failed writing markdown: %s", e)
            return False

        _touch(last_run)
        log.info("[advisor_report] Wrote report to %s (json: %s)", md_path, json_path)
        return True


def _daemon_loop(stop_event: threading.Event) -> None:
    interval_sec = max(60, _env_int("CLAUDE_ADVISOR_DAILY_REPORT_INTERVAL_SEC", 86_400))
    if _env_bool("CLAUDE_ADVISOR_DAILY_REPORT_RUN_ON_START", False):
        try:
            run_one_advisor_daily_report()
        except Exception as e:
            log.exception("[advisor_report] run on start failed: %s", e)
    while not stop_event.wait(timeout=interval_sec):
        try:
            run_one_advisor_daily_report()
        except Exception as e:
            log.exception("[advisor_report] periodic run failed: %s", e)


_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def start_advisor_report_daemon_if_enabled() -> None:
    """Start background thread if CLAUDE_ADVISOR_DAILY_REPORT_ENABLED and prerequisites are met."""
    global _thread
    if not _env_bool("CLAUDE_ADVISOR_DAILY_REPORT_ENABLED", False):
        return
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        log.warning(
            "[advisor_report] CLAUDE_ADVISOR_DAILY_REPORT_ENABLED but ANTHROPIC_API_KEY missing — daemon not started"
        )
        return
    if not _env_bool("CLAUDE_ADVISOR_ENABLED", False):
        log.warning(
            "[advisor_report] CLAUDE_ADVISOR_DAILY_REPORT_ENABLED but CLAUDE_ADVISOR_ENABLED not true — daemon not started"
        )
        return
    if _thread is not None and _thread.is_alive():
        return

    def _run() -> None:
        _daemon_loop(_stop_event)

    _thread = threading.Thread(target=_run, name="advisor_report", daemon=True)
    _thread.start()
    log.info(
        "[advisor_report] Daemon started (interval=%ss, dir=%s)",
        max(60, _env_int("CLAUDE_ADVISOR_DAILY_REPORT_INTERVAL_SEC", 86_400)),
        os.getenv("CLAUDE_ADVISOR_DAILY_REPORT_DIR", "reports"),
    )


def stop_advisor_report_daemon() -> None:
    """Best-effort stop (e.g. tests). Main farmer does not call this today."""
    _stop_event.set()
