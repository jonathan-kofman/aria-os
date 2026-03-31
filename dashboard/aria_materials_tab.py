"""
aria_materials_tab.py — Materials & Fluids Browser Tab
Exposes cem_core.py material/fluid library in the dashboard.
Allows comparison and CEM override.
"""
import streamlit as st
import pandas as pd
import numpy as np
import sys
sys.path.insert(0, '.')


MATERIALS = {
    "X1 420i Metal (60% 420SS + 40% Bronze)": {
        "density_kg_m3": 7860, "yield_MPa": 620, "ultimate_MPa": 800,
        "E_GPa": 200, "k_W_mK": 22.6, "Cp_J_kgK": 478,
        "T_max_K": 737, "machineable": True, "printable_LPBF": True,
        "cost_rel": 4, "notes": "Seraphim housing material. LPBF default."
    },
    "6061-T6 Aluminum": {
        "density_kg_m3": 2700, "yield_MPa": 276, "ultimate_MPa": 310,
        "E_GPa": 69, "k_W_mK": 167, "Cp_J_kgK": 896,
        "T_max_K": 473, "machineable": True, "printable_LPBF": False,
        "cost_rel": 1, "notes": "ARIA housing. Good for prototype."
    },
    "4140 Steel (QT 40-45 HRC)": {
        "density_kg_m3": 7850, "yield_MPa": 655, "ultimate_MPa": 1020,
        "E_GPa": 205, "k_W_mK": 42, "Cp_J_kgK": 473,
        "T_max_K": 700, "machineable": True, "printable_LPBF": False,
        "cost_rel": 2, "notes": "ARIA ratchet/pawl. Must be heat-treated."
    },
    "A2 Tool Steel (58 HRC)": {
        "density_kg_m3": 7860, "yield_MPa": 1800, "ultimate_MPa": 2100,
        "E_GPa": 210, "k_W_mK": 25, "Cp_J_kgK": 460,
        "T_max_K": 600, "machineable": True, "printable_LPBF": False,
        "cost_rel": 3, "notes": "ARIA pawl tip. High surface hardness."
    },
    "17-4PH Stainless (H900)": {
        "density_kg_m3": 7780, "yield_MPa": 1170, "ultimate_MPa": 1310,
        "E_GPa": 197, "k_W_mK": 18, "Cp_J_kgK": 460,
        "T_max_K": 600, "machineable": True, "printable_LPBF": True,
        "cost_rel": 4, "notes": "ARIA flyweights. Good fatigue strength."
    },
    "Inconel 718": {
        "density_kg_m3": 8220, "yield_MPa": 1034, "ultimate_MPa": 1240,
        "E_GPa": 165, "k_W_mK": 14, "Cp_J_kgK": 435,
        "T_max_K": 1200, "machineable": False, "printable_LPBF": True,
        "cost_rel": 8, "notes": "LRE thrust chamber. Excellent high-temp."
    },
    "Copper C18150": {
        "density_kg_m3": 8900, "yield_MPa": 380, "ultimate_MPa": 420,
        "E_GPa": 128, "k_W_mK": 320, "Cp_J_kgK": 385,
        "T_max_K": 800, "machineable": True, "printable_LPBF": True,
        "cost_rel": 6, "notes": "Regen cooling inner wall. Best thermal conductivity."
    },
    "Ti-6Al-4V": {
        "density_kg_m3": 4430, "yield_MPa": 880, "ultimate_MPa": 950,
        "E_GPa": 114, "k_W_mK": 7, "Cp_J_kgK": 526,
        "T_max_K": 600, "machineable": False, "printable_LPBF": True,
        "cost_rel": 7, "notes": "LRE injector body. Excellent strength/weight."
    },
}

FLUIDS = {
    "Kerosene (RP-1)": {
        "density_kg_m3": 820, "viscosity_Pa_s": 1.64e-3,
        "Cp_J_kgK": 2010, "k_W_mK": 0.14,
        "boiling_K": 450, "phase": "liquid",
        "notes": "ARIA rope lubricant? No. LRE fuel. Seraphim spec."
    },
    "LOX": {
        "density_kg_m3": 1141, "viscosity_Pa_s": 1.96e-4,
        "Cp_J_kgK": 1700, "k_W_mK": 0.152,
        "boiling_K": 90.2, "phase": "liquid",
        "notes": "LRE oxidizer. Cryogenic — handle with care."
    },
    "IPA (Isopropyl Alcohol)": {
        "density_kg_m3": 786, "viscosity_Pa_s": 2.4e-3,
        "Cp_J_kgK": 2570, "k_W_mK": 0.14,
        "boiling_K": 355.4, "phase": "liquid",
        "notes": "LOX simulant for waterflow tests. Seraphim protocol."
    },
    "Water": {
        "density_kg_m3": 998, "viscosity_Pa_s": 1.0e-3,
        "Cp_J_kgK": 4182, "k_W_mK": 0.598,
        "boiling_K": 373.15, "phase": "liquid",
        "notes": "Kerosene simulant for waterflow tests."
    },
    "Nitrogen (GN2)": {
        "density_kg_m3": 1.16, "viscosity_Pa_s": 1.76e-5,
        "Cp_J_kgK": 1040, "k_W_mK": 0.026,
        "boiling_K": 77.4, "phase": "gas",
        "notes": "Pressurant / purge gas."
    },
}


def render_materials_tab():
    st.markdown("## Materials & Fluids Library")
    st.caption(
        "All materials and fluids encoded in the CEM platform. "
        "Sourced from Seraphim team specs, manufacturer datasheets, and standard references."
    )

    tab_mat, tab_fluid, tab_compare, tab_override = st.tabs([
        "Materials", "Fluids", "Compare", "CEM Override"
    ])

    # ════════════════════════════════════════════════════════════════
    # MATERIALS TAB
    # ════════════════════════════════════════════════════════════════
    with tab_mat:
        st.markdown("### Material properties")
        filter_printable = st.checkbox("LPBF-printable only", value=False)
        filter_machine   = st.checkbox("CNC-machineable only", value=False)

        rows = []
        for name, m in MATERIALS.items():
            if filter_printable and not m["printable_LPBF"]: continue
            if filter_machine   and not m["machineable"]:    continue
            rows.append({
                "Material":          name,
                "σ_y (MPa)":         m["yield_MPa"],
                "σ_u (MPa)":         m["ultimate_MPa"],
                "ρ (kg/m³)":         m["density_kg_m3"],
                "k (W/mK)":          m["k_W_mK"],
                "T_max (K)":         m["T_max_K"],
                "E (GPa)":           m["E_GPa"],
                "LPBF":              "✅" if m["printable_LPBF"] else "—",
                "CNC":               "✅" if m["machineable"]    else "—",
                "Cost (rel.)":       "💰" * m["cost_rel"],
                "Notes":             m["notes"],
            })

        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No materials match the current filters.")

        # Bar chart: yield strength comparison
        st.markdown("#### Yield strength comparison")
        try:
            import plotly.graph_objs as go
            mat_names = [n[:30] for n in MATERIALS]
            yields    = [m["yield_MPa"] for m in MATERIALS.values()]
            colors_bar= ['#50fa7b' if m["printable_LPBF"] else '#4fc3f7'
                         for m in MATERIALS.values()]
            fig = go.Figure(go.Bar(
                x=mat_names, y=yields, marker_color=colors_bar,
                text=[f"{y} MPa" for y in yields], textposition='outside'))
            fig.update_layout(
                paper_bgcolor='#1a1a1a', plot_bgcolor='#1a1a1a',
                font_color='white', height=320,
                yaxis_title="Yield strength (MPa)",
                margin=dict(t=10, b=80), xaxis_tickangle=-30)
            fig.add_annotation(x=0.01, y=0.95, xref='paper', yref='paper',
                               text="Green = LPBF printable  Blue = CNC only",
                               showarrow=False, font=dict(color='white', size=9))
            st.plotly_chart(fig, use_container_width=True)
        except ImportError:
            st.bar_chart(pd.DataFrame({'yield_MPa': [m["yield_MPa"] for m in MATERIALS.values()]},
                                       index=[n[:25] for n in MATERIALS]))

    # ════════════════════════════════════════════════════════════════
    # FLUIDS TAB
    # ════════════════════════════════════════════════════════════════
    with tab_fluid:
        st.markdown("### Fluid properties")
        fluid_rows = []
        for name, f in FLUIDS.items():
            Pr = f["viscosity_Pa_s"] * f["Cp_J_kgK"] / f["k_W_mK"]
            fluid_rows.append({
                "Fluid":          name,
                "ρ (kg/m³)":      f["density_kg_m3"],
                "μ (Pa·s)":       f"{f['viscosity_Pa_s']:.2e}",
                "Cp (J/kgK)":     f["Cp_J_kgK"],
                "k (W/mK)":       f["k_W_mK"],
                "Pr":             f"{Pr:.2f}",
                "T_boil (K)":     f["boiling_K"],
                "Phase":          f["phase"],
                "Notes":          f["notes"],
            })
        st.dataframe(pd.DataFrame(fluid_rows), use_container_width=True, hide_index=True)

    # ════════════════════════════════════════════════════════════════
    # COMPARE TAB
    # ════════════════════════════════════════════════════════════════
    with tab_compare:
        st.markdown("### Head-to-head material comparison")
        col1, col2 = st.columns(2)
        mat_a = col1.selectbox("Material A", list(MATERIALS.keys()), index=0)
        mat_b = col2.selectbox("Material B", list(MATERIALS.keys()), index=1)

        a = MATERIALS[mat_a]; b = MATERIALS[mat_b]
        props = ["yield_MPa", "ultimate_MPa", "density_kg_m3",
                 "k_W_mK", "T_max_K", "E_GPa"]
        labels = ["Yield strength (MPa)", "Ultimate strength (MPa)",
                  "Density (kg/m³)", "Thermal conductivity (W/mK)",
                  "Max temp (K)", "Young's modulus (GPa)"]

        compare_rows = []
        for prop, label in zip(props, labels):
            va = a[prop]; vb = b[prop]
            better = "A" if va > vb else "B" if vb > va else "="
            compare_rows.append({
                "Property": label,
                mat_a[:20]: va,
                mat_b[:20]: vb,
                "Better": f"{'→ ' + mat_a[:15] if better == 'A' else '→ ' + mat_b[:15] if better == 'B' else 'Equal'}",
            })

        st.dataframe(pd.DataFrame(compare_rows), use_container_width=True, hide_index=True)

        # Specific strength
        ss_a = a["yield_MPa"] * 1e6 / (a["density_kg_m3"] * 9.81) / 1000
        ss_b = b["yield_MPa"] * 1e6 / (b["density_kg_m3"] * 9.81) / 1000
        col1.metric("Specific strength A", f"{ss_a:.1f} kN·m/kg")
        col2.metric("Specific strength B", f"{ss_b:.1f} kN·m/kg")

        # Wall thickness comparison at same Pc
        st.markdown("#### Required wall thickness at same pressure")
        Pc_bar = st.slider("Chamber/housing pressure (bar)", 1.0, 200.0, 34.474, 1.0)
        R_mm   = st.slider("Radius (mm)", 5.0, 200.0, 22.5, 0.5)
        SF     = st.slider("Safety factor", 1.5, 5.0, 3.0, 0.5)
        Pc_Pa  = Pc_bar * 1e5; R_m = R_mm / 1000
        t_a_mm = Pc_Pa * R_m * SF / (a["yield_MPa"] * 1e6) * 1000
        t_b_mm = Pc_Pa * R_m * SF / (b["yield_MPa"] * 1e6) * 1000
        col1.metric(f"Wall t — {mat_a[:20]}", f"{t_a_mm:.3f} mm")
        col2.metric(f"Wall t — {mat_b[:20]}", f"{t_b_mm:.3f} mm")

    # ════════════════════════════════════════════════════════════════
    # CEM OVERRIDE TAB
    # ════════════════════════════════════════════════════════════════
    with tab_override:
        st.markdown("### Override CEM material selection")
        st.caption(
            "Change the material used by the CEM for housing and ratchet. "
            "Click **Apply & Regenerate** to recompute all geometry with the new material."
        )

        col1, col2 = st.columns(2)
        housing_mat = col1.selectbox(
            "Housing material",
            list(MATERIALS.keys()),
            index=list(MATERIALS.keys()).index("6061-T6 Aluminum"),
        )
        ratchet_mat = col2.selectbox(
            "Ratchet/Pawl material",
            list(MATERIALS.keys()),
            index=list(MATERIALS.keys()).index("4140 Steel (QT 40-45 HRC)"),
        )

        h = MATERIALS[housing_mat]; r = MATERIALS[ratchet_mat]
        col1.metric("Yield strength",  f"{h['yield_MPa']} MPa")
        col1.metric("T_max",           f"{h['T_max_K']} K")
        col2.metric("Yield strength",  f"{r['yield_MPa']} MPa")
        col2.metric("LPBF printable",  "Yes" if r["printable_LPBF"] else "No")

        if not h["machineable"] and not h["printable_LPBF"]:
            st.warning(f"{housing_mat} is neither CNC-machineable nor LPBF-printable. "
                       "Verify manufacturing route before selecting.")

        if st.button("✅ Apply material override & regenerate CEM", type="primary"):
            if 'aria_inputs' not in st.session_state:
                st.warning("Run the CEM Design tab first to load base inputs.")
            else:
                inp = st.session_state['aria_inputs']
                inp.material_housing = housing_mat
                inp.material_ratchet = ratchet_mat

                # Also update yield strengths so physics recomputes
                # We store overrides so aria_cem.py can pick them up
                st.session_state['material_override_housing'] = h
                st.session_state['material_override_ratchet'] = r
                st.session_state['aria_inputs'] = inp

                st.success(f"Material override applied: housing={housing_mat}, "
                           f"ratchet={ratchet_mat}. "
                           "Go to CEM Design tab and click Regenerate.")
