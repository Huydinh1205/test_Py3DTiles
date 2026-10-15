#!/usr/bin/env python3
"""
Helper - Generate synthetic LAS files so the harness can be smoke tested
before the real DJI Terra datasets are available.

Produces two files that differ ONLY in LAS version / point format, which is
exactly the variable that decides whether preprocessing is needed:

  sample_las12_pf3.las   LAS 1.2 point format 3  -> classification CANNOT be
                         exported by py3dtiles, preprocessing required
  sample_las14_pf7.las   LAS 1.4 point format 7  -> classification exports fine

Both carry RGB, intensity and a realistic ASPRS class mix (ground plus three
vegetation strata), in GDA2020 MGA zone 56 (EPSG:7856), roughly Sydney.

Usage
-----
    python scripts/make_sample_data.py --points 500000 --out-dir data
"""

import argparse
from pathlib import Path

import numpy as np
import laspy

# Class code -> share of points. Loosely models a vegetated survey area.
CLASS_MIX = {
    2: 0.35,   # Ground
    3: 0.15,   # Low vegetation
    4: 0.20,   # Medium vegetation
    5: 0.28,   # High vegetation
    6: 0.02,   # Building
}

# Height band (metres above ground) per class, so the cloud looks plausible.
CLASS_HEIGHTS = {
    2: (0.0, 0.3),
    3: (0.3, 2.0),
    4: (2.0, 8.0),
    5: (8.0, 35.0),
    6: (2.0, 9.0),
}

EPSG = 7856  # GDA2020 / MGA zone 56


def build_points(n, rng):
    codes = np.array(list(CLASS_MIX.keys()))
    probs = np.array(list(CLASS_MIX.values()))
    probs = probs / probs.sum()
    classification = rng.choice(codes, size=n, p=probs).astype(np.uint8)

    # 400 m x 400 m patch with gently undulating terrain.
    x = 330000.0 + rng.random(n) * 400.0
    y = 6250000.0 + rng.random(n) * 400.0
    ground = 25.0 + 3.0 * np.sin(x / 70.0) + 2.0 * np.cos(y / 90.0)

    z = np.empty(n)
    for code, (lo, hi) in CLASS_HEIGHTS.items():
        mask = classification == code
        z[mask] = ground[mask] + lo + rng.random(mask.sum()) * (hi - lo)

    # Intensity correlates loosely with class, as it would in real data.
    intensity = np.where(
        classification == 2, rng.integers(200, 600, n),
        np.where(classification == 6, rng.integers(800, 1400, n),
                 rng.integers(1200, 2600, n)),
    ).astype(np.uint16)

    # 16-bit RGB: brown-ish ground, green vegetation, grey buildings.
    red = np.empty(n, dtype=np.uint16)
    green = np.empty(n, dtype=np.uint16)
    blue = np.empty(n, dtype=np.uint16)
    veg = np.isin(classification, [3, 4, 5])
    gnd = classification == 2
    bld = classification == 6
    red[veg] = rng.integers(8000, 20000, veg.sum())
    green[veg] = rng.integers(28000, 52000, veg.sum())
    blue[veg] = rng.integers(6000, 16000, veg.sum())
    red[gnd] = rng.integers(26000, 40000, gnd.sum())
    green[gnd] = rng.integers(20000, 32000, gnd.sum())
    blue[gnd] = rng.integers(12000, 22000, gnd.sum())
    red[bld] = rng.integers(34000, 46000, bld.sum())
    green[bld] = rng.integers(34000, 46000, bld.sum())
    blue[bld] = rng.integers(34000, 46000, bld.sum())

    return x, y, z, classification, intensity, red, green, blue


def write(path, version, point_format, data):
    x, y, z, classification, intensity, red, green, blue = data
    header = laspy.LasHeader(version=version, point_format=point_format)
    header.offsets = [np.floor(x.min()), np.floor(y.min()), np.floor(z.min())]
    header.scales = [0.001, 0.001, 0.001]
    try:
        from pyproj import CRS
        header.add_crs(CRS.from_epsg(EPSG))
    except Exception:
        pass  # file still usable, py3dtiles just needs --srs_in

    las = laspy.LasData(header)
    las.x, las.y, las.z = x, y, z
    las.classification = classification
    las.intensity = intensity
    las.red, las.green, las.blue = red, green, blue
    las.write(str(path))
    size_mb = Path(path).stat().st_size / 1024 / 1024
    print(f"  wrote {path}  (LAS {version} pf{point_format}, {size_mb:.1f} MB)")


def main():
    ap = argparse.ArgumentParser(description="Generate synthetic LAS test data.")
    ap.add_argument("--points", type=int, default=500_000, help="Points per file")
    ap.add_argument("--out-dir", default="data", help="Output directory")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    data = build_points(args.points, rng)

    print(f"Generating {args.points:,} points per file in {out}/")
    write(out / "sample_las12_pf3.las", "1.2", 3, data)
    write(out / "sample_las14_pf7.las", "1.4", 7, data)

    print("\nExpected behaviour when you run the harness on these:")
    print("  sample_las12_pf3.las -> step 1 says preprocessing REQUIRED,")
    print("                          validation FAILS without step 2")
    print("  sample_las14_pf7.las -> step 1 says no preprocessing needed,")
    print("                          validation PASSES")
    print(f"\n  EPSG for both files: {EPSG}")


if __name__ == "__main__":
    main()
