"""
aria_testdata_tab.py — Test Data Feedback Tab for ARIA Dashboard
Drop next to aria_dashboard.py. Add to SETUPS and routing same as CEM tab.

Add to aria_dashboard.py:
    from aria_testdata_tab import render_testdata_tab

SETUPS entry:
    "Test Data & Calibration": ["Ingest hot fire / drop test data"],

Routing:
    elif setup.startswith("Test Data"):
        render_testdata_tab()
"""

import streamlit as st
import pandas as pd
import numpy as np
import os, io, json
from datetime import datetime

def render_testdata_tab():
    st.markdown("## Test Data & Calibration")
    st.caption(
        "Upload a CSV from any hot fire or drop test. "
        "The model computes correction factors and shows what changed vs prediction. "
        "No data yet? Generate a realistic mock test to validate the pipeline."
    )

    # ── Expected column reference ─────────────────────────────────────────────
    with st.expander("Expected CSV columns (flexible — auto-detected)", expanded=False):
        st.markdown("""
| Column | Required | Notes |
|--------|----------|-------|
| `time_s` | Yes | Time in seconds |
| `Pc_bar` or `Pc_psi` | Yes | Chamber / rope tension pressure |
| `thrust_N` or `thrust_lbf` | No | For Isp calculation |
| `mdot_ox_kg_s` | No | LOX/fluid mass flow |
| `mdot_fuel_kg_s` | No | Fuel/fluid mass flow |
| `tension_N` | No | For ARIA drop tests |
| `T_wall_K` | No | Thermocouple data |

The column names are flexible — the ingester auto-detects common variations.
        """)

    st.markdown("---")

    # ── Tabs: Upload real data vs generate mock ───────────────────────────────
    tab_upload, tab_mock, tab_history = st.tabs(
        ["Upload Test Data", "Generate Mock Data", "Correction History"])

    # ════════════════════════════════════════════════════════════════════════
    # TAB 1 — Upload real data
    # ════════════════════════════════════════════════════════════════════════
    with tab_upload:
        st.markdown("### Upload test CSV")

        uploaded = st.file_uploader(
            "Drag and drop your test CSV here",
            type=["csv"],
            key="testdata_upload",
        )

        col1, col2 = st.columns(2)
        with col1:
            test_type = st.selectbox(
                "Test type",
                ["Drop test (ARIA)", "Hot fire (LRE)", "Waterflow / cold flow", "Other"],
            )
        with col2:
            predicted_cstar = st.number_input(
                "Predicted c* (m/s) — from CEM",
                min_value=0.0, max_value=3000.0, value=1687.0, step=1.0,
                help="From your CEM run — used to compute eta_c*",
            )
            predicted_isp = st.number_input(
                "Predicted Isp (s) — from CEM",
                min_value=0.0, max_value=500.0, value=253.6, step=0.1,
            )

        if uploaded is not None:
            try:
                df_raw = pd.read_csv(uploaded)
                st.success(f"Loaded {len(df_raw)} rows × {len(df_raw.columns)} columns")

                with st.expander("Raw data preview"):
                    st.dataframe(df_raw.head(20), use_container_width=True)

                # ── Auto-detect columns ───────────────────────────────────────
                cols_lower = {c.lower().strip(): c for c in df_raw.columns}

                def find_col(*aliases):
                    for a in aliases:
                        if a in cols_lower:
                            return cols_lower[a]
                    return None

                t_col    = find_col('time_s', 'time', 't', 't_s')
                pc_col   = find_col('pc_bar', 'pc', 'chamber_pressure', 'tension_n',
                                     'tension', 'pc_psi', 'pc_psia')
                f_col    = find_col('thrust_n', 'thrust', 'force', 'f_n', 'thrust_lbf')
                mo_col   = find_col('mdot_ox_kg_s', 'mdot_ox', 'mdot_lox', 'ox_flow')
                mf_col   = find_col('mdot_fuel_kg_s', 'mdot_fuel', 'mdot_kero', 'fuel_flow')
                wall_col = find_col('t_wall_k', 't_wall', 'wall_temp')

                detected = {k: v for k, v in {
                    'time': t_col, 'pressure/tension': pc_col, 'thrust': f_col,
                    'mdot_ox': mo_col, 'mdot_fuel': mf_col, 'T_wall': wall_col,
                }.items() if v}

                st.markdown("**Auto-detected columns:**")
                st.json(detected)

                if t_col is None or pc_col is None:
                    st.error("Could not detect time or pressure/tension column. "
                             "Rename columns to match the expected format above.")
                    return

                # ── Unit conversion ───────────────────────────────────────────
                pc_header = pc_col.lower()
                if 'psi' in pc_header:
                    pc_pa = df_raw[pc_col] * 6894.76
                elif 'bar' in pc_header:
                    pc_pa = df_raw[pc_col] * 1e5
                else:
                    pc_pa = df_raw[pc_col]   # assume Pa or N for tension

                t_arr = df_raw[t_col].values
                pc_arr = pc_pa.values

                f_arr = None
                if f_col:
                    f_header = f_col.lower()
                    f_arr = df_raw[f_col].values * (4.44822 if 'lbf' in f_header else 1.0)

                # ── Steady-state detection ────────────────────────────────────
                st.markdown("### Steady-state window")
                pc_max = float(np.max(pc_arr))
                ss_threshold = st.slider(
                    "Steady-state threshold (% of peak)",
                    min_value=50, max_value=95, value=80, step=5,
                ) / 100.0

                ss_mask = pc_arr > ss_threshold * pc_max
                ss_idx  = np.where(ss_mask)[0]

                if len(ss_idx) < 5:
                    st.warning("Very few steady-state points detected. Try lowering the threshold.")
                    ss_start = float(t_arr[0])
                    ss_end   = float(t_arr[-1])
                else:
                    buf = int(0.1 * len(ss_idx))
                    i0  = ss_idx[buf]
                    i1  = ss_idx[-buf] if buf > 0 else ss_idx[-1]
                    ss_start = float(t_arr[i0])
                    ss_end   = float(t_arr[i1])

                col_ss1, col_ss2 = st.columns(2)
                ss_start = col_ss1.number_input("SS start (s)", value=ss_start, step=0.1)
                ss_end   = col_ss2.number_input("SS end (s)",   value=ss_end,   step=0.1)

                ss_filt   = (t_arr >= ss_start) & (t_arr <= ss_end)
                pc_ss     = pc_arr[ss_filt]
                pc_mean   = float(np.mean(pc_ss)) if len(pc_ss) > 0 else 0.0
                pc_std    = float(np.std(pc_ss))  if len(pc_ss) > 0 else 0.0
                burn_time = ss_end - ss_start

                # ── Charts ────────────────────────────────────────────────────
                try:
                    import plotly.graph_objs as go
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=t_arr, y=pc_arr / (1e5 if 'pa' not in pc_header else 1.0),
                        mode='lines', name='Pressure / Tension',
                        line=dict(color='#4fc3f7', width=1.5)))
                    fig.add_vrect(x0=ss_start, x1=ss_end,
                                  fillcolor='rgba(80,250,123,0.15)',
                                  line_width=0, annotation_text='Steady state')
                    fig.update_layout(
                        paper_bgcolor='#1a1a1a', plot_bgcolor='#1a1a1a',
                        font_color='white', height=300,
                        xaxis_title='Time (s)',
                        yaxis_title='bar' if 'bar' in pc_header else 'N' if 'tension' in pc_header else 'value',
                        margin=dict(t=20, b=30))
                    st.plotly_chart(fig, use_container_width=True)
                except ImportError:
                    st.line_chart(pd.DataFrame({'pressure': pc_arr}, index=t_arr))

                # ── Correction factors ────────────────────────────────────────
                st.markdown("### Computed corrections")

                corrections = {
                    'test_id':      uploaded.name.replace('.csv', ''),
                    'test_date':    datetime.now().strftime('%Y-%m-%d %H:%M'),
                    'test_type':    test_type,
                    'burn_time_s':  round(burn_time, 2),
                    'Pc_mean':      round(pc_mean, 2),
                    'Pc_std':       round(pc_std, 2),
                    'Pc_rms_pct':   round(pc_std / max(pc_mean, 1) * 100, 2),
                    'n_ss_points':  int(np.sum(ss_filt)),
                }

                # Hard start check
                pc_peak = float(np.max(pc_arr))
                corrections['hard_start'] = pc_peak > pc_mean * 1.5
                corrections['Pc_peak_to_mean'] = round(pc_peak / max(pc_mean, 1), 3)

                # Combustion stability
                corrections['combustion_stable'] = corrections['Pc_rms_pct'] < 5.0

                # eta_c* if we have mass flow data
                mo_arr = df_raw[mo_col].values[ss_filt] if mo_col else None
                mf_arr = df_raw[mf_col].values[ss_filt] if mf_col else None

                if mo_arr is not None and mf_arr is not None and pc_mean > 0:
                    mdot_total = float(np.mean(mo_arr + mf_arr))
                    # At_design from predicted c*: At = mdot*c*/Pc
                    At_design = mdot_total * predicted_cstar / pc_mean
                    cstar_actual = pc_mean * At_design / mdot_total
                    corrections['eta_cstar'] = round(cstar_actual / predicted_cstar, 4)
                    corrections['cstar_actual'] = round(cstar_actual, 1)

                if f_arr is not None and mo_arr is not None and mf_arr is not None:
                    f_ss    = f_arr[ss_filt]
                    f_mean  = float(np.mean(f_ss))
                    mdot_ss = float(np.mean(mo_arr + mf_arr))
                    isp_act = f_mean / (mdot_ss * 9.80665)
                    corrections['Isp_actual'] = round(isp_act, 1)
                    corrections['eta_Isp']    = round(isp_act / predicted_isp, 4)
                    if 'eta_cstar' in corrections:
                        corrections['eta_Cf'] = round(corrections['eta_Isp'] /
                                                       corrections['eta_cstar'], 4)

                # Display corrections
                col_a, col_b, col_c, col_d = st.columns(4)
                col_a.metric("Burn time", f"{corrections['burn_time_s']:.2f} s")
                col_b.metric("Pc mean", f"{corrections['Pc_mean']/1e5:.2f} bar" if pc_mean > 1e4 else f"{corrections['Pc_mean']:.1f} N")
                col_c.metric("Pc stability", f"{corrections['Pc_rms_pct']:.2f}% RMS",
                             "✅ Stable" if corrections['combustion_stable'] else "⚠️ Check")
                col_d.metric("Hard start", "Yes ⚠️" if corrections['hard_start'] else "No ✅")

                if 'eta_cstar' in corrections:
                    col_e, col_f, col_g = st.columns(3)
                    col_e.metric("c* actual", f"{corrections.get('cstar_actual', '—')} m/s")
                    col_f.metric("η_c*", f"{corrections['eta_cstar']:.4f}",
                                 f"{'▲' if corrections['eta_cstar'] >= 1.0 else '▼'} vs prediction")
                    if 'eta_Isp' in corrections:
                        col_g.metric("η_Isp", f"{corrections['eta_Isp']:.4f}")

                # ── Design suggestions ────────────────────────────────────────
                st.markdown("### Design suggestions from this test")
                suggestions = []

                if corrections['hard_start']:
                    suggestions.append("⚠️ **Hard start detected** — peak/mean ratio "
                                       f"{corrections['Pc_peak_to_mean']:.2f}×. "
                                       "Consider reducing oxidizer lead time or adding "
                                       "a flow restrictor to LOX manifold.")

                if not corrections['combustion_stable']:
                    suggestions.append(f"⚠️ **Instability** — Pc RMS {corrections['Pc_rms_pct']:.1f}% "
                                       "(target <5%). Check injector element spacing "
                                       "vs acoustic mode frequency. Increase injector ΔP "
                                       "to 25% Pc.")

                if 'eta_cstar' in corrections:
                    eta = corrections['eta_cstar']
                    if eta < 0.92:
                        suggestions.append(f"⚠️ **Low c* efficiency** ({eta:.3f}) — "
                                           "Consider increasing impingement angle, "
                                           "adjusting O/F ratio, or checking injector Cd values "
                                           "against cold flow measurements.")
                    elif eta > 1.02:
                        suggestions.append(f"ℹ️ **η_c* > 1.0** ({eta:.3f}) — "
                                           "CEA data may need recalibration at this Pc. "
                                           "Verify mass flow measurements.")
                    else:
                        suggestions.append(f"✅ **Good c* efficiency** ({eta:.3f}) — "
                                           "Within 2% of prediction. "
                                           "Update Cd in CEM with measured orifice data.")

                if not suggestions:
                    suggestions.append("✅ All checks nominal — no design changes suggested.")

                for s in suggestions:
                    st.markdown(f"- {s}")

                # ── Save corrections ──────────────────────────────────────────
                st.markdown("### Save corrections")
                st.caption("Saved corrections persist across sessions and feed back into the CEM.")

                if st.button("💾 Save correction factors", type="primary"):
                    os.makedirs("test_sessions", exist_ok=True)
                    fname = f"test_sessions/{corrections['test_id']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                    with open(fname, 'w') as fp:
                        json.dump(corrections, fp, indent=2)
                    st.success(f"Saved to {fname}")
                    st.session_state['latest_corrections'] = corrections

                # ── Export corrected CSV ──────────────────────────────────────
                corr_df = pd.DataFrame([{
                    'Parameter': k, 'Value': str(v)
                } for k, v in corrections.items()])
                csv_bytes = corr_df.to_csv(index=False).encode()
                st.download_button(
                    "⬇️ Download corrections CSV",
                    csv_bytes,
                    file_name=f"corrections_{corrections['test_id']}.csv",
                    mime="text/csv",
                )

            except Exception as e:
                st.error(f"Error processing file: {e}")
                import traceback
                st.code(traceback.format_exc())

    # ════════════════════════════════════════════════════════════════════════
    # TAB 2 — Generate mock data
    # ════════════════════════════════════════════════════════════════════════
    with tab_mock:
        st.markdown("### Generate realistic mock test data")
        st.caption("Use this to validate the pipeline before you have real hardware data.")

        mock_type = st.selectbox(
            "Mock test type",
            ["ARIA drop test", "LRE hot fire", "Waterflow cold flow"],
            key="mock_type"
        )

        col1, col2, col3 = st.columns(3)
        with col1:
            if mock_type == "ARIA drop test":
                peak_val    = st.number_input("Peak tension (N)", 500.0, 8000.0, 3500.0, 100.0)
                burn_t      = st.number_input("Drop duration (s)", 0.1, 2.0, 0.3, 0.05)
                eta         = st.number_input("Efficiency (arrest fraction)", 0.5, 1.0, 0.85, 0.01)
            else:
                peak_val    = st.number_input("Design Pc (bar)", 5.0, 100.0, 34.474, 0.5)
                burn_t      = st.number_input("Burn time (s)", 1.0, 30.0, 7.0, 0.5)
                eta         = st.number_input("c* efficiency", 0.80, 1.02, 0.94, 0.01)
        with col2:
            noise_pct   = st.slider("Noise (%)", 0, 10, 2, 1)
            sample_rate = st.selectbox("Sample rate (Hz)", [100, 500, 1000], index=0)
        with col3:
            hard_start  = st.checkbox("Simulate hard start", value=False)
            instability = st.checkbox("Simulate instability (5% RMS)", value=False)

        if st.button("⚙️ Generate mock data", type="primary"):
            np.random.seed(42)
            dt     = 1.0 / sample_rate
            t_arr  = np.arange(0, burn_t + 1.5, dt)
            n      = len(t_arr)
            sig    = np.zeros(n)
            noise_scale = noise_pct / 100.0

            peak_pa = peak_val * (1e5 if mock_type != "ARIA drop test" else 1.0)

            for i, t in enumerate(t_arr):
                if t < 0.2:
                    ramp = (t / 0.2) ** 2
                    sig[i] = peak_pa * ramp * (1.5 if hard_start and t > 0.1 else 1.0)
                elif t < 0.5:
                    sig[i] = peak_pa * (1.0 - 0.05 * np.exp(-(t - 0.2) / 0.1))
                elif t <= burn_t:
                    noise = np.random.normal(0, noise_scale * peak_pa)
                    if instability:
                        noise += 0.03 * peak_pa * np.sin(2 * np.pi * 12 * t)
                    sig[i] = peak_pa * eta + noise
                else:
                    sig[i] = peak_pa * eta * np.exp(-(t - burn_t) / 0.15)
                sig[i] = max(sig[i], 0)

            col_name = 'Pc_bar' if mock_type != "ARIA drop test" else 'tension_N'
            scale    = 1e5 if mock_type != "ARIA drop test" else 1.0
            mock_df  = pd.DataFrame({
                'time_s': t_arr,
                col_name: sig / scale,
            })
            if mock_type == "LRE hot fire":
                OF = 1.6; mdot = 0.808
                mo = mdot * OF / (1 + OF)
                mf = mdot / (1 + OF)
                mock_df['mdot_ox_kg_s']   = np.where(
                    (t_arr >= 0.3) & (t_arr <= burn_t),
                    mo * (1 + np.random.normal(0, 0.005, n)), 0)
                mock_df['mdot_fuel_kg_s'] = np.where(
                    (t_arr >= 0.3) & (t_arr <= burn_t),
                    mf * (1 + np.random.normal(0, 0.005, n)), 0)
                mock_df['thrust_N'] = np.where(
                    (t_arr >= 0.3) & (t_arr <= burn_t),
                    2224.0 * eta + np.random.normal(0, 0.02 * 2224, n), 0)

            st.session_state['mock_df'] = mock_df
            st.success(f"Generated {len(mock_df)} samples at {sample_rate}Hz")

        if 'mock_df' in st.session_state:
            mock_df = st.session_state['mock_df']
            st.line_chart(mock_df.set_index('time_s').iloc[:, 0], use_container_width=True)
            csv_out = mock_df.to_csv(index=False).encode()
            st.download_button(
                "⬇️ Download mock CSV (use in Upload tab to test pipeline)",
                csv_out,
                file_name=f"mock_{mock_type.replace(' ', '_').lower()}.csv",
                mime="text/csv",
            )

    # ════════════════════════════════════════════════════════════════════════
    # TAB 3 — Correction history
    # ════════════════════════════════════════════════════════════════════════
    with tab_history:
        st.markdown("### Correction factor history")
        st.caption("All saved correction factors from past tests.")

        os.makedirs("test_sessions", exist_ok=True)
        session_files = sorted([f for f in os.listdir("test_sessions") if f.endswith(".json")])

        if not session_files:
            st.info("No saved corrections yet. Upload a test CSV and click Save.")
        else:
            records = []
            for fname in session_files:
                try:
                    with open(f"test_sessions/{fname}") as fp:
                        records.append(json.load(fp))
                except Exception:
                    pass

            if records:
                df_hist = pd.DataFrame(records)
                display_cols = [c for c in ['test_date', 'test_id', 'test_type',
                                            'burn_time_s', 'eta_cstar', 'eta_Isp',
                                            'Pc_rms_pct', 'combustion_stable',
                                            'hard_start'] if c in df_hist.columns]
                st.dataframe(df_hist[display_cols], use_container_width=True, hide_index=True)

                if 'eta_cstar' in df_hist.columns:
                    st.markdown("**η_c* trend across tests**")
                    trend = df_hist[['test_id', 'eta_cstar']].dropna()
                    if not trend.empty:
                        st.bar_chart(trend.set_index('test_id')['eta_cstar'])

                # Show latest corrections that can feed into CEM
                latest = st.session_state.get('latest_corrections', records[-1] if records else None)
                if latest and st.button("Apply latest corrections to CEM"):
                    st.session_state['cem_corrections'] = latest
                    st.success("Corrections loaded — open CEM Design tab and click Regenerate to apply.")
