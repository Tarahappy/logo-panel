"""Historical logo backfill from the Wayback Machine, with change dating.

For each firm:
  1. List one archived homepage capture per month (CDX index), from wayback_start_year.
  2. Coarse pass: take one capture per year (closest to wayback_capture_month); grab the logo.
  3. Where two consecutive yearly logos differ (new image AND phash or color distance above
     threshold), bisect the monthly captures between them to find the first month the new logo
     appears. Each fetch is logged in data/wayback_observations.csv (obs_type yearly/refine).
  4. Each dated change goes to data/wayback_changes.csv (last_old_ts, first_new_ts).

Firms are processed whole, in order, within a time budget; progress is kept in
data/wayback_state.json so nightly runs resume where they stopped.
Homepage history: config/homepage_history.csv (ticker,homepage,from_year,to_year) overrides the
current homepage for earlier years (e.g., a firm that moved from one domain to another).

Usage: python scripts/wayback_logos.py [--budget-minutes 50] [--tickers IBM,KO] [--redo]
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date

from common import DATA, SETTINGS, append_rows, domain_of, load_firms, read_csv, CONFIG
from features import color_distance, phash_distance
from grab import LOGO_FIELDS, best_logo, known_images, save_logo
import wayback

OBS = DATA / "wayback_observations.csv"
CHG = DATA / "wayback_changes.csv"
STATE = DATA / "wayback_state.json"
OBS_FIELDS = ["ticker", "company", "obs_type", "capture_ts", "capture_url", "page_status", *LOGO_FIELDS, "error"]
CHG_FIELDS = ["ticker", "company", "last_old_ts", "first_new_ts", "months_between", "old_image", "new_image",
              "phash_dist", "color_dist", "old_dominant_hue", "new_dominant_hue", "green_share_old",
              "green_share_new", "blue_share_old", "blue_share_new", "n_colors_old", "n_colors_new",
              "detected_on"]


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
        row["image_path"] = save_logo("wayback", firm["ticker"], cap["ts"][:8], raw, ext, known)
    else:
        row["error"] = "no usable logo"
    return row


def is_change(a: dict, b: dict) -> tuple[bool, int | None, float | None]:
    if not a.get("sha1") or not b.get("sha1") or a["sha1"] == b["sha1"]:
        return False, 0 if a.get("sha1") and a.get("sha1") == b.get("sha1") else None, None
    d = phash_distance(a.get("phash", ""), b.get("phash", ""))
    c = color_distance(a, b)
    big = (d is not None and d >= SETTINGS["change_threshold_phash"]) or \
          (c is not None and c >= SETTINGS["change_threshold_color"])
    return big, d, c


def closer_to_new(x: dict, old: dict, new: dict) -> bool | None:
    if not x.get("sha1"):
        return None
    if x["sha1"] == new.get("sha1"):
        return True
    if x["sha1"] == old.get("sha1"):
        return False
    d_old, d_new = phash_distance(x.get("phash", ""), old.get("phash", "")), phash_distance(x.get("phash", ""), new.get("phash", ""))
    if d_old is None or d_new is None:
        return None
    return d_new < d_old


def months_between(ts_a: str, ts_b: str) -> int:
    return (int(ts_b[:4]) - int(ts_a[:4])) * 12 + int(ts_b[4:6]) - int(ts_a[4:6])


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

    obs = []
    good = []
    for c in yearly:
        r = grab_capture(firm, c, known, "yearly")
        obs.append(r)
        if r.get("sha1"):
            good.append(r)

    changes = []
    ts_index = {c["ts"]: i for i, c in enumerate(caps)}
    for a, b in zip(good, good[1:]):
        big, d, cdist = is_change(a, b)
        if not big:
            continue
        lo, hi = ts_index[a["capture_ts"]], ts_index[b["capture_ts"]]
        old, new = a, b
        tried = set()
        while hi - lo > 1:
            mid = (lo + hi) // 2
            while mid in tried and mid < hi - 1:
                mid += 1
            if mid in tried:
                break
            tried.add(mid)
            r = grab_capture(firm, caps[mid], known, "refine")
            obs.append(r)
            side = closer_to_new(r, old, new)
            if side is None:
                # unusable capture: drop it by moving the nearer bound only if no other option
                continue
            if side:
                hi = mid
            else:
                lo = mid
        last_old, first_new = caps[lo]["ts"], caps[hi]["ts"]
        changes.append({
            "ticker": firm["ticker"], "company": firm["company"], "last_old_ts": last_old, "first_new_ts": first_new,
            "months_between": months_between(last_old, first_new), "old_image": a.get("image_path", ""),
            "new_image": b.get("image_path", ""), "phash_dist": d, "color_dist": cdist,
            "old_dominant_hue": a.get("dominant_hue"), "new_dominant_hue": b.get("dominant_hue"),
            "green_share_old": a.get("green_share"), "green_share_new": b.get("green_share"),
            "blue_share_old": a.get("blue_share"), "blue_share_new": b.get("blue_share"),
            "n_colors_old": a.get("n_colors"), "n_colors_new": b.get("n_colors"),
            "detected_on": date.today().isoformat(),
        })
    return obs, changes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-minutes", type=float, default=50)
    ap.add_argument("--tickers")
    ap.add_argument("--redo", action="store_true", help="reprocess firms already marked done")
    a = ap.parse_args()
    t0 = time.time()
    firms = load_firms(a.tickers)
    st = load_state()
    todo = [f for f in firms if a.redo or st.get(f["ticker"], {}).get("status") != "done"]
    if not todo:
        print("Backfill complete: every firm in config/firms.csv is done.")
        return
    known = known_images("wayback")
    done_now, per_firm = 0, []
    for f in todo:
        elapsed = (time.time() - t0) / 60
        avg = sum(per_firm) / len(per_firm) if per_firm else 4.0
        if elapsed + avg * 1.2 > a.budget_minutes:
            break
        t1 = time.time()
        try:
            obs, changes = process_firm(f, known)
        except Exception as e:
            print(f"{f['ticker']}: error {type(e).__name__}: {e}", flush=True)
            st[f["ticker"]] = {"status": "error", "error": str(e)[:200], "on": date.today().isoformat()}
            save_state(st)
            continue
        append_rows(OBS, obs, OBS_FIELDS)
        append_rows(CHG, changes, CHG_FIELDS)
        n_logo = sum(1 for r in obs if r.get("sha1") and r.get("obs_type") == "yearly")
        n_year = sum(1 for r in obs if r.get("obs_type") == "yearly")
        st[f["ticker"]] = {"status": "done", "on": date.today().isoformat(), "years": n_year,
                           "years_with_logo": n_logo, "changes": len(changes)}
        save_state(st)
        per_firm.append((time.time() - t1) / 60)
        done_now += 1
        print(f"{f['ticker']}: {n_logo}/{n_year} years with logo, {len(changes)} change(s), "
              f"{per_firm[-1]:.1f} min", flush=True)
    remaining = sum(1 for f in firms if st.get(f["ticker"], {}).get("status") != "done")
    print(f"processed {done_now} firm(s) this run; {remaining} remaining")
    if remaining == 0:
        print("Backfill complete: every firm in config/firms.csv is done.")


if __name__ == "__main__":
    main()
