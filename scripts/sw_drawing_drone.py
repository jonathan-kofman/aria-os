"""Drive `createDrawing` against the SW addin to make a .slddrw from an
already-saved assembly or part. Pairs with `sw_assemble_drone.py`.

Usage:
    python scripts/sw_drawing_drone.py \
        --source outputs/system_builds/drone_ukraine_v19/assembly.sldasm \
        [--out drawing.slddrw] [--sheet A3] [--no-bom]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _post(base: str, path: str, payload: dict, timeout: float = 600.0) -> dict:
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True,
                    help=".sldasm or .sldprt to draw from")
    ap.add_argument("--out", default=None,
                    help="output .slddrw (default: alongside source)")
    ap.add_argument("--sheet", default="A3", help="A, A2, A3, A4 (default A3)")
    ap.add_argument("--no-bom", action="store_true",
                    help="skip BOM table on assembly drawings")
    ap.add_argument("--port", type=int, default=7501)
    args = ap.parse_args()

    src = Path(args.source).resolve()
    if not src.is_file():
        raise SystemExit(f"source not found: {src}")
    out = (Path(args.out).resolve() if args.out
            else src.with_suffix(".slddrw"))

    base = f"http://localhost:{args.port}"
    print(f"[probe ] {base}/status")
    try:
        st = _get(base, "/status")
    except Exception as exc:
        raise SystemExit(
            f"SW addin not reachable at {base} ({exc}). "
            f"Is SolidWorks open and the ARIA add-in loaded?")
    if not st.get("sw_connected"):
        raise SystemExit(f"addin reachable but not connected to SW: {st}")
    print(f"  SW connected: doc={st.get('doc')!r} "
            f"ops_dispatched={st.get('ops_dispatched')}")

    payload = {
        "kind": "createDrawing",
        "params": {
            "source": str(src),
            "out": str(out),
            "sheet_size": args.sheet,
            "add_bom": not args.no_bom,
        },
    }
    print(f"[op    ] createDrawing source={src} out={out} sheet={args.sheet}")
    try:
        r = _post(base, "/op", payload, timeout=600.0)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"HTTP transport failure: {type(exc).__name__}: {exc}")

    print(f"[recv  ] {json.dumps(r, indent=2)[:1200]}")
    rc = 0 if (r.get("ok") and (r.get("result") or {}).get("ok")) else 2
    print(f"[done  ] drawing: {out} (exists={out.is_file()})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
