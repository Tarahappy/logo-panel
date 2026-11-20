"""Minimal Wayback Machine client (CDX index + raw snapshots)."""
from __future__ import annotations

import re
import time
from urllib.parse import quote

from common import SETTINGS, session

CDX = "https://web.archive.org/cdx/search/cdx"
_S = None


def _s():
    global _S
    if _S is None:
        _S = session(retries=4, backoff=3.0)
    return _S


def _sleep():
    time.sleep(SETTINGS.get("wayback_delay_seconds", 1.0))


def monthly_captures(url: str, start_year: int, end_year: int | None = None) -> list[dict]:
    """One successful HTML capture per month for a homepage. Returns [{'ts','original'}] sorted by ts."""
    # 2xx and 3xx: many homepages redirect (ibm.com -> /us-en/); the archive follows the redirect on fetch
    params = {
        "url": url, "output": "json", "fl": "timestamp,original,statuscode,mimetype",
        "filter": ["statuscode:[23]..", "!mimetype:image/.*"], "collapse": "timestamp:6",
        "from": str(start_year),
    }
    if end_year:
        params["to"] = str(end_year)
    for attempt in range(3):
        try:
            r = _s().get(CDX, params=params, timeout=120)
            if r.status_code == 200 and r.text.strip():
                rows = r.json()
                _sleep()
                return [{"ts": x[0], "original": x[1]} for x in rows[1:]]
            if r.status_code == 200:
                return []
        except Exception:
            pass
        time.sleep(10 * (attempt + 1))
    return []


def snapshot_url(ts: str, original: str, flag: str = "id_") -> str:
    return f"https://web.archive.org/web/{ts}{flag}/{original}"


def get_snapshot(ts: str, original: str, flag: str = "id_", timeout: int = 60):
    """Fetch an archived resource. id_ = raw original bytes; im_ = image. Returns (status, final_url, content, headers)."""
    try:
        r = _s().get(snapshot_url(ts, original, flag), timeout=timeout, allow_redirects=True)
        _sleep()
        return r.status_code, r.url, r.content, r.headers
    except Exception:
        _sleep()
        return 0, "", b"", {}


def original_from_wayback_url(url: str) -> str:
    """https://web.archive.org/web/2010.../http://x.com/a.png -> http://x.com/a.png"""
    m = re.match(r"https?://web\.archive\.org/web/\d+[a-z_]*/(.*)", url)
    return m.group(1) if m else url


def ts_from_wayback_url(url: str) -> str | None:
    m = re.match(r"https?://web\.archive\.org/web/(\d{14})", url)
    return m.group(1) if m else None


def latest_capture(url: str, within_days: int = 30) -> dict | None:
    import datetime as dt
    since = (dt.datetime.utcnow() - dt.timedelta(days=within_days)).strftime("%Y%m%d")
    params = {"url": url, "output": "json", "fl": "timestamp,original", "filter": ["statuscode:[23]..", "!mimetype:image/.*"],
              "from": since, "limit": "-1"}
    try:
        r = _s().get(CDX, params=params, timeout=90)
        rows = r.json() if r.status_code == 200 and r.text.strip() else []
        _sleep()
        if len(rows) > 1:
            return {"ts": rows[-1][0], "original": rows[-1][1]}
    except Exception:
        pass
    return None


def quote_url(u: str) -> str:
    return quote(u, safe=":/?&=%#+,;@")
