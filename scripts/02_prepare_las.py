#!/usr/bin/env python3
"""
Step 2 - Rewrite a legacy LAS file as LAS 1.4 point format 6/7.

Only run this if 01_inspect_las.py reported "Preprocessing needed: True".

Why this is necessary
---------------------
In LAS point formats 0-5 the classification code shares a single byte with
the synthetic / key-point / withheld flags. laspy exposes that byte as
'raw_classification' and exposes 'classification' only as a bit view of it.

py3dtiles builds its list of available extra fields from
point_format.dtype().fields (which contains 'raw_classification') but then
reads values by testing point_format.dimension_names (which contains
'classification'). The names never match, so the field is either dropped
with a warning or, worse, written as all zeros with no warning at all.

LAS 1.4 point formats 6 and above store classification as its own byte,
present under the same name in both lists, so py3dtiles reads it correctly.

This script performs a lossless rewrite:
  - point format 7 if the source has RGB, otherwise point format 6
  - scales and offsets are carried over unchanged, so coordinates are
    bit-identical, no resampling and no precision loss
  - the CRS (VLR/EVLR) is carried over
  - classification, intensity, return numbers, GPS time and RGB are copied
  - processed in chunks so a multi-gigabyte file does not need to fit in RAM

Usage
-----
    python scripts/02_prepare_las.py data/input.las
    python scripts/02_prepare_las.py "D:/survey/terra_lidar/cloud_merged.las" -o results/cloud_merged_las14.las
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

try:
    import laspy
except ImportError:
    sys.exit("laspy is not installed. Run: pip install -r requirements.txt")


# Dimensions worth copying. Anything not listed here is intentionally dropped
# because it has no use in a web viewer and only inflates the output.
DIMENSIONS_TO_COPY = [
    "intensity",
    "return_number",
    "number_of_returns",
    "classification",
    "scan_direction_flag",
    "edge_of_flight_line",
    "user_data",
    "point_source_id",
    "gps_time",
    "red",
    "green",
    "blue",
]

CHUNK_SIZE = 2_000_000  # points per chunk


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _laz_variable_chunks(header):
    """See 01_inspect_las.py: COPC / variable-size-chunk LAZ cannot be seeked."""
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


def _rewrite_same_format(reader, src_header, dst_path, started):
    """Copy every point record unchanged into an uncompressed LAS 1.4 file.

    Reads the source sequentially (chunk_iterator, no seek), which is correct
    for COPC. The output is plain LAS, so any later seek is a simple offset.
    """
    pf = src_header.point_format
    dst_header = laspy.LasHeader(version="1.4", point_format=pf.id)
    dst_header.scales = src_header.scales
    dst_header.offsets = src_header.offsets
    for extra in pf.extra_dimensions:
        dst_header.add_extra_dim(laspy.ExtraBytesParams(name=extra.name, type=extra.dtype))
    try:
        crs = src_header.parse_crs()
        if crs is not None:
            dst_header.add_crs(crs)
            print(f"CRS    : {crs.name} (carried over)")
    except Exception as exc:
        print(f"CRS    : could not be carried over ({exc}), pass --srs_in manually")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with laspy.open(str(dst_path), mode="w", header=dst_header, do_compress=False) as writer:
        for chunk in reader.chunk_iterator(CHUNK_SIZE):
            writer.write_points(chunk)
            written += len(chunk)
            print(f"  {written:,} / {src_header.point_count:,} points", end="\r")
    print(" " * 60, end="\r")
    print(f"Done in {time.perf_counter() - started:.1f} s")
    print(f"Output : {dst_path}  ({human_bytes(dst_path.stat().st_size)})")
    return dst_path


def prepare(src_path, dst_path):
    src_path, dst_path = Path(src_path), Path(dst_path)
    started = time.perf_counter()

    with laspy.open(str(src_path)) as reader:
        src_header = reader.header
        src_pf = src_header.point_format
        has_rgb = "red" in src_pf.dimension_names

        variable_chunks = _laz_variable_chunks(src_header)
        if int(src_pf.id) >= 6 and not variable_chunks:
            print(
                f"Source is already point format {src_pf.id}. "
                "No rewrite needed, step 2 can be skipped."
            )
            return None

        if int(src_pf.id) >= 6:
            # Modern point format, but variable-size LAZ chunks (COPC).
            # Keep the point format and copy records verbatim: lossless.
            print("Source uses variable-size LAZ chunks (COPC), which py3dtiles cannot")
            print("seek in. Rewriting as uncompressed LAS 1.4, same point format.")
            return _rewrite_same_format(reader, src_header, dst_path, started)

        target_pf = 7 if has_rgb else 6
        print(f"Source : LAS {src_header.version} point format {src_pf.id}")
        print(f"Target : LAS 1.4 point format {target_pf}")
        print(f"Points : {src_header.point_count:,}")

        # Build the new header, preserving the exact coordinate encoding.
        dst_header = laspy.LasHeader(version="1.4", point_format=target_pf)
        dst_header.scales = src_header.scales
        dst_header.offsets = src_header.offsets

        # Carry the CRS across so py3dtiles can still read it from the file.
        try:
            crs = src_header.parse_crs()
            if crs is not None:
                dst_header.add_crs(crs)
                print(f"CRS    : {crs.name} (carried over)")
            else:
                print("CRS    : not declared in source, pass --srs_in to py3dtiles")
        except Exception as exc:
            print(f"CRS    : could not be carried over ({exc}), pass --srs_in manually")

        # Only copy dimensions that actually exist in the source and the target.
        dims = [
            d
            for d in DIMENSIONS_TO_COPY
            if d in src_pf.dimension_names
            and d in dst_header.point_format.dimension_names
        ]
        print(f"Copying dimensions: {', '.join(dims)}")

        # Copy any user-defined extra dimensions too.
        for extra in src_pf.extra_dimensions:
            dst_header.add_extra_dim(
                laspy.ExtraBytesParams(name=extra.name, type=extra.dtype)
            )
            print(f"  carried over extra dimension: {extra.name}")
        extra_names = list(src_pf.extra_dimension_names)

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with laspy.open(str(dst_path), mode="w", header=dst_header) as writer:
            for chunk in reader.chunk_iterator(CHUNK_SIZE):
                out = laspy.ScaleAwarePointRecord.zeros(
                    len(chunk),
                    header=dst_header,
                )
                # Copy raw integer coordinates directly. Because scales and
                # offsets match, this is exact with no floating point round trip.
                out.X = chunk.X
                out.Y = chunk.Y
                out.Z = chunk.Z
                for d in dims:
                    setattr(out, d, getattr(chunk, d))
                for d in extra_names:
                    setattr(out, d, getattr(chunk, d))
                writer.write_points(out)
                written += len(chunk)
                print(f"  {written:,} / {src_header.point_count:,} points", end="\r")

    elapsed = time.perf_counter() - started
    print(" " * 60, end="\r")
    print(f"Done in {elapsed:.1f} s")
    print(f"Output : {dst_path}  ({human_bytes(dst_path.stat().st_size)})")
    return dst_path


def verify(src_path, dst_path):
    """Confirm the rewrite did not alter geometry or classification."""
    print("\nVerifying rewrite...")
    src = laspy.read(str(src_path))
    dst = laspy.read(str(dst_path))

    ok = True
    if len(src.points) != len(dst.points):
        print(f"  FAIL point count {len(src.points):,} vs {len(dst.points):,}")
        ok = False
    else:
        print(f"  OK   point count {len(dst.points):,}")

    for axis in ("X", "Y", "Z"):
        if np.array_equal(getattr(src, axis), getattr(dst, axis)):
            print(f"  OK   {axis} coordinates identical")
        else:
            print(f"  FAIL {axis} coordinates differ")
            ok = False

    for dim in ("classification", "intensity"):
        if dim in src.point_format.dimension_names:
            if np.array_equal(
                np.asarray(getattr(src, dim)), np.asarray(getattr(dst, dim))
            ):
                print(f"  OK   {dim} identical")
            else:
                print(f"  FAIL {dim} differs")
                ok = False

    print("  RESULT:", "lossless" if ok else "MISMATCH, do not use this output")
    return ok


def main():
    ap = argparse.ArgumentParser(
        description="Rewrite a legacy LAS file as LAS 1.4 pf 6/7 so py3dtiles can export classification."
    )
    ap.add_argument("las", help="Source .las or .laz file")
    ap.add_argument("-o", "--out", help="Output path (default: results/<input name>_las14.las inside this harness)")
    ap.add_argument(
        "--verify",
        action="store_true",
        help="Read both files fully afterwards and confirm the rewrite is lossless. "
             "Needs enough RAM for the whole file, so use it on the small dataset only.",
    )
    args = ap.parse_args()

    src = Path(args.las)
    # Default to this harness's results/ folder, never next to the source.
    # The source usually lives in a shared company folder (e.g. terra_lidar)
    # that a test run must not write into.
    if args.out:
        dst = Path(args.out)
    else:
        stem = src.name
        for suffix in (".copc.laz", ".laz", ".las"):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        dst = Path(__file__).resolve().parent.parent / "results" / f"{stem}_las14.las"

    result = prepare(src, dst)
    if result and args.verify:
        verify(src, dst)


if __name__ == "__main__":
    main()
