"""Small helpers: dates, hashing, text normalisation, token clustering."""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

UTC = timezone.utc

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those",
    "is", "are", "was", "were", "be", "been", "being", "to", "of", "in", "on", "at", "for", "from",
    "by", "with", "as", "about", "into", "over", "after", "before", "up", "down", "out", "its",
    "it", "his", "her", "their", "your", "our", "you", "we", "they", "he", "she", "will", "would",
    "can", "could", "should", "may", "might", "has", "have", "had", "do", "does", "did", "not",
    "no", "yes", "new", "says", "said", "say", "amid", "per", "vs", "via", "how", "why", "what",
    "when", "where", "who", "top", "set", "get", "gets", "now", "just", "more", "most", "also",
    "one", "two", "first", "last", "next", "week", "today", "update", "live", "news", "report",
}

_ws = re.compile(r"\s+")
_non_alnum = re.compile(r"[^a-z0-9 ]+")
_punct_tail = re.compile(r"\s+[-|–—]\s+[^-|–—]{1,40}$")


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(UTC).isoformat(timespec="seconds")


def parse_dt(value) -> datetime | None:
    """Parse the many date shapes found in feeds into an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    # RFC-822 (RSS pubDate)
    try:
        dt = parsedate_to_datetime(text)
        if dt:
            return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        pass
    # ISO-8601 variants
    cand = text.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %b %Y %H:%M:%S"):
        try:
            dt = datetime.fromisoformat(cand) if fmt is None else datetime.strptime(cand, fmt)
            return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    # "Oct 15, 2026" (Nasdaq news API) / "15 Oct 2026" / "Oct 15, 2026 08:15 AM"
    for fmt in ("%b %d, %Y %I:%M %p", "%b %d, %Y %H:%M", "%b %d, %Y",
                "%B %d, %Y %I:%M %p", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
                "%b %d %Y", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


_AGO_RE = re.compile(r"(\d+)\s*(second|sec|minute|min|hour|hr|day|week|month|year)s?\s+ago", re.I)
_AGO_DAYS = {
    "second": 1 / 86400, "sec": 1 / 86400, "minute": 1 / 1440, "min": 1 / 1440,
    "hour": 1 / 24, "hr": 1 / 24, "day": 1, "week": 7, "month": 30.4, "year": 365,
}


def parse_ago(value, ref: datetime | None = None) -> datetime | None:
    """Turn relative stamps ("4 days ago", "just now") into an absolute UTC time."""
    if not value:
        return None
    base = ref or now_utc()
    text = str(value).strip().lower()
    if text in ("just now", "now", "today", "moment ago", "moments ago"):
        return base
    m = _AGO_RE.search(text)
    if not m:
        return None
    return base - timedelta(days=int(m.group(1)) * _AGO_DAYS.get(m.group(2).lower(), 0))


def age_hours(published: datetime | None) -> float:
    if not published:
        return 0.0
    return max(0.0, (now_utc() - published).total_seconds() / 3600.0)


def time_ago(published_iso: str | None) -> str:
    dt = parse_dt(published_iso)
    if not dt:
        return ""
    secs = max(1, int((now_utc() - dt).total_seconds()))
    if secs < 90:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def sha1(text: str, n: int = 40) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:n]


def clean_text(text: str | None, limit: int | None = None) -> str:
    if not text:
        return ""
    t = html.unescape(str(text))
    t = re.sub(r"<script.*?</script>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = unicodedata.normalize("NFKC", t)
    t = _ws.sub(" ", t).strip()
    if limit and len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t


def trim_sentence(text: str | None, limit: int = 460) -> str:
    """Trim to ``limit`` chars on a sentence boundary when possible."""
    t = _ws.sub(" ", str(text or "")).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= int(limit * 0.45):
        return cut[: end + 1].strip()
    cut = cut.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut + "…") if cut else ""


def strip_source_suffix(title: str, source: str | None = None) -> str:
    """Google News titles end with ' - Publisher'; drop that for dedupe/tokenising."""
    t = clean_text(title)
    if source and t.endswith(f" - {source}"):
        return t[: -(len(source) + 3)].strip()
    return _punct_tail.sub("", t)


def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url or ""
    drop = {k for k in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                        "ocid", "cmpid", "ref", "sref", "fbclid", "gclid", "mod", "siteid")}
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if k.lower() not in drop]
    netloc = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme or "https", netloc, path, "", urlencode(q), ""))


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def tokens(text: str) -> set[str]:
    t = _non_alnum.sub(" ", strip_source_suffix(text).lower())
    return {w for w in t.split() if len(w) > 2 and w not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


def window_start(hours: float) -> datetime:
    return now_utc() - timedelta(hours=hours)


def norm_ticker(t: str) -> str:
    """BRK/B -> BRK.B, brk-b -> BRK.B, ' aapl ' -> AAPL."""
    s = str(t or "").strip().upper().replace("/", ".").replace(" ", "")
    s = re.sub(r"[^A-Z0-9.\-^]", "", s)
    if len(s) > 1:
        s = s.rstrip(".")
    return s


def ticker_variants(t: str) -> list[str]:
    """Class-share tickers are written BRK.B, BRK-B or BRKB depending on the source."""
    t = norm_ticker(t)
    out = [t]
    if "." in t:
        out += [t.replace(".", "-"), t.replace(".", "")]
    elif "-" in t:
        out += [t.replace("-", "."), t.replace("-", "")]
    return list(dict.fromkeys(out))


def normalize_side(value) -> str | None:
    """Canonical long/short tag. Anything unrecognised means 'no view'."""
    s = str(value or "").strip().lower()
    if s in ("long", "l", "buy", "b", "+"):
        return "long"
    if s in ("short", "s", "sell", "-"):
        return "short"
    return None


def fmt_num(n: float | int | None, digits: int = 2) -> str:
    if n is None:
        return ""
    try:
        return f"{float(n):,.{digits}f}"
    except (TypeError, ValueError):
        return ""
