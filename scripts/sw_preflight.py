"""Pre-flight sandbox check before driving the SW addin.

Run this BEFORE `sw_assemble_drone.py` / `sw_drawing_drone.py` /
`/api/system/full-build` to fail-fast with clear remediation rather
than producing a half-mated assembly when the addin is stale, the
DLL on disk is older than the source, or there are multiple SLDWORKS
processes fighting over port 7501.

Per the autonomy-first rule (memory: feedback_autonomy_first.md), this
module also exposes `auto_recover()` which attempts to fix common
failure modes by calling `sw_redeploy.py` — caller chooses preflight
("just tell me") or `ensure_ready()` ("fix it and retry").

Returns from `preflight()`:
    {
      "ok": bool,                # True if everything green
      "checks": [                # ordered list of checks performed
        {"name": str, "ok": bool, "detail": str?},
        ...
      ],
      "remediation": [str, ...]  # short list of suggested fixes if !ok
    }

CLI:
    python scripts/sw_preflight.py             # prints JSON, exit 0 / 1
    python scripts/sw_preflight.py --ensure    # auto-redeploy on stale DLL
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADDIN_OBJ = REPO_ROOT / "cad-plugins" / "solidworks" / "AriaSW" / \
              "obj" / "Debug" / "net48" / "AriaSW.dll"
ADDIN_BIN = REPO_ROOT / "cad-plugins" / "solidworks" / "AriaSW" / \
              "bin" / "Debug" / "net48" / "AriaSW.dll"
DEFAULT_PORT = 7501
SOURCE_DIR = REPO_ROOT / "cad-plugins" / "solidworks" / "AriaSW"


def _file_sha256(p: Path) -> str | None:
    if not p.is_file(): return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""): h.update(chunk)
    return h.hexdigest()[:16]


def _http_status(port: int, timeout: float = 2.0) -> dict | None:
    try:
        req = urllib.request.Request(
            f"http://localhost:{port}/status", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None


def _count_sw_procs() -> int:
    try:
        # tasklist is fastest on Windows; powershell would also work.
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq SLDWORKS.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10.0)
        if proc.returncode != 0: return -1
        # Output is one line per matching process; "INFO: No tasks..."
        # if zero. Lines that start with quoted SLDWORKS.exe are real.
        return sum(1 for line in proc.stdout.splitlines()
                    if line.strip().startswith('"SLDWORKS.exe"'))
    except Exception:
        return -1


def _newest_source_mtime() -> float:
    """Latest .cs mtime in the addin source — the build target should
    be newer than this if the addin reflects current code."""
    latest = 0.0
    for cs in SOURCE_DIR.rglob("*.cs"):
        try:
            m = cs.stat().st_mtime
            if m > latest: latest = m
        except OSError: continue
    return latest


def preflight(port: int = DEFAULT_PORT) -> dict:
    """Run all checks; return structured result. Never raises."""
    checks: list[dict] = []
    remediation: list[str] = []

    # 1. SW process count — exactly 1 expected. 0 = SW not running,
    #    2+ = leftover hung instance from a prior crash, will fight
    #    over the COM-registered addin task pane.
    n = _count_sw_procs()
    if n == 1:
        checks.append({"name": "sw_process_count", "ok": True,
                        "detail": "1 SLDWORKS.exe"})
    elif n == 0:
        checks.append({"name": "sw_process_count", "ok": False,
                        "detail": "no SLDWORKS.exe — start SolidWorks"})
        remediation.append(
            "Start SolidWorks and ensure the ARIA add-in is loaded.")
    elif n == -1:
        checks.append({"name": "sw_process_count", "ok": False,
                        "detail": "tasklist failed"})
    else:
        checks.append({"name": "sw_process_count", "ok": False,
                        "detail": f"{n} SLDWORKS.exe processes — port "
                                   f"7501 will collide"})
        remediation.append(
            "taskkill /F /IM SLDWORKS.exe  (kill all SW), then re-launch one.")

    # 2. Addin reachable on /status.
    st = _http_status(port)
    if st is None:
        checks.append({"name": "addin_http_listener", "ok": False,
                        "detail": f"localhost:{port}/status unreachable"})
        remediation.append(
            f"python scripts/sw_redeploy.py  (kill+swap+restart)")
    else:
        checks.append({"name": "addin_http_listener", "ok": True,
                        "detail": f"port {port} ok"})
        if not st.get("sw_connected"):
            checks.append({"name": "addin_sw_connected", "ok": False,
                            "detail": "addin loaded but COM not connected"})
            remediation.append(
                "Reload the ARIA add-in from SW Tools → Add-Ins.")
        else:
            checks.append({"name": "addin_sw_connected", "ok": True,
                            "detail": f"doc={st.get('doc')!r} "
                                      f"ops={st.get('ops_dispatched')}"})

    # 3. DLL freshness — bin/Debug AriaSW.dll should be newer than the
    #    newest .cs source file. If not, the running addin is stale.
    bin_hash = _file_sha256(ADDIN_BIN)
    obj_hash = _file_sha256(ADDIN_OBJ)
    src_mtime = _newest_source_mtime()
    bin_mtime = ADDIN_BIN.stat().st_mtime if ADDIN_BIN.is_file() else 0.0
    obj_mtime = ADDIN_OBJ.stat().st_mtime if ADDIN_OBJ.is_file() else 0.0

    if not bin_hash:
        checks.append({"name": "addin_dll_present", "ok": False,
                        "detail": f"missing {ADDIN_BIN}"})
        remediation.append(
            "dotnet build cad-plugins/solidworks/AriaSW/AriaSW.csproj")
    else:
        checks.append({"name": "addin_dll_present", "ok": True,
                        "detail": f"{bin_hash}"})

    # source-newer-than-bin = stale running addin
    if bin_mtime and src_mtime > bin_mtime + 5.0:  # 5s grace
        delta = int(src_mtime - bin_mtime)
        checks.append({"name": "addin_dll_fresh", "ok": False,
                        "detail": f"source is {delta}s newer than bin DLL"})
        remediation.append(
            "python scripts/sw_redeploy.py  (rebuild + swap + restart SW)")
    elif bin_mtime:
        checks.append({"name": "addin_dll_fresh", "ok": True,
                        "detail": "bin DLL is current with source"})

    # bin vs obj hash — should match after a successful build that
    # copied obj→bin. If different, last build wasn't deployed.
    if bin_hash and obj_hash and bin_hash != obj_hash:
        checks.append({"name": "addin_dll_deployed", "ok": False,
                        "detail": f"bin={bin_hash} != obj={obj_hash}"})
        remediation.append(
            "python scripts/sw_redeploy.py  (obj→bin not synced)")
    elif bin_hash and obj_hash:
        checks.append({"name": "addin_dll_deployed", "ok": True,
                        "detail": "bin == obj"})

    # 4. Active doc dirty? Best-effort; only flag if /status exposes it.
    if st and st.get("doc_dirty"):
        checks.append({"name": "active_doc_clean", "ok": False,
                        "detail": "active doc has unsaved changes"})
        remediation.append(
            "Close the active SW doc with unsaved changes before driving "
            "ops — they may collide with the orchestrator's saveAs.")

    return {
        "ok":          all(c["ok"] for c in checks),
        "checks":      checks,
        "remediation": remediation,
    }


def auto_recover(port: int = DEFAULT_PORT,
                  attempts: int = 1) -> dict:
    """Run preflight; if any check fails, attempt sw_redeploy.py once
    (subprocess) and re-check. Returns the final preflight result.

    Per autonomy-first: the caller doesn't need to know HOW we recovered,
    only whether it's now OK."""
    result = preflight(port)
    if result["ok"] or attempts < 1: return result

    # Only redeploy if remediation calls for it. Skip if SW isn't running
    # at all (we can't auto-launch SW without UAC + license server).
    needs_redeploy = any(
        "sw_redeploy.py" in r for r in result.get("remediation", []))
    if not needs_redeploy:
        return result
    redeploy = REPO_ROOT / "scripts" / "sw_redeploy.py"
    if not redeploy.is_file():
        return result

    print(f"[preflight] recovery: {redeploy.name}", file=sys.stderr)
    try:
        proc = subprocess.run(
            [sys.executable, str(redeploy)],
            capture_output=True, text=True, timeout=180.0)
        print(f"[preflight] redeploy rc={proc.returncode}", file=sys.stderr)
    except Exception as exc:
        print(f"[preflight] redeploy failed: {exc}", file=sys.stderr)
        return result

    # Settle period — give SW + addin a moment to reload.
    time.sleep(2.0)
    return preflight(port)


def ensure_ready(port: int = DEFAULT_PORT) -> None:
    """Convenience for callers: raise SystemExit with remediation if not
    ready. Use at the top of any sw_*.py driver."""
    r = auto_recover(port)
    if r["ok"]: return
    msg = ["[preflight] NOT ready — sandbox checks failed:"]
    for c in r["checks"]:
        mark = "OK " if c["ok"] else "FAIL"
        msg.append(f"  {mark}  {c['name']}: {c.get('detail', '')}")
    if r.get("remediation"):
        msg.append("[preflight] try:")
        for hint in r["remediation"]:
            msg.append(f"  - {hint}")
    raise SystemExit("\n".join(msg))


def main() -> int:
    do_ensure = "--ensure" in sys.argv
    port = DEFAULT_PORT
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    r = auto_recover(port) if do_ensure else preflight(port)
    print(json.dumps(r, indent=2))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
