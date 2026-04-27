"""End-to-end smoke driver for the SW addin's assembly ops.

Drives `beginAssembly` / `insertComponent` / `addMate` / `saveAs` against
the running SolidWorks add-in's HTTP listener (port 7501 by default) to
turn a v16-style drone bundle into a real `.sldasm` with proper mates.

Run this with SolidWorks open and the ARIA add-in loaded:

    python scripts/sw_assemble_drone.py
        --bundle outputs/system_builds/drone_ukraine_v16
        [--port 7501]

The driver is deliberately conservative — if any op fails it writes the
partial transcript to `<bundle>/sw_assembly_log.json` so the failure
mode is debuggable without re-running. Per the autonomy-first rule, op
errors propagate to the caller; the driver is the recovery layer for
the bundle pipeline (it doesn't try to silently route around a missing
mate, it surfaces the issue so the next layer — orchestrator — can
re-plan).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _post(base: str, path: str, payload: dict, timeout: float = 60.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(base: str, path: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(base + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _op(base: str, kind: str, params: dict, transcript: list,
          retries: int = 1) -> dict:
    """POST /op and append to transcript; raise if not ok."""
    last_err: str = ""
    for attempt in range(retries + 1):
        try:
            r = _post(base, "/op", {"kind": kind, "params": params},
                       timeout=120.0)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(1.0)
                continue
            raise SystemExit(f"HTTP transport failure on {kind}: {last_err}")
        transcript.append({"kind": kind, "params": params, "result": r})
        result = (r or {}).get("result", {})
        if not (r.get("ok") and result.get("ok")):
            return r  # surface to caller for graceful handling
        return r
    raise SystemExit(f"unreachable retry loop for {kind}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", required=True,
                      help="Path to the drone bundle (e.g. outputs/system_builds/drone_ukraine_v16)")
    ap.add_argument("--port", type=int, default=7501,
                      help="SolidWorks add-in HTTP port (default 7501)")
    ap.add_argument("--frame-step", default="drone_frame.step")
    ap.add_argument("--pcb-step", default="fc_pcb.step")
    ap.add_argument("--out", default="assembly.sldasm")
    args = ap.parse_args()

    bundle = Path(args.bundle).resolve()
    if not bundle.is_dir():
        raise SystemExit(f"bundle not found: {bundle}")
    frame_step = bundle / args.frame_step
    pcb_step   = bundle / args.pcb_step
    for p in (frame_step, pcb_step):
        if not p.is_file():
            raise SystemExit(f"missing component STEP: {p}")
    out_asm = bundle / args.out

    base = f"http://localhost:{args.port}"
    transcript: list = []

    print(f"[probe ] {base}/status")
    try:
        st = _get(base, "/status")
    except Exception as exc:
        raise SystemExit(
            f"SW addin not reachable at {base} ({exc}). "
            f"Is SolidWorks open and the ARIA add-in loaded?")
    if not st.get("sw_connected"):
        raise SystemExit(
            f"addin reachable but not connected to SW: {st}")
    print(f"  SW connected: doc={st.get('doc')!r} "
            f"ops_dispatched={st.get('ops_dispatched')}")

    # 1. Fresh assembly document
    print("[op    ] beginAssembly")
    _op(base, "beginAssembly", {}, transcript)

    # 2. Frame component first (becomes the fixed base by SW convention)
    print(f"[op    ] insertComponent <frame> {frame_step}")
    r_frame = _op(base, "insertComponent", {
        "file":  str(frame_step),
        "alias": "frame",
        "x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0,
    }, transcript)
    frame_name = (r_frame.get("result") or {}).get("name", "frame-1")

    # 3. PCB component, offset above the frame so initial render is sane
    print(f"[op    ] insertComponent <pcb>   {pcb_step}")
    r_pcb = _op(base, "insertComponent", {
        "file":  str(pcb_step),
        "alias": "pcb",
        "x_mm": 0.0, "y_mm": 0.0, "z_mm": 20.0,
    }, transcript)
    pcb_name = (r_pcb.get("result") or {}).get("name", "fc_pcb-1")

    # 4. Mates — let the addin resolve SelectByID2 reference strings
    #    server-side from aliases + plane shorthand. This shields the
    #    driver from SW's naming conventions (component instance suffix,
    #    "Top Plane" vs "Top", asm title with/without .SLDASM, etc.).
    #    Two mates lock the canonical "stack PCB on frame" pose:
    #      a. Top planes parallel  (rotation about Z aligned)
    #      b. Front planes parallel (no twist)
    print("[op    ] addMate parallel  pcb.Top || frame.Top")
    r_mate1 = _op(base, "addMate", {
        "type": "parallel",
        "alias1": "pcb",   "plane1": "Top",
        "alias2": "frame", "plane2": "Top",
    }, transcript)
    if not (r_mate1.get("result") or {}).get("ok"):
        print(f"  WARN: mate1 failed: {r_mate1.get('result')}")

    print("[op    ] addMate parallel  pcb.Front || frame.Front")
    r_mate2 = _op(base, "addMate", {
        "type": "parallel",
        "alias1": "pcb",   "plane1": "Front",
        "alias2": "frame", "plane2": "Front",
    }, transcript)
    if not (r_mate2.get("result") or {}).get("ok"):
        print(f"  WARN: mate2 failed: {r_mate2.get('result')}")

    # 5. Save assembly
    print(f"[op    ] saveAs {out_asm}")
    r_save = _op(base, "saveAs", {"path": str(out_asm)}, transcript)
    if not (r_save.get("result") or {}).get("ok"):
        print(f"  ERROR: saveAs failed: {r_save.get('result')}")
        rc = 2
    else:
        rc = 0

    log_path = bundle / "sw_assembly_log.json"
    log_path.write_text(json.dumps({
        "ok": rc == 0,
        "out": str(out_asm),
        "transcript": transcript,
    }, indent=2))
    print(f"[done  ] log: {log_path}")
    print(f"[done  ] assembly: {out_asm} (exists={out_asm.is_file()})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
