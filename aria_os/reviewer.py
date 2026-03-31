"""
aria_os/reviewer.py — unified file review dispatcher.

Accepts any of:
  .dxf  → civil engineering DXF review (aria_os.autocad.dxf_reviewer)
  .step → mechanical STEP redesign (aria_os.step_reviewer)
  .py   → KiCad pcbnew script review (aria_os.ecad.ecad_reviewer)
  .json → BOM JSON → resolve to pcbnew script if possible

Usage (CLI entry called by run_aria_os.py --review):
    review_file(path, hint="", state="national", interactive=True, repo_root=None)
"""
from __future__ import annotations

from pathlib import Path


def review_file(
    file_path: str | Path,
    hint: str = "",
    state: str = "national",
    interactive: bool = True,
    repo_root: Path | None = None,
) -> Path:
    """
    Dispatch to the correct reviewer based on file extension.

    Parameters
    ----------
    file_path   : path to the file to review (.dxf, .step, .py, .json)
    hint        : free-text guidance ("add pipe labels", "add bolt holes", "increase trace width")
    state       : 2-letter US state code — used for DXF standard checks
    interactive : if False, apply all suggestions without prompting
    repo_root   : repo root for LLM context loading; auto-detected if None

    Returns
    -------
    Path to the revised output file.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = path.suffix.lower()

    if suffix == ".dxf":
        from aria_os.autocad.dxf_reviewer import review_dxf
        return review_dxf(
            path,
            state=state,
            hint=hint,
            repo_root=repo_root,
            interactive=interactive,
        )

    elif suffix == ".step" or suffix == ".stp":
        from aria_os.step_reviewer import review_step
        return review_step(
            path,
            hint=hint,
            repo_root=repo_root,
            interactive=interactive,
        )

    elif suffix == ".py":
        # Assume KiCad pcbnew script
        from aria_os.ecad.ecad_reviewer import review_ecad
        return review_ecad(
            path,
            hint=hint,
            repo_root=repo_root,
            interactive=interactive,
        )

    elif suffix == ".json":
        # Try to resolve to pcbnew script: <stem>_bom.json → <stem>_pcbnew.py
        candidate = path.parent / path.name.replace("_bom.json", "_pcbnew.py")
        if candidate.exists():
            from aria_os.ecad.ecad_reviewer import review_ecad
            return review_ecad(
                candidate,
                hint=hint,
                repo_root=repo_root,
                interactive=interactive,
            )
        raise ValueError(
            f"Cannot review {path.name} — pass the _pcbnew.py script directly "
            f"or a .dxf / .step file."
        )

    else:
        raise ValueError(
            f"Unsupported file type '{suffix}'. "
            "Supported: .dxf (civil), .step/.stp (mechanical), .py (KiCad pcbnew)"
        )
