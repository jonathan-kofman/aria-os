"""Watchdog: rebuild + redeploy the SW addin every time a .cs file changes.

Closest pragmatic approximation of "hot reload" on .NET Framework 4.8
(which lacks collectible AssemblyLoadContext). Run this in a terminal
during a dev session — every save of any AriaSW source file triggers
`sw_redeploy.py`, so the next op against http://localhost:7501 hits the
new code without any manual steps.

A fully-hot solution (Rec #1 in the recommendations) requires splitting
op handlers into a separate AriaSwOps.dll loaded via Assembly.LoadFrom +
re-resolved on file change. That's a multi-hour refactor; this script
gives ~90% of the productivity benefit in 30 minutes.

Usage:
    python scripts/sw_watch_redeploy.py
    python scripts/sw_watch_redeploy.py --config Release
    python scripts/sw_watch_redeploy.py --debounce 1.5

Stop with Ctrl-C. The watcher polls mtimes (no stdlib watchdog dep);
debounce window collapses bursts of saves from IDEs into one redeploy.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT  = Path(__file__).resolve().parents[1]
ADDIN_SRC  = REPO_ROOT / "cad-plugins" / "solidworks" / "AriaSW"
REDEPLOY   = REPO_ROOT / "scripts" / "sw_redeploy.py"


def _scan_mtimes(root: Path) -> dict[str, float]:
    """Return {path: mtime} for every .cs/.csproj under the addin source."""
    out: dict[str, float] = {}
    for ext in ("*.cs", "*.csproj"):
        for p in root.rglob(ext):
            # Skip generated bin/obj artifacts so the watcher doesn't
            # ping-pong on its own builds.
            if "bin" in p.parts or "obj" in p.parts:
                continue
            try:
                out[str(p)] = p.stat().st_mtime
            except OSError:
                pass
    return out


def _redeploy(config: str) -> int:
    print(f"\n[watch] change detected -> running sw_redeploy.py --config {config}")
    sys.stdout.flush()
    return subprocess.run(
        [sys.executable, str(REDEPLOY), "--config", config]
    ).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="Debug",
                    choices=("Debug", "Release"))
    ap.add_argument("--debounce", type=float, default=1.5,
                    help="seconds to wait after last change before redeploy (collapses IDE save bursts)")
    ap.add_argument("--poll", type=float, default=0.5,
                    help="seconds between mtime scans")
    args = ap.parse_args()

    if not REDEPLOY.is_file():
        raise SystemExit(f"sw_redeploy.py missing at {REDEPLOY}")
    if not ADDIN_SRC.is_dir():
        raise SystemExit(f"addin source dir missing at {ADDIN_SRC}")

    print(f"[watch] watching {ADDIN_SRC} (.cs/.csproj) — Ctrl-C to stop")
    last = _scan_mtimes(ADDIN_SRC)
    pending_since: float | None = None
    try:
        while True:
            time.sleep(args.poll)
            current = _scan_mtimes(ADDIN_SRC)
            changed = [
                f for f, mt in current.items()
                if last.get(f) != mt
            ]
            new_files = [f for f in current if f not in last]
            removed = [f for f in last if f not in current]
            if changed or new_files or removed:
                pending_since = time.time()
                last = current
                summary = ", ".join(
                    Path(p).name for p in (changed + new_files)[:3])
                more = len(changed + new_files) - 3
                tail = f" (+{more} more)" if more > 0 else ""
                print(f"[watch] changed: {summary}{tail}")
            elif pending_since is not None:
                if time.time() - pending_since >= args.debounce:
                    pending_since = None
                    rc = _redeploy(args.config)
                    if rc != 0:
                        print(f"[watch] redeploy exited {rc}; will retry on next change")
    except KeyboardInterrupt:
        print("\n[watch] stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
