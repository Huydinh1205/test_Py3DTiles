#!/usr/bin/env python3
"""
Step 0 - Work out what each file in a DJI Terra LiDAR output folder is,
and which one should be published.

A typical folder looks like:

    cloud0.las ... cloud4.las     numbered point clouds
    cloud_merged.las              one combined point cloud
    cloud_merged.copc.laz         a Cloud Optimized Point Cloud copy

Rather than guess, this script reads every file and tests the relationships:

  Numbered files, per flight or per spatial tile?
      Per flight  -> GPS time ranges do not overlap, footprints overlap
      Per tile    -> footprints sit side by side, GPS times interleave

  Is the merged file the union of the numbered files?
      Compares the total point count and the class histogram.

  Is the .copc.laz a faithful copy of the merged .las?
      Compares point count, class histogram, bounds and point format.
      A COPC file must be LAS 1.4 point format 6, 7 or 8 (COPC spec), so if
      it matches the merged .las it can be converted by py3dtiles with NO
      preprocessing, even when the .las itself is an older point format.

Usage
-----
    python scripts/00_compare_terra_files.py "D:/path/to/terra_lidar"
    python scripts/00_compare_terra_files.py "D:/path/to/terra_lidar" --max-points 2000000

--max-points limits how many points are read per file for the GPS time and
class statistics. Leave it off for an exact answer, use it for a quick look
at very large files. Header facts (counts, bounds, format) are always exact.
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

try:
    import laspy
except ImportError:
    sys.exit("laspy is not installed. Run: pip install -r requirements.txt")


CHUNK = 5_000_000


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def is_copc(header):
    """A COPC file carries a VLR with user_id 'copc' (the COPC info VLR)."""
    for vlr in list(header.vlrs) + list(getattr(header, "evlrs", None) or []):
        if getattr(vlr, "user_id", "").strip("\x00").lower() == "copc":
            return True
    return False


def scan(path, max_points=None):
    """Header facts plus a streamed pass for GPS time and classification."""
    path = Path(path)
    info = {
        "name": path.name,
        "size": path.stat().st_size,
        "modified": dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
    }
    try:
        reader = laspy.open(str(path))
    except Exception as exc:
        msg = str(exc)
        if path.suffix.lower() == ".laz":
            msg += "  (reading .laz needs the lazrs backend: pip install \"laspy[lazrs]\")"
        info["error"] = msg
        return info

    with reader:
        h = reader.header
        pf = h.point_format
        info.update({
            "version": f"{h.version.major}.{h.version.minor}",
            "pf": int(pf.id),
            "points": int(h.point_count),
            "mins": [float(v) for v in h.mins],
            "maxs": [float(v) for v in h.maxs],
            "scales": [float(v) for v in h.scales],
            "copc": is_copc(h),
            "has_gps": "gps_time" in pf.dimension_names,
            "has_class": "classification" in pf.dimension_names,
        })
        try:
            crs = h.parse_crs()
            info["epsg"] = crs.to_epsg() if crs else None
            info["crs"] = crs.name if crs else None
        except Exception:
            info["epsg"], info["crs"] = None, None

        limit = max_points or info["points"]
        read = 0
        t_min, t_max = np.inf, -np.inf
        hist = np.zeros(256, dtype=np.int64)
        for chunk in reader.chunk_iterator(CHUNK):
            if read >= limit:
                break
            n = min(len(chunk), limit - read)
            if info["has_gps"]:
                t = np.asarray(chunk.gps_time[:n])
                t_min, t_max = min(t_min, t.min()), max(t_max, t.max())
            if info["has_class"]:
                hist += np.bincount(np.asarray(chunk.classification[:n], dtype=np.uint8),
                                    minlength=256)
            read += n
            print(f"    {path.name}: {read:,} / {limit:,} points read", end="\r")
        print(" " * 100, end="\r")

    info["points_scanned"] = read
    info["exact"] = read == info["points"]
    info["gps_range"] = [float(t_min), float(t_max)] if info["has_gps"] and read else None
    info["class_hist"] = {int(k): int(v) for k, v in enumerate(hist) if v}
    return info


def xy_overlap(a, b):
    """Overlap area of two XY bounding boxes as a share of the smaller box."""
    ox = max(0.0, min(a["maxs"][0], b["maxs"][0]) - max(a["mins"][0], b["mins"][0]))
    oy = max(0.0, min(a["maxs"][1], b["maxs"][1]) - max(a["mins"][1], b["mins"][1]))
    area = lambda f: (f["maxs"][0] - f["mins"][0]) * (f["maxs"][1] - f["mins"][1])
    smaller = min(area(a), area(b))
    return (ox * oy) / smaller if smaller > 0 else 0.0


def time_overlap(a, b):
    if not a.get("gps_range") or not b.get("gps_range"):
        return None
    lo = max(a["gps_range"][0], b["gps_range"][0])
    hi = min(a["gps_range"][1], b["gps_range"][1])
    return hi > lo


def same_hist(a, b):
    return a.get("class_hist") == b.get("class_hist")


def main():
    ap = argparse.ArgumentParser(description="Explain the files in a DJI Terra LiDAR output folder.")
    ap.add_argument("folder", help="Folder containing the .las / .laz files")
    ap.add_argument("--max-points", type=int,
                    help="Read at most this many points per file for statistics")
    ap.add_argument("--json", help="Write the full comparison to this JSON path")
    args = ap.parse_args()

    folder = Path(args.folder)
    files = sorted([p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in (".las", ".laz")])
    if not files:
        sys.exit(f"No .las or .laz files found in {folder}")

    print(f"Scanning {len(files)} files in {folder}\n")
    infos = [scan(p, args.max_points) for p in files]
    print()

    # ------------------------------------------------------------ inventory
    line = "=" * 108
    print(line)
    print("INVENTORY")
    print(line)
    print(f"  {'file':<26}{'size':>10}  {'LAS':>4} {'pf':>3}  {'points':>14}  "
          f"{'EPSG':>6}  {'COPC':>4}  {'classes present':<24} modified")
    for f in infos:
        if "error" in f:
            print(f"  {f['name']:<26}  ERROR: {f['error']}")
            continue
        classes = ",".join(str(k) for k in sorted(f["class_hist"])) or "-"
        print(f"  {f['name']:<26}{human_bytes(f['size']):>10}  {f['version']:>4} {f['pf']:>3}  "
              f"{f['points']:>14,}  {str(f['epsg'] or '-'):>6}  {'yes' if f['copc'] else 'no':>4}  "
              f"{classes[:24]:<24} {f['modified']}")
    if any(not f.get("exact", True) for f in infos if "error" not in f):
        print("\n  NOTE: --max-points was used, GPS time and class figures are from a sample.")

    good = [f for f in infos if "error" not in f]
    merged_las = [f for f in good if "merged" in f["name"].lower() and not f["copc"]]
    merged_copc = [f for f in good if f["copc"]]
    parts = [f for f in good if "merged" not in f["name"].lower() and not f["copc"]]

    findings = []

    # ------------------------------------------------- numbered files: what are they
    print("\n" + line)
    print("WHAT ARE THE NUMBERED FILES?")
    print(line)
    if len(parts) >= 2:
        print(f"  {'file':<14}{'X range':>26}{'Y range':>26}   GPS time range")
        for f in parts:
            gr = f["gps_range"]
            gps = f"{gr[0]:.1f} .. {gr[1]:.1f}" if gr else "n/a"
            print(f"  {f['name']:<14}{f['mins'][0]:>12.1f} .. {f['maxs'][0]:<11.1f}"
                  f"{f['mins'][1]:>12.1f} .. {f['maxs'][1]:<11.1f}   {gps}")

        pairs = [(a, b) for i, a in enumerate(parts) for b in parts[i + 1:]]
        mean_xy = np.mean([xy_overlap(a, b) for a, b in pairs])
        t_flags = [time_overlap(a, b) for a, b in pairs]
        any_time = any(t for t in t_flags if t is not None)
        print(f"\n  Mean footprint overlap between files : {mean_xy * 100:.0f}% of the smaller footprint")
        print(f"  Any GPS time ranges overlapping      : "
              f"{'n/a' if all(t is None for t in t_flags) else ('yes' if any_time else 'no')}")

        if not any_time and mean_xy > 0.2:
            verdict = ("PER FLIGHT. Each numbered file is one flight (sortie): their GPS times "
                       "do not overlap and their footprints cover the same ground. Publishing "
                       "them separately would stack duplicate surveys of the same area.")
        elif any_time and mean_xy < 0.2:
            verdict = ("PER SPATIAL TILE. The numbered files are adjacent blocks of one survey: "
                       "footprints sit side by side and GPS times interleave.")
        elif not any_time and mean_xy <= 0.2:
            verdict = ("PER FLIGHT, side by side. Separate flights (no GPS time overlap) that "
                       "each covered a different part of the area.")
        else:
            verdict = ("UNCLEAR. GPS times overlap and footprints overlap. Inspect in QGIS "
                       "before relying on either interpretation.")
        print(f"\n  VERDICT: {verdict}")
        findings.append(("numbered files", verdict))
    else:
        print("  Fewer than two numbered files, nothing to compare.")

    # -------------------------------------------- is merged = union of the parts
    if merged_las and parts:
        m = merged_las[0]
        print("\n" + line)
        print(f"IS {m['name']} THE UNION OF THE NUMBERED FILES?")
        print(line)
        total = sum(f["points"] for f in parts)
        diff = m["points"] - total
        print(f"  Sum of numbered files : {total:>16,}")
        print(f"  {m['name']:<22}: {m['points']:>16,}")
        print(f"  Difference            : {diff:>+16,}  ({diff / total * 100:+.3f}%)")

        exact_scans = m.get("exact") and all(f.get("exact") for f in parts)
        if exact_scans:
            union_hist = {}
            for f in parts:
                for k, v in f["class_hist"].items():
                    union_hist[k] = union_hist.get(k, 0) + v
            hist_match = union_hist == m["class_hist"]
            print(f"  Class histogram equal : {'yes' if hist_match else 'no'}")
        else:
            hist_match = None

        if diff == 0 and hist_match in (True, None):
            verdict = ("YES. The merged file contains exactly the points of the numbered files "
                       "combined. The numbered files add nothing for publishing.")
        elif diff < 0:
            verdict = ("NO, it is smaller. DJI Terra thinned or de-duplicated overlapping flights "
                       "when merging. The merged file is the cleaner deliverable.")
        else:
            verdict = "NO, it is larger. Unexpected, check the export settings in DJI Terra."
        print(f"\n  VERDICT: {verdict}")
        findings.append(("merged vs parts", verdict))

    # ----------------------------------------------- is the copc a faithful copy
    if merged_las and merged_copc:
        m, c = merged_las[0], merged_copc[0]
        print("\n" + line)
        print(f"IS {c['name']} A FAITHFUL COPY OF {m['name']}?")
        print(line)
        rows = [
            ("LAS version / point format", f"{m['version']} / pf{m['pf']}", f"{c['version']} / pf{c['pf']}"),
            ("Point count", f"{m['points']:,}", f"{c['points']:,}"),
            ("File size", human_bytes(m["size"]), human_bytes(c["size"])),
            ("EPSG", str(m["epsg"]), str(c["epsg"])),
            ("Min X / Y / Z", " / ".join(f"{v:.2f}" for v in m["mins"]),
             " / ".join(f"{v:.2f}" for v in c["mins"])),
            ("Max X / Y / Z", " / ".join(f"{v:.2f}" for v in m["maxs"]),
             " / ".join(f"{v:.2f}" for v in c["maxs"])),
            ("Scale", " / ".join(str(v) for v in m["scales"]), " / ".join(str(v) for v in c["scales"])),
            ("Modified", m["modified"], c["modified"]),
        ]
        print(f"  {'':<28}{m['name']:>32}{c['name']:>34}")
        for k, a, b in rows:
            flag = "" if a == b or k in ("File size", "Modified", "LAS version / point format") else "  <- differs"
            print(f"  {k:<28}{a:>32}{b:>34}{flag}")

        hist_ok = same_hist(m, c) if (m.get("exact") and c.get("exact")) else None
        print(f"  {'Class histogram equal':<28}{'' :>32}"
              f"{('yes' if hist_ok else 'NO') if hist_ok is not None else 'not checked (sampled)':>34}")

        count_ok = m["points"] == c["points"]
        bound_diff = float(max(
            np.max(np.abs(np.array(m["mins"]) - np.array(c["mins"]))),
            np.max(np.abs(np.array(m["maxs"]) - np.array(c["maxs"]))),
        ))
        newer = c["modified"] > m["modified"]

        # Same points (count + class histogram) is what makes it a faithful copy.
        # Header bounding boxes are metadata and can legitimately differ: a COPC
        # writer recomputes them from the points, other writers may round them.
        if count_ok and hist_ok in (True, None):
            verdict = (f"YES. Same points, re-packed as COPC (LAS {c['version']} pf{c['pf']}, compressed, "
                       "spatially ordered). ")
            if m["pf"] < 6:
                verdict += ("Because the .las is pf" + str(m["pf"]) + " and the COPC is pf" + str(c["pf"]) +
                            ", converting the COPC with py3dtiles keeps classification with NO "
                            "preprocessing step, while the .las would need 02_prepare_las.py.")
            else:
                verdict += "Either file converts without preprocessing."
            if newer:
                verdict += (" It was written after the .las, which fits QGIS creating it as an index "
                            "the first time someone opened the .las (QGIS 3.26+ indexes to COPC).")
        else:
            verdict = "NO, it differs from the .las. Treat the .las as the source of truth."
        print(f"\n  VERDICT: {verdict}")
        findings.append(("copc vs merged las", verdict))

        # Which header bounding box is accurate? The numbered files are an
        # independent witness: their combined extent must equal the true extent.
        if bound_diff > 0.01:
            print(f"\n  HEADER BOUNDS: the two headers disagree by up to {bound_diff:.2f} m.")
            if parts:
                pmin = np.min([f["mins"] for f in parts], axis=0)
                pmax = np.max([f["maxs"] for f in parts], axis=0)
                err_las = max(np.max(np.abs(pmin - m["mins"])), np.max(np.abs(pmax - m["maxs"])))
                err_copc = max(np.max(np.abs(pmin - c["mins"])), np.max(np.abs(pmax - c["maxs"])))
                print(f"  Combined extent of the numbered files:")
                print(f"    min {pmin[0]:.2f} / {pmin[1]:.2f} / {pmin[2]:.2f}   "
                      f"max {pmax[0]:.2f} / {pmax[1]:.2f} / {pmax[2]:.2f}")
                print(f"  Mismatch vs {m['name']:<24}: {err_las:.2f} m")
                print(f"  Mismatch vs {c['name']:<24}: {err_copc:.2f} m")
                if err_copc < err_las:
                    note = (f"The {m['name']} header bounding box is inaccurate (off by up to "
                            f"{err_las:.2f} m, some points lie outside it); the COPC header matches the "
                            "data. py3dtiles builds its octree from the header bounding box, which is "
                            "one more reason to convert from the COPC file.")
                else:
                    note = "The .las header matches the numbered files; the COPC header is the odd one out."
                print(f"\n  NOTE: {note}")
                findings.append(("header bounds", note))

    # ------------------------------------------------------------ recommendation
    print("\n" + line)
    print("WHICH FILE TO USE")
    print(line)
    if merged_las:
        m = merged_las[0]
        c = merged_copc[0] if merged_copc else None
        print(f"  Publish               : the merged point cloud, one dataset for the whole site.")
        if c and m["pf"] < 6 and c["pf"] >= 6:
            print(f"  Convert from          : {c['name']}  (already pf{c['pf']}, skips step 2)")
            print(f"  Cross-check against   : {m['name']}  (source of truth)")
        else:
            print(f"  Convert from          : {m['name']}")
        if parts:
            smallest = min(parts, key=lambda f: f["points"])
            print(f"  Quick test first on   : {smallest['name']}  ({smallest['points']:,} points, fastest)")
        big = c if (c and m["pf"] < 6 and c["pf"] >= 6) else m
        print(f"  Large-dataset test    : {big['name']}  ({big['points']:,} points)")
        print(f"  Do not publish        : the numbered files individually")
    else:
        print("  No merged file found. Publish the numbered files as one tileset by passing them all")
        print("  to py3dtiles convert together.")
    print(line)

    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"files": infos, "findings": findings}, indent=2, default=str))
        print(f"\nJSON written to {p}")


if __name__ == "__main__":
    main()
