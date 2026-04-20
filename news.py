"""
news.py — Aave news monitor + X helpers for funding_farmer symbol boost
Sources:
  1. Aave governance forum RSS  — real-time, official
  2. Snapshot (aavedao.eth)     — open / pending DAO polls only (not closed)
  3. Google News RSS            — web headlines (no API key)
  4. Reddit JSON                — r/aave + filtered r/ethereum (no API key)
  5. X API v2 (optional)        — official recent search only (no scraping)

For Aster: use ``fetch_x_recent_lines`` + ``extract_usdt_perp_symbols_from_xt`` with
``X_SEARCH_QUERY`` aimed at official Aster accounts (see .env.example).
"""

import base64
import html
import logging
import re
import shutil
import textwrap
import warnings
import xml.etree.ElementTree as ET
from typing import AbstractSet, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

try:
    from urllib3.exceptions import NotOpenSSLWarning

    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

import requests

from config import (
    NEWS_LOG_COLORS,
    NEWS_SKIP_REDDIT,
    REDDIT_USER_AGENT,
    X_API_KEY,
    X_API_SECRET,
    X_BEARER_TOKEN,
    X_MAX_RESULTS,
    X_SEARCH_QUERY,
)

log = logging.getLogger(__name__)

# ANSI for poll preview lines (matches classify keyword priority).
# Bold red/yellow: many themes render 91/93 faint vs 92 (green); bold improves X / log contrast.
_SGR_VERY_BAD = "\033[1;91m"
_SGR_BAD = "\033[1;33m"
_SGR_GOOD = "\033[92m"
_SGR_RESET = "\033[0m"

# Per-character keyword paint: highest severity wins on overlaps (very_bad > bad > good).
_TIER_SEV = {"very_bad": 3, "bad": 2, "good": 1}

GOVERNANCE_RSS = "https://governance.aave.com/latest.rss"
# Google News RSS — free, no key; query tuned for Aave / exploit narrative.
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q="
    + quote("Aave OR KelpDAO OR rsETH OR bad debt OR Aave lending", safe="")
    + "&hl=en-US&gl=US&ceid=US:en"
)

# Reddit’s CDN often returns 403 on /new.json for non–Mozilla User-Agents when using
# Python’s TLS stack; RSS is served as Atom (<entry>), not legacy RSS <item>.
_REDDIT_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 "
    "aster-aave-exploit-bot/1.0"
)


def _reddit_user_agent() -> str:
    return REDDIT_USER_AGENT or _REDDIT_DEFAULT_UA


def _reddit_headers(*, for_json: bool) -> dict:
    accept = (
        "application/json;q=0.9,*/*;q=0.8"
        if for_json
        else "application/atom+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.8"
    )
    return {
        "User-Agent": _reddit_user_agent(),
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.reddit.com/",
    }


def _rss_feed_headers(*, referer: str = "https://news.google.com/") -> dict:
    """Mozilla-style UA for non-Reddit RSS (Google News, etc.)."""
    return {
        "User-Agent": _reddit_user_agent(),
        "Accept": "application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }


# Broader subreddit: keep only titles matching these (lowercase substring).
_REDDIT_ETH_FILTER = (
    "aave",
    "kelp",
    "rseth",
    "bad debt",
    "lending protocol",
    "defi exploit",
    "governance",
)

SNAPSHOT_HUB = "https://hub.snapshot.org/graphql"
# Primary Aave DAO Snapshot space (temp check / ARFC / etc.); legacy `aave.eth` is largely unused on hub.
SNAPSHOT_SPACES = ("aavedao.eth",)
# Pull extra rows from Snapshot, then drop `closed` so logs are only open / pending-style polls.
SNAPSHOT_PROPOSALS_FETCH = 50
SNAPSHOT_OPEN_MAX = 20

_SNAPSHOT_QUERY = """
query Proposals($spaces: [String]!, $first: Int!) {
  proposals(
    first: $first
    skip: 0
    where: { space_in: $spaces }
    orderBy: "created"
    orderDirection: desc
  ) {
    id
    title
    state
  }
}
"""

GOOD_KEYWORDS = [
    "umbrella covered", "bad debt resolved", "debt cleared",
    "full coverage", "aave recovers", "weth withdrawals resumed",
    "rseth backed", "kelp repaid", "exploit contained",
    "withdrawals resumed", "withdrawals reopen", "withdrawals restored",
    "incident resolved", "incident contained", "funds recovered",
    "liquidity restored", "patch deployed", "mitigated",
    "no loss of user funds", "repayment completed", "made whole",
    "governance approved fix", "back to normal",
    # X / official comms — governance & shipping (multi-word to limit noise)
    "temperature check", "temp check", "temp-check", "arfc live", "arfc is live",
    "vote is live", "voting is live", "voting live", "on-chain vote", "on chain vote",
    "snapshot vote", "vote on snapshot", "governance vote", "governance forum",
    "forum discussion", "read the proposal", "proposal is live", "new proposal",
    "proposal passes", "proposal passed", "aip passed", "aip executed",
    "successfully deployed", "now deployed", "deployment live", "shipped to mainnet",
    "live on mainnet", "integration live", "partnership with", "partnership announcement",
    "security audit", "audit completed", "audit report", "formal verification",
    "bug bounty", "vulnerability patched", "vulnerability fixed", "patch is live",
    "risk stewards", "risk steward", "risk dashboard", "weekly update", "community update",
    "governance update", "protocol update", "thank you community", "thanks community",
    "no impact to user", "user funds remain", "funds remain safe", "operating normally",
    "liquidity added", "caps increased", "caps raised", "new asset listed", "asset listed",
    "gho is live", "gho borrow", "gho supply", "safety module", "e-mode update",
    "kelp update", "kelp announces", "rseth support", "steth collateral",
    "aave v3", "v3 deployment", "portal is live", "live on ethereum", "live on base",
    "bgd labs", "bgd update", "aave labs",
]
BAD_KEYWORDS = [
    "bad debt", "haircut", "weth locked", "umbrella insufficient",
    "aave insolvent", "exploit worsens", "second exploit",
    "more bad debt", "rseth depeg", "contagion",
    "dao exploit",
    "withdrawals paused", "withdrawals halted", "withdrawals disabled",
    "exploit suspected", "security incident", "smart contract vulnerability",
    "oracle manipulation", "liquidity crisis", "under-collateralized",
    "circuit breaker", "insolvency risk", "suspended deposits",
    "active investigation", "irregular activity", "possible exploit",
    "reentrancy", "price manipulation", "griefing attack",
    # X / incident vocabulary
    "security breach", "unauthorized access", "unauthorised access",
    "privilege escalation", "flash loan attack", "dns hijack", "phishing attack",
    "exploit confirmed", "hack confirmed", "confirmed exploit", "root cause analysis",
    "zero-day", "zero day", "wallet drained", "drained from", "outflows spike",
    "depeg risk", "peg stress", "liquidation cascade", "mass liquidation",
    "read-only mode", "read only mode", "markets frozen", "market frozen",
    "precautionary pause", "temporary pause", "elevated risk", "under review",
    "warning", "warnings",
    "abnormal activity", "suspicious activity", "incident response",
    "social engineering", "malicious contract", "malicious proposal",
    "rug pull", "rugpull", "insider threat", "key compromise",
    # Official incident threads on X (often say “paused / investigate” without older list phrases)
    "we have paused", "have paused", "has been paused", "was paused",
    "suspicious cross-chain", "cross-chain activity", "while we investigate",
    "under investigation", "paused contracts", "paused rseth",
    # Headline vocabulary (Google News / Reddit)
    "liquidity crunch", "withdrawal panic", "withdraw now", "withdraw everything",
    "panic", "crunch", "drains", "drained", "hacked", "ransom",
]
VERY_BAD_KEYWORDS = [
    "aave pause", "aave shutdown", "emergency shutdown",
    "governance attack", "aave exploit", "aave hacked",
    "emergency pause", "exploit ongoing", "funds drained",
    "bridge exploit", "critical exploit", "protocol exploit",
    "supply paused", "borrow paused", "emergency close",
    # Short X-style crisis phrases
    "protocol hacked", "protocol compromised", "aave compromised",
    "all markets paused", "markets paused", "global pause", "full pause",
    "do not interact", "do not use", "revoke approvals", "revoke allowance",
    "active exploit", "ongoing hack", "wallet drainer", "drainer contract",
    "governance hijack", "malicious upgrade", "emergency multisig",
    "total loss", "total drain", "vault emptied", "bridge hacked",
]

# Forum RSS discusses “bad debt / contagion” academically — skip those phrases for [gov] only.
_BAD_PHRASES_SKIP_ON_GOV = frozenset({"bad debt", "contagion"})

# When multiple new lines match sentiment, prefer official X first, then Snapshot → forum → Google → Reddit.
_FEED_ORDER = {
    "[xt]": 0,
    "[snap]": 1,
    "[gov]": 2,
    "[gn]": 3,
    "[rd]": 4,
}

_seen: set = set()

_HTML_RE = re.compile(r"<[^>]+>")

# X API v2 (app-only bearer). OAuth token URL is still on twitter.com per X docs.
_X_API_V2 = "https://api.twitter.com/2"
_X_OAUTH_TOKEN_URL = "https://api.twitter.com/oauth2/token"
_x_oauth_bearer: Optional[str] = None

# Default recent-search when X_SEARCH_QUERY is unset: official protocol / Kelp / Aave ecosystem
# accounts only (no broad keyword OR — less noise). Override in env for a wider net.
_X_DEFAULT_RECENT_QUERY = (
    "(from:AaveAave OR from:AaveLabs OR from:KelpDAO OR from:bgdlabs) "
    "-is:retweet lang:en"
)


def _fetch_governance() -> list:
    try:
        r    = requests.get(GOVERNANCE_RSS, timeout=10)
        root = ET.fromstring(r.text)
        out  = []
        for item in root.findall(".//item")[:15]:
            title = item.findtext("title", "").lower()
            desc  = item.findtext("description", "").lower()
            out.append(f"[gov] {title} {desc[:200]}")
        return out
    except Exception as e:
        log.warning(f"Governance RSS: {e}")
        return []


def _fetch_snapshot() -> list:
    try:
        r = requests.post(
            SNAPSHOT_HUB,
            json={
                "query": _SNAPSHOT_QUERY,
                "variables": {
                    "spaces": list(SNAPSHOT_SPACES),
                    "first": SNAPSHOT_PROPOSALS_FETCH,
                },
            },
            timeout=12,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        payload = r.json()
        errs = payload.get("errors")
        if errs:
            msg = errs[0].get("message", str(errs[0])) if isinstance(errs[0], dict) else str(errs[0])
            log.warning("Snapshot GraphQL: %s", msg)
            return []
        proposals = (payload.get("data") or {}).get("proposals") or []
        out: List[str] = []
        for p in proposals:
            pid = (p.get("id") or "").strip().lower()
            title = (p.get("title") or "").strip().lower()
            state = (p.get("state") or "").strip().lower()
            if not pid or not title:
                continue
            if state == "closed":
                continue
            out.append(f"[snap] {state} | {title} | {pid}")
            if len(out) >= SNAPSHOT_OPEN_MAX:
                break
        return out
    except Exception as e:
        log.warning(f"Snapshot: {e}")
        return []


def _fetch_rss_tagged(url: str, tag: str, max_items: int = 12) -> list:
    try:
        r = requests.get(url, timeout=10, headers=_rss_feed_headers())
        r.raise_for_status()
        root = ET.fromstring(r.text)
        out: List[str] = []
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip().lower()
            desc = (item.findtext("description") or "").strip().lower()[:220]
            if not title:
                continue
            out.append(f"[{tag}] {title} {desc}")
        if not out:
            titles = _reddit_feed_titles(r.text, max_items)
            out = [f"[{tag}] {t}" for t in titles]
        return out
    except Exception as e:
        log.warning("%s feed: %s", tag, e)
        return []


def _fetch_google_news() -> list:
    return _fetch_rss_tagged(GOOGLE_NEWS_RSS, "gn", max_items=12)


def _reddit_feed_titles(xml_text: str, limit: int) -> List[str]:
    """Titles from Reddit feed XML (RSS 2.0 <item> or Atom <entry>)."""
    root = ET.fromstring(xml_text)
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    titles: List[str] = []
    for item in root.findall(".//item")[:limit]:
        t = (item.findtext("title") or "").strip().lower()
        if t:
            titles.append(t)
    if titles:
        return titles
    for entry in root.findall(".//entry")[:limit]:
        t = (entry.findtext("title") or "").strip().lower()
        if t:
            titles.append(t)
    return titles


def _fetch_reddit_rss_new(subreddit: str, tag: str, limit: int, host: str) -> list:
    """Fetch subreddit /new via .rss (Atom or RSS; used when JSON is blocked). host is www or old."""
    try:
        url = f"https://{host}.reddit.com/r/{subreddit}/new/.rss?limit={limit}"
        r = requests.get(url, timeout=12, headers=_reddit_headers(for_json=False))
        r.raise_for_status()
        titles = _reddit_feed_titles(r.text, limit)
        return [f"[{tag}] {t}" for t in titles]
    except Exception as e:
        log.debug("Reddit r/%s RSS (%s): %s", subreddit, host, e)
        return []


def _fetch_reddit_rss_fallback(subreddit: str, tag: str, limit: int) -> list:
    for host in ("www", "old"):
        rows = _fetch_reddit_rss_new(subreddit, tag, limit, host)
        if rows:
            log.info("Reddit r/%s: using %s.reddit.com RSS (%d items)", subreddit, host, len(rows))
            return rows
    log.warning(
        "Reddit r/%s: blocked (JSON + Atom RSS). Try REDDIT_USER_AGENT= (browser UA in env), "
        "another network, or NEWS_SKIP_REDDIT=true (see env.example).",
        subreddit,
    )
    return []


def _reddit_json_listing_children(url: str, limit: int) -> Optional[list]:
    """GET JSON listing; return children list or None if unusable (403/empty)."""
    r = requests.get(url, timeout=12, headers=_reddit_headers(for_json=True))
    if r.status_code == 403:
        return None
    if r.status_code != 200:
        r.raise_for_status()
    data = r.json()
    children = (data.get("data") or {}).get("children") or []
    return children[:limit]


def _children_to_tagged_lines(children: list, tag: str, limit: int) -> List[str]:
    out: List[str] = []
    for c in children[:limit]:
        p = c.get("data") or {}
        t = (p.get("title") or "").strip().lower()
        if t:
            out.append(f"[{tag}] {t}")
    return out


def _fetch_reddit_new(subreddit: str, tag: str, limit: int) -> list:
    q = f"?limit={limit}&raw_json=1"
    json_urls = (
        f"https://www.reddit.com/r/{subreddit}/new.json{q}",
        f"https://old.reddit.com/r/{subreddit}/new.json{q}",
    )
    last_err: Optional[Exception] = None
    for url in json_urls:
        try:
            children = _reddit_json_listing_children(url, limit)
            if children is not None:
                lines = _children_to_tagged_lines(children, tag, limit)
                host = "old" if "old.reddit.com" in url else "www"
                log.debug("Reddit r/%s: JSON via %s.reddit.com (%d titles)", subreddit, host, len(lines))
                return lines
            log.info("Reddit r/%s JSON 403 on %s — trying next host or .rss", subreddit, url.split("/")[2])
        except Exception as e:
            last_err = e
            log.debug("Reddit r/%s JSON %s: %s", subreddit, url, e)
    if last_err:
        log.warning("Reddit r/%s: %s — trying .rss fallback", subreddit, last_err)
    else:
        log.info("Reddit r/%s JSON blocked (403) — trying .rss fallback", subreddit)
    return _fetch_reddit_rss_fallback(subreddit, tag, limit)


def _fetch_reddit_aave() -> list:
    return _fetch_reddit_new("aave", "rd", limit=12)


def _fetch_reddit_eth_filtered() -> list:
    raw = _fetch_reddit_new("ethereum", "rd", limit=30)
    out: List[str] = []
    for line in raw:
        body = line[5:].strip() if line.startswith("[rd] ") else line
        if any(k in body for k in _REDDIT_ETH_FILTER):
            out.append(line)
    return out[:10]


def _fetch_reddit_merged() -> list:
    if NEWS_SKIP_REDDIT:
        return []
    aave_rd = _fetch_reddit_aave()
    eth_rd = _fetch_reddit_eth_filtered()
    seen_rd: set = set()
    out: List[str] = []
    for line in aave_rd + eth_rd:
        key = line[5:].strip() if line.startswith("[rd] ") else line
        if key in seen_rd:
            continue
        seen_rd.add(key)
        out.append(line)
    return out


def _x_resolve_bearer() -> Optional[str]:
    """App-only bearer: paste X_BEARER_TOKEN, or use X_API_KEY + X_API_SECRET (OAuth2 client_credentials)."""
    global _x_oauth_bearer
    if X_BEARER_TOKEN:
        return X_BEARER_TOKEN
    if not X_API_KEY or not X_API_SECRET:
        return None
    if _x_oauth_bearer:
        return _x_oauth_bearer
    try:
        raw = f"{X_API_KEY}:{X_API_SECRET}".encode("utf-8")
        b64 = base64.b64encode(raw).decode("ascii")
        r = requests.post(
            _X_OAUTH_TOKEN_URL,
            headers={
                "Authorization": f"Basic {b64}",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        r.raise_for_status()
        token = (r.json() or {}).get("access_token")
        if not token:
            log.warning("X OAuth2: no access_token in response")
            return None
        _x_oauth_bearer = str(token).strip()
        return _x_oauth_bearer
    except Exception as e:
        log.warning("X OAuth2 (client_credentials): %s", e)
        return None


# USDT-M perp tickers in tweet text (digits allowed, e.g. 1000PEPEUSDT).
_USDT_PERP_SYMBOL_RE = re.compile(r"\b([0-9A-Z]{2,32}USDT)\b")


def fetch_x_recent_lines() -> List[str]:
    """
    X API v2 recent search only (same rows as embedded in ``fetch_headlines``).
    For funding_farmer symbol boost — avoids governance / Snapshot / Reddit fetches.
    """
    return _fetch_x_recent()


def _xt_line_body_for_symbols(line: str) -> str:
    """Strip ``[xt]`` prefix and trailing ``| id=…`` from a headline line."""
    if not line.startswith("[xt] "):
        return ""
    rest = line[5:].strip()
    sep = " | id="
    if sep in rest:
        rest = rest.split(sep, 1)[0].strip()
    return rest


def extract_usdt_perp_symbols_from_xt(
    lines: Iterable[str],
    valid_symbols: Optional[AbstractSet[str]] = None,
) -> Set[str]:
    """
    Parse ``*USDT`` tokens from ``[xt]`` lines (from ``fetch_x_recent_lines`` / ``fetch_headlines``).

    When ``valid_symbols`` is set (e.g. Aster ``exchangeInfo`` keys), only symbols present there are kept.
    """
    out: Set[str] = set()
    for line in lines:
        body = _xt_line_body_for_symbols(line)
        if not body:
            continue
        upper = body.upper()
        for m in _USDT_PERP_SYMBOL_RE.finditer(upper):
            sym = m.group(1)
            if valid_symbols is not None and sym not in valid_symbols:
                continue
            out.add(sym)
    return out


def _fetch_x_recent() -> list:
    """
    Official GET /2/tweets/search/recent (last ~7 days). Disabled without bearer credentials.
    """
    global _x_oauth_bearer
    bearer = _x_resolve_bearer()
    if not bearer:
        return []
    query = (X_SEARCH_QUERY or "").strip() or _X_DEFAULT_RECENT_QUERY
    try:
        r = requests.get(
            f"{_X_API_V2}/tweets/search/recent",
            headers={"Authorization": f"Bearer {bearer}"},
            params={
                "query": query,
                "max_results": str(X_MAX_RESULTS),
                "tweet.fields": "created_at",
            },
            timeout=15,
        )
        if r.status_code == 401:
            _x_oauth_bearer = None
        if not r.ok:
            try:
                detail = r.json()
            except Exception:
                detail = (r.text or "")[:300]
            log.warning("X recent search: HTTP %s — %s", r.status_code, detail)
            return []
        payload = r.json() or {}
        errs = payload.get("errors")
        if errs:
            log.warning("X recent search: %s", errs[:1])
            return []
        rows = payload.get("data") or []
        out: List[str] = []
        for tw in rows:
            if not isinstance(tw, dict):
                continue
            tid = str(tw.get("id") or "").strip()
            text = (tw.get("text") or "").strip()
            if not tid or not text:
                continue
            body = " ".join(text.lower().split())[:420]
            out.append(f"[xt] {body} | id={tid}")
        return out
    except Exception as e:
        log.warning("X recent search: %s", e)
        return []


def fetch_headlines() -> list:
    # X first: official-account posts are most authoritative; same order as classify priority.
    return (
        _fetch_x_recent()
        + _fetch_governance()
        + _fetch_snapshot()
        + _fetch_google_news()
        + _fetch_reddit_merged()
    )


def fresh_headlines(headlines: List[str]) -> List[str]:
    """
    Headlines not yet in the sentiment dedup set.
    Call once per poll, before classify(headlines), so logs match what classify will treat as new.
    """
    return [h for h in headlines if h not in _seen]


def _headline_feed_tag(line: str) -> str:
    for p in ("[xt]", "[snap]", "[gov]", "[gn]", "[rd]"):  # [xt] checked first — matches classify priority
        if line.startswith(p):
            return p
    return ""


def _classify_sort_key(line: str) -> Tuple[int, str]:
    return (_FEED_ORDER.get(_headline_feed_tag(line), 99), line)


def _bad_keywords_for_line(line: str) -> List[str]:
    """Governance forum text is noisy; drop a few ultra-generic BAD phrases for [gov] only."""
    if _headline_feed_tag(line) == "[gov]":
        return [kw for kw in BAD_KEYWORDS if kw not in _BAD_PHRASES_SKIP_ON_GOV]
    return BAD_KEYWORDS


def _line_matches_very_bad(line: str) -> bool:
    ln = line.lower()
    return any(kw in ln for kw in VERY_BAD_KEYWORDS)


def _line_matches_bad(line: str) -> bool:
    ln = line.lower()
    return any(kw in ln for kw in _bad_keywords_for_line(line))


def _line_matches_good(line: str) -> bool:
    ln = line.lower()
    return any(kw in ln for kw in GOOD_KEYWORDS)


def classify(headlines: List[str]) -> Optional[str]:
    global _seen
    new = [h for h in headlines if h not in _seen]
    _seen.update(headlines)
    if not new:
        return None
    new_sorted = sorted(new, key=_classify_sort_key)
    for h in new_sorted:
        if _line_matches_very_bad(h):
            log.warning(f"VERY BAD: {h}")
            return "very_bad"
    for h in new_sorted:
        if _line_matches_bad(h):
            log.warning(f"BAD: {h}")
            return "bad"
    for h in new_sorted:
        if _line_matches_good(h):
            log.info(f"GOOD: {h}")
            return "good"
    return None


def _line_sentiment(line: str) -> Optional[str]:
    """Same tiers and BAD screening as classify (per line; no feed-order needed)."""
    if _line_matches_very_bad(line):
        return "very_bad"
    if _line_matches_bad(line):
        return "bad"
    if _line_matches_good(line):
        return "good"
    return None


def _colorize_line(text: str, sentiment: Optional[str], enabled: bool) -> str:
    if not enabled or not sentiment:
        return text
    prefix = {
        "very_bad": _SGR_VERY_BAD,
        "bad": _SGR_BAD,
        "good": _SGR_GOOD,
    }.get(sentiment, "")
    if not prefix:
        return text
    return f"{prefix}{text}{_SGR_RESET}"


def _keyword_char_tiers(text: str, raw_line: str) -> List[Optional[str]]:
    """
    Per-character sentiment tier for `text` (substring match; same lists as classify).

    1) ``very_bad`` vs ``bad``: higher severity wins on overlaps.
    2) ``good`` phrases: painted after (1) and override ``bad`` on the same characters
       so positive phrases like ``bad debt resolved`` are not left half-yellow; they
       never override ``very_bad``.
    [gov] uses `_bad_keywords_for_line` omissions for BAD only.
    """
    n = len(text)
    lower = text.lower()
    sev = [0] * n
    best: List[Optional[str]] = [None] * n
    for tier, kws in (
        ("very_bad", VERY_BAD_KEYWORDS),
        ("bad", _bad_keywords_for_line(raw_line)),
    ):
        tr = _TIER_SEV[tier]
        for kw in kws:
            if not kw:
                continue
            kl = kw.lower()
            pos = 0
            while True:
                i = lower.find(kl, pos)
                if i < 0:
                    break
                j = min(i + len(kl), n)
                for k in range(i, j):
                    if tr > sev[k]:
                        sev[k] = tr
                        best[k] = tier
                pos = i + 1
    trg = _TIER_SEV["good"]
    vb = _TIER_SEV["very_bad"]
    for kw in GOOD_KEYWORDS:
        if not kw:
            continue
        kl = kw.lower()
        pos = 0
        while True:
            i = lower.find(kl, pos)
            if i < 0:
                break
            j = min(i + len(kl), n)
            for k in range(i, j):
                if sev[k] < vb:
                    sev[k] = trg
                    best[k] = "good"
            pos = i + 1
    return best


def _sgr_from_char_tiers(segment: str, char_tiers: List[Optional[str]]) -> str:
    """Apply ANSI runs to segment; len(char_tiers) must equal len(segment)."""
    if len(segment) != len(char_tiers):
        return segment
    colors = {"very_bad": _SGR_VERY_BAD, "bad": _SGR_BAD, "good": _SGR_GOOD}
    out: List[str] = []
    p = 0
    n = len(segment)
    while p < n:
        t = char_tiers[p]
        q = p + 1
        while q < n and char_tiers[q] == t:
            q += 1
        chunk = segment[p:q]
        if t:
            out.append(f"{colors[t]}{chunk}{_SGR_RESET}")
        else:
            out.append(chunk)
        p = q
    return "".join(out)


def _segment_keyword_colors(segment: str, raw_line: str, enabled: bool) -> str:
    """
    Color every substring that matches VERY_BAD / BAD / GOOD (same lists as classify).
    Used when the whole string is one segment; poll lines use full-body tiers + wrap.
    """
    if not enabled or not segment:
        return segment
    return _sgr_from_char_tiers(segment, _keyword_char_tiers(segment, raw_line))


def iter_sentiment_keyword_legend_lines(
    width: int,
    color_sentiment: bool,
) -> Iterable[str]:
    yield ""
    title = "Sentiment keyword key (substring match; colors match poll lines)"
    yield title
    yield "-" * min(max(len(title), 24), width)
    yield (
        "[gov] omits BAD phrases: bad debt, contagion. "
        "Tie-break per tier: [xt] > [snap] > [gov] > [gn] > [rd]."
    )
    yield ""
    label_w = 12
    wrap_w = max(40, width - label_w - 1)
    for human, tier, kws in (
        ("VERY BAD", "very_bad", VERY_BAD_KEYWORDS),
        ("BAD", "bad", BAD_KEYWORDS),
        ("GOOD", "good", GOOD_KEYWORDS),
    ):
        prefix = f"{human:<{label_w}} "
        joined = ", ".join(kws)
        chunks = textwrap.wrap(
            joined,
            width=wrap_w,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        pad = " " * (label_w + 1)
        first = prefix + chunks[0]
        yield _colorize_line(first, tier if color_sentiment else None, color_sentiment)
        for c in chunks[1:]:
            yield _colorize_line(pad + c, tier if color_sentiment else None, color_sentiment)
    yield ""


def print_sentiment_keyword_legend(
    color_sentiment: Optional[bool] = None,
    width: Optional[int] = None,
) -> None:
    """Print tier-colored keyword lists (same logic as classify / poll lines)."""
    w = width if width is not None else _log_width()
    use_c = NEWS_LOG_COLORS if color_sentiment is None else color_sentiment
    for line in iter_sentiment_keyword_legend_lines(w, use_c):
        print(line)


def _plain(text: str) -> str:
    """Readable text for CLI preview only; classification uses raw headline strings."""
    t = _HTML_RE.sub(" ", text)
    # Descriptions are truncated at 200 chars; tail may be `<a href="https...` with no `>`.
    lt, gt = t.rfind("<"), t.rfind(">")
    if lt > gt:
        t = t[:lt]
    return " ".join(html.unescape(t).split())


def _body_after_tag(line: str) -> str:
    if line.startswith("[gov] "):
        return line[6:].strip()
    if line.startswith("[snap] "):
        return line[7:].strip()
    if line.startswith("[gn] "):
        return line[5:].strip()
    if line.startswith("[rd] "):
        return line[5:].strip()
    if line.startswith("[xt] "):
        return line[5:].strip()
    return line.strip()


def _split_sources(
    headlines: List[str],
) -> Tuple[List[str], List[str], List[str], List[str], List[str], List[str]]:
    gov, snap, gn, rd, xt, other = [], [], [], [], [], []
    for h in headlines:
        if h.startswith("[gov] "):
            gov.append(h)
        elif h.startswith("[snap] "):
            snap.append(h)
        elif h.startswith("[gn] "):
            gn.append(h)
        elif h.startswith("[rd] "):
            rd.append(h)
        elif h.startswith("[xt] "):
            xt.append(h)
        else:
            other.append(h)
    return gov, snap, gn, rd, xt, other


def _log_width(default: int = 96) -> int:
    try:
        return max(60, min(104, shutil.get_terminal_size((100, 24)).columns - 2))
    except Exception:
        return default


def _iter_section_lines(
    title: str,
    lines: List[str],
    width: int,
    *,
    show_empty: bool,
    color_sentiment: bool = False,
    fresh_raw: Optional[AbstractSet[str]] = None,
) -> Iterable[str]:
    if not lines and not show_empty:
        return
    yield title
    yield "-" * min(max(len(title), 24), width)
    if not lines:
        yield "  (no items)"
        return
    label_w = 4
    wrap_w = max(40, width - label_w - 1)
    for i, raw in enumerate(lines, 1):
        if fresh_raw is not None:
            # + = first time we have shown this raw line this process (matches classify dedup).
            label = f"{'+' if raw in fresh_raw else ' '}{i:2d}."
        else:
            label = f"{i:2}."
        body = _plain(_body_after_tag(raw))
        chunks = textwrap.wrap(
            body,
            width=wrap_w,
            break_long_words=True,
            break_on_hyphens=False,
        ) or ["(empty)"]
        pad = " " * (label_w - len(label))
        # Match keywords on the full body, then slice tiers per wrap chunk — otherwise a
        # phrase split across lines (e.g. "bad" / "debt") never matches substring search.
        body_tiers = (
            _keyword_char_tiers(body, raw) if (color_sentiment and body) else None
        )
        pos = 0
        colored_chunks: List[str] = []
        for c in chunks:
            if body_tiers is not None:
                while pos < len(body) and body[pos].isspace():
                    pos += 1
                idx: Optional[int] = None
                if pos + len(c) <= len(body) and body[pos : pos + len(c)] == c:
                    idx = pos
                else:
                    # textwrap can diverge from a simple cursor (quotes / rare spacing); recover.
                    lo = max(0, pos - 24)
                    f = body.find(c, lo)
                    if f >= 0 and f <= pos + 8:
                        idx = f
                if idx is not None:
                    colored_chunks.append(
                        _sgr_from_char_tiers(c, body_tiers[idx : idx + len(c)]),
                    )
                    pos = idx + len(c)
                else:
                    colored_chunks.append(_segment_keyword_colors(c, raw, color_sentiment))
            else:
                colored_chunks.append(c)
        c0 = colored_chunks[0]
        first_ln = f" {label}{pad}{c0}"
        if first_ln.strip():
            yield first_ln
        indent = " " * (label_w + 1)
        for c in colored_chunks[1:]:
            cont = indent + c
            if cont.strip():
                yield cont


def readable_poll_log_lines(
    headlines: List[str],
    *,
    width: Optional[int] = None,
    show_empty_sections: bool = False,
    color_sentiment: Optional[bool] = None,
    fresh_raw: Optional[AbstractSet[str]] = None,
) -> List[str]:
    """
    Human-readable lines for logging (HTML stripped, wrapped). Split by source tag.
    When show_empty_sections is False, omits a source block if it has no rows (typical bot path).
    When color_sentiment is None, uses config NEWS_LOG_COLORS (ANSI: matched phrases very bad=red,
    bad=yellow, good=green inside each line; overlaps follow classify priority).
    When fresh_raw is set (usually ``frozenset(fresh_headlines(...))``), each row label uses
    ``+`` for lines in that set (new this poll vs dedup cache) and a leading space for already-seen.
    """
    w = width if width is not None else _log_width()
    use_color = NEWS_LOG_COLORS if color_sentiment is None else color_sentiment
    gov, snap, gn, rd, xt, other = _split_sources(headlines)
    out: List[str] = []
    for title, rows in (
        ("News · X — official accounts first (API v2 recent search)", xt),
        ("News · Aave governance (RSS)", gov),
        ("News · Snapshot (aavedao.eth — open / pending only)", snap),
        ("News · Google News (Aave / DeFi)", gn),
        ("News · Reddit (r/aave + r/ethereum filter)", rd),
    ):
        if out:
            out.append("")
        out.extend(
            _iter_section_lines(
                title,
                rows,
                w,
                show_empty=show_empty_sections,
                color_sentiment=use_color,
                fresh_raw=fresh_raw,
            ),
        )
    if other:
        if out:
            out.append("")
        out.extend(
            _iter_section_lines(
                "News · other",
                other,
                w,
                show_empty=True,
                color_sentiment=use_color,
                fresh_raw=fresh_raw,
            ),
        )
    while out and not out[-1].strip():
        out.pop()
    return out


def _print_feed_section(
    title: str,
    lines: List[str],
    width: int,
    *,
    color_sentiment: bool = False,
    fresh_raw: Optional[AbstractSet[str]] = None,
) -> None:
    for line in _iter_section_lines(
        title,
        lines,
        width,
        show_empty=True,
        color_sentiment=color_sentiment,
        fresh_raw=fresh_raw,
    ):
        print(line)


def _cli_main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _seen.clear()

    term_w = max(60, min(110, shutil.get_terminal_size((100, 24)).columns))
    print_sentiment_keyword_legend(color_sentiment=NEWS_LOG_COLORS, width=term_w)
    xt = _fetch_x_recent()
    gov = _fetch_governance()
    snap = _fetch_snapshot()
    gn = _fetch_google_news()
    rd = _fetch_reddit_merged()
    headlines = xt + gov + snap + gn + rd
    fresh = fresh_headlines(headlines)

    use_c = NEWS_LOG_COLORS
    print(
        "Feed layout matches the bot (readable_poll_log_lines). "
        "Row prefix: + = new before classify; blank = already in dedup this process."
    )
    for line in readable_poll_log_lines(
        headlines,
        width=term_w,
        show_empty_sections=True,
        color_sentiment=use_c,
        fresh_raw=frozenset(fresh),
    ):
        print(line)

    print()
    print(
        "Classification (`[xt]` first, then `[snap]` / `[gov]` / `[gn]` / `[rd]` — same order as the bot)"
    )
    print("-" * min(term_w, 58))
    sent = classify(headlines)
    print(f"  sentiment: {sent!r}")
    print()


if __name__ == "__main__":
    _cli_main()
