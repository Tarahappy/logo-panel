"""Offline tests (no network). Run: pytest -q"""
import io
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from common import normalize_name  # noqa: E402
from features import color_distance, compute_features, phash_distance, to_image  # noqa: E402
from grab import best_logo, svg_inherits_color  # noqa: E402
from logo_extract import find_logo_candidates  # noqa: E402
import wayback_logos as W  # noqa: E402


def png(draw_fn, size=(200, 80), bg=(0, 0, 0, 0)):
    im = Image.new("RGBA", size, bg)
    draw_fn(ImageDraw.Draw(im))
    b = io.BytesIO(); im.save(b, "PNG")
    return b.getvalue()


RED = png(lambda d: d.ellipse([20, 10, 180, 70], fill=(220, 20, 30, 255)))
BLUE_ON_WHITE = png(lambda d: d.rectangle([30, 20, 170, 60], fill=(20, 60, 200, 255)), bg=(255, 255, 255, 255))
GREEN = png(lambda d: d.polygon([(100, 5), (190, 75), (10, 75)], fill=(20, 160, 60, 255)))
BADGE = png(lambda d: d.text((60, 30), "ACME", fill=(255, 255, 255, 255)), bg=(200, 20, 30, 255))


def test_normalize_name():
    assert normalize_name("The Walt Disney Company") == "WALT DISNEY"
    assert normalize_name("Coca-Cola Company (The)") == "COCA COLA"
    assert normalize_name("Walmart Apollo, LLC") == "WALMART APOLLO"
    assert normalize_name("AT&T Inc.") == "AT AND T"


def test_header_logo_beats_partner_and_social():
    html = """<html><body><header class="site-header"><a href="/"><img src="/img/acme-logo.svg" alt="Acme Corp"></a>
    </header><main><img src="/img/partner-logo-x.png" alt="Partner X"><a href="https://twitter.com/acme">
    <img src="/img/twitter-logo.png"></a></main><footer><img src="/img/acme-logo-white.svg" alt="Acme"></footer></body></html>"""
    c = find_logo_candidates(html, "https://acme.com/", "Acme Corp")
    assert c[0]["url"] == "https://acme.com/img/acme-logo.svg"
    urls = [x["url"] for x in c]
    assert "https://acme.com/img/twitter-logo.png" not in urls
    assert "https://acme.com/img/partner-logo-x.png" not in urls


def test_jsonld_logo_found():
    html = """<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization",
    "name":"Acme","logo":{"@type":"ImageObject","url":"https://cdn.acme.com/brand/acme.png"}}</script>"""
    c = find_logo_candidates(html, "https://acme.com/", "Acme")
    assert c and c[0]["kind"] == "jsonld" and c[0]["url"].endswith("acme.png")


def test_old_table_site_fallback():
    html = """<table><tr><td><a href="/"><img src="/images/hdr_left.gif" alt="Acme Corporation"></a></td>
    <td><img src="/images/spacer.gif"></td></tr></table>"""
    c = find_logo_candidates(html, "http://www.acme.com/", "Acme Corporation")
    assert c and c[0]["url"] == "http://www.acme.com/images/hdr_left.gif"


def test_color_features():
    f = compute_features(to_image(RED))
    assert f["dominant_hue"] == "red" and f["red_share"] > 0.9
    f = compute_features(to_image(BLUE_ON_WHITE))
    assert f["dominant_hue"] == "blue" and f["blue_share"] > 0.9   # white background removed
    f = compute_features(to_image(BADGE))
    assert f["red_share"] > 0.6                                   # colored badge kept as logo color
    assert color_distance(compute_features(to_image(RED)), compute_features(to_image(GREEN))) > 0.9


def test_svg_color_inheritance():
    plain = b'<svg xmlns="http://www.w3.org/2000/svg" fill="none"><path d="M0 0h10v10H0z"/></svg>'
    colored = b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="#E41E2B" d="M0 0h10v10H0z"/></svg>'
    assert svg_inherits_color(plain, "image/svg+xml", "x.svg") is True
    assert svg_inherits_color(colored, "image/svg+xml", "x.svg") is False


def test_best_logo_downloads_and_scores():
    html = '<header><a href="/"><img class="logo" src="/logo.png" alt="Acme"></a></header>'
    info, raw, ext = best_logo(html, "https://acme.com/", "Acme", lambda u: (200, RED, "image/png"))
    assert raw == RED and ext == "png" and info["dominant_hue"] == "red" and info["color_reliable"] == 1


def test_change_detection_and_bisection(monkeypatch):
    """Logo switches from red to green in 2013-04; yearly pass sees 2012 red, 2013 green; bisection dates it."""
    caps = []
    for y in range(2010, 2016):
        for m in range(1, 13):
            caps.append({"ts": f"{y}{m:02d}15000000", "original": "http://acme.com/"})
    monkeypatch.setattr(W.wayback, "monthly_captures", lambda url, y0, y1=None: caps)
    red_f = {**compute_features(to_image(RED)), "sha1": "r", "strong": "1", "color_reliable": "1"}
    green_f = {**compute_features(to_image(GREEN)), "sha1": "g", "strong": "1", "color_reliable": "1"}
    calls = []

    def fake_grab(firm, cap, known, obs_type):
        calls.append(cap["ts"])
        f = green_f if cap["ts"] >= "20130415" else red_f
        return {**f, "ticker": firm["ticker"], "obs_type": obs_type, "capture_ts": cap["ts"], "image_path": f["sha1"]}

    monkeypatch.setattr(W, "grab_capture", fake_grab)
    monkeypatch.setattr(W, "homepage_for_years", lambda firm: [("http://acme.com/", 2010, 2015)])
    obs, changes = W.process_firm({"ticker": "ACME", "company": "Acme", "homepage": "http://acme.com/"}, {})
    shape = [c for c in changes if c["change_type"] == "shape"]
    assert len(shape) == 1
    ch = shape[0]
    assert ch["last_old_ts"].startswith("201303") and ch["first_new_ts"].startswith("201304")
    assert ch["old_dominant_hue"] == "red" and ch["new_dominant_hue"] == "green"
    assert len(calls) <= 6 + 5  # 6 yearly + a few bisection steps


def test_no_change_when_same_logo_different_file(monkeypatch):
    a = {**compute_features(to_image(RED)), "sha1": "a"}
    b = {**compute_features(to_image(RED)), "sha1": "b"}  # re-encoded, same look
    big, d, c = W.is_change(a, b)
    assert not big and d == 0


def test_wayback_url_helpers():
    import wayback
    u = "https://web.archive.org/web/20110211132627id_/https://www.ibm.com/us-en/"
    assert wayback.original_from_wayback_url(u) == "https://www.ibm.com/us-en/"
    assert wayback.ts_from_wayback_url(u) == "20110211132627"


def test_grab_capture_follows_archived_redirect(monkeypatch, tmp_path):
    """Root capture redirects to /us-en/; the relative logo path must resolve against /us-en/."""
    import wayback
    seen = []

    def fake_snapshot(ts, original, flag="id_", timeout=60):
        seen.append((flag, original))
        if flag == "id_":
            html = b'<header><a href="/"><img class="logo" src="img/logo.png" alt="IBM"></a></header>'
            return 200, "https://web.archive.org/web/20110301000000id_/https://www.ibm.com/us-en/", html, {}
        if original == "https://www.ibm.com/us-en/img/logo.png":
            return 200, "", RED, {"Content-Type": "image/png"}
        return 404, "", b"", {}

    monkeypatch.setattr(wayback, "get_snapshot", fake_snapshot)
    monkeypatch.setattr(W, "save_logo", lambda *a, **k: "data/logos/wayback/IBM/x.png")
    row = W.grab_capture({"ticker": "IBM", "company": "IBM"}, {"ts": "20110215000000", "original": "http://ibm.com/"},
                         {}, "yearly")
    assert row.get("dominant_hue") == "red" and row["image_path"].endswith("x.png")
    assert ("im_", "https://www.ibm.com/us-en/img/logo.png") in seen


def _mk(img_bytes, sha, ts, strong="1"):
    return {**compute_features(to_image(img_bytes)), "sha1": sha, "strong": strong, "color_reliable": "1",
            "capture_ts": ts, "image_path": sha}


def test_one_off_pick_is_not_a_change():
    """red, red, PROMO(green), red, red: the single green pick is ignored; no change."""
    seq = [_mk(RED, "r", "2001"), _mk(RED, "r", "2002"), _mk(GREEN, "g", "2003"), _mk(RED, "r", "2004"),
           _mk(RED, "r", "2005")]
    runs = W.stable_runs(seq)
    assert len(runs) == 1 and len(runs[0]) == 4


def test_lasting_shape_change_is_kept():
    seq = [_mk(RED, "r", "2001"), _mk(RED, "r", "2002"), _mk(GREEN, "g", "2003"), _mk(GREEN, "g", "2004")]
    runs = W.stable_runs(seq)
    assert len(runs) == 2 and runs[1][0]["sha1"] == "g"


def test_white_and_black_versions_hash_alike():
    from features import phash_distance
    black = png(lambda d: d.rectangle([30, 20, 170, 60], fill=(0, 0, 0, 255)))
    white = png(lambda d: d.rectangle([30, 20, 170, 60], fill=(255, 255, 255, 255)))
    fb, fw = compute_features(to_image(black)), compute_features(to_image(white))
    assert phash_distance(fb["phash"], fw["phash"]) <= 4


def test_promo_images_skipped_and_css_logo_found():
    html = """<style>#masthead .logo a { background: url(/i/v17/t/acme-mark.png) no-repeat; }</style>
    <a href="/promo/x"><img src="/img/0__deep_thunder__promo__318x98.jpg" alt="Acme promo"></a>"""
    c = find_logo_candidates(html, "https://acme.com/", "Acme")
    urls = [x["url"] for x in c]
    assert "https://acme.com/i/v17/t/acme-mark.png" in urls
    assert not any("promo" in u for u in urls)


def test_append_rows_handles_new_columns(tmp_path):
    from common import append_rows, read_csv
    p = tmp_path / "x.csv"
    append_rows(p, [{"a": 1, "b": 2}], ["a", "b"])
    append_rows(p, [{"a": 3, "c": 4, "b": 5}], ["a", "c", "b"])
    rows = read_csv(p)
    assert rows[0] == {"a": "1", "c": "", "b": "2"} and rows[1] == {"a": "3", "c": "4", "b": "5"}


def test_logo_in_black_box_matches_plain_logo():
    from features import phash_distance
    plain = png(lambda d: d.rectangle([60, 25, 140, 55], fill=(0, 0, 0, 255)))
    boxed = png(lambda d: d.rectangle([60, 25, 140, 55], fill=(255, 255, 255, 255)), bg=(0, 0, 0, 255))
    assert phash_distance(compute_features(to_image(plain))["phash"], compute_features(to_image(boxed))["phash"]) <= 4
