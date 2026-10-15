# Copy-paste commands (Windows PowerShell)

Every block below can be pasted into PowerShell as is. Run **block A once**,
then **block B every time you open a new terminal**, then any of the steps.

Paths are pre-filled for the Copes Hut Dec 2025 dataset. To test another
dataset, change `$DATA` in block B only.

---

## A. One-time setup

Only the first time, or after moving the folder (a venv cannot be moved,
it must be recreated).

```powershell
# Where the harness lives. Keep it outside OneDrive.
cd C:\work\test_Py3DTiles

# Python 3.11 (NOT 3.14, see README "Install")
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# Sanity check: should print 12.1.1
.venv\Scripts\python.exe -c "import py3dtiles; print(py3dtiles.__version__)"
```

---

## B. Every new terminal: set the paths

```powershell
cd C:\work\test_Py3DTiles
$PY   = ".venv\Scripts\python.exe"
$DATA = "Z:\NESP-25007\NESP - December 2025\NESP - Lidar\Copes Hut-Dec25\lidars\terra_las"
$FILE = "C:\work\data\cloud_merged.copc.laz"
```

`$DATA` is the DJI Terra output folder on the network drive. `$FILE` is the
local copy that gets converted (block 1).

---

## 0. What are the files in the Terra folder?

Read only, nothing is written to the Z: drive.

```powershell
& $PY scripts\00_compare_terra_files.py $DATA --json results\00_terra_folder.json
```

## 1. Copy the file to the local disk

Timings over a network drive measure the network, not the conversion.

```powershell
New-Item -ItemType Directory -Force -Path C:\work\data | Out-Null
Copy-Item -LiteralPath "$DATA\cloud_merged.copc.laz" -Destination C:\work\data\ -Force
"{0}  {1:N1} MB" -f (Get-Item $FILE).Name, ((Get-Item $FILE).Length/1MB)
```

## 2. Inspect the file: header, classes, every attribute

This is the output to read when deciding which attributes to keep.

```powershell
& $PY scripts\01_inspect_las.py $FILE --json results\01_inspect_copc.json
```

Look at the table **All dimensions in the file**:
keep an attribute only if `export` is `yes`, it is not marked `constant`,
and the analysts actually use it in BunjilView.

## 3. Full test, classification only

Tiling + timing + memory + exact validation, in one go.

```powershell
& $PY run_test.py $FILE --fields classification --name copes_class
```

## 4. Full test, classification + intensity

```powershell
& $PY run_test.py $FILE --fields classification intensity --name copes_class_int
```

## 5. What does intensity cost?

Compares run 3 and run 4.

```powershell
$a = Get-Content results\copes_class\03_benchmark.json -Raw | ConvertFrom-Json
$b = Get-Content results\copes_class_int\03_benchmark.json -Raw | ConvertFrom-Json
"classification only        : {0,8:N1} MB  {1,7:N1} s  peak {2,6:N0} MB" -f ($a.output_bytes/1MB), $a.elapsed_seconds, ($a.peak_memory_bytes/1MB)
"classification + intensity : {0,8:N1} MB  {1,7:N1} s  peak {2,6:N0} MB" -f ($b.output_bytes/1MB), $b.elapsed_seconds, ($b.peak_memory_bytes/1MB)
"intensity adds             : {0,8:N1} MB  ({1:N1} %)" -f (($b.output_bytes-$a.output_bytes)/1MB), (100*($b.output_bytes-$a.output_bytes)/$a.output_bytes)
```

## 6. Look at it in the browser

Starts a small web server in its own window (close that window to stop it)
and opens the viewer with the tileset already loaded.

```powershell
Start-Process -FilePath (Resolve-Path $PY).Path -ArgumentList "-m http.server 8080" -WorkingDirectory (Get-Location).Path
Start-Sleep -Seconds 2
Start-Process "http://localhost:8080/viewer/index.html?tileset=../results/copes_class_int/tiles/tileset.json"
```

To view the other run, change `copes_class_int` to `copes_class` in the
last line. In the viewer, switch **Colour by** to Classification and
Intensity, and note the numbers in the Performance panel.

---

## Running steps 3 and 4 by hand (optional)

`run_test.py` already does this. Use these only to repeat one part.

```powershell
# Tiling only
& $PY scripts\03_convert_benchmark.py $FILE --out results\manual\tiles --extra-fields classification intensity --json results\manual\03_benchmark.json

# Validation only, against the same source file
& $PY scripts\04_validate_tileset.py results\manual\tiles --source $FILE --fields classification intensity --json results\manual\04_validation.json
```

## A numbered tile instead (small, quick check)

`cloud1.las` is LAS 1.2, so run_test rewrites it to LAS 1.4 first (step 2)
automatically.

```powershell
& $PY run_test.py "$DATA\cloud1.las" --fields classification intensity --name cloud1_quick
```

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| `& : The term '.venv\Scripts\python.exe' is not recognized` | Block B was not run in this terminal, or you are not in `C:\work\test_Py3DTiles` |
| `mapbox_earcut` / `nmake` error during install | The venv was made with Python 3.14. Delete `.venv`, rerun block A |
| Step 3 prints nothing for many minutes | Check the file is local (block 1), not on Z: |
| Viewer says "Failed to load" | The web server window from step 6 is not running, or the run name in the URL is wrong |
| Viewer Intensity is all white or all black | Type a different number in **Intensity white point** |
