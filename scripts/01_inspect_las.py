#!/usr/bin/env python3
"""
Step 1 - Inspect a LAS/LAZ file before conversion.

Purpose
-------
Answer Phil's question "whether any additional preprocessing is actually
needed rather than adding it by default".

The only thing that decides this is the LAS point format:

  LAS 1.4, point format 6/7/8
      'classification' is a standalone byte in the point record.
      py3dtiles exports it directly with --extra-fields classification.
      NO preprocessing needed.

  LAS 1.0-1.3, point format 0-5
      'classification' is bit-packed into a byte that laspy exposes under
      the name 'raw_classification'. py3dtiles discovers available fields
      from point_format.dtype() (which lists 'raw_classification') but
      reads them by checking point_format.dimension_names (which lists
      'classification'). The two names never agree, so:
          --extra-fields classification      -> warning, field dropped
          --extra-fields raw_classification  -> NO warning, writes all zeros
      The second case is silent data loss. Preprocessing IS needed:
      run 02_prepare_las.py to rewrite the file as LAS 1.4 pf 6/7.

Usage
-----
    python scripts/01_inspect_las.py data/mydataset.las
    python scripts/01_inspect_las.py data/mydataset.las --json results/inspect.json
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

try:
    import laspy
except ImportError:
    sys.exit("laspy is not installed. Run: pip install -r requirements.txt")


# ASPRS standard classification codes (LAS 1.4 R15, table 17).
ASPRS_CLASSES = {
    0: "Created, never classified",
    1: "Unclassified",
    2: "Ground",
    3: "Low vegetation",
    4: "Medium vegetation",
    5: "High vegetation",
    6: "Building",
    7: "Low point (noise)",
    8: "Model key-point (legacy)",
    9: "Water",
    10: "Rail",
    11: "Road surface",
    12: "Overlap (legacy)",
    13: "Wire - guard",
    14: "Wire - conductor",
    15: "Transmission tower",
    16: "Wire-structure connector",
    17: "Bridge deck",
    18: "High noise",
}

# Point formats where 'classification' is its own byte and py3dtiles can read it.
MODERN_POINT_FORMATS = (6, 7, 8, 9, 10)


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def inspect(path, sample_limit=5_000_000):
    """Read the header plus a bounded sample of points. Never loads a huge file whole."""
    path = Path(path)
    report = {"file": str(path), "file_size_bytes": path.stat().st_size}

    with laspy.open(str(path)) as reader:
        header = reader.header
        pf = header.point_format

        report["las_version"] = f"{header.version.major}.{header.version.minor}"
        report["point_format"] = int(pf.id)
        report["point_count"] = int(header.point_count)
        report["scales"] = [float(v) for v in header.scales]
        report["offsets"] = [float(v) for v in header.offsets]
        report["mins"] = [float(v) for v in header.mins]
        report["maxs"] = [float(v) for v in header.maxs]

        # Coordinate reference system declared inside the file, if any.
        try:
            crs = header.parse_crs()
        except Exception:
            crs = None
        if crs is not None:
            epsg = crs.to_epsg()
            report["crs"] = {
                "name": crs.name,
                "epsg": int(epsg) if epsg else None,
                "is_projected": bool(crs.is_projected),
                "is_vertical_compound": len(crs.sub_crs_list) > 1
                if hasattr(crs, "sub_crs_list") and crs.sub_crs_list
                else False,
            }
        else:
            report["crs"] = None

        # These two lists are what drive py3dtiles' --extra-fields behaviour.
        report["dtype_field_names"] = list(pf.dtype().fields.keys())
        report["dimension_names"] = list(pf.dimension_names)
        report["extra_dimension_names"] = list(pf.extra_dimension_names)
        report["has_rgb"] = "red" in pf.dimension_names

        # A field is usable with --extra-fields only if its name appears in BOTH lists.
        usable = sorted(
            set(report["dtype_field_names"]).intersection(report["dimension_names"])
            - {"X", "Y", "Z", "red", "green", "blue"}
        )
        report["usable_extra_fields"] = usable

        # Sample points for the attribute statistics.
        n_sample = min(int(header.point_count), sample_limit)
        pts = reader.read_points(n_sample)
        report["sampled_points"] = int(n_sample)

    # Classification histogram.
    if "classification" in report["dimension_names"]:
        values, counts = np.unique(np.asarray(pts.classification), return_counts=True)
        report["classification_histogram"] = {
            int(v): {
                "count": int(c),
                "percent": round(100.0 * c / n_sample, 2),
                "label": ASPRS_CLASSES.get(int(v), "Reserved / user defined"),
            }
            for v, c in zip(values, counts)
        }
        report["is_classified"] = not set(int(v) for v in values).issubset({0, 1})
    else:
        report["classification_histogram"] = None
        report["is_classified"] = False

    # Intensity statistics.
    if "intensity" in report["dimension_names"]:
        inten = np.asarray(pts.intensity)
        report["intensity"] = {
            "min": int(inten.min()),
            "max": int(inten.max()),
            "mean": round(float(inten.mean()), 1),
            "all_zero": bool(inten.max() == 0),
        }
    else:
        report["intensity"] = None

    # RGB bit depth. py3dtiles assumes 16-bit; 8-bit data renders black
    # unless --color_scale 256 is passed.
    if report["has_rgb"]:
        rgb_max = int(max(np.max(pts.red), np.max(pts.green), np.max(pts.blue)))
        report["rgb"] = {
            "max_value": rgb_max,
            "likely_8bit": 0 < rgb_max < 256,
            "all_zero": rgb_max == 0,
        }
    else:
        report["rgb"] = None

    # Point density. Only meaningful for projected (metre-based) CRS.
    dx = report["maxs"][0] - report["mins"][0]
    dy = report["maxs"][1] - report["mins"][1]
    area = dx * dy
    report["extent_x"] = round(dx, 2)
    report["extent_y"] = round(dy, 2)
    report["extent_z"] = round(report["maxs"][2] - report["mins"][2], 2)
    report["density_pts_per_sq_unit"] = (
        round(report["point_count"] / area, 1) if area > 0 else None
    )

    # The verdict that drives step 2.
    pf_id = report["point_format"]
    if pf_id in MODERN_POINT_FORMATS:
        report["preprocessing_required"] = False
        report["verdict"] = (
            f"LAS {report['las_version']} point format {pf_id}. "
            "'classification' is a standalone field, py3dtiles can export it directly. "
            "No preprocessing needed."
        )
        report["recommended_extra_fields"] = [
            f for f in ("classification", "intensity") if f in usable
        ]
    else:
        report["preprocessing_required"] = True
        report["verdict"] = (
            f"LAS {report['las_version']} point format {pf_id}. "
            "'classification' is bit-packed and CANNOT be exported by py3dtiles from "
            "this format. Passing --extra-fields raw_classification produces a tileset "
            "full of zeros with no warning. Run 02_prepare_las.py to rewrite as "
            "LAS 1.4 point format 7 first."
        )
        report["recommended_extra_fields"] = [
            f for f in ("intensity",) if f in usable
        ]

    return report


def print_report(r):
    line = "=" * 78
    print(line)
    print(f"LAS INSPECTION: {Path(r['file']).name}")
    print(line)
    print(f"  File size            : {human_bytes(r['file_size_bytes'])}")
    print(f"  LAS version          : {r['las_version']}")
    print(f"  Point format         : {r['point_format']}")
    print(f"  Point count          : {r['point_count']:,}")
    print(f"  Points sampled       : {r['sampled_points']:,}")

    print("\n  -- Georeferencing --")
    if r["crs"]:
        print(f"  CRS                  : {r['crs']['name']}")
        print(f"  EPSG                 : {r['crs']['epsg'] or 'not resolvable'}")
        print(f"  Projected            : {r['crs']['is_projected']}")
    else:
        print("  CRS                  : NOT DECLARED IN FILE")
        print("                         -> you must pass --srs_in to py3dtiles manually")
    print(f"  Extent X / Y / Z     : {r['extent_x']} / {r['extent_y']} / {r['extent_z']}")
    print(f"  Z range              : {r['mins'][2]:.2f} to {r['maxs'][2]:.2f}")
    if r["density_pts_per_sq_unit"]:
        print(f"  Density              : {r['density_pts_per_sq_unit']} pts per sq unit")

    print("\n  -- Attributes --")
    print(f"  Has RGB              : {r['has_rgb']}")
    if r["rgb"]:
        print(f"  RGB max value        : {r['rgb']['max_value']}")
        if r["rgb"]["all_zero"]:
            print("    WARNING: all RGB values are zero.")
        elif r["rgb"]["likely_8bit"]:
            print("    WARNING: looks like 8-bit RGB. Pass --color_scale 256")
            print("             to py3dtiles or the tileset will render black.")
    if r["intensity"]:
        i = r["intensity"]
        print(f"  Intensity            : min {i['min']}, max {i['max']}, mean {i['mean']}")
        if i["all_zero"]:
            print("    WARNING: intensity is all zero, not worth exporting.")

    print("\n  -- Classification --")
    if r["classification_histogram"]:
        for code, info in sorted(r["classification_histogram"].items()):
            print(
                f"    {code:>3}  {info['label']:<28} "
                f"{info['count']:>12,}  {info['percent']:>6.2f}%"
            )
        if not r["is_classified"]:
            print("\n    WARNING: only codes 0/1 present. This file has NOT been")
            print("             classified. Tree height and canopy structure work")
            print("             needs classes 2 (ground) and 3/4/5 (vegetation).")
            print("             This is a gap in the post-processing step, not in")
            print("             py3dtiles. Flag it separately in the assessment.")
    else:
        print("    No classification dimension present.")

    print("\n  -- py3dtiles --extra-fields compatibility --")
    print("  A field can only be exported if its name appears in BOTH")
    print("  point_format.dtype() and point_format.dimension_names.")
    print(f"  Usable field names   : {', '.join(r['usable_extra_fields'])}")
    print(f"  Recommended          : {', '.join(r['recommended_extra_fields']) or 'none'}")

    print("\n  -- VERDICT --")
    print(f"  Preprocessing needed : {r['preprocessing_required']}")
    for chunk in [r["verdict"][i:i + 72] for i in range(0, len(r["verdict"]), 72)]:
        print(f"  {chunk}")
    print(line)


def main():
    ap = argparse.ArgumentParser(description="Inspect a LAS/LAZ file before 3D Tiles conversion.")
    ap.add_argument("las", help="Path to the .las or .laz file")
    ap.add_argument("--json", help="Also write the full report to this JSON path")
    ap.add_argument(
        "--sample-limit",
        type=int,
        default=5_000_000,
        help="Max points to read for attribute statistics (default 5,000,000)",
    )
    args = ap.parse_args()

    report = inspect(args.las, args.sample_limit)
    print_report(report)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2))
        print(f"\nJSON report written to {out}")


if __name__ == "__main__":
    main()
