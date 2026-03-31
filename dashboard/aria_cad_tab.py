import streamlit as st

from aria_os.dashboard_bridge import (
    get_parts_library,
    get_material_study_results,
    get_cem_constants,
    get_assembly_status,
    get_manufacturing_readiness,
)


def render_cad_tab():
    st.header("ARIA-OS: CAD & Manufacturing")

    st.subheader("Parts Library")
    parts = get_parts_library()
    if parts:
        import pandas as pd
        df = pd.DataFrame(
            [
                {
                    "Part": p["name"][:60],
                    "BBox (mm)": (
                        f"{(p.get('bbox_mm') or {}).get('x', 0):.0f}×"
                        f"{(p.get('bbox_mm') or {}).get('y', 0):.0f}×"
                        f"{(p.get('bbox_mm') or {}).get('z', 0):.0f}"
                    ),
                    "SF": (p.get("sf_value") if p.get("sf_value") is not None else "N/A"),
                    "STEP": ("OK" if p.get("step_path") else "MISSING"),
                    "STEP size (KB)": (p.get("step_size_kb") if p.get("step_size_kb") is not None else "—"),
                }
                for p in parts
            ]
        )
        st.dataframe(df, use_container_width=True)
    else:
        st.info("No parts generated yet. Run `python run_aria_os.py ...` to generate parts.")

    st.subheader("Material Study")
    mat_results = get_material_study_results()
    if mat_results:
        # Show compact summary of the top recommendation per part
        for part_name, result in list(mat_results.items())[:50]:
            cols = st.columns(3)
            cols[0].write(part_name[:50])
            cols[1].write(result.get("recommendation", "N/A"))
            sf = result.get("recommendation_sf")
            cols[2].write(f"{sf:.2f}x" if isinstance(sf, (int, float)) else "N/A")
    else:
        st.info("No material study results found in `outputs/material_studies/`.")

    st.subheader("Firmware Constants (CEM export)")
    constants = get_cem_constants()
    if constants:
        col1, col2 = st.columns(2)
        col1.metric("SPOOL_R (m)", f"{constants.get('SPOOL_R', 'N/A')}")
        col1.metric("GEAR_RATIO", f"{constants.get('GEAR_RATIO', 'N/A')}")
        col2.metric("T_BASELINE (N)", f"{constants.get('T_BASELINE', 'N/A')}")
        col2.metric("SPD_RETRACT (m/s)", f"{constants.get('SPD_RETRACT', 'N/A')}")
    else:
        st.info("No CEM constants found at `outputs/cem_constants.json`.")

    st.subheader("Assembly")
    assy = get_assembly_status()
    if assy:
        st.metric("Parts in Assembly", assy.get("part_count", 0))
        if assy.get("steps_missing"):
            st.warning(f"Missing STEP files: {len(assy['steps_missing'])}")
            st.write(assy["steps_missing"])
        if assy.get("optimization_notes"):
            st.json(assy["optimization_notes"])
    else:
        st.info("No assembly config found at `assembly_configs/aria_clutch_assembly.json`.")

    st.subheader("Manufacturing Readiness")
    mfg = get_manufacturing_readiness()
    if mfg:
        parts = mfg.get("parts", [])
        if parts:
            import pandas as pd
            st.dataframe(pd.DataFrame(parts), use_container_width=True)
        ansi = mfg.get("ansi", {})
        if ansi:
            st.write("ANSI Z359.14 compliance (current):")
            st.json(ansi)
        ns = mfg.get("next_steps", [])
        if ns:
            st.write("Next steps:")
            for item in ns:
                st.write(f"- {item}")
    else:
        st.info("No manufacturing readiness file found at `outputs/manufacturing_readiness.md`.")

