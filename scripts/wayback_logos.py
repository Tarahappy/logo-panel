"""Historical logo backfill from the Wayback Machine, with change dating.

For each firm:
  1. List one archived homepage capture per month (CDX index), from wayback_start_year.
  2. Coarse pass: take one capture per year (closest to wayback_capture_month); grab the logo.
  3. Keep only confident picks (`strong` = 1: a logo-marked image, inline SVG, schema.org logo or
     CSS logo background). Group consecutive confident picks with the same shape into runs.
     A run seen only once (in the middle of the history) is treated as a one-off pick and ignored,
     so a stray promo image or a temporary site element does not count as a logo change.
  4. Shape change: a new stable run. The monthly captures between the last old and first new
     sighting are bisected to date the switch to the month (obs_type refine).
     Color change: within one shape, a lasting switch in color profile (both logos color-reliable,
     each seen at least twice). Dated to the yearly window.
  5. Each change goes to data/wayback_changes.csv with change_type shape or color.

Firms run several at a time (--workers) within a time budget; progress is kept in
data/wayback_state.json so nightly runs resume where they stopped.
Homepage history: config/homepage_history.csv (ticker,homepage,from_year,to_year) overrides the
current homepage for earlier years (e.g., a firm that moved from one domain to another).

Usage: python scripts/wayback_logos.py [--budget-minutes 50] [--tickers IBM,KO] [--redo] [--workers 3]
"""
from __future__ import annotations

import argparse
import json
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date

import pandas as pd

from common import CONFIG, DATA, LOGOS, SETTINGS, append_rows, domain_of, load_firms, read_csv
from features import color_distance, phash_distance
from grab import LOGO_FIELDS, best_logo, known_images, save_logo
import wayback

OBS = DATA / "wayback_observations.csv"
CHG = DATA / "wayback_changes.csv"
STATE = DATA / "wayback_state.json"
OBS_FIELDS = ["ticker", "company", "obs_type", "capture_ts", "capture_url", "page_status", *LOGO_FIELDS, "error"]
CHG_FIELDS = ["ticker", "company", "change_type", "last_old_ts", "first_new_ts", "months_between", "old_image",
              "new_image", "phash_dist", "color_dist", "old_dominant_hue", "new_dominant_hue", "green_share_old",
              "green_share_new", "blue_share_old", "blue_share_new", "n_colors_old", "n_colors_new",
              "old_run_years", "new_run_years", "detected_on"]
LOCK = threading.Lock()


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True))


def homepage_for_years(firm: dict) -> list[tuple[str, int, int]]:
    """[(homepage, from_year, to_year)] covering the backfill window."""
    start, end = SETTINGS["wayback_start_year"], date.today().year
    hist = [r for r in read_csv(CONFIG / "homepage_history.csv") if r.get("ticker", "").upper() == firm["ticker"].upper()]
    if not hist:
        return [(firm["homepage"], start, end)]
    out = []
    for r in hist:
        out.append((r["homepage"], int(r.get("from_year") or start), int(r.get("to_year") or end)))
    return sorted(out, key=lambda x: x[1])


def grab_capture(firm: dict, cap: dict, known: dict, obs_type: str) -> dict:
    row = {"ticker": firm["ticker"], "company": firm["company"], "obs_type": obs_type,
           "capture_ts": cap["ts"], "capture_url": wayback.snapshot_url(cap["ts"], cap["original"], "")}
    st, final_url, content, headers = wayback.get_snapshot(cap["ts"], cap["original"], "id_")
    row["page_status"] = st
    if st != 200 or not content:
        row["error"] = f"page {st}"
        return row
    # after an archived redirect, resolve relative links against the page actually served
    base = wayback.original_from_wayback_url(final_url) if final_url else cap["original"]
    page_ts = wayback.ts_from_wayback_url(final_url) or cap["ts"]

    def wfetch(u, ts=page_ts):
        s2, _, c2, h2 = wayback.get_snapshot(ts, u, "im_", timeout=45)
        return s2, c2, (h2.get("Content-Type", "") if h2 else "")

    info, raw, ext = best_logo(content.decode("utf-8", "replace"), base, firm["company"], wfetch)
    row.update(info)
    if raw:
        with LOCK:
            row["image_path"] = save_logo("wayback", firm["ticker"], cap["ts"][:8], raw, ext, known)
    else:
        row["error"] = "no usable logo"
    return row


def strong(r: dict) -> bool:
    return bool(r.get("sha1")) and str(r.get("strong")) == "1"


def same_shape(a: dict, b: dict) -> bool:
    if a.get("sha1") and a.get("sha1") == b.get("sha1"):
        return True
    d = phash_distance(a.get("phash", ""), b.get("phash", ""))
    return d is not None and d < SETTINGS["change_threshold_phash"]


def is_change(a: dict, b: dict) -> tuple[bool, int | None, float | None]:
    """Shape change between two picks (kept for callers and tests)."""
    if not a.get("sha1") or not b.get("sha1") or a["sha1"] == b["sha1"]:
        return False, 0 if a.get("sha1") and a.get("sha1") == b.get("sha1") else None, None
    d = phash_distance(a.get("phash", ""), b.get("phash", ""))
    c = color_distance(a, b)
    return (d is not None and d >= SETTINGS["change_threshold_phash"]), d, c


def color_reliable(r: dict) -> bool:
    return str(r.get("color_reliable")) == "1"


def stable_runs(picks: list[dict]) -> list[list[dict]]:
    """Group consecutive same-shape picks; drop one-off runs except the most recent one; merge
    neighbours that turn out to be the same shape once a one-off between them is dropped."""
    runs: list[list[dict]] = []
    for r in picks:
        if runs and same_shape(runs[-1][-1], r):
            runs[-1].append(r)
        else:
            runs.append([r])
    kept = [run for i, run in enumerate(runs) if len(run) >= 2 or i == len(runs) - 1]
    merged: list[list[dict]] = []
    for run in kept:
        if merged and same_shape(merged[-1][-1], run[0]):
            merged[-1].extend(run)
        else:
            merged.append(run)
    return merged


def color_segments(run: list[dict]) -> list[list[dict]]:
    """Within one shape run, split color-reliable picks where the color profile switches; keep
    segments seen at least twice."""
    segs: list[list[dict]] = []
    for r in [x for x in run if color_reliable(x)]:
        c = color_distance(segs[-1][-1], r) if segs else None
        if segs and (c is None or c < SETTINGS["change_threshold_color"]):
            segs[-1].append(r)
        else:
            segs.append([r])
    return [s for s in segs if len(s) >= 2]


def closer_to_new(x: dict, old: dict, new: dict) -> bool | None:
    if not strong(x):
        return None
    if x["sha1"] == new.get("sha1"):
        return True
    if x["sha1"] == old.get("sha1"):
        return False
    d_old = phash_distance(x.get("phash", ""), old.get("phash", ""))
    d_new = phash_distance(x.get("phash", ""), new.get("phash", ""))
    if d_old is None or d_new is None:
        return None
    return d_new < d_old


def months_between(ts_a: str, ts_b: str) -> int:
    return (int(ts_b[:4]) - int(ts_a[:4])) * 12 + int(ts_b[4:6]) - int(ts_a[4:6])


def change_row(firm, kind, a, b, last_old, first_new, old_run, new_run) -> dict:
    return {
        "ticker": firm["ticker"], "company": firm["company"], "change_type": kind,
        "last_old_ts": last_old, "first_new_ts": first_new, "months_between": months_between(last_old, first_new),
        "old_image": a.get("image_path", ""), "new_image": b.get("image_path", ""),
        "phash_dist": phash_distance(a.get("phash", ""), b.get("phash", "")), "color_dist": color_distance(a, b),
        "old_dominant_hue": a.get("dominant_hue"), "new_dominant_hue": b.get("dominant_hue"),
        "green_share_old": a.get("green_share"), "green_share_new": b.get("green_share"),
        "blue_share_old": a.get("blue_share"), "blue_share_new": b.get("blue_share"),
        "n_colors_old": a.get("n_colors"), "n_colors_new": b.get("n_colors"),
        "old_run_years": len(old_run), "new_run_years": len(new_run), "detected_on": date.today().isoformat(),
    }


def process_firm(firm: dict, known: dict) -> tuple[list[dict], list[dict]]:
    caps: list[dict] = []
    for hp, y0, y1 in homepage_for_years(firm):
        for c in wayback.monthly_captures(domain_of(hp) if hp.rstrip("/").count("/") <= 2 else hp, y0, y1):
            if y0 <= int(c["ts"][:4]) <= y1:
                caps.append(c)
    caps.sort(key=lambda c: c["ts"])
    if not caps:
        return [{"ticker": firm["ticker"], "company": firm["company"], "obs_type": "none",
                 "error": "no captures"}], []
    month = SETTINGS.get("wayback_capture_month", 6)
    by_year: dict[str, list[dict]] = {}
    for c in caps:
        by_year.setdefault(c["ts"][:4], []).append(c)
    yearly = [min(v, key=lambda c: abs(int(c["ts"][4:6]) - month)) for _, v in sorted(by_year.items())]

    obs = [grab_capture(firm, c, known, "yearly") for c in yearly]
    runs = stable_runs([r for r in obs if strong(r)])
    ts_index = {c["ts"]: i for i, c in enumerate(caps)}
    changes = []

    # shape changes, dated by bisection over the monthly captures
    for old_run, new_run in zip(runs, runs[1:]):
        a, b = old_run[-1], new_run[0]
        lo, hi = ts_index[a["capture_ts"]], ts_index[b["capture_ts"]]
        tried: set[int] = set()
        while hi - lo > 1:
            mid = (lo + hi) // 2
            while mid in tried and mid < hi - 1:
                mid += 1
            if mid in tried:
                break
            tried.add(mid)
            r = grab_capture(firm, caps[mid], known, "refine")
            obs.append(r)
            side = closer_to_new(r, a, b)
            if side is None:
                continue
            if side:
                hi = mid
            else:
                lo = mid
        changes.append(change_row(firm, "shape", a, b, caps[lo]["ts"], caps[hi]["ts"], old_run, new_run))

    # color changes within a shape (recolors), dated to the yearly window
    for run in runs:
        segs = color_segments(run)
        for old_seg, new_seg in zip(segs, segs[1:]):
            a, b = old_seg[-1], new_seg[0]
            changes.append(change_row(firm, "color", a, b, a["capture_ts"], b["capture_ts"], old_seg, new_seg))
    changes.sort(key=lambda c: c["first_new_ts"])
    return obs, changes


def remove_previous(tickers: set[str]) -> None:
    """For --redo: drop earlier rows, changes and stored images of these firms."""
    for p in (OBS, CHG):
        if p.exists():
            df = pd.read_csv(p, dtype=str)
            df[~df.ticker.isin(tickers)].to_csv(p, index=False)
    for t in tickers:
        shutil.rmtree(LOGOS / "wayback" / t, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-minutes", type=float, default=50)
    ap.add_argument("--tickers")
    ap.add_argument("--redo", action="store_true", help="reprocess firms already marked done (needs --tickers)")
    ap.add_argument("--workers", type=int, default=SETTINGS.get("wayback_workers", 3))
    a = ap.parse_args()
    t0 = time.time()
    firms = load_firms(a.tickers)
    st = load_state()
    if a.redo:
        if not a.tickers:
            raise SystemExit("--redo needs --tickers, so a full history is never wiped by accident")
        remove_previous({f["ticker"] for f in firms})
        for f in firms:
            st.pop(f["ticker"], None)
        save_state(st)
    todo = [f for f in firms if st.get(f["ticker"], {}).get("status") != "done"]
    if not todo:
        print("Backfill complete: every firm in config/firms.csv is done.")
        return
    known = known_images("wayback")
    per_firm: list[float] = []
    done_now = 0

    def run(f):
        t1 = time.time()
        obs, changes = process_firm(f, known)
        return f, obs, changes, (time.time() - t1) / 60

    queue = list(todo)
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        running = set()
        while queue or running:
            while queue and len(running) < a.workers:
                elapsed = (time.time() - t0) / 60
                avg = sum(per_firm) / len(per_firm) if per_firm else 6.0
                if elapsed + avg * 1.2 > a.budget_minutes:
                    queue = []
                    break
                running.add(ex.submit(run, queue.pop(0)))
            if not running:
                break
            done, running = wait(running, return_when=FIRST_COMPLETED)
            for fu in done:
                try:
                    f, obs, changes, mins = fu.result()
                except Exception as e:
                    print(f"error {type(e).__name__}: {e}", flush=True)
                    continue
                with LOCK:
                    append_rows(OBS, obs, OBS_FIELDS)
                    append_rows(CHG, changes, CHG_FIELDS)
                    n_logo = sum(1 for r in obs if strong(r) and r.get("obs_type") == "yearly")
                    n_year = sum(1 for r in obs if r.get("obs_type") == "yearly")
                    st[f["ticker"]] = {"status": "done", "on": date.today().isoformat(), "years": n_year,
                                       "years_confident_logo": n_logo, "changes": len(changes)}
                    save_state(st)
                per_firm.append(mins)
                done_now += 1
                print(f"{f['ticker']}: confident logo in {n_logo}/{n_year} years, {len(changes)} change(s), "
                      f"{mins:.1f} min", flush=True)
    remaining = sum(1 for f in load_firms() if st.get(f["ticker"], {}).get("status") != "done")
    print(f"processed {done_now} firm(s) this run; {remaining} remaining")
    if remaining == 0:
        print("Backfill complete: every firm in config/firms.csv is done.")


if __name__ == "__main__":
    main()
