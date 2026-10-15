#!/usr/bin/env python3
"""
Step 3 - Run py3dtiles convert (the tiling step) and measure it.

Captures, for each run:
  - wall clock processing time
  - peak resident memory across py3dtiles and all of its worker processes
  - output size on disk, and the ratio against the source file
  - number of tiles produced and the tileset tree depth
  - the exact command line used, so the number is reproducible

Usage
-----
    python scripts/03_convert_benchmark.py data/input_las14.las \
        --out results/tiles_input --srs-in 28356 --extra-fields classification intensity

    # 3D Tiles 1.1 (glTF output) instead of the 1.0 .pnts default
    python scripts/03_convert_benchmark.py data/input_las14.las \
        --out results/tiles_11 --srs-in 28356 --spec-version 1.1

Notes
-----
--srs-out defaults to 4978 (ECEF). CesiumJS requires the tileset in 4978,
so do not change this unless you know why.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None


# Entry point for the py3dtiles subprocess.
#
# The laspy override fixes a hang on .laz / .copc.laz input. laspy defaults to
# the multi-threaded lazrs decoder, which starts a thread pool in the parent
# process when py3dtiles reads the file metadata. py3dtiles then forks its
# worker processes; on Linux a forked child inherits the pool's state but not
# its threads, and deadlocks the first time it decompresses. Verified: plain
# py3dtiles hung indefinitely on a 300k-point .laz that converts in 2 s once
# the single-threaded decoder is forced. Windows and macOS spawn rather than
# fork so are not affected, but the override is harmless there. py3dtiles
# already runs one worker per core, so no throughput is lost.
PY3DTILES_ENTRY = (
    "import sys\n"
    "try:\n"
    "    import laspy\n"
    "    laspy.LazBackend.detect_available = staticmethod(lambda: (laspy.LazBackend.Lazrs,))\n"
    "except Exception:\n"
    "    pass\n"
    "from py3dtiles.command_line import main\n"
    "sys.argv[0] = 'py3dtiles'\n"
    "sys.exit(main())\n"
)


def laz_variable_chunks(path):
    """True for COPC and any other LAZ whose chunks vary in size.

    py3dtiles reads by seeking to 100,000-point offsets and laspy cannot seek
    correctly in such files: everything after the first chunk comes back as
    zeros, silently. See 01_inspect_las.py. Plain .las and fixed-chunk .laz
    are fine.
    """
    try:
        import laspy
        from lazrs import LazVlr
        with laspy.open(str(path)) as f:
            for vlr in f.header.vlrs:
                if getattr(vlr, "user_id", "").strip("\x00") == "laszip encoded":
                    return bool(LazVlr(vlr.record_data).uses_variable_size_chunks())
    except Exception:
        pass
    return False


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def dir_stats(path):
    """Total size, file count and per-extension counts for the output tree."""
    total = 0
    counts = {}
    for p in Path(path).rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            counts[p.suffix or "(none)"] = counts.get(p.suffix or "(none)", 0) + 1
    return total, counts


def tileset_depth(tileset_json):
    """Maximum depth of the tileset tree, a rough proxy for LOD levels."""
    try:
        data = json.loads(Path(tileset_json).read_text())
    except Exception:
        return None

    def walk(node, depth=1):
        children = node.get("children") or []
        if not children:
            return depth
        return max(walk(c, depth + 1) for c in children)

    root = data.get("root")
    return walk(root) if root else None


class PeakMemorySampler(threading.Thread):
    """Poll RSS of the process tree so we capture worker processes too."""

    def __init__(self, pid, interval=0.2):
        super().__init__(daemon=True)
        self.pid = pid
        self.interval = interval
        self.peak_bytes = 0
        self._stop = threading.Event()

    def run(self):
        if psutil is None:
            return
        try:
            proc = psutil.Process(self.pid)
        except psutil.NoSuchProcess:
            return
        while not self._stop.is_set():
            try:
                total = proc.memory_info().rss
                for child in proc.children(recursive=True):
                    try:
                        total += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                self.peak_bytes = max(self.peak_bytes, total)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


def rusage_peak_bytes():
    """Fallback peak memory when psutil is unavailable."""
    try:
        import resource
    except ImportError:
        return None  # Windows has no resource module
    maxrss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # macOS reports bytes, Linux reports kilobytes.
    return maxrss if sys.platform == "darwin" else maxrss * 1024


def run_convert(args):
    src = Path(args.las)
    out = Path(args.out)

    if not src.exists():
        sys.exit(f"Input file not found: {src}")
    if laz_variable_chunks(src) and not args.allow_variable_chunks:
        sys.exit(
            f"Refusing to convert {src.name}: it is a LAZ file with variable-size chunks "
            "(COPC). py3dtiles seeks within the file and laspy's seek on such files "
            "silently returns zeros, so the tileset would be garbage with no error. "
            "Run 02_prepare_las.py first and convert the plain .las it writes. "
            "(--allow-variable-chunks overrides, only to demonstrate the bug.)"
        )
    if out.exists() and not args.keep:
        shutil.rmtree(out)
    # py3dtiles creates the output folder itself but not its parents.
    out.parent.mkdir(parents=True, exist_ok=True)

    # py3dtiles has no __main__ module, so "python -m py3dtiles" fails.
    # Call its console entry point through the current interpreter instead.
    # This works inside any venv and on Windows without relying on PATH.
    cmd = [
        sys.executable, "-c", PY3DTILES_ENTRY, "convert", str(src),
        "--out", str(out),
        "--srs_out", str(args.srs_out),
        "--jobs", str(args.jobs),
        "--overwrite",
    ]
    if args.srs_in:
        cmd += ["--srs_in", str(args.srs_in)]
    if args.cache_size:
        cmd += ["--cache_size", str(args.cache_size)]
    if args.color_scale:
        cmd += ["--color_scale", str(args.color_scale)]
    if args.spec_version:
        cmd += ["--spec-version", str(args.spec_version)]
    for field in args.extra_fields or []:
        cmd += ["--extra-fields", field]

    print("=" * 78)
    print("PY3DTILES CONVERSION BENCHMARK")
    print("=" * 78)
    print(f"  Machine   : {platform.platform()}")
    print(f"  CPU count : {os.cpu_count()}")
    print(f"  Input     : {src.name}  ({human_bytes(src.stat().st_size)})")
    print(f"  Command   : py3dtiles {' '.join(cmd[3:])}")
    if psutil is None:
        print("  NOTE      : psutil not installed, peak memory will be approximate")
    print("-" * 78)

    started = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    sampler = PeakMemorySampler(proc.pid)
    sampler.start()

    stdout_lines = []
    for line in proc.stdout:
        stdout_lines.append(line.rstrip())
        print("  |", line.rstrip())
    proc.wait()
    sampler.stop()
    elapsed = time.perf_counter() - started

    peak = sampler.peak_bytes or rusage_peak_bytes()

    if proc.returncode != 0:
        print("-" * 78)
        print(f"  CONVERSION FAILED (exit code {proc.returncode})")
        return {
            "status": "failed",
            "returncode": proc.returncode,
            "command": cmd,
            "stdout": stdout_lines,
        }

    out_bytes, ext_counts = dir_stats(out)
    src_bytes = src.stat().st_size
    depth = tileset_depth(out / "tileset.json")

    # Silent-failure detector. py3dtiles prints this warning and then drops
    # the field, which is easy to miss in a long log.
    dropped = [ln for ln in stdout_lines if "does not have the field" in ln]

    result = {
        "status": "ok",
        "command": cmd,
        "input_file": str(src),
        "input_bytes": src_bytes,
        "output_dir": str(out),
        "output_bytes": out_bytes,
        "size_ratio": round(out_bytes / src_bytes, 3) if src_bytes else None,
        "elapsed_seconds": round(elapsed, 2),
        "peak_memory_bytes": peak,
        "tile_file_counts": ext_counts,
        "tileset_depth": depth,
        "spec_version": args.spec_version or "1.0",
        "extra_fields_requested": args.extra_fields or [],
        "dropped_field_warnings": dropped,
        "machine": platform.platform(),
        "cpu_count": os.cpu_count(),
        "jobs": args.jobs,
    }

    print("-" * 78)
    print(f"  Processing time : {elapsed:.1f} s")
    if peak:
        print(f"  Peak memory     : {human_bytes(peak)}")
    print(f"  Output size     : {human_bytes(out_bytes)}  "
          f"({result['size_ratio']}x the source file)")
    print(f"  Tile files      : {ext_counts}")
    print(f"  Tileset depth   : {depth} levels")
    if dropped:
        print("\n  WARNING: py3dtiles dropped one or more requested fields:")
        for ln in dropped:
            print(f"    {ln}")
        print("  Run 04_validate_tileset.py before trusting this output.")
    print("=" * 78)
    return result


def main():
    ap = argparse.ArgumentParser(description="Benchmark a py3dtiles conversion.")
    ap.add_argument("las", help="Input .las or .laz file")
    ap.add_argument("--out", required=True, help="Output tileset directory")
    ap.add_argument("--srs-in", help="Input EPSG code, numeric part only (e.g. 28356). "
                                     "Required if the file has no CRS.")
    ap.add_argument("--srs-out", default=4978,
                    help="Output EPSG. Keep 4978 (ECEF) for CesiumJS.")
    ap.add_argument("--extra-fields", nargs="*", default=[],
                    help="Attributes to carry into the tileset, e.g. classification intensity")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2,
                    help="Parallel worker count (default: all cores)")
    ap.add_argument("--cache-size", type=int, help="Cache size in MB")
    ap.add_argument("--color-scale", type=int,
                    help="Set to 256 if step 1 flagged 8-bit RGB")
    ap.add_argument("--spec-version", choices=["1.0", "1.1"],
                    help="3D Tiles spec version. 1.1 emits .glb instead of .pnts")
    ap.add_argument("--allow-variable-chunks", action="store_true",
                    help="Convert a COPC / variable-chunk LAZ anyway. Produces a broken "
                         "tileset; only for demonstrating the bug.")
    ap.add_argument("--keep", action="store_true",
                    help="Do not delete an existing output directory first")
    ap.add_argument("--json", help="Write the benchmark result to this JSON path")
    args = ap.parse_args()

    result = run_convert(args)

    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, indent=2))
        print(f"\nBenchmark JSON written to {p}")

    sys.exit(0 if result["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
