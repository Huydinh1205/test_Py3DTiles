#!/usr/bin/env python3
"""
Step 1 - Inspect a LAS/LAZ file before conversion.

Purpose
-------
Answer the question: is any additional preprocessing actually needed,
rather than adding it by default?

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

# Plain-English meaning of each standard LAS dimension.
DIMENSION_MEANINGS = {
    "X": "Easting",
    "Y": "Northing",
    "Z": "Elevation",
    "intensity": "Strength of the returned laser pulse",
    "return_number": "Which return of its pulse this point is (1 = first)",
    "number_of_returns": "How many returns its pulse produced in total",
    "synthetic": "Flag: point created by software, not measured",
    "key_point": "Flag: model key point, keep when thinning",
    "withheld": "Flag: point should be ignored",
    "overlap": "Flag: point lies in a flight-line overlap",
    "scanner_channel": "Which scanner channel recorded it",
    "scan_direction_flag": "Mirror direction when recorded",
    "edge_of_flight_line": "Flag: last point before the scan reverses",
    "classification": "ASPRS class code (2 ground, 3-5 vegetation, 6 building...)",
    "classification_flags": "Packed synthetic/key-point/withheld/overlap bits",
    "user_data": "Free byte, software specific",
    "scan_angle_rank": "Scan angle, whole degrees",
    "scan_angle": "Scan angle, 0.006 degree steps",
    "point_source_id": "Flight line or source file ID",
    "gps_time": "Time the point was recorded (GPS seconds)",
    "red": "Colour, red channel (16-bit)",
    "green": "Colour, green channel (16-bit)",
    "blue": "Colour, blue channel (16-bit)",
    "nir": "Near-infrared channel",
}

# Point formats where 'classification' is its own byte and py3dtiles can read it.
MODERN_POINT_FORMATS = (6, 7, 8, 9, 10)


def laz_variable_chunks(header):
    """True if this is a LAZ file whose compressed chunks vary in size.

    COPC files always do (one chunk per octree node). laspy cannot seek
    correctly inside such files: it locates chunks as if they all had the
    same size. py3dtiles reads every file by seeking to 100,000-point
    offsets, so from the second chunk on it receives wrong data. Verified on
    a real COPC file: every seek past the first chunk returned wrong X and
    wrong classification, with both lazrs backends. Reading sequentially
    (no seek) is correct, which is how 02_prepare_las.py reads it.
    """
    try:
        from lazrs import LazVlr
    except ImportError:
        return False
    for vlr in header.vlrs:
        if getattr(vlr, "user_id", "").strip("\x00") == "laszip encoded":
            try:
                return bool(LazVlr(vlr.record_data).uses_variable_size_chunks())
            except Exception:
                return False
    return False


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
        report["laz_variable_chunks"] = laz_variable_chunks(header)

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

    # Every dimension in the file, with real values from the sample.
    dims = []
    for name in pf.dimension_names:
        # X/Y/Z are stored as scaled integers; report real-world coordinates.
        vals = np.asarray(getattr(pts, name.lower())) if name in ("X", "Y", "Z") else np.asarray(pts[name])
        info = {
            "name": name,
            "dtype": str(vals.dtype),
            "min": float(vals.min()) if vals.size else None,
            "max": float(vals.max()) if vals.size else None,
            "distinct": int(len(np.unique(vals))) if vals.size else 0,
            "usable_extra_field": name in usable,
            "meaning": DIMENSION_MEANINGS.get(name, "Extra dimension (vendor or user defined)"),
        }
        # 8-bit sensor values stretched to 16-bit (x256) show up as all
        # multiples of 256, e.g. a maximum of 65280 = 255 x 256.
        if vals.dtype == np.uint16 and vals.size and vals.max() > 255:
            info["stretched_8bit"] = bool(np.mean(vals % 256 == 0) > 0.99)
        dims.append(info)
    report["dimensions"] = dims

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
    if report["laz_variable_chunks"]:
        report["preprocessing_required"] = True
        report["verdict"] = (
            f"LAS {report['las_version']} point format {pf_id}, compressed with VARIABLE-SIZE "
            "chunks (every COPC file is). py3dtiles reads by seeking, and laspy cannot seek "
            "correctly in such files: everything after the first chunk comes back wrong "
            "(positions, classification, intensity) with no error. Run 02_prepare_las.py to "
            "rewrite it as plain LAS 1.4 first. The rewrite reads sequentially, which is correct."
        )
        report["recommended_extra_fields"] = [
            f for f in ("classification", "intensity") if f in usable
        ]
    elif pf_id in MODERN_POINT_FORMATS:
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
    if r.get("laz_variable_chunks"):
        print("  Compression          : LAZ, VARIABLE-SIZE chunks (COPC). py3dtiles cannot read")
        print("                         this directly, see verdict.")

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

    print("\n  -- All dimensions in the file (values from the sample) --")
    print(f"  {'dimension':<21}{'min':>14}{'max':>16}{'distinct':>10}  export  meaning")
    for d in r["dimensions"]:
        fmt = (lambda v: f"{v:,.2f}") if d["dtype"].startswith("float") else (lambda v: f"{v:,.0f}")
        lo = fmt(d["min"]) if d["min"] is not None else "-"
        hi = fmt(d["max"]) if d["max"] is not None else "-"
        # Position and colour are always written by py3dtiles, no flag needed.
        flag = "auto" if d["name"] in ("X", "Y", "Z", "red", "green", "blue") else (
            "yes" if d["usable_extra_field"] else "-")
        note = d["meaning"]
        if d["distinct"] == 1:
            note += "  [constant, carries no information]"
        if d.get("stretched_8bit"):
            note += "  [8-bit values x256]"
        print(f"  {d['name']:<21}{lo:>14}{hi:>16}{d['distinct']:>10,}  {flag:^6}  {note}")
    print("  export: auto = always in the tileset (position, RGB)")
    print("          yes  = can be added with --extra-fields")
    print("          -    = cannot be exported by py3dtiles (bit-packed field)")

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
