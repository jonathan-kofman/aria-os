"""
aria_outputs_tab.py — Streamlit tab for browsing generated CAD/CAM outputs.

Shows metric cards (file counts + disk usage), a filterable dataframe,
per-row path display, and subdirectory expanders.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import streamlit as st

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUTPUTS_DIR = _REPO_ROOT / "outputs"

# File-type classification by extension
_TYPE_MAP: dict[str, str] = {
    ".step": "STEP",
    ".stp": "STEP",
    ".stl": "STL",
    ".svg": "SVG",
    ".png": "PNG",
    ".jpg": "PNG",
    ".jpeg": "PNG",
    ".json": "CAM",
    ".py": "CAM",
    ".csv": "CAM",
    ".ghx": "ECAD",
    ".dxf": "ECAD",
}

_SCREENSHOT_EXTS = {".png", ".jpg", ".jpeg"}


def _classify(path: Path) -> str:
    return _TYPE_MAP.get(path.suffix.lower(), "OTHER")


def _collect_files(root: Path) -> list[dict]:
    """Walk outputs/ and return a list of file metadata dicts."""
    rows: list[dict] = []
    if not root.exists():
        return rows
    for dirpath, _dirs, filenames in os.walk(root):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                stat = fp.stat()
            except OSError:
                continue
            rows.append(
                {
                    "path": fp,
                    "filename": name,
                    "subdir": str(fp.parent.relative_to(root)),
                    "type": _classify(fp),
                    "size_mb": round(stat.st_size / (1024 * 1024), 4),
                    "modified": datetime.fromtimestamp(stat.st_mtime),
                }
            )
    return rows


def render_outputs_tab() -> None:
    st.header("Outputs Browser")
    st.caption(f"Scanning `{_OUTPUTS_DIR.relative_to(_REPO_ROOT)}/`")

    if not _OUTPUTS_DIR.exists():
        st.warning(
            f"`outputs/` directory not found at `{_OUTPUTS_DIR}`. "
            "Run the CAD pipeline at least once to generate files."
        )
        return

    all_files = _collect_files(_OUTPUTS_DIR)

    if not all_files:
        st.info("No output files found yet. Run the pipeline to generate parts.")
        return

    # ── Metric cards ────────────────────────────────────────────────────────
    step_count = sum(1 for f in all_files if f["type"] == "STEP")
    stl_count = sum(1 for f in all_files if f["type"] == "STL")
    total_mb = sum(f["size_mb"] for f in all_files)
    screenshot_count = sum(
        1 for f in all_files if f["path"].suffix.lower() in _SCREENSHOT_EXTS
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("STEP files", step_count)
    c2.metric("STL files", stl_count)
    c3.metric("Total size", f"{total_mb:.2f} MB")
    c4.metric("Screenshots", screenshot_count)

    st.divider()

    # ── Filters ─────────────────────────────────────────────────────────────
    all_types = sorted({f["type"] for f in all_files})
    all_subdirs = sorted({f["subdir"] for f in all_files})

    col_f1, col_f2, col_f3 = st.columns([2, 2, 3])
    with col_f1:
        selected_types = st.multiselect(
            "Filter by type",
            all_types,
            default=[],
            placeholder="All types",
        )
    with col_f2:
        selected_subdirs = st.multiselect(
            "Filter by subdirectory",
            all_subdirs,
            default=[],
            placeholder="All subdirectories",
        )
    with col_f3:
        search_term = st.text_input("Search filename", placeholder="e.g. ratchet")

    # Apply filters
    filtered = all_files
    if selected_types:
        filtered = [f for f in filtered if f["type"] in selected_types]
    if selected_subdirs:
        filtered = [f for f in filtered if f["subdir"] in selected_subdirs]
    if search_term.strip():
        term = search_term.strip().lower()
        filtered = [f for f in filtered if term in f["filename"].lower()]

    st.caption(f"{len(filtered)} file(s) shown (of {len(all_files)} total)")

    st.divider()

    # ── Flat filterable table ────────────────────────────────────────────────
    if filtered:
        import pandas as pd  # only needed here

        df = pd.DataFrame(
            [
                {
                    "filename": f["filename"],
                    "subdir": f["subdir"],
                    "type": f["type"],
                    "size_mb": f["size_mb"],
                    "modified": f["modified"].strftime("%Y-%m-%d %H:%M"),
                }
                for f in filtered
            ]
        )
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()

    # ── Grouped by subdirectory ──────────────────────────────────────────────
    st.subheader("Browse by subdirectory")

    grouped: dict[str, list[dict]] = {}
    for f in filtered:
        grouped.setdefault(f["subdir"], []).append(f)

    for subdir, files in sorted(grouped.items()):
        subdir_label = subdir if subdir != "." else "(root)"
        total_subdir_mb = sum(f["size_mb"] for f in files)
        expander_label = (
            f"{subdir_label}  —  {len(files)} file(s), {total_subdir_mb:.3f} MB"
        )
        with st.expander(expander_label, expanded=False):
            for f in sorted(files, key=lambda x: x["filename"]):
                cols = st.columns([4, 1, 2, 2])
                cols[0].markdown(f"`{f['filename']}`")
                cols[1].caption(f["type"])
                cols[2].caption(f"{f['size_mb']:.4f} MB")
                cols[3].caption(f["modified"].strftime("%Y-%m-%d %H:%M"))

                # "Open folder" — show absolute path (browsers can't open folders)
                if st.button(
                    "Show path",
                    key=f"path_{f['path']}",
                    use_container_width=False,
                ):
                    st.code(str(f["path"]))
