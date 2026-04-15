"""
aria_os.visual_qa.cli — command-line entry for the visual_qa package.

Usage:
    python -m aria_os.visual_qa render-dxf <dxf> <png_out> [--layers L1,L2]
    python -m aria_os.visual_qa render-stl <stl> <out_dir> [--goal "text"]
    python -m aria_os.visual_qa verify-sheet-metal <dxf> \
        [--expected-bbox WxH] [--expected-holes N] [--tol 0.05]

All subcommands print a JSON result to stdout and exit 0 on success,
1 on any failure (check ``ok`` / ``passed`` in the JSON).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


_HELP = __doc__


def _parse_kv_flags(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """Tiny argv splitter — no argparse to stay consistent with ARIA-OS."""
    positional: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key = a.lstrip("-")
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                flags[key] = argv[i + 1]
                i += 2
                continue
            flags[key] = "true"
            i += 1
            continue
        positional.append(a)
        i += 1
    return positional, flags


def _cmd_render_dxf(argv: list[str]) -> dict[str, Any]:
    pos, flags = _parse_kv_flags(argv)
    if len(pos) < 2:
        return {"ok": False, "error": "usage: render-dxf <dxf> <png_out> [--layers L1,L2]"}
    from .dxf_renderer import render_dxf
    layers = None
    if "layers" in flags:
        layers = [s.strip() for s in flags["layers"].split(",") if s.strip()]
    return render_dxf(pos[0], pos[1], layers=layers)


def _cmd_render_stl(argv: list[str]) -> dict[str, Any]:
    pos, flags = _parse_kv_flags(argv)
    if len(pos) < 2:
        return {"ok": False, "error": "usage: render-stl <stl> <out_dir> [--goal text]"}
    from .stl_renderer import render_stl
    goal = flags.get("goal", "stl preview")
    return render_stl(pos[0], pos[1], goal=goal)


def _cmd_verify_sheet_metal(argv: list[str]) -> dict[str, Any]:
    pos, flags = _parse_kv_flags(argv)
    if len(pos) < 1:
        return {"ok": False, "error": "usage: verify-sheet-metal <dxf> [--expected-bbox WxH] [--expected-holes N] [--tol 0.05]"}
    from .dxf_verify import verify_sheet_metal_dxf
    expected_bbox = None
    if "expected-bbox" in flags:
        try:
            w_s, h_s = flags["expected-bbox"].lower().split("x")
            expected_bbox = (float(w_s), float(h_s))
        except Exception:
            return {"ok": False, "error": "expected-bbox must be WxH (e.g. 120x80)"}
    expected_holes = int(flags.get("expected-holes", "0"))
    tol = float(flags.get("tol", "0.05"))
    result = verify_sheet_metal_dxf(
        pos[0],
        expected_bbox_mm=expected_bbox,
        expected_holes=expected_holes,
        bbox_tolerance=tol,
    )
    # Normalise key so callers can uniformly check result["ok"].
    result["ok"] = bool(result.get("passed", False))
    return result


_COMMANDS = {
    "render-dxf": _cmd_render_dxf,
    "render-stl": _cmd_render_stl,
    "verify-sheet-metal": _cmd_verify_sheet_metal,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0

    cmd = argv[0]
    if cmd not in _COMMANDS:
        print(f"unknown subcommand: {cmd}")
        print(_HELP)
        return 2

    rest = argv[1:]
    # Per-subcommand help
    if rest and rest[0] in ("-h", "--help"):
        doc = _COMMANDS[cmd].__doc__ or f"see: python -m aria_os.visual_qa {cmd}"
        print(doc)
        return 0

    try:
        result = _COMMANDS[cmd](rest)
    except Exception as exc:  # defensive — should never trigger
        result = {"ok": False, "error": f"unhandled exception: {exc}"}

    print(json.dumps(result, indent=2, default=str))
    ok = bool(result.get("ok") or result.get("passed"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
