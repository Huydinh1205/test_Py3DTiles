# py3dtiles Feasibility Test Harness

Test bench for the LiDAR half of Phil's "first principles" assessment
(email of 15 Oct 2026, *Before we proceed with purchasing FME*).

**Question being answered:** can processed LAS/LAZ point clouds be published
to BunjilView's CesiumJS viewer using py3dtiles plus a small amount of glue
code, instead of buying FME, and do the attributes we need survive the trip?

---

## The headline finding this harness is built around

py3dtiles can report a **successful conversion while silently writing the
classification attribute as all zeros**. The exit code is 0, the log is clean,
the point cloud renders beautifully, and every point is class 0.

The cause is a name mismatch inside py3dtiles' LAS reader
(`py3dtiles/reader/las_reader.py`):

| What the reader does | Which name list it uses |
|---|---|
| Discover available fields | `point_format.dtype().fields` |
| Read the values back out | `point_format.dimension_names` |

In **LAS 1.0 to 1.3, point formats 0 to 5**, the classification code is
bit-packed into a byte together with the synthetic / key-point / withheld
flags. laspy exposes that byte as `raw_classification` and exposes
`classification` only as a bit view of it. So the two lists never agree:

| Command on a LAS 1.2 pf3 file | Result |
|---|---|
| `--extra-fields classification` | warning `does not have the field`, attribute dropped |
| `--extra-fields raw_classification` | **no warning, attribute written as all zeros** |

In **LAS 1.4, point formats 6 and above**, classification is its own byte,
present under the same name in both lists, and
`--extra-fields classification` works correctly.

Verified against py3dtiles 12.1.1 by comparing value histograms before and
after conversion:

```
LAS 1.2 pf3, --extra-fields raw_classification
  source  : {1: 39854, 2: 39979, 3: 40016, 4: 40058, 5: 40093}
  tileset : {0: 211244}  (all tile files)     <- silent total loss

LAS 1.4 pf7, --extra-fields classification
  tileset : {1: 42080, 2: 42263, 3: 42296, 4: 42247, 5: 42358}   <- correct
```

**Consequences for the assessment**

1. Whether preprocessing is needed depends entirely on the point format DJI
   Terra writes. Check that first, do not assume either way. This is exactly
   Phil's "assess whether any additional preprocessing is actually needed
   rather than adding it by default".
2. If preprocessing is needed it is a lossless LAS 1.4 rewrite, roughly five
   lines of laspy. It does not justify a separate conversion platform.
3. The publishing service **must** validate output by reading attributes back
   out and comparing them against the source. Checking that the converter
   exited cleanly is not enough. This supports Phil's requirement that
   "incomplete or failed datasets must not become visible to customers".

---

## Install

Needs Python 3.9 or newer.

```bash
cd test_Py3DTiles
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`.laz` support comes from the `lazrs` backend in `requirements.txt`. Without
it, laspy can read `.las` only.

---

## Quick start

### 1. Optional: smoke test with synthetic data

Skip this if the real Terra output is already on the machine.

```bash
python scripts/make_sample_data.py --points 500000
python run_test.py data/sample_las14_pf7.las      # expect PASS
python run_test.py data/sample_las12_pf3.las      # expect PASS, after an automatic LAS 1.4 rewrite
```

To see the failure mode the harness exists to catch:

```bash
python run_test.py data/sample_las12_pf3.las --skip-prepare   # expect FAIL
```

Self-test results of the harness (py3dtiles 12.1.1, synthetic data):

| Scenario | Expected | Got |
|---|---|---|
| LAS 1.4 pf7, 300k points | PASS | PASS, exact match |
| LAS 1.2 pf3, automatic LAS 1.4 rewrite | PASS | PASS, exact match |
| LAS 1.2 pf3, `--skip-prepare` | FAIL | FAIL, classification dropped |
| LAS 1.2 pf3, `--skip-prepare --fields raw_classification` | FAIL | FAIL, all-zero output detected |
| LAS 1.4 pf7, `--spec-version 1.1` (.glb) | PASS | PASS, exact match |
| LAS 1.4 pf7, 3M points, 6 LOD levels | PASS | PASS, exact match, 14 s, 818 MB peak, output 0.5x source |
| COPC `.copc.laz` (LAS 1.4 pf7), 670k points | PASS | PASS, exact match, no preprocessing, 3.7 s |

The 3M-point timing was on a 2-core machine, so treat it as a floor, not
a forecast for the workstation.

### 2. Run it on a real dataset

Drop the DJI Terra output into `data/`, then:

```bash
python run_test.py data/your_dataset.las
```

Add `--srs-in 7856` if the file does not declare a CRS (step 1 will say so).
EPSG 7856 is GDA2020 MGA zone 56. Use the zone the survey was flown in.

### 3. Look at it in a browser

```bash
python -m http.server 8080
# then open http://localhost:8080/viewer/index.html
```

See `viewer/README_VIEWER.txt`.

---

## Run each step on its own

`run_test.py` simply calls the scripts below in order. Every one of them also
runs on its own, so each part can be tested and inspected separately. Replace
`TERRA` with the path to the `terra_lidar` folder.

**Step 0. Understand the Terra output folder** (no conversion, read only)

```bash
python scripts/00_compare_terra_files.py "TERRA"
```

Tells you whether `cloud0..N.las` are per-flight or per-tile, whether
`cloud_merged.las` is exactly their union, whether `cloud_merged.copc.laz` is
a faithful copy of it, and which file to convert.

**Step 1. Inspect one file**

```bash
python scripts/01_inspect_las.py "TERRA/cloud_merged.copc.laz"
```

LAS version, point format, CRS, classes present, and the verdict on whether
step 2 is needed.

**Step 2. Rewrite to LAS 1.4** (only if step 1 says so)

```bash
python scripts/02_prepare_las.py "TERRA/cloud_merged.las" -o results/cloud_merged_las14.las
```

**Step 3. Tiling.** This is the step that builds the 3D Tiles tileset

```bash
python scripts/03_convert_benchmark.py "TERRA/cloud_merged.copc.laz" \
    --out results/cloud_merged/tiles \
    --extra-fields classification intensity
```

Output: `results/cloud_merged/tiles/tileset.json` plus the tile tree under
`points/`. `--srs-in` is only needed if step 1 reported no CRS.

**Step 4. Validate the tiles against the source**

```bash
python scripts/04_validate_tileset.py results/cloud_merged/tiles \
    --source "TERRA/cloud_merged.copc.laz" \
    --fields classification intensity
```

**Step 5. Look at it**

```bash
python -m http.server 8080
# open http://localhost:8080/viewer/index.html
# load ../results/cloud_merged/tiles/tileset.json
```

On Windows PowerShell, replace the trailing `\` line continuations with a
backtick, or put each command on one line.

## What each piece does

| File | Role |
|---|---|
| `run_test.py` | Runs all four steps and prints a paste-ready summary table |
| `scripts/00_compare_terra_files.py` | Reads a whole DJI Terra output folder and explains the relationship between the numbered files, the merged file and the COPC copy. Recommends which file to convert |
| `scripts/01_inspect_las.py` | Reads the header. Decides whether preprocessing is needed, flags unclassified data, 8-bit RGB, missing CRS, all-zero intensity |
| `scripts/02_prepare_las.py` | Lossless rewrite to LAS 1.4 pf6/7. Chunked, so a multi-gigabyte file does not need to fit in RAM. `--verify` confirms coordinates and classification are bit-identical |
| `scripts/03_convert_benchmark.py` | Runs `py3dtiles convert`, measures wall time, peak memory across all worker processes, output size, tile count, LOD depth |
| `scripts/04_validate_tileset.py` | Reads attributes back out of the `.pnts` batch tables or `.glb` custom attributes and compares the distribution against the source. This is the step that catches silent loss |
| `scripts/make_sample_data.py` | Synthetic LAS pairs for testing the harness itself |
| `viewer/index.html` | CesiumJS inspector: colour by RGB, classification or intensity, toggle classes, read frames per second and GPU memory. Detects the attributes in the tileset and warns if classification is missing. Handles both 1.0 and 1.1 tilesets. No Cesium ion token required |

Each script runs standalone with `--help` if you want to drive the steps by
hand rather than through `run_test.py`.

---

## How validation compares the tileset with the source

py3dtiles 12.1.1 writes two **overview** tiles at the top of the tree, the
root content (`preview.pnts`) and the root's only child (`points/r.pnts`).
Their points are copies of points held further down. Every other tile
together partitions the source **exactly**, each point appearing once.

Script 04 reads `tileset.json` to identify the two overview tiles, excludes
them, and then compares the sorted attribute values against the sorted source
values element by element. Verified on trees up to 6 levels and ~500 tiles,
for both 3D Tiles 1.0 and 1.1:

```
Mode   : EXACT (3,000,000 source points, 3,000,000 tile points)
Every source value is present in the tileset exactly once.
```

So the check is not "roughly the same distribution", it is "no value was
lost, altered or duplicated". If a future py3dtiles version changes the
layout and the counts stop lining up, the script falls back to a
distribution comparison with a tolerance and says so in its output.

If you ever count points across every file in the output and get slightly
more than the source, that is the overview tiles, not a defect.

## Flags that matter

| Flag | Why |
|---|---|
| `--srs_out 4978` | ECEF. CesiumJS requires it. The harness sets this by default, do not change it |
| `--srs_in <epsg>` | Numeric part only, e.g. `7856`, not `EPSG:7856`. Required when the file declares no CRS |
| `--extra-fields <name>` | Repeat per attribute. Each one adds bytes per point. In testing, adding classification and intensity grew the tileset about 21 percent |
| `--color_scale 256` | Needed when source RGB is 8-bit, otherwise the tileset renders black. Step 1 detects this and `run_test.py` applies it automatically |
| `--spec-version 1.1` | Emits `.glb` instead of `.pnts`. **Do not use for BunjilView yet**, see below |
| `--jobs` | Defaults to all cores in this harness. py3dtiles' own default is 2, which understates throughput |

---

## .laz and .copc.laz input: a hang the harness works around

Plain `py3dtiles convert` hung indefinitely on Linux when given a `.laz` or
`.copc.laz` file, while the same points as `.las` converted in about 2
seconds. Cause: laspy defaults to the multi-threaded `lazrs` decoder, which
starts a thread pool in the parent process when py3dtiles reads the file
metadata. py3dtiles then forks its worker processes, and a forked child
inherits the pool's state but not its threads, so it deadlocks on the first
decompression.

`03_convert_benchmark.py` forces the single-threaded decoder in the
py3dtiles subprocess, which fixes it. Windows and macOS start workers with
spawn rather than fork, so they should not hit this, but a production
publishing service on a Linux worker would. Record it in the assessment as a
small, known integration detail for Tri's team, not a blocker.

Tested: a 670,000-point COPC file converts in 3.7 s with an exact
attribute match after the fix.

## 3D Tiles 1.0 vs 1.1: use 1.0

Tested in CesiumJS 1.145.0 with the viewer in this folder. The data inside
the 1.1 tiles is correct (script 04 confirms an exact match), but Cesium
does not display it correctly without custom code:

| | 1.0 (`.pnts`, default) | 1.1 (`.glb`) |
|---|---|---|
| Source RGB | Correct | **Renders as plain white points.** py3dtiles writes `COLOR_0` as unsigned bytes with `normalized: false`, which glTF 2.0 does not allow for colours, and uses a lit PBR material |
| Style by `${classification}` | Works | **Does not work under any name.** Attributes are plain glTF vertex attributes (`_CLASSIFICATION`) with no metadata extension, so Cesium's declarative style language cannot see them |
| Workaround | None needed | An UNLIT `CustomShader` reading `fsInput.attributes.color_0 / 255.0` and `fsInput.attributes.classification`. The viewer here applies it automatically |
| Output size | baseline | about the same in testing |

Choosing 1.1 would therefore add CustomShader work to the BunjilView
viewer for no measurable benefit. Recommend 1.0 until py3dtiles writes
normalised colours and structural metadata.

## Still open

Things this harness deliberately does not answer, and which belong in the
assessment as open items:

- **Vertical accuracy.** Needs a comparison against survey control points,
  not something a converter test can establish.
- **Is the source even classified?** If DJI Terra output carries only codes
  0 and 1, the preservation question is moot and the gap is upstream in
  post-processing. Step 1 flags this.
- **Does BunjilView need intensity at all?** Confirm with the analysts
  before paying for it in tileset size.
- **Real-world viewer performance.** The numbers here are measured with the
  globe and imagery disabled, so they are an optimistic upper bound.
- **`merge` for incremental publishing.** py3dtiles can build a meta-tileset
  and rebuild only the changed part, which fits the publishing service
  design. Not yet tested here.
