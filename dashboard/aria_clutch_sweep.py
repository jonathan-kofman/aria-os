"""
aria_clutch_sweep.py — Centrifugal Clutch Sensitivity Sweep Tab
Heatmap: flyweight mass × spring preload → engagement speed + detection margin.
Add to dashboard same as other tabs.
"""
import streamlit as st
import numpy as np
import pandas as pd
import sys
sys.path.insert(0, '.')


def _clutch_engagement(mass_g, spring_N, radius_mm, spool_r_mm):
    """Compute engagement rope speed and detection margin for given flyweight params."""
    G = 9.81
    m  = mass_g / 1000.0        # kg
    r  = radius_mm / 1000.0     # m (flyweight centroid radius)
    Rs = spool_r_mm / 1000.0    # m (spool radius for rope speed conversion)
    # At engagement: m * omega^2 * r = spring_N
    # omega = sqrt(spring_N / (m * r))
    if m <= 0 or r <= 0:
        return None, None
    omega_engage = np.sqrt(spring_N / (m * r))   # rad/s (spool)
    v_engage     = omega_engage * Rs              # m/s (rope speed)
    v_normal_climb = 0.3                          # m/s typical climbing speed
    margin         = v_engage / v_normal_climb
    return round(v_engage, 3), round(margin, 2)


def render_clutch_sweep():
    st.markdown("## Centrifugal Clutch Sensitivity Sweep")
    st.caption(
        "Heatmap showing engagement speed and detection margin across "
        "flyweight mass × spring preload combinations. "
        "Target: engage at 1.0–2.0 m/s, detection margin ≥ 3.0×."
    )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Geometry (fixed)**")
        spool_r_mm  = st.number_input("Spool hub radius (mm)", 30.0, 120.0, 60.0, 5.0)
        fw_r_mm     = st.number_input("Flyweight centroid radius (mm)", 20.0, 100.0, 60.0, 5.0,
                                      help="Distance from rotation axis to flyweight CG")
        n_flyweights= st.selectbox("Number of flyweights", [2, 3, 4], index=1)
    with col2:
        st.markdown("**Sweep ranges**")
        mass_min    = st.number_input("Min flyweight mass (g)", 10.0,  500.0,  50.0, 10.0)
        mass_max    = st.number_input("Max flyweight mass (g)", 50.0, 1000.0, 400.0, 10.0)
        spring_min  = st.number_input("Min spring preload (N)", 0.5,   20.0,   1.0,  0.5,
                                      help="Per flyweight")
        spring_max  = st.number_input("Max spring preload (N)", 2.0,   50.0,  15.0,  0.5)
        n_steps     = st.slider("Grid resolution", 10, 40, 20, 5)

    st.markdown("**Targets**")
    col_t1, col_t2, col_t3 = st.columns(3)
    v_target_lo = col_t1.number_input("Min engagement speed (m/s)", 0.3, 3.0, 1.0, 0.1)
    v_target_hi = col_t2.number_input("Max engagement speed (m/s)", 0.5, 5.0, 2.0, 0.1)
    margin_min  = col_t3.number_input("Min detection margin (×)", 1.5, 8.0, 3.0, 0.5)

    # ── Compute grid ──────────────────────────────────────────────────────────
    masses  = np.linspace(mass_min,   mass_max,   n_steps)
    springs = np.linspace(spring_min, spring_max, n_steps)

    v_grid      = np.zeros((n_steps, n_steps))
    margin_grid = np.zeros((n_steps, n_steps))

    for i, m in enumerate(masses):
        for j, s in enumerate(springs):
            v, marg = _clutch_engagement(m, s, fw_r_mm, spool_r_mm)
            v_grid[i, j]      = v      if v      is not None else 0
            margin_grid[i, j] = marg   if marg   is not None else 0

    # ── Plots ─────────────────────────────────────────────────────────────────
    try:
        import plotly.graph_objs as go

        tab_v, tab_m, tab_ok = st.tabs([
            "Engagement Speed (m/s)",
            "Detection Margin (×)",
            "✅ Valid Design Space"
        ])

        mass_labels   = [f"{m:.0f}" for m in masses]
        spring_labels = [f"{s:.1f}" for s in springs]

        with tab_v:
            fig = go.Figure(go.Heatmap(
                z=v_grid, x=spring_labels, y=mass_labels,
                colorscale='RdYlGn',
                zmin=0, zmax=max(v_target_hi * 1.5, v_grid.max()),
                colorbar=dict(title='m/s'),
            ))
            # Overlay target contours
            fig.add_contour_helper = None  # placeholder
            fig.update_layout(
                xaxis_title="Spring preload per flyweight (N)",
                yaxis_title="Flyweight mass (g)",
                paper_bgcolor='#1a1a1a', plot_bgcolor='#1a1a1a',
                font_color='white', height=420,
                margin=dict(t=20, b=50),
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"Green zone: {v_target_lo:.1f}–{v_target_hi:.1f} m/s engagement. "
                       "Red = too slow (may false-trip), dark green = too fast (late catch).")

        with tab_m:
            fig2 = go.Figure(go.Heatmap(
                z=margin_grid, x=spring_labels, y=mass_labels,
                colorscale='RdYlGn',
                zmin=0, zmax=min(10, margin_grid.max()),
                colorbar=dict(title='×'),
            ))
            fig2.update_layout(
                xaxis_title="Spring preload per flyweight (N)",
                yaxis_title="Flyweight mass (g)",
                paper_bgcolor='#1a1a1a', plot_bgcolor='#1a1a1a',
                font_color='white', height=420,
                margin=dict(t=20, b=50),
            )
            st.plotly_chart(fig2, use_container_width=True)
            st.caption(f"Detection margin = engagement speed / normal climbing speed (0.3 m/s). "
                       f"Green = ≥{margin_min:.1f}×. Values below {margin_min:.1f}× risk false triggers.")

        with tab_ok:
            # Boolean: both speed and margin in target zone
            ok_grid = (
                (v_grid >= v_target_lo) &
                (v_grid <= v_target_hi) &
                (margin_grid >= margin_min)
            ).astype(float)

            n_valid = int(ok_grid.sum())
            fig3 = go.Figure(go.Heatmap(
                z=ok_grid, x=spring_labels, y=mass_labels,
                colorscale=[[0, '#3a1a1a'], [1, '#50fa7b']],
                zmin=0, zmax=1,
                showscale=False,
            ))
            fig3.update_layout(
                xaxis_title="Spring preload per flyweight (N)",
                yaxis_title="Flyweight mass (g)",
                paper_bgcolor='#1a1a1a', plot_bgcolor='#1a1a1a',
                font_color='white', height=420,
                margin=dict(t=20, b=50),
            )
            st.plotly_chart(fig3, use_container_width=True)

            if n_valid == 0:
                st.error("No valid design points in this sweep range. "
                         "Adjust targets or expand mass/spring ranges.")
            else:
                st.success(f"{n_valid}/{n_steps*n_steps} design points meet all criteria "
                           f"({100*n_valid/(n_steps*n_steps):.1f}% of sweep space).")

    except ImportError:
        # Fallback without plotly
        st.warning("Install plotly for heatmap charts. Showing table instead.")
        rows = []
        for i, m in enumerate(masses[::2]):
            for j, s in enumerate(springs[::3]):
                v, marg = _clutch_engagement(m, s, fw_r_mm, spool_r_mm)
                ok = v_target_lo <= v <= v_target_hi and marg >= margin_min
                rows.append({'mass_g': m, 'spring_N': s,
                             'engage_v_ms': v, 'margin_x': marg, 'valid': ok})
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

    # ── Optimal point picker ──────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Find optimal design point")
    st.caption("Enter a specific mass + spring combination to evaluate.")

    col_e1, col_e2 = st.columns(2)
    eval_mass   = col_e1.number_input("Flyweight mass (g)",   10.0, 1000.0,
                                      float(st.session_state.get('aria_geom').clutch.flyweight_mass_g
                                            if 'aria_geom' in st.session_state else 213.0),
                                      1.0)
    eval_spring = col_e2.number_input("Spring preload (N)",    0.1,  50.0,
                                      float(st.session_state.get('aria_geom').clutch.spring_preload_N
                                            if 'aria_geom' in st.session_state else 5.78),
                                      0.1)

    v_eval, m_eval = _clutch_engagement(eval_mass, eval_spring, fw_r_mm, spool_r_mm)
    if v_eval is not None:
        col_r1, col_r2, col_r3 = st.columns(3)
        col_r1.metric("Engagement speed", f"{v_eval:.3f} m/s",
                      "✅ In range" if v_target_lo <= v_eval <= v_target_hi else "⚠️ Out of range")
        col_r2.metric("Detection margin", f"{m_eval:.2f}×",
                      "✅ OK" if m_eval >= margin_min else "⚠️ Too low — false trips possible")
        col_r3.metric("Valid design", "✅ YES" if (v_target_lo <= v_eval <= v_target_hi and m_eval >= margin_min) else "❌ NO")

        # INA / off-shelf spring recommendation
        st.markdown("**Nearest standard spring preload values:**")
        standard_springs = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0]
        for s in standard_springs:
            v_s, m_s = _clutch_engagement(eval_mass, s, fw_r_mm, spool_r_mm)
            ok = v_s is not None and v_target_lo <= v_s <= v_target_hi and m_s >= margin_min
            st.write(f"  {'✅' if ok else '  '} Spring {s:.1f} N → v={v_s:.3f} m/s, margin={m_s:.2f}×")
