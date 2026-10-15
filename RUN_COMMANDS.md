# Copy-paste commands (Windows PowerShell)

Every block below can be pasted into PowerShell as is. Run **block A once**,
then **block B every time you open a new terminal**, then any of the steps.

Block B is the only place with paths. Fill it in once, keep your filled-in
copy outside the repo (for example as `RUN_COMMANDS.local.md`, which git
ignores), and never commit real paths.

The source data is read where it is, nothing is copied. All outputs go to
`results\` inside this folder, which git also ignores.

---

## A. One-time setup

Only the first time, or after moving the folder (a venv cannot be moved,
it must be recreated).

```powershell
cd "<path to test_Py3DTiles>"

# Python 3.11 (NOT 3.14, see README "Install")
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# Sanity check: should print 12.1.1
.venv\Scripts\python.exe -c "import py3dtiles; print(py3dtiles.__version__)"
```

---

## B. Every new terminal: set the paths

```powershell
cd "<path to test_Py3DTiles>"
$PY   = ".venv\Scripts\python.exe"
$DATA = "<path to the DJI Terra terra_las folder>"
$FILE = "$DATA\cloud_merged.copc.laz"
```

---

## 0. What are the files in the Terra folder?

Read only, nothing is written next to the data.

```powershell
& $PY scripts\00_compare_terra_files.py $DATA --json results\00_terra_folder.json
```

## 1. Inspect the file: header, classes, every attribute

This is the output to read when deciding which attributes to keep.

```powershell
& $PY scripts\01_inspect_las.py $FILE --json results\01_inspect.json
```

Look at the table **All dimensions in the file**:
keep an attribute only if `export` is `yes`, it is not marked `constant`,
and the analysts actually use it in the viewer.

## 2. Full test, classification only

Tiling + timing + memory + exact validation, in one go. Output:
`results\merged_class\`.

Because the input is COPC, `run_test.py` first rewrites it as a plain
`.las` inside `results\` (about 600 MB for 18.8M points, deleted again after
tiling unless you pass `--keep-prepared`). The conversion time it reports is
for the tiling step only; the rewrite time is printed separately.

```powershell
& $PY run_test.py $FILE --fields classification --name merged_class
```

## 3. Full test, classification + intensity

Output: `results\merged_class_int\`.

```powershell
& $PY run_test.py $FILE --fields classification intensity --name merged_class_int
```

## 4. What does intensity cost?

Compares run 2 and run 3.

```powershell
$a = Get-Content results\merged_class\03_benchmark.json -Raw | ConvertFrom-Json
$b = Get-Content results\merged_class_int\03_benchmark.json -Raw | ConvertFrom-Json
"classification only        : {0,8:N1} MB  {1,7:N1} s  peak {2,6:N0} MB" -f ($a.output_bytes/1MB), $a.elapsed_seconds, ($a.peak_memory_bytes/1MB)
"classification + intensity : {0,8:N1} MB  {1,7:N1} s  peak {2,6:N0} MB" -f ($b.output_bytes/1MB), $b.elapsed_seconds, ($b.peak_memory_bytes/1MB)
"intensity adds             : {0,8:N1} MB  ({1:N1} %)" -f (($b.output_bytes-$a.output_bytes)/1MB), (100*($b.output_bytes-$a.output_bytes)/$a.output_bytes)
```

Note: the source file is read over the network, so the times include
network transfer. That is realistic if production processing also reads
from the shared drive; say so in the assessment either way.

## 5. Look at it in the browser

Starts the viewer server in its own window (close that window to stop it)
and opens the viewer with the tileset already loaded. The server only
listens on this computer, so nobody else on the network can browse it.

```powershell
Start-Process -FilePath (Resolve-Path $PY).Path -ArgumentList "scripts\serve_viewer.py --port 8080" -WorkingDirectory (Get-Location).Path
Start-Sleep -Seconds 2
Start-Process "http://localhost:8080/viewer/index.html?tileset=../results/merged_class_int/tiles/tileset.json"
```

To view the other run, change `merged_class_int` to `merged_class` in the
last line. In the viewer, switch **Colour by** to Classification and
Intensity, and note the numbers in the Performance panel.

---

## Where everything ends up

```
results\
  00_terra_folder.json        step 0
  01_inspect.json             step 1
  merged_class\               step 2
    01_inspect.json             source file properties and verdict
    03_benchmark.json           time, peak memory, output size
    04_validation.json          per-attribute pass/fail
    tiles\                      the 3D Tiles tileset (tileset.json + points\)
  merged_class_int\           step 3, same layout
```

To write somewhere else, add `--out-root "<folder>"` to `run_test.py`, then
start the viewer server with `--results "<folder>"`.

---

## Running the steps by hand (optional)

`run_test.py` already does this. Use these only to repeat one part.

**Never feed the `.copc.laz` straight into the tiling step.** py3dtiles
cannot seek in COPC files and silently produces a tileset where every point
after the first 100,000 is zero. Step 3 refuses such a file; rewrite it
first (step 2 below), then tile the plain `.las` it writes.

```powershell
# Rewrite COPC -> plain .las (sequential read, lossless). ~600 MB for 18.8M points.
& $PY scripts\02_prepare_las.py $FILE -o results\manual\merged_plain.las

# Tiling only
& $PY scripts\03_convert_benchmark.py results\manual\merged_plain.las --out results\manual\tiles --extra-fields classification intensity --json results\manual\03_benchmark.json

# Validation only. The source for comparison can be the original COPC
# (it is read sequentially here, which is correct) or the plain .las.
& $PY scripts\04_validate_tileset.py results\manual\tiles --source $FILE --fields classification intensity --json results\manual\04_validation.json

# Remove the 600 MB intermediate when done
Remove-Item results\manual\merged_plain.las
```

## A numbered tile instead (small, quick check)

`cloud1.las` is LAS 1.2, so run_test rewrites it to LAS 1.4 first
automatically. The rewritten copy goes into `results\`, not next to the data.

```powershell
& $PY run_test.py "$DATA\cloud1.las" --fields classification intensity --name cloud1_quick
```

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| `& : The term '.venv\Scripts\python.exe' is not recognized` | Block B was not run in this terminal, or the `cd` path is wrong |
| `mapbox_earcut` / `nmake` error during install | The venv was made with Python 3.14. Delete `.venv`, rerun block A |
| Step 2 prints nothing for many minutes | Large file over a slow network link. Check the drive is reachable, then wait |
| Step 4 FAILs with ~100,000 correct values and a 14-level tileset | The COPC was tiled directly (old harness version, or `--skip-prepare`). Update `scripts\` and rerun |
| Step 3 says "Refusing to convert ... variable-size chunks" | Correct behaviour. Run step 2 first, or use `run_test.py` which does it for you |
| Viewer says "Failed to load" | The server window from step 5 is not running, or the run name in the URL is wrong |
| Viewer Intensity is all white or all black | Type a different number in **Intensity white point** |
