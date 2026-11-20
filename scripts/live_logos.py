"""Monthly snapshot of each firm's current homepage logo.

Fetch order: plain HTTP -> (optional) headless Chrome via Playwright -> latest Wayback capture
from the past 30 days. One row per firm per run in data/live_observations.csv; each unique
logo image is stored once under data/logos/live/<TICKER>/.

Usage: python scripts/live_logos.py [--tickers AAPL,IBM] [--threads 8] [--no-wayback]
"""
from __future__ import annotations

import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from common import DATA, SETTINGS, append_rows, load_firms, polite_sleep, read_csv, session
from features import color_distance, phash_distance
from grab import LOGO_FIELDS, best_logo, known_images, save_logo
import wayback

OUT = DATA / "live_observations.csv"
FIELDS = ["obs_date", "ticker", "company", "homepage", "final_url", "http_status", "method", "capture_ts",
          *LOGO_FIELDS, "phash_dist_prev", "color_dist_prev", "changed_vs_prev", "error"]
_local = threading.local()


def _sess():
    if not hasattr(_local, "s"):
        _local.s = session(retries=2, backoff=1.5)
    return _local.s


def http_fetch(url: str, referer: str = ""):
    try:
        r = _sess().get(url, timeout=25, headers={"Referer": referer} if referer else None)
        return r.status_code, r.content, r.headers.get("Content-Type", ""), r.url
    except Exception:
        return 0, b"", "", url


def browser_html(url: str) -> tuple[str, str] | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(user_agent=_sess().headers["User-Agent"])
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(3000)
            html, final = pg.content(), pg.url
            b.close()
            return html, final
    except Exception:
        return None


def process(firm: dict, prev: dict, known: dict, use_wayback: bool, lock: threading.Lock) -> dict:
    today = date.today().isoformat()
    row = {"obs_date": today, "ticker": firm["ticker"], "company": firm["company"], "homepage": firm["homepage"]}
    hp = firm["homepage"]
    status, content, ctype, final = http_fetch(hp)
    row.update(http_status=status, final_url=final, method="http")
    info, raw, ext = {}, None, ""
    if status == 200 and content and "html" in (ctype or "html"):
        html = content.decode("utf-8", "replace")
        info, raw, ext = best_logo(html, final, firm["company"], lambda u: http_fetch(u, final)[:3])
    if not raw:
        bh = browser_html(hp)
        if bh:
            info, raw, ext = best_logo(bh[0], bh[1], firm["company"], lambda u: http_fetch(u, bh[1])[:3])
            row["method"] = "browser"
        if not raw and use_wayback:
            cap = wayback.latest_capture(hp, within_days=30)
            if cap:
                st, wurl, wcontent, _ = wayback.get_snapshot(cap["ts"], cap["original"], "id_")
                if st == 200 and wcontent:
                    wbase = wayback.original_from_wayback_url(wurl) if wurl else cap["original"]
                    wts = wayback.ts_from_wayback_url(wurl) or cap["ts"]

                    def wfetch(u, ts=wts):
                        s2, _, c2, h2 = wayback.get_snapshot(ts, u, "im_")
                        return s2, c2, h2.get("Content-Type", "") if h2 else ""
                    info, raw, ext = best_logo(wcontent.decode("utf-8", "replace"), wbase,
                                               firm["company"], wfetch)
                    row.update(method="wayback", capture_ts=cap["ts"])
    row.update(info)
    if raw:
        with lock:
            row["image_path"] = save_logo("live", firm["ticker"], today, raw, ext, known)
        p = prev.get(firm["ticker"])
        if p and p.get("sha1"):
            d = phash_distance(p.get("phash", ""), row.get("phash", ""))
            c = color_distance(p, row)
            row["phash_dist_prev"] = "" if d is None else d
            row["color_dist_prev"] = "" if c is None else c
            big = (d is not None and d >= SETTINGS["change_threshold_phash"]) or \
                  (c is not None and c >= SETTINGS["change_threshold_color"])
            row["changed_vs_prev"] = int(p["sha1"] != row["sha1"] and big)
    else:
        row["error"] = "no usable logo" if status == 200 else f"http {status}"
    polite_sleep()
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers")
    ap.add_argument("--threads", type=int, default=SETTINGS.get("live_threads", 8))
    ap.add_argument("--no-wayback", action="store_true")
    a = ap.parse_args()
    firms = load_firms(a.tickers)
    today = date.today().isoformat()
    done_today = {r["ticker"] for r in read_csv(OUT) if r.get("obs_date") == today}
    firms = [f for f in firms if f["ticker"] not in done_today]  # resume a run cut short
    prev = {}
    for r in read_csv(OUT):
        if r.get("sha1"):
            prev[r["ticker"]] = r
    known = known_images("live")
    lock = threading.Lock()
    rows = []
    with ThreadPoolExecutor(max_workers=a.threads) as ex:
        futs = {ex.submit(process, f, prev, known, not a.no_wayback, lock): f for f in firms}
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                rows.append(fu.result())
            except Exception as e:  # keep going
                f = futs[fu]
                rows.append({"obs_date": date.today().isoformat(), "ticker": f["ticker"], "company": f["company"],
                             "homepage": f["homepage"], "error": f"crash: {type(e).__name__}: {e}"[:200]})
            append_rows(OUT, [rows[-1]], FIELDS)  # save as we go, so a timeout loses nothing
            if i % 50 == 0:
                print(f"{i}/{len(firms)} done", flush=True)
    ok = sum(1 for r in rows if r.get("sha1"))
    by = {}
    for r in rows:
        by[r.get("method", "")] = by.get(r.get("method", ""), 0) + (1 if r.get("sha1") else 0)
    print(f"logos captured: {ok}/{len(rows)}  by method: {by}  changed: {sum(1 for r in rows if str(r.get('changed_vs_prev')) == '1')}")


if __name__ == "__main__":
    main()
