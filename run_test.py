#!/usr/bin/env python3
"""
One command end-to-end test of the py3dtiles publishing path.

Chains the four steps and prints a summary table that can be pasted straight
into the feasibility assessment:

  1. inspect the LAS file and decide whether preprocessing is needed
  2. rewrite to LAS 1.4 pf7 only if step 1 says so
  3. convert to 3D Tiles, measuring time, peak memory and output size
  4. read the attributes back out of the tiles and compare with the source

Usage
-----
    python run_test.py data/mydataset.las
    python run_test.py data/mydataset.las --srs-in 7856
    python run_test.py data/mydataset.las --fields classification
    python run_test.py data/mydataset.las --spec-version 1.1

Every artefact lands in results/<dataset name>/ so several datasets can be
tested side by side without overwriting each other.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"


def human_bytes(n):
    if n is None:
        return "n/a"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def run_step(title, cmd, allow_failure=False):
    print("\n" + "#" * 78)
    print(f"# {title}")
    print("#" * 78)
    proc = subprocess.run([sys.executable] + cmd)
    if proc.returncode != 0 and not allow_failure:
        print(f"\nStep failed: {title}")
        sys.exit(proc.returncode)
    return proc.returncode


def main():
    ap = argparse.ArgumentParser(description="End-to-end py3dtiles feasibility test.")
    ap.add_argument("las", help="Input .las or .laz file")
    ap.add_argument("--srs-in", help="Input EPSG, numeric part only. "
                                     "Needed only if the file declares no CRS.")
    ap.add_argument("--fields", nargs="*", default=["classification", "intensity"],
                    help="Attributes to carry through and verify")
    ap.add_argument("--spec-version", choices=["1.0", "1.1"], default="1.0")
    ap.add_argument("--jobs", type=int, help="py3dtiles parallel workers")
    ap.add_argument("--color-scale", type=int, help="Pass 256 if RGB is 8-bit")
    ap.add_argument("--out-root", default=str(ROOT / "results"),
                    help="Folder that receives all outputs (default: results/ inside this "
                         "harness). Each run gets its own subfolder <out-root>/<name>/. "
                         "Nothing is ever written next to the input file.")
    ap.add_argument("--name",
                    help="Results subfolder name (default: the input file name). "
                         "Use different names to keep several runs of the same file.")
    ap.add_argument("--keep-prepared", action="store_true",
                    help="Keep the intermediate LAS 1.4 file written by step 2 "
                         "(deleted after tiling by default, it can be several hundred MB)")
    ap.add_argument("--skip-prepare", action="store_true",
                    help="Do not rewrite to LAS 1.4 even if step 1 recommends it. "
                         "Use this to demonstrate the failure mode.")
    args = ap.parse_args()

    src = Path(args.las).resolve()
    if not src.exists():
        sys.exit(f"Input file not found: {src}")

    out_root = Path(args.out_root).resolve()
    run_dir = out_root / (args.name or src.stem)
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    # ---------------- Step 1: inspect ----------------
    inspect_json = run_dir / "01_inspect.json"
    run_step(
        "STEP 1 of 4  Inspect the source LAS file",
        [str(SCRIPTS / "01_inspect_las.py"), str(src), "--json", str(inspect_json)],
    )
    inspect = json.loads(inspect_json.read_text())

    # Fall back to the EPSG declared in the file if none was given.
    srs_in = args.srs_in
    if not srs_in and inspect.get("crs") and inspect["crs"].get("epsg"):
        srs_in = str(inspect["crs"]["epsg"])
        print(f"\nUsing EPSG {srs_in} read from the file header.")
    if not srs_in:
        print("\nWARNING: no CRS in the file and no --srs-in given.")
        print("         py3dtiles will refuse to reproject. Re-run with --srs-in.")

    # ---------------- Step 2: prepare (conditional) ----------------
    convert_input = src
    prepared = False
    if inspect["preprocessing_required"] and not args.skip_prepare:
        prepared_path = run_dir / f"{src.stem}_las14.las"
        run_step(
            "STEP 2 of 4  Rewrite as LAS 1.4 pf7 (required by step 1)",
            [str(SCRIPTS / "02_prepare_las.py"), str(src), "-o", str(prepared_path)],
        )
        convert_input = prepared_path
        prepared = True
    elif inspect["preprocessing_required"] and args.skip_prepare:
        print("\n" + "#" * 78)
        print("# STEP 2 of 4  SKIPPED on request")
        print("#" * 78)
        print("Step 1 said preprocessing is required. Skipping it means")
        print("classification will not survive. Step 4 is expected to FAIL.")
    else:
        print("\n" + "#" * 78)
        print("# STEP 2 of 4  Not needed, source point format already supports")
        print("#              direct classification export")
        print("#" * 78)

    # ---------------- Step 3: convert and benchmark ----------------
    tiles_dir = run_dir / "tiles"
    bench_json = run_dir / "03_benchmark.json"
    cmd = [
        str(SCRIPTS / "03_convert_benchmark.py"), str(convert_input),
        "--out", str(tiles_dir),
        "--spec-version", args.spec_version,
        "--json", str(bench_json),
    ]
    if srs_in:
        cmd += ["--srs-in", str(srs_in)]
    if args.jobs:
        cmd += ["--jobs", str(args.jobs)]
    if args.color_scale:
        cmd += ["--color-scale", str(args.color_scale)]
    elif inspect.get("rgb") and inspect["rgb"].get("likely_8bit"):
        cmd += ["--color-scale", "256"]
        print("\nAuto-applying --color-scale 256 (step 1 detected 8-bit RGB).")
    if args.fields:
        cmd += ["--extra-fields"] + list(args.fields)

    if args.skip_prepare and inspect.get("laz_variable_chunks"):
        cmd.append("--allow-variable-chunks")

    run_step("STEP 3 of 4  Convert to 3D Tiles and benchmark", cmd)
    bench = json.loads(bench_json.read_text())

    if prepared and not args.keep_prepared:
        size = convert_input.stat().st_size
        convert_input.unlink()
        print(f"\nDeleted intermediate {convert_input.name} ({human_bytes(size)}). "
              "Use --keep-prepared to keep it.")

    # ---------------- Step 4: validate ----------------
    validate_json = run_dir / "04_validation.json"
    rc = run_step(
        "STEP 4 of 4  Read attributes back out of the tiles and verify",
        [
            str(SCRIPTS / "04_validate_tileset.py"), str(tiles_dir),
            "--source", str(src),
            "--fields", *args.fields,
            "--json", str(validate_json),
        ] + (["--srs-in", str(srs_in)] if srs_in else []),
        allow_failure=True,
    )
    validation = json.loads(validate_json.read_text())

    # ---------------- Summary ----------------
    total = time.time() - started
    src_bytes = src.stat().st_size

    print("\n\n" + "=" * 78)
    print("SUMMARY  (paste-ready for the feasibility assessment)")
    print("=" * 78)
    rows = [
        ("Dataset", src.name),
        ("Source size", human_bytes(src_bytes)),
        ("Point count", f"{inspect['point_count']:,}"),
        ("LAS version / point format",
         f"{inspect['las_version']} / pf{inspect['point_format']}"),
        ("Source CRS",
         (inspect["crs"]["name"] if inspect.get("crs") else "not declared")),
        ("EPSG used for conversion", srs_in or "none"),
        ("Classified", "yes" if inspect["is_classified"] else "NO (codes 0/1 only)"),
        ("Preprocessing required",
         ("yes, COPC -> plain .las rewrite" if inspect.get("laz_variable_chunks")
          else "yes, LAS 1.4 rewrite") if inspect["preprocessing_required"] else "no"),
        ("Preprocessing applied", "yes" if prepared else "no"),
        ("3D Tiles spec version", bench.get("spec_version")),
        ("Conversion time", f"{bench.get('elapsed_seconds')} s"),
        ("Peak memory", human_bytes(bench.get("peak_memory_bytes"))),
        ("Output size", human_bytes(bench.get("output_bytes"))),
        ("Output / source ratio", f"{bench.get('size_ratio')}x"),
        ("Tile files", str(bench.get("tile_file_counts"))),
        ("LOD levels", str(bench.get("tileset_depth"))),
        ("Workers used", str(bench.get("jobs"))),
    ]
    for field, result in validation.get("fields", {}).items():
        label = "Positions X/Y/Z" if field == "positions" else f"Attribute '{field}'"
        rows.append((label, result.get("status", "?").upper()))
    rows.append(("OVERALL VALIDATION",
                 "PASS" if validation.get("overall_pass") else "FAIL"))
    rows.append(("Total harness runtime", f"{total:.1f} s"))

    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key:<{width}} : {value}")

    print("\n  Artefacts in: " + str(run_dir))
    print("    01_inspect.json     source file properties and verdict")
    print("    03_benchmark.json   timing, memory, output size")
    print("    04_validation.json  per-attribute pass/fail detail")
    print("    tiles/              the tileset itself")
    print("\n  To eyeball it in a browser, from the test_Py3DTiles folder run:")
    print(f'    python scripts/serve_viewer.py --results "{out_root}"')
    print("  then open:")
    print(f"    http://localhost:8080/viewer/index.html?tileset=../results/{run_dir.name}/tiles/tileset.json")
    print("=" * 78)

    sys.exit(rc)


if __name__ == "__main__":
    main()
