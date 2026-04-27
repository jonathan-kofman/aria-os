"""Export each open SW drawing doc to PDF, regardless of which is active.

The running addin's `saveAs` op uses the cached `_model` handle which can
lag behind the user-visible active doc. This script connects to the
already-running SW COM instance via pywin32, walks the open-doc list,
and calls `Extension.SaveAs` on each .slddrw it finds — yielding one
PDF per drawing so we can render and verify part-level vs assembly-level
documentation independently.

Usage:
    python scripts/sw_export_drawings.py --bundle <bundle_dir>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pythoncom  # noqa: F401  # imports COM marshaller
import win32com.client


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--out-suffix", default="_drawing.pdf")
    args = ap.parse_args()

    bundle = Path(args.bundle).resolve()
    if not bundle.is_dir():
        raise SystemExit(f"bundle not found: {bundle}")

    sw = win32com.client.GetActiveObject("SldWorks.Application")
    print(f"connected to SW: visible={sw.Visible}")

    # Find every .slddrw in the bundle and OpenDoc6 it (activates if
    # already open) then SaveAs PDF. OpenDoc6 reuses the existing
    # in-memory model so we don't lose the active drawing's view layout.
    drawings = sorted(bundle.glob("*.SLDDRW")) + sorted(bundle.glob("*.slddrw"))
    # Filter SW lock files (~$ prefix) and dedupe case-insensitive.
    seen = set()
    unique = []
    for d in drawings:
        if d.name.startswith("~$"): continue
        key = d.name.lower()
        if key in seen: continue
        seen.add(key)
        unique.append(d)
    drawings = unique
    if not drawings:
        print("no .slddrw found in bundle")
        return 2
    print(f"found {len(drawings)} drawing(s)")

    exported = []
    for slddrw in drawings:
        try:
            # Older OpenDoc takes only (path, type); SW activates if already
            # open and silently no-ops the disk read. swDocDRAWING = 3.
            doc = sw.OpenDoc(str(slddrw), 3)
            if doc is None:
                print(f"  could not open {slddrw}")
                continue
            stem = slddrw.stem
            out_pdf = bundle / f"{stem}{args.out_suffix}"
            # SaveAs2 (older signature) avoids byref-output COM marshalling
            # that breaks under late binding for SaveAs(...).
            ok = doc.SaveAs4(str(out_pdf), 0, 1, "")
            size = out_pdf.stat().st_size if out_pdf.exists() else 0
            exported.append({
                "drawing": slddrw.name,
                "pdf": out_pdf.name,
                "ok": bool(ok),
                "size": size,
            })
            print(f"  '{slddrw.name}' -> '{out_pdf.name}' ok={ok} size={size}")
        except Exception as exc:
            # Fall back to SaveAs2 if SaveAs4 unsupported on this SW build.
            try:
                ok = doc.SaveAs2(str(out_pdf), 0, False, False)
                size = out_pdf.stat().st_size if out_pdf.exists() else 0
                exported.append({"drawing": slddrw.name, "pdf": out_pdf.name,
                                  "ok": bool(ok), "size": size, "via": "SaveAs2"})
                print(f"  '{slddrw.name}' -> '{out_pdf.name}' (SaveAs2) ok={ok} size={size}")
            except Exception as exc2:
                print(f"  skip {slddrw.name}: SaveAs4 {exc} / SaveAs2 {exc2}")

    print(f"\n{len(exported)} drawings exported")
    return 0 if exported else 2


if __name__ == "__main__":
    sys.exit(main())
