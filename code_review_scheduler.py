"""
Optional daemon: periodic Anthropic code review → Markdown file.
Started from funding_farmer.run(); does not place trades.
"""
from __future__ import annotations

import csv
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger("code_review")

_REPO_ROOT = Path(__file__).resolve().parent

_DEFAULT_PATHS = (
    "funding_farmer.py,exchange.py,config.py,delta_neutral.py,aster_client.py"
)

SYSTEM_PROMPT = """You are a senior reviewer for an Aster DEX funding-rate farming bot (Python).
You receive allowlisted source excerpts, optional git diff, optional recent logs/trades — no secrets.
You do NOT execute anything or recommend live trading actions.

Output ONLY Markdown (no JSON, no markdown code fence wrapping the whole document), with exactly these top-level headings in order:
## Executive summary
## Risks and footguns
## Suggestions
## Follow-ups

Be concise and actionable. Under ## Suggestions use numbered items. Tie suggestions to evidence from the payload.
"""


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


def _is_blocked_relpath(rel: str) -> bool:
    low = rel.lower().replace("\\", "/")
    base = Path(low).name
    bl = base.lower()
    if bl.startswith(".env") or bl == "env":
        return True
    if any(x in bl for x in ("secret", "credential", "password")):
        return True
    if "private" in bl:
        return True
    if bl.endswith("_keys.py") or bl == "keys.py":
        return True
    return False


def _safe_resolve_under_root(rel: str, root: Path) -> Optional[Path]:
    rel = rel.strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    if _is_blocked_relpath(rel):
        return None
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    if not p.is_file():
        return None
    return p


def _read_file_tail_bytes(path: Path, max_bytes: int) -> str:
    raw = path.read_bytes()
    if len(raw) <= max_bytes:
        return raw.decode("utf-8", errors="replace")
    chunk = raw[-max_bytes:]
    text = chunk.decode("utf-8", errors="replace")
    return "[…truncated from start of file…]\n" + text


def _tail_log_lines(path: Path, max_lines: int) -> str:
    if not path.is_file() or max_lines <= 0:
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-max_lines:])


def _tail_csv_plain(path: Path, max_rows: int) -> str:
    if not path.is_file() or max_rows <= 0:
        return ""
    rows: List[str] = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        r = csv.reader(f)
        for row in r:
            rows.append(",".join(row))
    tail = rows[-max_rows:] if len(rows) > max_rows else rows
    return "\n".join(tail)


def _git_diff(paths: List[str], root: Path, timeout_sec: int) -> Tuple[str, Optional[str]]:
    if not paths:
        return "", None
    try:
        cp = subprocess.run(
            ["git", "diff", "--", *paths],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        out = (cp.stdout or "").strip()
        if len(out) > 120_000:
            out = out[-120_000:] + "\n[…git diff truncated…]"
        return out, None
    except FileNotFoundError:
        return "", "git not found in PATH"
    except subprocess.TimeoutExpired:
        return "", "git diff timed out"


def _profit_digest(timeout_sec: int, max_chars: int) -> str:
    root = str(_repo_root())
    parts: List[str] = []
    for sub in ("summary", "kpi"):
        try:
            cp = subprocess.run(
                [os.sys.executable, "profit_assistant.py", sub],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=max(1, timeout_sec),
                check=False,
            )
            parts.append(f"### profit_assistant {sub}\n{(cp.stdout or '').strip()}")
        except Exception as e:
            parts.append(f"### profit_assistant {sub}\n<error: {e}>")
    joined = "\n\n".join(parts)
    return joined[:max_chars] if len(joined) > max_chars else joined


def _build_user_payload() -> str:
    root = _repo_root()
    raw_paths = os.getenv("CODE_REVIEW_PATHS", _DEFAULT_PATHS).strip() or _DEFAULT_PATHS
    rel_list = [p.strip() for p in raw_paths.split(",") if p.strip()]
    max_file = max(1000, _env_int("CODE_REVIEW_MAX_FILE_BYTES", 80_000))
    max_total = max(5000, _env_int("CODE_REVIEW_MAX_TOTAL_BYTES", 200_000))

    sections: List[str] = []
    total = 0

    if _env_bool("CODE_REVIEW_INCLUDE_LOG_TAIL", False):
        lp = Path(os.getenv("FUNDING_FARMER_LOG", "funding_farmer.log").strip() or "funding_farmer.log")
        if not lp.is_absolute():
            lp = root / lp
        n = _env_int("CODE_REVIEW_MAX_LOG_LINES", 120)
        chunk = _tail_log_lines(lp, n)
        if chunk:
            block = f"## Recent log tail ({lp.name})\n```\n{chunk}\n```\n"
            total += len(block.encode())
            sections.append(block)

    if _env_bool("CODE_REVIEW_INCLUDE_TRADES_TAIL", False):
        tp = Path(os.getenv("TRADE_LOG_FILE", "trades.csv").strip() or "trades.csv")
        if not tp.is_absolute():
            tp = root / tp
        n = _env_int("CODE_REVIEW_MAX_TRADE_ROWS", 40)
        chunk = _tail_csv_plain(tp, n)
        if chunk:
            block = f"## Recent trades CSV ({tp.name})\n```\n{chunk}\n```\n"
            total += len(block.encode())
            sections.append(block)

    if _env_bool("CODE_REVIEW_INCLUDE_KPI", False):
        to = _env_int("CODE_REVIEW_KPI_TIMEOUT_SEC", 30)
        cap = _env_int("CODE_REVIEW_KPI_MAX_CHARS", 8000)
        block = "## KPI digest\n```\n" + _profit_digest(to, cap) + "\n```\n"
        total += len(block.encode())
        sections.append(block)

    git_paths: List[str] = []
    if _env_bool("CODE_REVIEW_INCLUDE_GIT_DIFF", False):
        for rel in rel_list:
            if _safe_resolve_under_root(rel, root):
                git_paths.append(rel)
        if not git_paths:
            sections.append("## Git diff\n_(skipped: no resolved allowlist paths for diff)_\n")
            total += len(sections[-1].encode())
        else:
            diff, err = _git_diff(git_paths, root, _env_int("CODE_REVIEW_GIT_DIFF_TIMEOUT_SEC", 45))
            if err:
                sections.append(f"## Git diff\n_(skipped: {err})_\n")
                total += len(sections[-1].encode())
            elif diff:
                block = f"## Git diff\n```diff\n{diff}\n```\n"
                total += len(block.encode())
                sections.append(block)
            else:
                sections.append(
                    "## Git diff\n_(empty — no uncommitted changes in listed paths)_\n"
                )
                total += len(sections[-1].encode())

    files_md: List[str] = []
    for rel in rel_list:
        sp = _safe_resolve_under_root(rel, root)
        if not sp:
            files_md.append(f"## FILE {rel}\n_(skipped: missing or blocked path)_\n")
            continue
        body = _read_file_tail_bytes(sp, max_file)
        block = f"## FILE {rel}\n```python\n{body}\n```\n"
        files_md.append(block)

    files_blob = "\n".join(files_md)
    total += len(files_blob.encode())

    header = "# Code review input\n\n"
    out = header + "".join(sections) + "\n# Allowlisted sources\n\n" + files_blob

    enc = out.encode()
    if len(enc) > max_total:
        out = enc[: max_total - 80].decode("utf-8", errors="replace") + "\n\n[…TOTAL_USER_MESSAGE_TRUNCATED…]\n"
    return out


def _output_path() -> Path:
    root = _repo_root()
    mode = os.getenv("CODE_REVIEW_OUTPUT_MODE", "append").strip().lower() or "append"
    out = os.getenv("CODE_REVIEW_OUTPUT", "reviews/advisor.md").strip() or "reviews/advisor.md"
    p = Path(out)
    if not p.is_absolute():
        p = root / p
    if mode == "daily":
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stem = p.stem
        parent = p.parent
        return parent / f"{stem}-{day}.md"
    return p


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


def _append_markdown(md_path: Path, body: str) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sep = "\n\n---\n\n"
    header = f"## Automated review @ {ts} UTC\n\n"
    if md_path.is_file() and md_path.stat().st_size > 0:
        with open(md_path, "a", encoding="utf-8") as f:
            f.write(sep + header + body.strip() + "\n")
    else:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# Funding farmer — code reviews\n\n{header}{body.strip()}\n")


_run_lock = threading.Lock()


def run_one_code_review_markdown() -> bool:
    """Returns True if a review was written."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.warning("[code_review] ANTHROPIC_API_KEY not set — skipping")
        return False

    last_run = Path(
        os.getenv("CODE_REVIEW_LAST_RUN_FILE", ".code_review_last_run").strip()
        or ".code_review_last_run"
    )
    if not last_run.is_absolute():
        last_run = _repo_root() / last_run

    interval_sec = max(60, _env_int("CODE_REVIEW_INTERVAL_SEC", 86_400))
    min_sec = os.getenv("CODE_REVIEW_MIN_INTERVAL_SEC", "").strip()
    min_interval = int(min_sec) if min_sec.isdigit() else interval_sec

    with _run_lock:
        if not _interval_ok(last_run, min_interval):
            log.info("[code_review] Min interval not elapsed — skip")
            return False

        try:
            import anthropic
        except ImportError:
            log.error("[code_review] anthropic package not installed")
            return False

        user_msg = _build_user_payload()
        model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5").strip()
        max_tokens = max(512, _env_int("CODE_REVIEW_MAX_TOKENS", 4096))

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = ""
        for block in msg.content:
            if hasattr(block, "text"):
                text += block.text

        out_path = _output_path()
        _append_markdown(out_path, text.strip() or "_(empty model response)_")
        _touch(last_run)
        log.info("[code_review] Wrote review to %s", out_path)
        return True


def _daemon_loop(stop_event: threading.Event) -> None:
    interval_sec = max(60, _env_int("CODE_REVIEW_INTERVAL_SEC", 86_400))
    if _env_bool("CODE_REVIEW_RUN_ONCE_ON_START", False):
        try:
            run_one_code_review_markdown()
        except Exception as e:
            log.exception("[code_review] run on start failed: %s", e)
    while not stop_event.wait(timeout=interval_sec):
        try:
            run_one_code_review_markdown()
        except Exception as e:
            log.exception("[code_review] periodic run failed: %s", e)


_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def start_code_review_daemon_if_enabled() -> None:
    """Start background thread if CODE_REVIEW_ENABLED and API key present."""
    global _thread
    if not _env_bool("CODE_REVIEW_ENABLED", False):
        return
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        log.warning("[code_review] CODE_REVIEW_ENABLED but ANTHROPIC_API_KEY missing — daemon not started")
        return
    if _thread is not None and _thread.is_alive():
        return

    def _run() -> None:
        _daemon_loop(_stop_event)

    _thread = threading.Thread(target=_run, name="code_review", daemon=True)
    _thread.start()
    log.info(
        "[code_review] Daemon started (interval=%ss, output_mode=%s)",
        max(60, _env_int("CODE_REVIEW_INTERVAL_SEC", 86_400)),
        os.getenv("CODE_REVIEW_OUTPUT_MODE", "append"),
    )


def stop_code_review_daemon() -> None:
    """Best-effort stop (e.g. tests). Main farmer does not call this today."""
    _stop_event.set()
