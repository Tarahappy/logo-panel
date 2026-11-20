"""Design-mark (logo) trademark filings by sample firms, from the USPTO Trademark Case Files Dataset.

Needs a free USPTO Open Data Portal API key in the environment variable USPTO_API_KEY
(GitHub: Settings > Secrets and variables > Actions > New repository secret).

Steps
  1. Download owner, case_file, design_search, statement tables (CSV zips) for the release year.
  2. Match owner names to sample firms (normalized exact match, plus prefix match flagged for review;
     extra names in config/firm_aliases.csv, e.g. 'WALMART APOLLO' for WMT).
  3. Keep marks with a design element (mark drawing code 2, 3 or 5).
  4. Write
       data/uspto/firm_design_marks.csv      one row per firm x design mark
       data/uspto/firm_design_codes.csv      design search codes for those marks
       data/uspto/firm_color_statements.csv  statements that mention color (color claims, descriptions)
       data/uspto/owner_match_review.csv     every matched owner name, with match type and mark count

Usage: python scripts/uspto_trademarks.py [--release 2023] [--keep-downloads]
"""
from __future__ import annotations

import argparse
import os
import re
import zipfile
from pathlib import Path

import pandas as pd
import requests

from common import CONFIG, DATA, normalize_name, read_csv

API = "https://api.uspto.gov/api/v1/datasets/products/files/TRCFECO2/{release}/{name}.csv.zip"
OUT = DATA / "uspto"
DL = DATA / "_downloads"
TABLES = ["owner", "case_file", "design_search", "statement"]
CASE_COLS = ["serial_no", "registration_no", "filing_dt", "registration_dt", "abandon_dt", "mark_draw_cd",
             "mark_id_char", "draw_color_file_in", "draw_color_cur_in", "lb_itu_file_in", "cfh_status_cd",
             "cfh_status_dt", "reg_cancel_dt"]


def download(name: str, release: str, key: str) -> Path:
    DL.mkdir(parents=True, exist_ok=True)
    dest = DL / f"{name}_{release}.csv.zip"
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    url = API.format(release=release, name=name)
    with requests.get(url, headers={"X-API-KEY": key}, stream=True, timeout=600, allow_redirects=True) as r:
        if r.status_code != 200:
            raise SystemExit(f"Download failed for {name}: HTTP {r.status_code} {r.text[:300]}\n"
                             "Check USPTO_API_KEY and the release year on the dataset page.")
        with dest.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    print(f"downloaded {name}: {dest.stat().st_size / 1e6:.0f} MB", flush=True)
    return dest


def read_zip_csv(path: Path, usecols=None, chunksize=None):
    z = zipfile.ZipFile(path)
    member = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
    return pd.read_csv(z.open(member), usecols=usecols, dtype=str, chunksize=chunksize,
                       encoding="latin-1", on_bad_lines="skip", low_memory=False)


# words that typically follow a brand in a trademark-holding company's name (Amazon Technologies, Target Brands)
# normalized names too generic to match as a leading phrase (exact matches and aliases only)
GENERIC_PREFIXES = {"U S", "UNITED STATES", "AMERICAN INTERNATIONAL", "AMERICAN", "UNITED", "GENERAL", "NATIONAL",
                    "INTERNATIONAL", "FIRST", "PUBLIC", "CENTRAL"}
# one-word firm names that are common words or surnames: exact names and aliases only (no leading-phrase match)
GENERIC_ONE_WORD = {"WILLIAMS", "SOUTHERN", "NORTHERN", "PROGRESSIVE", "EVEREST", "ROLLINS", "HARTFORD", "BLACKSTONE",
                    "CUMMINS", "ALLSTATE", "CARNIVAL", "COHERENT", "ALPHABET", "TAPESTRY", "TRAVELERS", "GARTNER",
                    "BOOKING", "BALL", "TARGET", "MOSAIC", "COOPER", "APPLE", "VISA", "AMAZON", "ORACLE", "ADOBE"}
IP_WORDS = {"INTELLECTUAL", "IP", "BRANDS", "BRAND", "TECHNOLOGIES", "TECHNOLOGY", "LICENSING", "INTERNATIONAL",
            "PROPERTIES", "PROPERTY", "ENTERPRISES", "WORLDWIDE", "GLOBAL", "USA", "AMERICA", "TRADEMARKS"}


def firm_names() -> tuple[dict[str, str], list[tuple[str, str]], dict[str, str]]:
    """exact: normalized name -> ticker; prefixes: names >= 2 tokens or >= 7 chars, matched as a leading phrase;
    short: one-word names (>= 4 chars) matched only when followed by a typical IP-holding word."""
    exact, prefixes, short = {}, [], {}
    skip = {r["ticker"].upper() for r in read_csv(CONFIG / "skip_tickers.csv")}
    for r in read_csv(CONFIG / "firms.csv"):
        if r["ticker"].upper() in skip:
            continue
        n = normalize_name(r["company"])
        if n:
            exact.setdefault(n, r["ticker"])
            if n in GENERIC_PREFIXES or n in GENERIC_ONE_WORD or len(n.replace(" ", "")) < 4:
                continue
            if len(n.split()) >= 2 or len(n) >= 7:
                prefixes.append((n, r["ticker"]))
            elif len(n) >= 4:
                short.setdefault(n, r["ticker"])
    for r in read_csv(CONFIG / "firm_aliases.csv"):
        n = normalize_name(r.get("owner_name", ""))
        if n and r.get("ticker"):
            exact[n] = r["ticker"].upper()
    prefixes.sort(key=lambda x: -len(x[0]))
    return exact, prefixes, short


def exclusions() -> list[tuple[str, str]]:
    """(ticker, normalized owner-name start) pairs found to be different companies (config/owner_exclusions.csv)."""
    return [(r["ticker"].upper(), normalize_name(r["owner_name_starts_with"]))
            for r in read_csv(CONFIG / "owner_exclusions.csv") if r.get("owner_name_starts_with")]


def match_owner(name_norm: str, exact: dict, prefixes: list, short: dict) -> tuple[str, str]:
    if name_norm in exact:
        return exact[name_norm], "exact"
    for p, t in prefixes:
        if name_norm.startswith(p + " "):
            return t, "prefix"
    toks = name_norm.split()
    if len(toks) >= 2 and toks[0] in short and toks[1] in IP_WORDS:
        return short[toks[0]], "prefix_ip"
    return "", ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default=os.environ.get("USPTO_RELEASE", "2023"))
    ap.add_argument("--keep-downloads", action="store_true")
    a = ap.parse_args()
    key = os.environ.get("USPTO_API_KEY", "").strip()
    if not key:
        raise SystemExit("USPTO_API_KEY is not set. Get a free key at https://data.uspto.gov (My ODP > API key) "
                         "and add it as a GitHub Actions secret named USPTO_API_KEY.")
    OUT.mkdir(parents=True, exist_ok=True)
    paths = {t: download(t, a.release, key) for t in TABLES}

    # 1. owners -> firms
    exact, prefixes, short = firm_names()
    matched = []
    cache: dict[str, tuple[str, str]] = {}

    excl = exclusions()

    def m(n: str) -> tuple[str, str]:
        if n not in cache:
            t, kind = match_owner(n, exact, prefixes, short)
            if t and any(t == et and (n == en or n.startswith(en + " ")) for et, en in excl):
                t, kind = "", ""
            cache[n] = (t, kind)
        return cache[n]

    for chunk in read_zip_csv(paths["owner"], usecols=["serial_no", "own_name", "own_type_cd"], chunksize=1_000_000):
        chunk = chunk.dropna(subset=["own_name"])
        norm = chunk["own_name"].map(normalize_name)
        res = norm.map(m)
        chunk["ticker"] = res.map(lambda x: x[0])
        chunk["match_type"] = res.map(lambda x: x[1])
        matched.append(chunk[chunk["ticker"] != ""])
    own = pd.concat(matched, ignore_index=True)
    review = (own.groupby(["ticker", "own_name", "match_type"]).serial_no.nunique()
              .reset_index(name="n_marks").sort_values(["ticker", "n_marks"], ascending=[True, False]))
    review.to_csv(OUT / "owner_match_review.csv", index=False)
    serials = set(own.serial_no)
    print(f"owner rows matched: {len(own)}  marks: {len(serials)}  firms: {own.ticker.nunique()}", flush=True)

    # 2. case files -> design marks
    keep = []
    for chunk in read_zip_csv(paths["case_file"], usecols=lambda c: c in CASE_COLS, chunksize=1_000_000):
        keep.append(chunk[chunk.serial_no.isin(serials)])
    case = pd.concat(keep, ignore_index=True)
    case = case[case.mark_draw_cd.fillna("").str[:1].isin(["2", "3", "5"])]
    # one row per firm x mark; prefer original applicant (own_type_cd 10) else any
    firm_mark = (own[own.serial_no.isin(case.serial_no)]
                 .sort_values("own_type_cd")
                 .drop_duplicates(["ticker", "serial_no"])[["ticker", "serial_no", "own_name", "match_type", "own_type_cd"]])
    marks = firm_mark.merge(case, on="serial_no", how="left")
    for c in ("filing_dt", "registration_dt", "abandon_dt", "cfh_status_dt", "reg_cancel_dt"):
        if c in marks:
            marks[c] = pd.to_datetime(marks[c], errors="coerce").dt.date
    marks["design_only"] = (marks.mark_draw_cd.str[:1] == "2").astype(int)
    marks["firm_was_applicant"] = (marks.own_type_cd == "10").astype(int)  # 10 = original applicant
    marks.sort_values(["ticker", "filing_dt"]).to_csv(OUT / "firm_design_marks.csv", index=False)
    design_serials = set(marks.serial_no)
    print(f"design marks: {len(marks)} across {marks.ticker.nunique()} firms", flush=True)

    # 3. design codes
    dcs = []
    for chunk in read_zip_csv(paths["design_search"], chunksize=2_000_000):
        dcs.append(chunk[chunk.serial_no.isin(design_serials)])
    dc = pd.concat(dcs, ignore_index=True).merge(firm_mark[["ticker", "serial_no"]], on="serial_no")
    dc.to_csv(OUT / "firm_design_codes.csv", index=False)

    # 4. statements mentioning color
    sts = []
    color = re.compile(r"colou?r", re.I)
    for chunk in read_zip_csv(paths["statement"], chunksize=2_000_000):
        chunk = chunk[chunk.serial_no.isin(design_serials)]
        chunk = chunk[chunk.statement_text.fillna("").str.contains(color)]
        sts.append(chunk)
    st = pd.concat(sts, ignore_index=True).merge(firm_mark[["ticker", "serial_no"]], on="serial_no")
    not_claimed = st.statement_text.str.contains(r"not claimed as a feature", case=False, na=False)
    claimed = st.statement_text.str.contains(r"claimed as a feature", case=False, na=False) & ~not_claimed
    st["color_claimed"] = claimed.astype(int)
    st["color_not_claimed"] = not_claimed.astype(int)
    st.to_csv(OUT / "firm_color_statements.csv", index=False)
    print(f"design codes: {len(dc)}  color statements: {len(st)}", flush=True)

    if not a.keep_downloads:
        for p in paths.values():
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
