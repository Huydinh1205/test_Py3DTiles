#!/usr/bin/env python3
"""
Serve the viewer and an output folder from one local web address.

A browser will only load a tileset over HTTP, and the viewer can only reach
tilesets served from the same address as itself. When run_test.py writes its
outputs somewhere other than this harness's results/ folder (--out-root),
plain "python -m http.server" can no longer see them. This script fixes that
by mapping two paths onto one server:

    http://localhost:8080/viewer/...    ->  <this harness>/viewer/...
    http://localhost:8080/results/...   ->  the folder given by --results

So the viewer link is the same whichever output folder was used:

    http://localhost:8080/viewer/index.html?tileset=../results/<run name>/tiles/tileset.json

The server listens on this computer only (127.0.0.1), never on the network.
Stop it with Ctrl+C, or by closing its window.

Usage
-----
    python scripts/serve_viewer.py
    python scripts/serve_viewer.py --results "D:/py3dtiles_results" --port 8080
"""

import argparse
import http.server
import posixpath
import urllib.parse
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent


def make_handler(results_root):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = urllib.parse.urlsplit(path).path
            path = posixpath.normpath(urllib.parse.unquote(path))
            # Drop empty, '.' and '..' segments so nothing outside the two
            # roots can be requested.
            parts = [p for p in path.split("/") if p not in ("", ".", "..")]
            if parts and parts[0] == "results":
                return str(Path(results_root, *parts[1:]))
            return str(Path(HARNESS, *parts))

        def end_headers(self):
            # Always fetch fresh tiles after a re-run.
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, fmt, *args):
            pass  # keep the window quiet; errors still show in the browser

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Serve the viewer plus an output folder.")
    ap.add_argument("--results", default=str(HARNESS / "results"),
                    help="Folder that run_test.py wrote into (its --out-root)")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    results_root = Path(args.results).resolve()
    if not results_root.is_dir():
        raise SystemExit(f"Results folder not found: {results_root}")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(results_root))
    print(f"Viewer  : http://localhost:{args.port}/viewer/index.html")
    print(f"Results : {results_root}")
    print("Runs available:")
    for run in sorted(p for p in results_root.iterdir() if (p / "tiles" / "tileset.json").exists()):
        print(f"  http://localhost:{args.port}/viewer/index.html?tileset=../results/{run.name}/tiles/tileset.json")
    print("\nServing. Press Ctrl+C or close this window to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
