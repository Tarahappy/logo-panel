"""Shared helpers: paths, HTTP session, name normalization, small I/O utilities."""
from __future__ import annotations

import csv
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
DATA = ROOT / "data"
LOGOS = DATA / "logos"
REPORTS = ROOT / "reports"

SETTINGS = json.loads((CONFIG / "settings.json").read_text())
CONTACT = os.environ.get("CONTACT_EMAIL", SETTINGS.get("contact_email", ""))
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/124.0 Safari/537.36 logo-panel-research ({CONTACT})"
)


def session(retries: int = 3, backoff: float = 2.0) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def polite_sleep(seconds: float | None = None) -> None:
    time.sleep(SETTINGS.get("request_delay_seconds", 1.5) if seconds is None else seconds)


def domain_of(url: str) -> str:
    host = urlparse(url if "://" in url else "http://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------- names
_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES", "LLC", "L L C",
    "LTD", "LIMITED", "PLC", "LP", "L P", "LLP", "NV", "N V", "SA", "S A", "AG", "SE", "AB",
    "HOLDINGS", "HOLDING", "GROUP", "THE", "NA", "N A", "TRUST", "BANCORP", "CLASS A", "CLASS B",
    "CLASS C", "DE", "DELAWARE",
}


def normalize_name(name: str) -> str:
    """Uppercase, strip punctuation and legal suffixes. 'The Walt Disney Company' -> 'WALT DISNEY'."""
    if not isinstance(name, str):
        return ""
    s = name.upper().replace("&", " AND ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    toks = s.split()
    changed = True
    while changed and toks:
        changed = False
        for n in (2, 1):
            if len(toks) > n and " ".join(toks[-n:]) in _SUFFIXES:
                toks = toks[:-n]
                changed = True
                break
        if toks and toks[0] == "THE" and len(toks) > 1:
            toks = toks[1:]
            changed = True
    return " ".join(toks)


# ---------------------------------------------------------------- csv helpers
def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_rows(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """Append rows; if the file's columns differ from fieldnames (code changed), rewrite it with
    fieldnames first so every column keeps its name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != fieldnames:
            old = read_csv(path)
            cols = fieldnames + [c for c in header if c not in fieldnames]
            write_rows(path, old, cols)
            fieldnames = cols
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def write_rows(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def load_firms(tickers: str | None = None) -> list[dict]:
    skip = {r["ticker"].upper() for r in read_csv(CONFIG / "skip_tickers.csv")}
    firms = [r for r in read_csv(CONFIG / "firms.csv")
             if (r.get("homepage") or "").strip() and r["ticker"].upper() not in skip]
    if tickers:
        want = {t.strip().upper() for t in tickers.split(",") if t.strip()}
        firms = [r for r in firms if r["ticker"].upper() in want]
    return firms
