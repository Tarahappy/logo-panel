"""Combine all streams into analysis files.

Outputs
  data/logo_change_events.csv   one row per detected logo change (Wayback-dated and live), with the
                                nearest USPTO design-mark filing and a blank `validated` column
  data/panel_firm_quarter.csv   firm-quarter panel: current logo features (carried forward), change
                                flags, and USPTO design-mark filing counts
  reports/logo_timelines.html   contact sheet of each firm's logos over time, for validation
                                (download the repo or the reports folder to view images)

Usage: python scripts/build_panel.py
"""
from __future__ import annotations

import html
from datetime import date

import pandas as pd

from common import CONFIG, DATA, REPORTS, SETTINGS, read_csv
from features import FEATURE_FIELDS

FEATS = [f for f in FEATURE_FIELDS if f not in ("phash", "dhash", "colorhash")]


def _ts_to_date(ts: str) -> pd.Timestamp:
    return pd.to_datetime(str(ts)[:8], format="%Y%m%d", errors="coerce")


def observations() -> pd.DataFrame:
    frames = []
    if (DATA / "wayback_observations.csv").exists():
        w = pd.read_csv(DATA / "wayback_observations.csv", dtype=str)
        w = w[w.get("sha1").notna()] if "sha1" in w else w.iloc[0:0]
        w["date"] = w.capture_ts.map(_ts_to_date)
        w["source"] = "wayback"
        frames.append(w)
    if (DATA / "live_observations.csv").exists():
        l = pd.read_csv(DATA / "live_observations.csv", dtype=str)
        l = l[l.get("sha1").notna()] if "sha1" in l else l.iloc[0:0]
        l["date"] = pd.to_datetime(l.obs_date, errors="coerce")
        l["source"] = "live"
        frames.append(l)
    if not frames:
        return pd.DataFrame(columns=["ticker", "date", "source", "sha1", "image_path", *FEATURE_FIELDS])
    o = pd.concat(frames, ignore_index=True)
    return o.dropna(subset=["date"]).sort_values(["ticker", "date"])


def change_events(obs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if (DATA / "wayback_changes.csv").exists():
        c = pd.read_csv(DATA / "wayback_changes.csv", dtype=str)
        for r in c.to_dict("records"):
            rows.append({"ticker": r["ticker"], "source": "wayback", "change_type": r.get("change_type", "shape"),
                         "date_lower": _ts_to_date(r["last_old_ts"]),
                         "date_upper": _ts_to_date(r["first_new_ts"]), "phash_dist": r.get("phash_dist"),
                         "color_dist": r.get("color_dist"), "old_dominant_hue": r.get("old_dominant_hue"),
                         "new_dominant_hue": r.get("new_dominant_hue"), "green_share_old": r.get("green_share_old"),
                         "green_share_new": r.get("green_share_new"), "old_image": r.get("old_image"),
                         "new_image": r.get("new_image")})
    if (DATA / "live_observations.csv").exists():
        l = pd.read_csv(DATA / "live_observations.csv", dtype=str)
        l = l[l.get("sha1").notna()] if "sha1" in l else l.iloc[0:0]
        l["date"] = pd.to_datetime(l.obs_date, errors="coerce")
        for t, g in l.sort_values("date").groupby("ticker"):
            prev = None
            for r in g.to_dict("records"):
                if prev is not None and str(r.get("changed_vs_prev")) == "1":
                    rows.append({"ticker": t, "source": "live", "change_type": "shape" if (
                                    pd.to_numeric(r.get("phash_dist_prev"), errors="coerce") or 0) >= SETTINGS["change_threshold_phash"]
                                 else "color", "date_lower": prev["date"], "date_upper": r["date"],
                                 "phash_dist": r.get("phash_dist_prev"), "color_dist": r.get("color_dist_prev"),
                                 "old_dominant_hue": prev.get("dominant_hue"), "new_dominant_hue": r.get("dominant_hue"),
                                 "green_share_old": prev.get("green_share"), "green_share_new": r.get("green_share"),
                                 "old_image": prev.get("image_path"), "new_image": r.get("image_path")})
                prev = r
    ev = pd.DataFrame(rows, columns=["ticker", "source", "change_type", "date_lower", "date_upper", "phash_dist", "color_dist",
                                     "old_dominant_hue", "new_dominant_hue", "green_share_old", "green_share_new",
                                     "old_image", "new_image"])
    if ev.empty:
        return ev
    ev["event_month"] = ev.date_upper.dt.to_period("M").astype(str)
    ev["months_uncertain"] = ((ev.date_upper - ev.date_lower).dt.days / 30.44).round(1)
    ev["green_shift"] = pd.to_numeric(ev.green_share_new, errors="coerce") - pd.to_numeric(ev.green_share_old, errors="coerce")
    # nearest USPTO design-mark filing within +/- 24 months
    marks_p = DATA / "uspto" / "firm_design_marks.csv"
    if marks_p.exists():
        m = pd.read_csv(marks_p, dtype=str, usecols=lambda c: c in ("ticker", "serial_no", "filing_dt", "lb_itu_file_in"))
        m["filing_dt"] = pd.to_datetime(m.filing_dt, errors="coerce")
        lead, ser, itu = [], [], []
        for r in ev.itertuples():
            g = m[(m.ticker == r.ticker)].copy()
            g["gap"] = (r.date_upper - g.filing_dt).dt.days / 30.44
            g = g[g.gap.abs() <= 24]
            if g.empty:
                lead.append(None); ser.append(None); itu.append(None)
            else:
                best = g.loc[g.gap.abs().idxmin()]
                lead.append(round(best.gap, 1)); ser.append(best.serial_no); itu.append(best.lb_itu_file_in)
        ev["uspto_filing_lead_months"] = lead
        ev["uspto_serial_no"] = ser
        ev["uspto_intent_to_use"] = itu
    old = {}
    if (DATA / "logo_change_events.csv").exists():  # keep manual validation
        for r in read_csv(DATA / "logo_change_events.csv"):
            old[(r["ticker"], r["source"], r["event_month"])] = (r.get("validated", ""), r.get("notes", ""))
    ev["validated"] = [old.get((r.ticker, r.source, r.event_month), ("", ""))[0] for r in ev.itertuples()]
    ev["notes"] = [old.get((r.ticker, r.source, r.event_month), ("", ""))[1] for r in ev.itertuples()]
    ev["date_lower"] = ev.date_lower.dt.date
    ev["date_upper"] = ev.date_upper.dt.date
    return ev.sort_values(["ticker", "date_upper"])


def firm_quarter(obs: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    firms = pd.read_csv(CONFIG / "firms.csv", dtype=str)
    start = pd.Period(f"{SETTINGS['wayback_start_year']}Q1", "Q")
    end = pd.Period(date.today(), "Q")
    quarters = pd.period_range(start, end, freq="Q")
    grid = pd.MultiIndex.from_product([firms.ticker, quarters], names=["ticker", "quarter"]).to_frame(index=False)
    grid["q_end"] = grid.quarter.dt.end_time.dt.normalize()
    if not obs.empty:
        o = obs[["ticker", "date", "source", "sha1", "image_path", *FEATS]].rename(columns={"date": "obs_date"})
        o = o.sort_values("obs_date")
        grid = grid.sort_values("q_end")
        grid = pd.merge_asof(grid, o, left_on="q_end", right_on="obs_date", by="ticker", direction="backward")
    if not ev.empty:
        e = ev.copy()
        e["quarter"] = pd.PeriodIndex(pd.to_datetime(e.date_upper), freq="Q")
        e["is_shape"] = (e.change_type == "shape").astype(int)
        e["is_color"] = (e.change_type == "color").astype(int)
        agg = e.groupby(["ticker", "quarter"]).agg(logo_change=("ticker", "size"),
                                                   shape_change=("is_shape", "sum"),
                                                   color_change=("is_color", "sum"),
                                                   change_phash_dist=("phash_dist", "first"),
                                                   change_color_dist=("color_dist", "first"),
                                                   change_green_shift=("green_shift", "first")).reset_index()
        grid = grid.merge(agg, on=["ticker", "quarter"], how="left")
        for c in ("logo_change", "shape_change", "color_change"):
            grid[c] = grid[c].fillna(0).astype(int)
    else:
        grid["logo_change"] = grid["shape_change"] = grid["color_change"] = 0
    marks_p = DATA / "uspto" / "firm_design_marks.csv"
    if marks_p.exists():
        m = pd.read_csv(marks_p, dtype=str)
        m["quarter"] = pd.PeriodIndex(pd.to_datetime(m.filing_dt, errors="coerce"), freq="Q")
        m["itu"] = (m.get("lb_itu_file_in", "0") == "1").astype(int)
        m["color_drawing"] = (m.get("draw_color_file_in", "0") == "1").astype(int)
        mq = m.groupby(["ticker", "quarter"]).agg(n_design_filings=("serial_no", "nunique"),
                                                  n_design_itu_filings=("itu", "sum"),
                                                  n_color_drawing_filings=("color_drawing", "sum")).reset_index()
        grid = grid.merge(mq, on=["ticker", "quarter"], how="left")
        for c in ("n_design_filings", "n_design_itu_filings", "n_color_drawing_filings"):
            grid[c] = grid[c].fillna(0).astype(int)
    grid = grid.sort_values(["ticker", "quarter"])
    grid["n_changes_to_date"] = grid.groupby("ticker").logo_change.cumsum()
    grid["quarter"] = grid.quarter.astype(str)
    return grid.drop(columns=["q_end"])


def gallery(obs: pd.DataFrame, ev: pd.DataFrame) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    changes = {(r.ticker, str(r.new_image)) for r in ev.itertuples()} if not ev.empty else set()
    parts = ["<!doctype html><meta charset='utf-8'><title>Logo timelines</title>",
             "<style>body{font:14px system-ui;margin:24px}h2{font-size:15px;margin:18px 0 6px}"
             ".row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}.c{text-align:center;font-size:11px}"
             ".c img{max-height:60px;max-width:160px;border:1px solid #ddd;padding:4px;background:#fff}"
             ".chg img{border:2px solid #d33}</style>",
             "<h1>Logo timelines</h1><p>Each unique logo per firm, first date seen. Red border = detected change.</p>"]
    if not obs.empty:
        for t, g in obs.groupby("ticker"):
            g = g.drop_duplicates("sha1", keep="first")
            parts.append(f"<h2>{html.escape(t)}</h2><div class='row'>")
            for r in g.itertuples():
                cls = "c chg" if (t, str(r.image_path)) in changes else "c"
                parts.append(f"<div class='{cls}'><img src='../{html.escape(str(r.image_path))}' loading='lazy'>"
                             f"<div>{r.date.date()} {html.escape(str(r.source))}</div></div>")
            parts.append("</div>")
    (REPORTS / "logo_timelines.html").write_text("\n".join(parts), encoding="utf-8")


def main() -> None:
    obs = observations()
    ev = change_events(obs)
    ev.to_csv(DATA / "logo_change_events.csv", index=False)
    panel = firm_quarter(obs, ev)
    panel.to_csv(DATA / "panel_firm_quarter.csv", index=False)
    gallery(obs, ev)
    print(f"observations: {len(obs)}  change events: {len(ev)}  panel rows: {len(panel)}")


if __name__ == "__main__":
    main()
