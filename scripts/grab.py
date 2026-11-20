"""From a page's HTML, download the best logo candidate and compute its features.

`fetch(url) -> (status, content_bytes, content_type)` is passed in, so the same code serves
live pages (plain HTTP) and archived pages (Wayback im_/id_ URLs).
"""
from __future__ import annotations

import re

from common import LOGOS, ROOT, SETTINGS
from features import (FEATURE_FIELDS, compute_features, decode_data_uri, ext_for, looks_like_svg, sha1,
                      to_image, usable)
from logo_extract import STRONG_KINDS, css_logo_candidates, find_logo_candidates, stylesheet_links

LOGO_FIELDS = ["logo_kind", "logo_url", "logo_score", "logo_reason", "n_candidates", "sha1", "image_path",
               "strong", "color_reliable", *FEATURE_FIELDS]
STRONG_SCORE = 30


def is_strong(kind: str, score, reason: str, photo_like=0) -> bool:
    """A confident logo pick: a logo-marked image, schema.org logo, CSS logo background, or an inline
    SVG that is itself labeled as the logo. Weak fallbacks
    (old-site guesses) and app icons are kept but not used to detect changes."""
    reason = reason or ""
    try:
        if kind not in STRONG_KINDS or float(score) < STRONG_SCORE or "weak" in reason:
            return False
    except (TypeError, ValueError):
        return False
    if kind == "svg-inline" and "logo-attr" not in reason and "name-in-alt" not in reason:
        return False  # e.g. an arrow or menu icon that merely sits inside the logo link
    return True


def best_logo(html: str, base_url: str, company: str, fetch) -> tuple[dict, bytes | None, str]:
    cands = find_logo_candidates(html, base_url, company, max_n=SETTINGS.get("max_logo_candidates", 4))
    if not any(c["kind"] in STRONG_KINDS and c["score"] >= 30 for c in cands):
        # the logo may be a background image defined in the site's stylesheet (common 2005-2020)
        css_cands = []
        for href in stylesheet_links(html, base_url):
            st, content, _ = fetch(href)
            if st == 200 and content:
                css_cands.extend(css_logo_candidates(content.decode("utf-8", "replace"), href))
            if css_cands:
                break
        cands = sorted(css_cands[:3], key=lambda c: -c["score"]) + cands
    info: dict = {"n_candidates": len(cands)}
    fallback = None  # a white-on-transparent variant (made for dark headers), used only if nothing better
    for c in cands:
        raw, mime, url = None, "", c.get("url", "")
        if c["kind"] == "svg-inline":
            raw, mime = c["svg"].encode("utf-8"), "image/svg+xml"
        elif url.startswith("data:"):
            try:
                raw, mime = decode_data_uri(url)
            except Exception:
                continue
        else:
            status, content, ctype = fetch(url)
            if status != 200 or not content:
                continue
            raw, mime = content, (ctype or "").split(";")[0]
            if "text/html" in mime and not content.lstrip().startswith(b"<svg"):
                continue
        img = to_image(raw, mime, url)
        if not usable(img, SETTINGS.get("min_logo_pixels", 16)):
            continue
        feats = compute_features(img)
        rec = dict(info)
        rec.update(feats)
        rec.update(logo_kind=c["kind"], logo_score=c["score"], logo_reason=c["reason"],
                   logo_url=("inline-svg" if c["kind"] == "svg-inline" else (url[:60] + "...") if url.startswith("data:") else url),
                   sha1=sha1(raw))
        rec["color_reliable"] = 0 if svg_inherits_color(raw, mime, url) else 1
        rec["strong"] = int(is_strong(c["kind"], c["score"], c["reason"], feats.get("photo_like", 0)))
        if is_white_variant(feats):
            rec["logo_reason"] += ",white-variant"
            rec["color_reliable"] = 0
            fallback = fallback or (rec, raw, ext_for(raw, mime, url))
            continue
        return rec, raw, ext_for(raw, mime, url)
    if fallback:
        return fallback
    return info, None, ""


_COLOR_RE = re.compile(rb"(fill|stroke|stop-color|color)\s*[:=]\s*[\"']?\s*(#[0-9a-f]{3,8}|rgba?\(|hsla?\(|[a-z]+)", re.I)
_NEUTRAL = {b"none", b"black", b"#000", b"#000000", b"currentcolor", b"inherit", b"transparent", b"white",
            b"#fff", b"#ffffff", b"evenodd", b"nonzero"}


def svg_inherits_color(raw: bytes, mime: str, url: str) -> bool:
    """True for an SVG with no explicit non-neutral color: its color comes from the page's CSS."""
    if not looks_like_svg(raw, mime, url):
        return False
    if b"<image" in raw and b"data:image" in raw:  # wraps an embedded picture, colors are inside it
        return False
    for m in _COLOR_RE.finditer(raw[:200000]):
        v = m.group(2).lower().rstrip(b"\"'; ")
        if v in _NEUTRAL or m.group(1).lower() == b"color" and v in _NEUTRAL:
            continue
        if v.startswith(b"#") or v.startswith(b"rgb") or v.startswith(b"hsl") or v.isalpha():
            if v.startswith(b"url"):
                continue
            return False
    return True


def is_white_variant(f: dict) -> bool:
    try:
        return float(f["neutral_share"]) > 0.95 and float(f["mean_lightness"]) > 0.9
    except (KeyError, ValueError, TypeError):
        return False


def save_logo(folder: str, ticker: str, stamp: str, raw: bytes, ext: str, known: dict[str, str]) -> str:
    """Save once per unique image (by sha1) per firm; return the repo-relative path."""
    digest = sha1(raw)
    key = f"{ticker}:{digest}"
    if key in known:
        return known[key]
    d = LOGOS / folder / ticker
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{stamp}_{digest[:8]}.{ext}"
    p.write_bytes(raw)
    rel = str(p.relative_to(ROOT))
    known[key] = rel
    return rel


def known_images(folder: str) -> dict[str, str]:
    """Map 'TICKER:sha1prefix' style keys for images already on disk (sha1 recomputed)."""
    from features import sha1 as _sha1
    out = {}
    base = LOGOS / folder
    if not base.exists():
        return out
    for p in base.glob("*/*"):
        if p.is_file():
            out[f"{p.parent.name}:{_sha1(p.read_bytes())}"] = str(p.relative_to(ROOT))
    return out
