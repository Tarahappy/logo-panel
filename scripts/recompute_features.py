"""Recompute image features for every stored logo, after a change to features.py.

Rewrites the feature columns (and color_reliable) in data/live_observations.csv and
data/wayback_observations.csv from the saved images; nothing is downloaded.

Usage: python scripts/recompute_features.py && python scripts/build_panel.py
"""
from __future__ import annotations

import pandas as pd

from common import DATA, ROOT
from features import FEATURE_FIELDS, compute_features, to_image
from grab import is_strong, is_white_variant, svg_inherits_color


def main() -> None:
    cache: dict[str, dict] = {}
    for name in ("live_observations.csv", "wayback_observations.csv"):
        p = DATA / name
        if not p.exists():
            continue
        df = pd.read_csv(p, dtype=str)
        for c in FEATURE_FIELDS + ["color_reliable", "strong"]:
            if c not in df:
                df[c] = None
        for i, path in df["image_path"].items():
            if not isinstance(path, str) or not path:
                continue
            if path not in cache:
                f = ROOT / path
                if not f.exists():
                    continue
                raw = f.read_bytes()
                img = to_image(raw, "", path)
                if img is None:
                    continue
                feats = compute_features(img)
                feats["color_reliable"] = 0 if (svg_inherits_color(raw, "", path) or is_white_variant(feats)) else 1
                cache[path] = feats
            for k, v in cache[path].items():
                df.at[i, k] = "" if v is None else str(v)
            df.at[i, "strong"] = str(int(is_strong(df.at[i, "logo_kind"], df.at[i, "logo_score"],
                                                   df.at[i, "logo_reason"], cache[path].get("photo_like", 0))))
        df.to_csv(p, index=False)
        print(f"{name}: recomputed {df.image_path.notna().sum()} rows")


if __name__ == "__main__":
    main()
