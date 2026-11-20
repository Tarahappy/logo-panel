"""Decode a logo (raster, SVG, data URI) and compute color, complexity and hash features.

Foreground = non-transparent pixels, minus a near-white background (detected on the border).
A colored border is kept as part of the logo (badge-style logos).
Hue families (HSV hue, degrees) for pixels with saturation >= 0.20 and value >= 0.20:
  red 345-15, orange 15-40, yellow 40-70, green 70-165, teal 165-200, blue 200-255, purple 255-345.
Everything else in the foreground counts as neutral (black, gray, white).
dominant_hue = the largest color family when color covers at least 10% of the logo, else "neutral".
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
import zlib
from urllib.parse import unquote

import numpy as np
from PIL import Image, ImageFilter, ImageOps

try:
    import imagehash
except ImportError:  # pragma: no cover
    imagehash = None
try:
    import cairosvg
except Exception:  # pragma: no cover
    cairosvg = None

HUES = [("red", 345, 15), ("orange", 15, 40), ("yellow", 40, 70), ("green", 70, 165),
        ("teal", 165, 200), ("blue", 200, 255), ("purple", 255, 345)]
FEATURE_FIELDS = [
    "width", "height", "aspect", "fg_share", "neutral_share",
    *[f"{h}_share" for h, _, _ in HUES],
    "dominant_hue", "mean_saturation", "mean_lightness", "n_colors",
    "edge_density", "compressed_kb", "phash", "dhash", "colorhash",
]


def decode_data_uri(uri: str) -> tuple[bytes, str]:
    head, _, payload = uri.partition(",")
    mime = head[5:].split(";")[0] if head.startswith("data:") else ""
    raw = base64.b64decode(payload) if ";base64" in head else unquote(payload).encode("utf-8")
    return raw, mime


def looks_like_svg(raw: bytes, mime: str = "", url: str = "") -> bool:
    if "svg" in (mime or "") or re.search(r"\.svgz?(\?|#|$)", url or "", re.I):
        return True
    head = raw[:600].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in raw[:2000].lower())


def to_image(raw: bytes, mime: str = "", url: str = "", size: int = 512) -> Image.Image | None:
    """Return an RGBA image, or None if it cannot be decoded."""
    try:
        if looks_like_svg(raw, mime, url):
            if cairosvg is None:
                return None
            png = cairosvg.svg2png(bytestring=raw, output_width=size, unsafe=False)
            img = Image.open(io.BytesIO(png))
        else:
            img = Image.open(io.BytesIO(raw))
            if getattr(img, "n_frames", 1) > 1:
                img.seek(0)
            if img.format == "ICO":
                sizes = sorted(img.info.get("sizes", []) or [], key=lambda s: s[0] * s[1])
                if sizes:
                    img.size = sizes[-1]
        img = img.convert("RGBA")
        img.load()
        return img
    except Exception:
        return None


def _border_color(arr: np.ndarray) -> np.ndarray | None:
    """Most common opaque color on the image border, or None if the border is transparent."""
    border = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]])
    opaque = border[border[:, 3] > 200][:, :3]
    if len(opaque) < len(border) * 0.5:
        return None
    q = (opaque // 16).astype(int)
    keys, counts = np.unique(q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2], return_counts=True)
    k = keys[counts.argmax()]
    mask = (q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]) == k
    return opaque[mask].mean(axis=0)


def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    rgb = rgb.astype(float) / 255.0
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    mx, mn = rgb.max(axis=1), rgb.min(axis=1)
    d = mx - mn
    h = np.zeros_like(mx)
    nz = d > 1e-9
    rm = nz & (mx == r)
    gm = nz & (mx == g) & ~rm
    bm = nz & ~rm & ~gm
    h[rm] = ((g[rm] - b[rm]) / d[rm]) % 6
    h[gm] = (b[gm] - r[gm]) / d[gm] + 2
    h[bm] = (r[bm] - g[bm]) / d[bm] + 4
    h = h * 60.0
    s = np.where(mx > 1e-9, d / np.maximum(mx, 1e-9), 0)
    return np.stack([h, s, mx], axis=1)


def _n_colors(rgb: np.ndarray, min_share: float = 0.03) -> int:
    """Distinct colors covering at least min_share of foreground pixels (coarse 4-bit quantization,
    merging near neighbours)."""
    if len(rgb) == 0:
        return 0
    q = (rgb // 32).astype(int)
    keys, counts = np.unique(q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2], return_counts=True)
    keep = keys[counts >= min_share * len(rgb)]
    cents = np.stack([keep // 64, (keep // 8) % 8, keep % 8], axis=1)
    merged: list[np.ndarray] = []
    for c in cents:
        if not any(np.abs(c - m).max() <= 1 for m in merged):
            merged.append(c)
    return len(merged)


def compute_features(img: Image.Image) -> dict:
    img = img.copy()
    img.thumbnail((256, 256))
    w, h = img.size
    arr = np.asarray(img)
    flat = arr.reshape(-1, 4)
    alpha = flat[:, 3] > 128
    bg = _border_color(arr)
    # only a near-white border is treated as background; a colored border is usually part of a badge logo
    if bg is not None and bg.min() < 215:
        bg = None
        alpha = np.ones(len(flat), dtype=bool) if (flat[:, 3] > 128).mean() > 0.95 else alpha
    if bg is not None:
        dist = np.abs(flat[:, :3].astype(float) - bg).max(axis=1)
        fg_mask = alpha & (dist > 40)
    else:
        fg_mask = alpha
    fg = flat[fg_mask][:, :3]
    out: dict = {"width": w, "height": h, "aspect": round(w / h, 3) if h else ""}
    out["fg_share"] = round(fg_mask.mean(), 4) if len(flat) else 0
    if len(fg) == 0:
        fg = flat[alpha][:, :3] if alpha.any() else flat[:, :3]
    hsv = _rgb_to_hsv(fg)
    chroma = (hsv[:, 1] >= 0.20) & (hsv[:, 2] >= 0.20)
    n = max(len(fg), 1)
    out["neutral_share"] = round(1 - chroma.sum() / n, 4)
    best, best_share = "neutral", 0.0
    for name, lo, hi in HUES:
        hh = hsv[:, 0]
        m = chroma & ((hh >= lo) | (hh < hi)) if lo > hi else chroma & (hh >= lo) & (hh < hi)
        share = round(m.sum() / n, 4)
        out[f"{name}_share"] = share
        if share > best_share:
            best, best_share = name, share
    # brand color = largest chromatic family, if color covers at least 10% of the logo (black text ignored)
    out["dominant_hue"] = best if (1 - out["neutral_share"]) >= 0.10 else "neutral"
    out["mean_saturation"] = round(float(hsv[:, 1].mean()), 4)
    out["mean_lightness"] = round(float(hsv[:, 2].mean()), 4)
    out["n_colors"] = _n_colors(fg)

    # complexity on a white-composited grayscale version
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    gray = Image.alpha_composite(white, img).convert("L")
    gray = ImageOps.pad(gray, (256, 256), color=255)
    edges = np.asarray(gray.filter(ImageFilter.FIND_EDGES))
    out["edge_density"] = round(float((edges > 40).mean()), 4)
    out["compressed_kb"] = round(len(zlib.compress(gray.tobytes(), 9)) / 1024, 2)
    if imagehash is not None:
        rgb_white = Image.alpha_composite(white, img).convert("RGB")
        out["phash"] = str(imagehash.phash(rgb_white))
        out["dhash"] = str(imagehash.dhash(rgb_white))
        out["colorhash"] = str(imagehash.colorhash(rgb_white, binbits=3))
    else:
        out["phash"] = out["dhash"] = out["colorhash"] = ""
    return out


def phash_distance(a: str, b: str) -> int | None:
    if not a or not b or imagehash is None:
        return None
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def color_distance(f1: dict, f2: dict) -> float | None:
    """Total variation distance between two hue-share profiles (0 = identical, 1 = disjoint)."""
    keys = ["neutral_share"] + [f"{h}_share" for h, _, _ in HUES]
    try:
        a = np.array([float(f1[k]) for k in keys]); b = np.array([float(f2[k]) for k in keys])
    except (KeyError, ValueError, TypeError):
        return None
    if a.sum() == 0 or b.sum() == 0:
        return None
    return round(0.5 * float(np.abs(a / a.sum() - b / b.sum()).sum()), 4)


def sha1(raw: bytes) -> str:
    return hashlib.sha1(raw).hexdigest()


def ext_for(raw: bytes, mime: str = "", url: str = "") -> str:
    if looks_like_svg(raw, mime, url):
        return "svg"
    try:
        fmt = (Image.open(io.BytesIO(raw)).format or "png").lower()
    except Exception:
        return "bin"
    return {"jpeg": "jpg", "ico": "ico", "webp": "webp", "gif": "gif", "png": "png"}.get(fmt, fmt)


def usable(img: Image.Image | None, min_px: int = 16) -> bool:
    if img is None:
        return False
    w, h = img.size
    if w < min_px or h < min_px:
        return False
    a = np.asarray(img)[:, :, 3]
    return (a > 128).mean() > 0.01
