"""One-command SW addin redeploy.

Encapsulates today's manual workflow:
   1. dotnet build -c <CONFIG>     (SW holds DLL lock; copy step "fails")
   2. taskkill /F /IM SLDWORKS.exe (release lock)
   3. rename bin/<CONFIG>/AriaSW.dll -> .locked-<rand>  (kernel may still
                                                          hold a kernel
                                                          handle even
                                                          after SW dies)
   4. copy obj/<CONFIG>/AriaSW.dll -> bin/<CONFIG>/AriaSW.dll
   5. start SLDWORKS.exe
   6. poll http://localhost:7501/status until {"sw_connected": true}

Why this matters: every code change to AriaSwAddin.cs costs ~90 s of
human attention without this. With it, the cycle is one command and the
script idles while SW boots.

Usage:
    python scripts/sw_redeploy.py
    python scripts/sw_redeploy.py --config Release
    python scripts/sw_redeploy.py --no-restart      # only kill + swap
    python scripts/sw_redeploy.py --no-build        # only swap+restart
    python scripts/sw_redeploy.py --port 7501

Exits non-zero if any step fails so it can chain into a CI loop.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ADDIN_PROJ_DIR = Path(__file__).resolve().parents[1] / \
                   "cad-plugins" / "solidworks" / "AriaSW"
SW_EXE_CANDIDATES = [
    r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
    r"C:\Program Files\SOLIDWORKS\SLDWORKS.exe",
]


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"[run] {' '.join(cmd)}")
    return subprocess.run(cmd, **kw)


def _kill_sw() -> bool:
    """Force-kill SLDWORKS.exe; return True if any was killed."""
    r = _run(["taskkill", "/F", "/IM", "SLDWORKS.exe"],
              capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip() or "(no SW running)")
    return "SUCCESS" in out


def _wait_sw_dead(timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq SLDWORKS.exe", "/NH"],
                           capture_output=True, text=True)
        if "SLDWORKS.exe" not in (r.stdout or ""):
            return True
        time.sleep(0.5)
    return False


def _swap_dll(config: str) -> Path:
    """Rename locked bin DLL out of the way, copy fresh obj DLL in.
    Returns the path written.
    """
    bin_dll = ADDIN_PROJ_DIR / "bin" / config / "net48" / "AriaSW.dll"
    obj_dll = ADDIN_PROJ_DIR / "obj" / config / "net48" / "AriaSW.dll"
    if not obj_dll.is_file():
        raise SystemExit(f"obj DLL missing — run dotnet build first: {obj_dll}")
    if bin_dll.is_file():
        # Even after SW process death, Windows can keep a kernel-mode
        # handle. Renaming uses NtSetInformationFile which works while
        # the handle is being torn down — much more reliable than delete.
        suffix = uuid.uuid4().hex[:8]
        dst = bin_dll.with_name(bin_dll.name + f".locked-{suffix}")
        try:
            os.replace(bin_dll, dst)
            print(f"[swap] {bin_dll.name} -> {dst.name}")
        except OSError as exc:
            # Last resort: try a few times with a short backoff.
            for i in range(5):
                time.sleep(0.5)
                try:
                    os.replace(bin_dll, dst)
                    print(f"[swap] {bin_dll.name} -> {dst.name} (retry {i})")
                    break
                except OSError: pass
            else:
                raise SystemExit(f"could not move locked DLL: {exc}")
    bin_dll.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(obj_dll, bin_dll)
    print(f"[swap] {obj_dll} -> {bin_dll} ({bin_dll.stat().st_size} bytes)")
    return bin_dll


def _start_sw() -> None:
    for exe in SW_EXE_CANDIDATES:
        if Path(exe).is_file():
            print(f"[start] {exe}")
            # DETACHED_PROCESS so this script can return.
            subprocess.Popen(
                [exe],
                creationflags=0x00000008  # DETACHED_PROCESS
                                if os.name == "nt" else 0,
            )
            return
    raise SystemExit(
        f"could not find SLDWORKS.exe in any of {SW_EXE_CANDIDATES}")


def _poll_addin(port: int, timeout_s: float = 240.0) -> dict:
    """Block until /status reports sw_connected:true."""
    deadline = time.time() + timeout_s
    last_err = ""
    print(f"[poll ] http://localhost:{port}/status")
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"http://localhost:{port}/status", method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                import json as _json
                body = _json.loads(resp.read().decode("utf-8"))
                if body.get("sw_connected"):
                    print(f"[poll ] connected: {body}")
                    return body
                last_err = f"not yet connected: {body}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_err = str(exc)
        time.sleep(2.0)
    raise SystemExit(f"timed out waiting for SW addin: {last_err}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="Debug",
                    choices=("Debug", "Release"),
                    help="dotnet build config (default: Debug)")
    ap.add_argument("--no-build", action="store_true",
                    help="skip dotnet build, only swap + restart")
    ap.add_argument("--no-restart", action="store_true",
                    help="kill SW + swap DLL only; do not restart SW")
    ap.add_argument("--port", type=int, default=7501,
                    help="SW addin HTTP port to poll (default 7501)")
    args = ap.parse_args()

    t0 = time.time()
    if not args.no_build:
        # Build with SW running — copy step in MSBuild will fail (locked),
        # but the obj/<config>/net48/AriaSW.dll IS produced. We need
        # exactly that artifact for the swap step.
        print("[build] dotnet build -c " + args.config)
        b = subprocess.run(
            ["dotnet", "build", "-c", args.config],
            cwd=str(ADDIN_PROJ_DIR),
            capture_output=True, text=True)
        # Failure on copy-to-bin is expected when SW is running. Surface
        # any C# compile errors though.
        compile_errors = [
            line for line in b.stdout.splitlines()
            if " error CS" in line]
        if compile_errors:
            print("[build] compile errors:")
            for line in compile_errors[:10]:
                print(f"  {line}")
            return 2
        print("[build] obj DLL produced (copy-to-bin step expected to fail when SW is running)")

    killed = _kill_sw()
    if killed:
        if not _wait_sw_dead():
            print("[warn ] SW still listed after taskkill; proceeding anyway")
    _swap_dll(args.config)

    if args.no_restart:
        print(f"[done ] no-restart mode; SW left dead. ({time.time() - t0:.1f}s)")
        return 0

    _start_sw()
    info = _poll_addin(args.port)
    print(f"[done ] addin live in {time.time() - t0:.1f}s; recipes={info.get('recipe_count')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
