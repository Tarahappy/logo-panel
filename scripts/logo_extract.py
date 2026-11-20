"""Find the most likely corporate logo on a homepage.

Works on live HTML and on archived (Wayback) HTML. Returns ranked candidates; the caller
downloads them in order and keeps the first that decodes into a usable image.

Signals, strongest first:
  1. schema.org Organization "logo" in JSON-LD
  2. <img>/<svg> whose src/alt/class/id (or an ancestor's class/id) says "logo", inside the
     header/nav or a link to the homepage, with the company name in alt/title as a bonus
  3. apple-touch-icon / mask-icon / large icons (fallback, flagged kind="icon")
Images that look like partner, social, award or client logos are penalized.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

BAD_WORDS = re.compile(
    r"facebook|twitter|linkedin|youtube|instagram|tiktok|pinterest|social|share|award|partner|"
    r"client|customer|sponsor|badge|certif|app-?store|google-?play|cookie|onetrust|trustarc|"
    r"truste|bbb|payment|visa-|mastercard|paypal|flag|country|language|avatar|icon-search|"
    r"hamburger|menu-icon|close|arrow|chevron|play-button|spinner|loader|pixel|1x1|spacer|blank",
    re.I,
)
LOGO_WORD = re.compile(r"logo|brand|wordmark|logotype|site-?mark|masthead", re.I)
HEADER_WORD = re.compile(r"header|masthead|navbar|nav-?bar|site-?nav|top-?bar|global-?nav", re.I)
FOOTER_WORD = re.compile(r"footer", re.I)


def _attrs_text(el: Tag) -> str:
    parts = []
    for k in ("src", "data-src", "srcset", "alt", "title", "class", "id", "aria-label", "role", "href"):
        v = el.get(k)
        if v:
            parts.append(" ".join(v) if isinstance(v, list) else str(v))
    return " ".join(parts)


def _ancestor_text(el: Tag, depth: int = 5) -> tuple[str, bool, bool, bool]:
    """Return (concatenated class/id text of ancestors, in_header, in_footer, links_home)."""
    texts, in_header, in_footer, links_home = [], False, False, False
    node = el.parent
    for _ in range(depth):
        if node is None or not isinstance(node, Tag):
            break
        t = " ".join(filter(None, [" ".join(node.get("class") or []), node.get("id") or "",
                                   node.get("aria-label") or ""]))
        texts.append(t)
        if node.name in ("header", "nav") or HEADER_WORD.search(t):
            in_header = True
        if node.name == "footer" or FOOTER_WORD.search(t):
            in_footer = True
        if node.name == "a":
            href = (node.get("href") or "").strip()
            if href in ("/", "./", "#", "") or re.fullmatch(r"https?://[^/]+/?(\?.*)?", href or "") \
                    or re.fullmatch(r"/[a-z]{2}(-[a-z]{2})?/?", href or "", re.I):
                links_home = True
        node = node.parent
    return " ".join(texts), in_header, in_footer, links_home


def _best_src(img: Tag) -> str | None:
    for k in ("src", "data-src", "data-lazy-src", "data-original"):
        v = (img.get(k) or "").strip()
        if v and not v.startswith("data:image/gif") and not v.startswith("about:"):
            return v
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        # take the largest candidate
        items = [s.strip().split(" ") for s in srcset.split(",") if s.strip()]
        if items:
            return items[-1][0]
    return None


def _svg_markup(svg: Tag) -> str:
    s = str(svg)
    if "xmlns=" not in s[:200]:
        s = s.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    return s


def _name_tokens(company: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", (company or "").lower())
    stop = {"inc", "corp", "corporation", "company", "co", "the", "group", "holdings", "plc", "ltd", "class",
            "and", "of", "a", "b", "c"}
    return [t for t in toks if t not in stop and len(t) > 2]


def find_logo_candidates(html: str, base_url: str, company: str = "", max_n: int = 4) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    base_tag = soup.find("base", href=True)
    base = urljoin(base_url, base_tag["href"]) if base_tag else base_url
    name_toks = _name_tokens(company)
    cands: list[dict] = []

    # 1. JSON-LD Organization logo
    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            blob = json.loads(s.string or s.get_text() or "")
        except Exception:
            continue
        stack = blob if isinstance(blob, list) else [blob]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                if "@graph" in obj and isinstance(obj["@graph"], list):
                    stack.extend(obj["@graph"])
                typ = obj.get("@type")
                typ = " ".join(typ) if isinstance(typ, list) else str(typ or "")
                logo = obj.get("logo")
                if logo and re.search(r"Organization|Corporation|Brand|WebSite|LocalBusiness", typ):
                    url = logo.get("url") or logo.get("contentUrl") if isinstance(logo, dict) else logo
                    if isinstance(url, str) and url.strip():
                        cands.append({"kind": "jsonld", "url": urljoin(base, url.strip()), "score": 60,
                                      "reason": "schema.org logo"})
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(obj, list):
                stack.extend(obj)

    # 2. <img> and inline <svg>
    weak: list[dict] = []
    for el in soup.find_all(["img", "svg", "image"]):
        own = _attrs_text(el)
        anc, in_header, in_footer, links_home = _ancestor_text(el)
        score, reasons = 0, []
        if LOGO_WORD.search(own):
            score += 30; reasons.append("logo-attr")
        if LOGO_WORD.search(anc):
            score += 20; reasons.append("logo-ancestor")
        if in_header:
            score += 15; reasons.append("header")
        if links_home:
            score += 15; reasons.append("links-home")
        if in_footer:
            score -= 15; reasons.append("footer")
        alt = " ".join(filter(None, [el.get("alt") or "", el.get("title") or "", el.get("aria-label") or ""])).lower()
        if name_toks and any(t in alt for t in name_toks):
            score += 10; reasons.append("name-in-alt")
        if BAD_WORDS.search(own) or BAD_WORDS.search(anc[:300]):
            score -= 40; reasons.append("bad-word")
        if score < 25:
            # older table-based sites: a linked image whose alt names the firm, early in the page
            if el.name == "img" and score >= 10 and ("name-in-alt" in reasons or "links-home" in reasons):
                src = _best_src(el)
                if src and not src.startswith("data:"):
                    weak.append({"kind": "img", "url": urljoin(base, src), "score": score,
                                 "reason": ",".join(reasons + ["weak"])})
            continue
        if el.name == "svg":
            # inline svg; skip tiny icon sprites (<use> only)
            if el.find("use") and not el.find(["path", "polygon", "rect", "circle", "g"]):
                continue
            cands.append({"kind": "svg-inline", "svg": _svg_markup(el), "score": score - 2,
                          "reason": ",".join(reasons)})
        elif el.name == "image":
            href = el.get("href") or el.get("xlink:href")
            if href:
                cands.append({"kind": "img", "url": urljoin(base, href), "score": score,
                              "reason": ",".join(reasons)})
        else:
            src = _best_src(el)
            if not src:
                continue
            if src.startswith("data:image/svg+xml") or src.startswith("data:image/png") or src.startswith("data:image/jpeg"):
                cands.append({"kind": "img-data", "url": src, "score": score, "reason": ",".join(reasons)})
            else:
                cands.append({"kind": "img", "url": urljoin(base, src), "score": score, "reason": ",".join(reasons)})

    if not any(c["kind"] in ("img", "svg-inline", "img-data") for c in cands):
        cands.extend(weak[:2])

    # 3. icons (fallback only)
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        if "apple-touch-icon" in rel or "mask-icon" in rel:
            cands.append({"kind": "icon", "url": urljoin(base, link["href"]), "score": 5, "reason": rel})
        elif "icon" in rel:
            sizes = link.get("sizes") or ""
            big = re.search(r"(\d+)x\1", sizes)
            if big and int(big.group(1)) >= 96:
                cands.append({"kind": "icon", "url": urljoin(base, link["href"]), "score": 4, "reason": f"icon {sizes}"})

    # dedupe by url/svg, keep highest score, first-seen order as tiebreak
    seen, out = {}, []
    for i, c in enumerate(cands):
        c["order"] = i
        key = c.get("url") or c.get("svg", "")[:500]
        if key in seen:
            if c["score"] > out[seen[key]]["score"]:
                out[seen[key]] = c
            continue
        seen[key] = len(out)
        out.append(c)
    # small boost for the first strong candidate in document order (the header logo usually comes first)
    strong = [c for c in out if c["kind"] in ("img", "svg-inline", "img-data")]
    if strong:
        first = min(strong, key=lambda c: c["order"])
        first["score"] += 5
    out.sort(key=lambda c: (-c["score"], c["order"]))
    return out[:max_n]


def same_site(url: str, base: str) -> bool:
    a, b = urlparse(url).netloc.lower(), urlparse(base).netloc.lower()
    a, b = a.removeprefix("www."), b.removeprefix("www.")
    return a == b or a.endswith("." + b) or b.endswith("." + a)
