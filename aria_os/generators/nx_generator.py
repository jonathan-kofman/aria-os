"""
aria_os/generators/nx_generator.py

Generates Siemens NX NXOpen Python journal scripts via Claude, then attempts
headless batch execution via run_journal.exe.

Execution strategy (option A):
  1. Claude generates the NXOpen Python journal
  2. run_journal.exe -batch <journal.py> is attempted
  3. NX Student Edition may reject headless — caught and reported
  4. Journal file always written as artifact regardless of batch outcome

NX installation: C:/Program Files/Siemens/NXStudentEdition2506
Journal runner:  NXBIN/run_journal.exe
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default NX root — overridable via NX_ROOT env var
_DEFAULT_NX_ROOT = r"C:\Program Files\Siemens\NXStudentEdition2506"

# Output file markers the journal writes to disk
_STEP_MARKER = "ARIA_STEP_OUTPUT:"
_STL_MARKER = "ARIA_STL_OUTPUT:"

# ── NXOpen journal system prompt ──────────────────────────────────────────────

_NX_SYSTEM_PROMPT = """You are an expert Siemens NX automation engineer writing NXOpen Python journal scripts.

Rules:
- Import only: NXOpen, NXOpen.UF, NXOpen.Features, NXOpen.GeometricUtilities, math, os, sys
- Always start with: import NXOpen; theSession = NXOpen.Session.GetSession(); workPart = theSession.Parts.Work
- For new parts: workPart = theSession.Parts.NewDisplay("output.prt", NXOpen.DisplayPartOption.AllowAdditional)
- Use NXOpen.Features.BlockFeatureBuilder for boxes
- Use NXOpen.Features.CylinderBuilder for cylinders
- Use NXOpen.Features.BooleanBuilder for boolean operations
- Export STEP: use theSession.ApplicationManager... or NXOpen.CAEAnalysisBuilder
- Print output paths using EXACTLY: print("ARIA_STEP_OUTPUT:" + step_path)
- Print STL path if exported: print("ARIA_STL_OUTPUT:" + stl_path)
- NEVER use plt, tkinter, cv2, or any UI library
- NEVER use open() or file I/O beyond os.path operations
- All units are millimeters
- End with: theSession.ApplicationManager.SaveAs(workPart, step_path)

Output ONLY the Python script — no markdown, no explanation."""


def generate_nx_journal(
    goal: str,
    spec: dict,
    build_recipe: str,
    output_dir: Path | str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Generate an NXOpen Python journal for the given goal.

    Returns {journal_path, script_code, error}.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    step_path = str(output_dir / "output.stp").replace("\\", "/")
    stl_path = str(output_dir / "output.stl").replace("\\", "/")
    prt_path = str(output_dir / "output.prt").replace("\\", "/")

    prompt = f"""Goal: {goal}

Spec: {json.dumps(spec, default=str)[:800]}

Build recipe:
{build_recipe[:3000]}

Output paths (use exactly):
  PRT: {prt_path}
  STEP: {step_path}
  STL: {stl_path}

Write a complete NXOpen Python journal that:
1. Creates the geometry described in the build recipe
2. Exports STEP to: {step_path}
3. Prints: ARIA_STEP_OUTPUT:{step_path}

Use NXOpen API only."""

    try:
        from ..llm_client import call_llm
        script = call_llm(prompt, _NX_SYSTEM_PROMPT, repo_root)
    except Exception as exc:
        logger.warning("NX journal LLM call failed: %s", exc)
        script = None

    if not script:
        return {"journal_path": "", "script_code": "", "error": "LLM returned no script"}

    # Strip markdown fences if present
    script = script.strip()
    if script.startswith("```"):
        lines = script.split("\n")
        script = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    journal_path = output_dir / "aria_nx_journal.py"
    journal_path.write_text(script, encoding="utf-8")
    logger.info("NX journal written: %s", journal_path)

    return {
        "journal_path": str(journal_path),
        "script_code": script,
        "step_path": step_path,
        "stl_path": stl_path,
        "error": None,
    }


def run_nx_headless(
    journal_path: str | Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Attempt to run an NXOpen journal via run_journal.exe in batch mode.

    Returns {success, step_path, stl_path, stdout, error}.
    Student Edition NX may reject -batch; this is caught and reported gracefully.
    """
    journal_path = Path(journal_path)
    if not journal_path.exists():
        return {"success": False, "error": f"journal not found: {journal_path}"}

    nx_root = Path(os.environ.get("NX_ROOT", _DEFAULT_NX_ROOT))
    runner = nx_root / "NXBIN" / "run_journal.exe"

    if not runner.exists():
        return {"success": False, "error": f"run_journal.exe not found at {runner}"}

    # Prepare NX environment variables
    env = os.environ.copy()
    env.setdefault("UGII_BASE_DIR", str(nx_root))
    env.setdefault("UGII_ROOT_DIR", str(nx_root / "UGMANAGER"))
    env.setdefault("PATH", str(nx_root / "NXBIN") + os.pathsep + env.get("PATH", ""))

    # Try batch flags in order of likelihood to work on Student Edition
    batch_flags = [
        [str(runner), "-new", str(journal_path)],          # standard batch
        [str(runner), "-batch", str(journal_path)],         # some NX versions
        [str(runner), str(journal_path)],                   # no flag (may open GUI briefly)
    ]

    stdout_text = ""
    last_error = ""

    for cmd in batch_flags:
        try:
            logger.info("NX run attempt: %s", " ".join(cmd))
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            stdout_text = result.stdout + result.stderr
            if result.returncode == 0 or _STEP_MARKER in stdout_text:
                break
            last_error = f"returncode={result.returncode}"
        except subprocess.TimeoutExpired:
            last_error = "timeout (120s)"
            break
        except FileNotFoundError as exc:
            last_error = str(exc)
            break
        except Exception as exc:
            last_error = str(exc)

    # Parse output paths from journal stdout markers
    step_path = ""
    stl_path = ""
    for line in stdout_text.splitlines():
        if line.startswith(_STEP_MARKER):
            step_path = line[len(_STEP_MARKER):].strip()
        if line.startswith(_STL_MARKER):
            stl_path = line[len(_STL_MARKER):].strip()

    # Also probe expected output path directly
    if not step_path:
        # Check if journal wrote to the expected location
        journal_dir = journal_path.parent
        for candidate in journal_dir.glob("*.stp"):
            step_path = str(candidate)
            break

    step_exists = step_path and Path(step_path).exists()

    if step_exists:
        logger.info("NX headless success: %s", step_path)
        return {
            "success": True,
            "step_path": step_path,
            "stl_path": stl_path if (stl_path and Path(stl_path).exists()) else "",
            "stdout": stdout_text[:2000],
            "error": None,
        }

    # Detect Student Edition license restriction
    if any(kw in stdout_text.lower() for kw in ("license", "student", "restricted", "not permitted", "headless")):
        error = "NX Student Edition does not permit headless batch execution"
    else:
        error = last_error or "no STEP output produced"

    logger.warning("NX batch failed: %s", error)
    return {"success": False, "step_path": "", "stl_path": "", "stdout": stdout_text[:500], "error": error}
