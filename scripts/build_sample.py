"""Build config/firms.csv: current S&P 500 constituents (Wikipedia) + official homepages (Wikidata).

Homepage choice, among a firm's Wikidata "official website" (P856) statements:
  not ended (no end-time qualifier) > preferred rank > English or no language qualifier >
  domain resembles the company name > .com > https > shortest path.
All candidate sites, with start/end years, go to config/homepage_candidates.csv; use them to fill
config/homepage_history.csv for firms whose domain changed (Wayback backfill reads that file).
Manual fixes go in config/homepage_overrides.csv (ticker,homepage) and always win.
Firms that leave the index stay in firms.csv with in_sp500_now=0.

Usage: python scripts/build_sample.py
"""
from __future__ import annotations

import io
import re
import time
from datetime import date
from urllib.parse import unquote, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

from common import CONFIG, CONTACT, read_csv, write_rows

WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_API = "https://en.wikipedia.org/w/api.php"
WD_API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
FIELDS = ["ticker", "company", "cik", "gics_sector", "gics_sub_industry", "hq", "date_added", "founded",
          "wikidata", "homepage", "homepage_source", "in_sp500_now", "first_seen"]
ENGLISH = "Q1860"

S = requests.Session()
S.headers.update({"User-Agent": f"logo-panel-research/1.0 ({CONTACT}) python-requests"})


def _get(url, **kw):
    for attempt in range(4):
        r = S.get(url, timeout=120, **kw) if "data" not in kw else S.post(url, timeout=120, **kw)
        if r.status_code == 200:
            return r
        wait = int(r.headers.get("Retry-After", "0") or 0) or 15 * (attempt + 1)
        print(f"{urlparse(url).netloc} {r.status_code}, waiting {wait}s", flush=True)
        time.sleep(min(wait, 120))
    r.raise_for_status()


def sp500() -> pd.DataFrame:
    html = _get(WIKI).text
    df = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns={"Symbol": "ticker", "Security": "company", "GICS Sector": "gics_sector",
                            "GICS Sub-Industry": "gics_sub_industry", "Headquarters Location": "hq",
                            "Date added": "date_added", "CIK": "cik", "Founded": "founded"})
    df["cik"] = df["cik"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(10)
    # article titles from the Security column links
    soup = BeautifulSoup(html, "lxml")
    titles = {}
    for tr in soup.select("table#constituents tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            a = tds[1].find("a", href=True)
            t = tds[0].get_text(strip=True)
            if a and "/wiki/" in a["href"]:
                titles[t] = a.get("title") or unquote(a["href"].split("/wiki/", 1)[1]).replace("_", " ")
    df["article"] = df.ticker.map(titles)
    return df[["ticker", "company", "cik", "gics_sector", "gics_sub_industry", "hq", "date_added", "founded", "article"]]


def qids_by_cik(ciks: list[str]) -> dict[str, str]:
    out = {}
    for i in range(0, len(ciks), 100):
        values = " ".join(f'"{c}"' for c in ciks[i:i + 100])
        q = f"SELECT ?cik ?item WHERE {{ VALUES ?cik {{ {values} }} ?item wdt:P5531 ?cik . }}"
        r = _get(SPARQL, data={"query": q}, headers={"Accept": "application/sparql-results+json"})
        for b in r.json()["results"]["bindings"]:
            out.setdefault(b["cik"]["value"], b["item"]["value"].rsplit("/", 1)[-1])
        time.sleep(5)
    return out


def qids_by_article(titles: list[str]) -> dict[str, str]:
    out = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        r = _get(WIKI_API, params={"action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                                   "redirects": 1, "format": "json", "titles": "|".join(chunk)})
        j = r.json()["query"]
        back = {}
        for m in j.get("normalized", []) + j.get("redirects", []):
            back[m["to"]] = back.get(m["from"], m["from"])
        for p in j.get("pages", {}).values():
            q = p.get("pageprops", {}).get("wikibase_item")
            if q:
                out[back.get(p["title"], p["title"])] = q
                out[p["title"]] = q
        time.sleep(1)
    return out


def websites(qids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for i in range(0, len(qids), 50):
        r = _get(WD_API, params={"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]), "props": "claims",
                                 "format": "json"})
        for qid, ent in r.json().get("entities", {}).items():
            for c in ent.get("claims", {}).get("P856", []):
                try:
                    url = c["mainsnak"]["datavalue"]["value"]
                except KeyError:
                    continue
                q = c.get("qualifiers", {})
                langs = [x.get("datavalue", {}).get("value", {}).get("id") for x in q.get("P407", [])]
                end = [x.get("datavalue", {}).get("value", {}).get("time", "") for x in q.get("P582", [])]
                start = [x.get("datavalue", {}).get("value", {}).get("time", "") for x in q.get("P580", [])]
                out.setdefault(qid, []).append({"url": url, "rank": c.get("rank"), "langs": [l for l in langs if l],
                                                "end": (end[0][1:5] if end and end[0] else ""),
                                                "start": (start[0][1:5] if start and start[0] else "")})
        time.sleep(1)
    return out


def _name_sim(url: str, company: str) -> int:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    base = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
    toks = [t for t in re.findall(r"[a-z0-9]+", company.lower()) if t not in
            {"inc", "corp", "corporation", "company", "co", "the", "group", "holdings", "plc", "ltd", "class", "and"}]
    joined = "".join(toks)
    if not toks:
        return 0
    if base == joined or base == toks[0]:
        return 3
    if toks[0] in base or base in joined:
        return 2
    if any(t in base for t in toks if len(t) > 3):
        return 1
    return 0


def choose(cands: list[dict], company: str) -> str:
    def key(c):
        p = urlparse(c["url"])
        tld = p.netloc.lower().rsplit(".", 1)[-1]
        return (bool(c["end"]), c["rank"] != "preferred", not (not c["langs"] or ENGLISH in c["langs"]),
                -_name_sim(c["url"], company), tld != "com", p.scheme != "https", len(p.path.strip("/")), len(c["url"]))
    url = sorted(cands, key=key)[0]["url"]
    return url


def main() -> None:
    df = sp500()
    # primary route: the Wikipedia article linked from the constituents table
    by_art = qids_by_article(df.article.dropna().tolist())
    df["wikidata"] = df.article.map(by_art).astype(object)
    sites = websites(sorted({q for q in df.wikidata.dropna()}))
    # fallback: Wikidata item that carries the firm's SEC CIK (query service; skipped if it is down)
    need = df[~df.wikidata.isin(list(sites))]
    if len(need):
        try:
            by_cik = qids_by_cik(need.cik.tolist())
            more = websites(sorted(set(by_cik.values()) - set(sites)))
            sites.update(more)
            for idx, r in need.iterrows():
                q = by_cik.get(r.cik)
                if q in sites:
                    df.at[idx, "wikidata"] = q
        except Exception as e:
            print(f"CIK fallback skipped: {e}")

    overrides = {r["ticker"].upper(): r["homepage"].strip() for r in read_csv(CONFIG / "homepage_overrides.csv")
                 if r.get("homepage", "").strip()}
    existing = {r["ticker"]: r for r in read_csv(CONFIG / "firms.csv")}
    today = date.today().isoformat()
    rows, cand_rows = [], []
    for rec in df.to_dict("records"):
        t = rec["ticker"]
        cands = sites.get(rec.get("wikidata") or "", [])
        for c in cands:
            cand_rows.append({"ticker": t, "company": rec["company"], "url": c["url"], "rank": c["rank"],
                              "languages": " ".join(c["langs"]), "start_year": c["start"], "end_year": c["end"]})
        hp, src = (choose(cands, rec["company"]), "wikidata") if cands else ("", "")
        if t.upper() in overrides:
            hp, src = overrides[t.upper()], "override"
        elif not hp and t in existing and existing[t].get("homepage"):
            hp, src = existing[t]["homepage"], existing[t].get("homepage_source", "previous")
        rec.update(homepage=hp, homepage_source=src, in_sp500_now=1,
                   first_seen=existing.get(t, {}).get("first_seen") or today)
        rows.append(rec)
    current = {r["ticker"] for r in rows}
    for t, r in existing.items():
        if t not in current:
            r["in_sp500_now"] = 0
            rows.append(r)
    rows.sort(key=lambda r: r["ticker"])
    write_rows(CONFIG / "firms.csv", rows, FIELDS)
    write_rows(CONFIG / "homepage_candidates.csv", cand_rows,
               ["ticker", "company", "url", "rank", "languages", "start_year", "end_year"])
    missing = [r["ticker"] for r in rows if not r.get("homepage")]
    print(f"firms: {len(rows)}  with homepage: {len(rows) - len(missing)}  missing: {missing}")


if __name__ == "__main__":
    main()
