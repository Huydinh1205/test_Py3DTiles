#!/usr/bin/env python3
"""
Step 4 - Validate that the tileset actually carries the attributes we need.

This is the step that matters most. py3dtiles can report a successful
conversion while silently writing an attribute as all zeros, so "the command
exited 0" is not evidence that the data survived. The only reliable check is
to read the values back out of the tiles and compare them against the source.

What it does
------------
  - Reads every .pnts batch table (3D Tiles 1.0) or .glb custom attribute
    (3D Tiles 1.1) in the output directory
  - Rebuilds the classification / intensity distribution from the tiles
  - Compares it against the same distribution in the source LAS
  - Reports PASS or FAIL per attribute
  - Sanity checks tileset.json: root bounding volume, geometric error,
    transform, and the presence of a root content

How the comparison works
------------------------
py3dtiles 12.1.1 lays the tileset out like this (verified on trees up to
6 levels and ~500 tiles, for both 3D Tiles 1.0 and 1.1):

  root content          preview.pnts / preview.glb, a coarse overview
  root's only child     points/r.pnts / points/r.glb, another overview
                        whose points are duplicated further down the tree
  every other tile      together these partition the source EXACTLY,
                        each source point appears once

The two overview tiles are identified from tileset.json, not by file name.
So the validator excludes the two overview tiles, then:

  EXACT mode (preferred)
      If the remaining tiles hold exactly as many points as the source,
      the sorted values of each attribute are compared element by element
      against the sorted source values. Any difference at all is a FAIL.
      This catches silent zeroing, truncation, rescaling and dropped points.

  FALLBACK mode
      If the counts do not line up (a py3dtiles version that duplicates
      points in intermediate tiles too, for instance) the validator falls
      back to comparing the distribution: percentages per value for
      categorical fields such as classification, and summary statistics
      for continuous fields such as intensity, within a tolerance.

Usage
-----
    python scripts/04_validate_tileset.py results/tiles_input \
        --source data/input_las14.las \
        --fields classification intensity
"""

import argparse
import json
import struct
import sys
from pathlib import Path

import numpy as np

try:
    import laspy
except ImportError:
    sys.exit("laspy is not installed. Run: pip install -r requirements.txt")


# glTF componentType -> numpy dtype
GLTF_COMPONENT_TYPES = {
    5120: np.int8,
    5121: np.uint8,
    5122: np.int16,
    5123: np.uint16,
    5125: np.uint32,
    5126: np.float32,
}

# 3D Tiles 1.0 batch table componentType -> numpy dtype
PNTS_COMPONENT_TYPES = {
    "BYTE": np.int8,
    "UNSIGNED_BYTE": np.uint8,
    "SHORT": np.int16,
    "UNSIGNED_SHORT": np.uint16,
    "INT": np.int32,
    "UNSIGNED_INT": np.uint32,
    "FLOAT": np.float32,
    "DOUBLE": np.float64,
}


def read_pnts(path):
    """Return (point_count, {attribute_name: ndarray}) from a .pnts tile."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"pnts":
        return 0, {}
    (_magic, _ver, _blen, ft_json_len, ft_bin_len,
     bt_json_len, bt_bin_len) = struct.unpack("<4sIIIIII", raw[:28])

    off = 28
    ft_json = json.loads(raw[off:off + ft_json_len])
    off += ft_json_len + ft_bin_len

    if bt_json_len == 0:
        return int(ft_json.get("POINTS_LENGTH", 0)), {}

    bt_json = json.loads(raw[off:off + bt_json_len])
    bt_bin = raw[off + bt_json_len: off + bt_json_len + bt_bin_len]

    n = int(ft_json["POINTS_LENGTH"])
    attrs = {}
    for name, spec in bt_json.items():
        if not isinstance(spec, dict) or "componentType" not in spec:
            continue  # per-tile constant or unsupported entry
        dtype = PNTS_COMPONENT_TYPES.get(spec["componentType"])
        if dtype is None:
            continue
        attrs[name] = np.frombuffer(
            bt_bin, dtype=dtype, count=n, offset=spec.get("byteOffset", 0)
        )
    return n, attrs


def read_glb(path):
    """Return (point_count, {attribute_name: ndarray}) from a .glb tile."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"glTF":
        return 0, {}
    _magic, _ver, total_len = struct.unpack("<4sII", raw[:12])

    off, json_chunk, bin_chunk = 12, None, None
    while off < total_len:
        clen, ctype = struct.unpack("<II", raw[off:off + 8])
        off += 8
        data = raw[off:off + clen]
        off += clen
        if ctype == 0x4E4F534A:      # 'JSON'
            json_chunk = data
        elif ctype == 0x004E4942:    # 'BIN'
            bin_chunk = data

    if json_chunk is None:
        return 0, {}
    gltf = json.loads(json_chunk)

    attrs, count = {}, 0
    for mesh in gltf.get("meshes", []):
        for prim in mesh.get("primitives", []):
            for attr_name, accessor_idx in prim.get("attributes", {}).items():
                acc = gltf["accessors"][accessor_idx]
                n = int(acc["count"])
                if attr_name == "POSITION":
                    count = max(count, n)
                # Custom attributes are prefixed with an underscore by py3dtiles,
                # e.g. extra field 'classification' becomes '_CLASSIFICATION'.
                if not attr_name.startswith("_"):
                    continue
                dtype = GLTF_COMPONENT_TYPES.get(acc["componentType"])
                if dtype is None or bin_chunk is None:
                    continue
                bv = gltf["bufferViews"][acc["bufferView"]]
                start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
                values = np.frombuffer(bin_chunk, dtype=dtype, count=n, offset=start)
                attrs[attr_name.lstrip("_").lower()] = values
    return count, attrs


# Tile attribute name -> the source LAS dimension it should be compared with.
SOURCE_FIELD_ALIASES = {"raw_classification": "classification"}


def overview_tile_paths(tiles_dir):
    """Resolve the overview tiles from tileset.json.

    Returns the root content and, when the root has exactly one child,
    that child's content. Both hold points duplicated deeper in the tree.
    """
    tiles_dir = Path(tiles_dir)
    try:
        data = json.loads((tiles_dir / "tileset.json").read_text())
    except Exception:
        return {}
    root = data.get("root", {})

    def uri_of(node):
        content = node.get("content") or {}
        return content.get("uri") or content.get("url")

    found = {}
    if uri_of(root):
        found[(tiles_dir / uri_of(root)).resolve()] = "root overview"
    children = root.get("children") or []
    if len(children) == 1 and uri_of(children[0]):
        found[(tiles_dir / uri_of(children[0])).resolve()] = "level-1 overview"
    return found


def collect_tileset(tiles_dir):
    """Walk the output directory and merge tile attributes, excluding overviews."""
    tiles_dir = Path(tiles_dir)
    overviews = overview_tile_paths(tiles_dir)
    pnts = sorted(tiles_dir.rglob("*.pnts"))
    glbs = sorted(tiles_dir.rglob("*.glb"))

    merged, total_points, tile_count, skipped = {}, 0, 0, []
    for path in pnts + glbs:
        kind = overviews.get(path.resolve())
        if kind:
            skipped.append((kind, path.relative_to(tiles_dir).as_posix()))
            continue
        n, attrs = read_pnts(path) if path.suffix == ".pnts" else read_glb(path)
        total_points += n
        tile_count += 1
        for name, values in attrs.items():
            merged.setdefault(name.lower(), []).append(values)

    return {
        "tile_count": tile_count,
        "total_points_in_tiles": total_points,
        "skipped_overview_tiles": skipped,
        "format": "3D Tiles 1.0 (.pnts)" if pnts else "3D Tiles 1.1 (.glb)",
        "attributes": {k: np.concatenate(v) for k, v in merged.items()},
    }


def source_values(las_path, field, chunk_size=5_000_000):
    """Every value of one attribute from the source file, read in chunks.

    Only the one field is kept in memory: 1 byte per point for
    classification, 2 for intensity. 100 million points is 100 to 200 MB.
    """
    with laspy.open(str(las_path)) as reader:
        if field not in reader.header.point_format.dimension_names:
            return None
        parts = [np.asarray(getattr(chunk, field)).copy()
                 for chunk in reader.chunk_iterator(chunk_size)]
    return np.concatenate(parts) if parts else np.array([])


def summary_stats(values):
    v = values.astype(np.float64)
    return {
        "min": float(v.min()), "max": float(v.max()), "mean": round(float(v.mean()), 3),
        "p05": float(np.percentile(v, 5)), "p50": float(np.percentile(v, 50)),
        "p95": float(np.percentile(v, 95)),
    }


def read_pnts_positions(path):
    """float64 Nx3 positions in the tile's own frame (RTC_CENTER applied)."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"pnts":
        return None
    _m, _v, _l, ft_json_len, ft_bin_len, _bj, _bb = struct.unpack("<4sIIIIII", raw[:28])
    ft = json.loads(raw[28:28 + ft_json_len])
    if "POSITION" not in ft:
        return None  # quantized positions are not produced by py3dtiles
    n = int(ft["POINTS_LENGTH"])
    start = 28 + ft_json_len + ft["POSITION"].get("byteOffset", 0)
    pos = np.frombuffer(raw, dtype=np.float32, count=n * 3, offset=start).reshape(n, 3)
    pos = pos.astype(np.float64)
    if "RTC_CENTER" in ft:
        pos += np.asarray(ft["RTC_CENTER"], dtype=np.float64)
    return pos


def tile_positions_ecef(tiles_dir):
    """Every point of the tileset in ECEF (EPSG:4978), overview tiles excluded.

    Walks tileset.json accumulating each tile's 4x4 transform (stored
    column-major) from the root down, exactly as a 3D Tiles viewer does.
    Only .pnts content is decoded; returns None for .glb tilesets.
    """
    tiles_dir = Path(tiles_dir)
    data = json.loads((tiles_dir / "tileset.json").read_text())
    overviews = overview_tile_paths(tiles_dir)
    chunks, saw_glb = [], False

    def walk(node, parent, base_dir):
        m = parent
        if node.get("transform"):
            m = parent @ np.array(node["transform"], dtype=np.float64).reshape(4, 4).T
        content = node.get("content") or {}
        uri = content.get("uri") or content.get("url")
        if uri:
            path = (base_dir / uri).resolve()
            nonlocal saw_glb
            if path.suffix == ".json":
                # External tileset. py3dtiles splits any subtree whose JSON
                # exceeds 100 KB into tileset.<node>.json, so dense datasets
                # always have these. Its root continues this branch.
                sub = json.loads(path.read_text())
                walk(sub["root"], m, path.parent)
            elif path.suffix == ".glb":
                saw_glb = True
            elif path not in overviews:
                local = read_pnts_positions(path)
                if local is not None and len(local):
                    homo = np.hstack([local, np.ones((len(local), 1))])
                    chunks.append((homo @ m.T)[:, :3])
        for child in node.get("children") or []:
            walk(child, m, base_dir)

    walk(data["root"], np.eye(4), tiles_dir)
    if saw_glb or not chunks:
        return None
    return np.vstack(chunks)


def source_xyz(las_path, chunk_size=5_000_000):
    """Real-world coordinates of every source point, read sequentially."""
    xs, ys, zs = [], [], []
    with laspy.open(str(las_path)) as reader:
        for chunk in reader.chunk_iterator(chunk_size):
            xs.append(np.asarray(chunk.x)); ys.append(np.asarray(chunk.y)); zs.append(np.asarray(chunk.z))
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)


def check_geometry(tiles_dir, source, srs_in, tolerance):
    """Do the tile points sit where the source points are?

    Converts tile positions from ECEF back to the source CRS and compares
    them with the source: the extent, and (when counts match) every sorted
    X, Y and Z value. Wrong reads, shuffled blocks or reprojection mistakes
    all show up as differences far above the float32 noise (well under 1 cm).
    """
    from pyproj import CRS, Transformer

    ecef = tile_positions_ecef(tiles_dir)
    if ecef is None:
        return {"status": "skipped", "reason": "only .pnts tilesets (3D Tiles 1.0) are decoded"}

    crs = None
    if srs_in:
        crs = CRS.from_epsg(int(srs_in))
    else:
        with laspy.open(str(source)) as reader:
            crs = reader.header.parse_crs()
    if crs is None:
        return {"status": "skipped", "reason": "source CRS unknown, pass --srs-in"}

    to_src = Transformer.from_crs(CRS.from_epsg(4978), crs, always_xy=True)
    tx, ty, tz = to_src.transform(ecef[:, 0], ecef[:, 1], ecef[:, 2])
    del ecef
    sx, sy, sz = source_xyz(source)

    result = {"tile_points": int(len(tx)), "source_points": int(len(sx)), "axes": {}}
    ok = True
    for name, s_vals, t_vals in (("X", sx, tx), ("Y", sy, ty), ("Z", sz, tz)):
        t_vals = np.asarray(t_vals)
        extent_err = max(abs(float(s_vals.min()) - float(t_vals.min())),
                         abs(float(s_vals.max()) - float(t_vals.max())))
        axis = {"source_min": float(s_vals.min()), "source_max": float(s_vals.max()),
                "tile_min": float(t_vals.min()), "tile_max": float(t_vals.max()),
                "extent_error_m": extent_err}
        if len(s_vals) == len(t_vals):
            axis["max_sorted_error_m"] = float(np.max(np.abs(np.sort(s_vals) - np.sort(t_vals))))
            axis_ok = axis["max_sorted_error_m"] <= tolerance
        else:
            axis_ok = extent_err <= tolerance
        axis["ok"] = bool(axis_ok)
        ok &= axis_ok
        result["axes"][name] = axis
    result["status"] = "pass" if ok else "fail"
    return result


def check_tileset_json(tiles_dir):
    path = Path(tiles_dir) / "tileset.json"
    if not path.exists():
        return {"ok": False, "error": "tileset.json is missing"}
    data = json.loads(path.read_text())
    root = data.get("root", {})
    bv = root.get("boundingVolume", {})
    return {
        "ok": True,
        "asset_version": data.get("asset", {}).get("version"),
        "geometric_error_root": data.get("geometricError"),
        "root_geometric_error": root.get("geometricError"),
        "bounding_volume_type": next(iter(bv), None),
        "has_transform": "transform" in root,
        "refine": root.get("refine"),
        "root_has_content": "content" in root,
        "child_count": len(root.get("children", [])),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Verify a 3D Tiles output preserved the attributes we need."
    )
    ap.add_argument("tiles_dir", help="Tileset directory produced by step 3")
    ap.add_argument("--source", required=True, help="The LAS file that was converted")
    ap.add_argument("--fields", nargs="*", default=["classification", "intensity"],
                    help="Attributes to verify")
    ap.add_argument("--tolerance", type=float, default=1.0,
                    help="Fallback mode only: allowed difference, percentage points "
                         "for categorical fields, percent for statistics (default 1.0)")
    ap.add_argument("--srs-in", help="Source EPSG for the position check, if the file has no CRS")
    ap.add_argument("--geometry-tolerance", type=float, default=0.01,
                    help="Allowed position error in source units, normally metres (default 0.01)")
    ap.add_argument("--no-geometry", action="store_true",
                    help="Skip the position check (it reads every point, so it needs RAM: "
                         "roughly 100 bytes per point)")
    ap.add_argument("--json", help="Write the validation result to this JSON path")
    args = ap.parse_args()

    print("=" * 78)
    print("TILESET VALIDATION")
    print("=" * 78)

    meta = check_tileset_json(args.tiles_dir)
    print("  -- tileset.json --")
    if not meta["ok"]:
        print(f"  FAIL {meta['error']}")
        sys.exit(1)
    for key in ("asset_version", "geometric_error_root", "bounding_volume_type",
                "has_transform", "refine", "root_has_content", "child_count"):
        print(f"  {key:<24}: {meta[key]}")

    tiles = collect_tileset(args.tiles_dir)
    print("\n  -- tiles --")
    print(f"  Format                  : {tiles['format']}")
    print(f"  Tile files read         : {tiles['tile_count']}")
    print(f"  Points across all tiles : {tiles['total_points_in_tiles']:,}")
    print(f"  Attributes found        : "
          f"{', '.join(sorted(tiles['attributes'])) or 'NONE'}")
    for kind, name in tiles["skipped_overview_tiles"]:
        print(f"  Excluded ({kind:<16}) : {name}")

    results, all_pass = {}, True
    source_count = None
    for field in args.fields:
        print(f"\n  -- {field} --")
        # 'raw_classification' is the packed LAS 1.2 byte that py3dtiles
        # accepts but cannot read. Compare it against the real class codes
        # so the silent all-zero output is reported as the failure it is.
        src_vals = source_values(args.source, SOURCE_FIELD_ALIASES.get(field, field))
        if src_vals is None:
            print(f"  SKIP: '{field}' is not present in the source file")
            results[field] = {"status": "skipped", "reason": "not in source"}
            continue
        source_count = len(src_vals)

        key = field.lower()
        if key not in tiles["attributes"]:
            print(f"  FAIL: '{field}' is NOT present in the tileset.")
            print("        The attribute was requested but did not survive conversion.")
            print("        Check step 1's verdict: a LAS 1.2 point format cannot")
            print("        export classification. Run 02_prepare_las.py first.")
            results[field] = {"status": "missing_from_tileset"}
            all_pass = False
            continue

        tile_vals = tiles["attributes"][key]

        # The silent failure mode: every value zero while the source is not.
        if not tile_vals.any() and src_vals.any():
            print("  FAIL: every value in the tileset is zero while the source")
            print("        has real values. This is the silent data loss case.")
            print("        Cause: the field name was accepted by py3dtiles but")
            print("        could not be read from this LAS point format.")
            results[field] = {"status": "all_zero"}
            all_pass = False
            continue

        # ---------- EXACT mode ----------
        if len(tile_vals) == len(src_vals):
            identical = np.array_equal(np.sort(tile_vals), np.sort(src_vals.astype(tile_vals.dtype)))
            print(f"  Mode   : EXACT ({len(src_vals):,} source points, "
                  f"{len(tile_vals):,} tile points)")
            if identical:
                print("  Every source value is present in the tileset exactly once.")
            else:
                diff = int(np.sum(np.sort(tile_vals) != np.sort(src_vals.astype(tile_vals.dtype))))
                print(f"  {diff:,} values differ between source and tileset.")
            # Show the class table for categorical fields, it reads well in a report.
            if len(np.unique(src_vals)) <= 64:
                s_u, s_c = np.unique(src_vals, return_counts=True)
                t_map = dict(zip(*np.unique(tile_vals, return_counts=True)))
                print(f"  {'value':>8} {'source':>12} {'tileset':>12}")
                for u, c in zip(s_u, s_c):
                    print(f"  {int(u):>8} {int(c):>12,} {int(t_map.get(u, 0)):>12,}")
            else:
                ss, ts = summary_stats(src_vals), summary_stats(tile_vals)
                print(f"  {'stat':>8} {'source':>12} {'tileset':>12}")
                for k in ss:
                    print(f"  {k:>8} {ss[k]:>12} {ts[k]:>12}")
            print(f"  RESULT: {'PASS' if identical else 'FAIL'}")
            results[field] = {"status": "pass" if identical else "fail", "mode": "exact"}
            if len(np.unique(src_vals)) <= 64:
                # Kept for the viewer legend: which classes exist and how many points.
                s_u, s_c = np.unique(src_vals, return_counts=True)
                results[field]["value_counts"] = {int(u): int(c) for u, c in zip(s_u, s_c)}
            all_pass &= identical
            continue

        # ---------- FALLBACK mode ----------
        print(f"  Mode   : DISTRIBUTION (counts differ: source {len(src_vals):,}, "
              f"tiles {len(tile_vals):,})")
        if len(np.unique(src_vals)) <= 64:
            s_u, s_c = np.unique(src_vals, return_counts=True)
            t_u, t_c = np.unique(tile_vals, return_counts=True)
            s_pct = {int(u): 100.0 * c / s_c.sum() for u, c in zip(s_u, s_c)}
            t_pct = {int(u): 100.0 * c / t_c.sum() for u, c in zip(t_u, t_c)}
            field_pass, rows = True, []
            print(f"  {'value':>8} {'source %':>10} {'tileset %':>10} {'diff':>8}")
            for code in sorted(set(s_pct) | set(t_pct)):
                sp, tp = s_pct.get(code, 0.0), t_pct.get(code, 0.0)
                ok = abs(sp - tp) <= args.tolerance
                field_pass &= ok
                rows.append({"value": code, "source_pct": round(sp, 3),
                             "tileset_pct": round(tp, 3), "ok": ok})
                print(f"  {code:>8} {sp:>9.3f}% {tp:>9.3f}% {abs(sp-tp):>8.3f}"
                      f"{'' if ok else '  MISMATCH'}")
        else:
            ss, ts = summary_stats(src_vals), summary_stats(tile_vals)
            field_pass, rows = True, []
            print(f"  {'stat':>8} {'source':>12} {'tileset':>12}")
            for k in ss:
                denom = max(abs(ss[k]), 1.0)
                ok = abs(ss[k] - ts[k]) / denom * 100 <= args.tolerance
                field_pass &= ok
                rows.append({"stat": k, "source": ss[k], "tileset": ts[k], "ok": ok})
                print(f"  {k:>8} {ss[k]:>12} {ts[k]:>12}{'' if ok else '  MISMATCH'}")
        print(f"  RESULT: {'PASS' if field_pass else 'FAIL'} "
              f"(tolerance {args.tolerance})")
        results[field] = {"status": "pass" if field_pass else "fail",
                          "mode": "distribution", "rows": rows}
        all_pass &= field_pass

    geometry = {"status": "skipped", "reason": "--no-geometry"}
    if not args.no_geometry:
        print("\n  -- positions (X, Y, Z) --")
        geometry = check_geometry(args.tiles_dir, args.source, args.srs_in, args.geometry_tolerance)
        if geometry["status"] == "skipped":
            print(f"  SKIP: {geometry['reason']}")
        else:
            print(f"  Tile points decoded : {geometry['tile_points']:,}   source points: {geometry['source_points']:,}")
            print(f"  {'axis':>5} {'source min':>15} {'tile min':>15} {'source max':>15} {'tile max':>15} {'max error':>11}")
            for name, a in geometry["axes"].items():
                err = a.get("max_sorted_error_m", a["extent_error_m"])
                print(f"  {name:>5} {a['source_min']:>15.3f} {a['tile_min']:>15.3f} "
                      f"{a['source_max']:>15.3f} {a['tile_max']:>15.3f} {err:>9.4f} m"
                      f"{'' if a['ok'] else '  MISMATCH'}")
            print(f"  RESULT: {geometry['status'].upper()} (tolerance {args.geometry_tolerance} m; "
                  "every sorted coordinate compared when counts match)")
            all_pass &= geometry["status"] == "pass"
        results["positions"] = geometry

    print("\n" + "=" * 78)
    print(f"OVERALL: {'PASS' if all_pass else 'FAIL'}")
    print("=" * 78)

    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "tileset_dir": args.tiles_dir,
            "source": args.source,
            "tileset_json": meta,
            "tile_summary": {k: v for k, v in tiles.items() if k != "attributes"},
            "fields": results,
            "overall_pass": bool(all_pass),
        }, indent=2, default=str))
        print(f"Validation JSON written to {p}")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
