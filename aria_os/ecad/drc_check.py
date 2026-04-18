"""
DRC / ERC validation via the headless `kicad-cli` shipped with KiCad 8+.

Graceful-degrade: if kicad-cli isn't on PATH, run_drc/run_erc return a
structured "skipped" result so the pipeline continues without aborting.
Install kicad-cli by installing KiCad 8+ — see scripts/PRO_HEADLESS_SETUP.md.

Usage
-----
    from aria_os.ecad.drc_check import run_drc, run_erc
    drc = run_drc("board.kicad_pcb", out_dir="outputs/drc/")
    # drc = {"available": bool, "passed": bool, "violations": [...], "report_path": str}

CLI contract (KiCad 8.0+):
    kicad-cli pcb drc --output report.json --format json board.kicad_pcb
    kicad-cli sch erc --output report.json --format json sch.kicad_sch
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def _find_kicad_cli() -> str | None:
    """Locate kicad-cli.exe on PATH or in common Windows install dirs.
    KiCad 10 defaults to AppData/Local install — check there first.
    """
    found = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if found:
        return found
    candidates: list[str] = []
    # Per-user AppData install (KiCad 10 default)
    local_app = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
        "~/AppData/Local")
    candidates.append(os.path.join(local_app, "Programs", "KiCad"))
    # System-wide installs (older versions / admin installs)
    candidates += [
        r"C:\Program Files\KiCad",
        r"C:\Program Files (x86)\KiCad",
    ]
    for base in candidates:
        if not os.path.isdir(base):
            continue
        for ver in sorted(os.listdir(base), reverse=True):
            candidate = os.path.join(base, ver, "bin", "kicad-cli.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def kicad_share_dir() -> str | None:
    """Return KiCad's share/kicad/ root (parent of symbols/, footprints/).
    Used by the symbol library lookup for real component symbols."""
    cli = _find_kicad_cli()
    if cli is None:
        return None
    # Walk from .../bin/kicad-cli.exe → .../share/kicad/
    install_root = os.path.dirname(os.path.dirname(cli))  # strip bin/
    candidate = os.path.join(install_root, "share", "kicad")
    if os.path.isdir(candidate):
        return candidate
    return None


def run_drc(pcb_path: str | Path,
            out_dir: str | Path,
            *,
            severity_fail: set[str] | None = None) -> dict:
    """Run DRC on a .kicad_pcb. Returns dict with violations + pass/fail.

    severity_fail: severity levels that should cause `passed=False`.
        Default {"error"}. Use {"error", "warning"} to be strict.
    """
    if severity_fail is None:
        severity_fail = {"error"}

    cli = _find_kicad_cli()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "drc_report.json"

    if cli is None:
        return {
            "available": False,
            "passed": None,
            "violations": [],
            "report_path": None,
            "error": "kicad-cli not found; install KiCad 8+ to enable DRC",
        }

    try:
        subprocess.run(
            [cli, "pcb", "drc",
             "--output", str(report_path),
             "--format", "json",
             "--severity-all",
             "--exit-code-violations",
             str(pcb_path)],
            check=False, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"available": True, "passed": False, "violations": [],
                "report_path": None, "error": "kicad-cli pcb drc timed out"}
    except Exception as exc:
        return {"available": True, "passed": False, "violations": [],
                "report_path": None, "error": f"{type(exc).__name__}: {exc}"}

    if not report_path.is_file():
        return {"available": True, "passed": False, "violations": [],
                "report_path": None,
                "error": "DRC completed but no report produced"}

    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": True, "passed": False, "violations": [],
                "report_path": str(report_path),
                "error": f"report unreadable: {exc}"}

    violations = data.get("violations", []) or []
    unconnected = data.get("unconnected_items", []) or []
    schem_parity = data.get("schematic_parity", []) or []
    all_items = [
        *({**v, "_category": "violation"} for v in violations),
        *({**v, "_category": "unconnected"} for v in unconnected),
        *({**v, "_category": "schematic_parity"} for v in schem_parity),
    ]
    worst = {v.get("severity", "info") for v in all_items}
    passed = worst.isdisjoint(severity_fail)

    return {
        "available": True,
        "passed": passed,
        "n_violations": len(violations),
        "n_unconnected": len(unconnected),
        "n_schematic_parity": len(schem_parity),
        "worst_severity": next(
            (s for s in ("error", "warning", "info") if s in worst), None),
        "violations": all_items[:50],  # cap for build_summary size
        "report_path": str(report_path),
    }


def run_erc(sch_path: str | Path, out_dir: str | Path) -> dict:
    """Run ERC on a .kicad_sch. Returns dict with violations + pass/fail."""
    cli = _find_kicad_cli()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "erc_report.json"

    if cli is None:
        return {
            "available": False, "passed": None, "violations": [],
            "report_path": None,
            "error": "kicad-cli not found; install KiCad 8+ to enable ERC",
        }

    try:
        subprocess.run(
            [cli, "sch", "erc",
             "--output", str(report_path),
             "--format", "json",
             "--severity-all",
             str(sch_path)],
            check=False, capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        return {"available": True, "passed": False, "violations": [],
                "report_path": None, "error": f"{type(exc).__name__}: {exc}"}

    if not report_path.is_file():
        return {"available": True, "passed": False, "violations": [],
                "report_path": None, "error": "ERC completed but no report"}

    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": True, "passed": False, "violations": [],
                "report_path": str(report_path),
                "error": f"report unreadable: {exc}"}

    violations = data.get("violations", []) or []
    passed = not any(v.get("severity") == "error" for v in violations)
    return {
        "available": True,
        "passed": passed,
        "n_violations": len(violations),
        "violations": violations[:50],
        "report_path": str(report_path),
    }
