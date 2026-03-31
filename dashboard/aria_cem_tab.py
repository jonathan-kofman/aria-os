"""
aria_cem_tab.py — ARIA CEM Design Tab for Streamlit Dashboard
Drop this file into your repo root alongside aria_dashboard.py.

Add to aria_dashboard.py:
    from aria_cem_tab import render_cem_tab

Then add to your sidebar setup list:
    "CEM Design (physics-derived geometry)"

And in your setup routing:
    elif setup.startswith("CEM Design"):
        render_cem_tab()

Requires: aria_cem.py and aria_cem_data files in same directory
"""

import streamlit as st
from .aria_design_history import log_cem_snapshot
import numpy as np
import pandas as pd
import io
import csv
import sys
import os

sys.path.insert(0, '.')


def render_cem_tab():
    st.markdown("## CEM Design — Physics-Derived Geometry")
    st.caption(
        "Change any parameter → hit **Regenerate** → download CSVs → import into Fusion. "
        "Every dimension recomputes from physics. No manual CAD edits needed."
    )

    # ── Parameter inputs ──────────────────────────────────────────────────────
    st.markdown("### Design Requirements")
    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("**Safety / ANSI**")
        max_arrest_kN    = st.number_input("Max arrest force (kN)", 4.0, 8.0, 6.0, 0.1,
                                            help="ANSI Z359.14 limit is 6.0 kN")
        min_hold_kN      = st.number_input("Ratchet hold force (kN)", 6.0, 12.0, 8.0, 0.5,
                                            help="ANSI requires ≥ 8 kN static hold")
        fall_detect_v    = st.number_input("Fall detection speed (m/s)", 0.8, 3.0, 1.5, 0.1,
                                            help="Clutch engages above this rope speed")
        SF_struct        = st.number_input("Structural safety factor", 2.0, 5.0, 3.0, 0.5)
        SF_fatigue       = st.number_input("Fatigue safety factor", 3.0, 8.0, 5.0, 0.5)

    with col2:
        st.markdown("**Geometry**")
        drum_d_mm        = st.number_input("Brake drum diameter (mm)", 150.0, 300.0, 200.0, 10.0)
        spool_d_mm       = st.number_input("Spool hub diameter (mm)", 80.0, 200.0, 120.0, 10.0)
        spool_od_mm      = st.number_input("Spool OD (mm)", 200.0, 700.0, 600.0, 10.0,
                                            help="Outer diameter of rope spool where rope wraps (affects motor torque and clutch RPM)")
        housing_od_mm    = st.number_input("Housing OD (mm)", 200.0, 400.0, 260.0, 10.0)
        rope_d_mm        = st.number_input("Rope diameter (mm)", 8.5, 11.0, 10.0, 0.5)
        rope_cap_m       = st.number_input("Rope capacity (m)", 20.0, 60.0, 40.0, 5.0)

    with col3:
        st.markdown("**Motor / Control**")
        tension_N        = st.number_input("Operating tension (N)", 20.0, 80.0, 40.0, 5.0)
        feed_speed_ms    = st.number_input("Feed speed (m/s)", 0.3, 2.0, 0.8, 0.1)
        retract_speed_ms = st.number_input("Retract speed (m/s)", 0.5, 3.0, 1.5, 0.1)
        bolt_pattern_mm  = st.number_input("Wall bolt circle (mm)", 100.0, 250.0, 150.0, 10.0)
        motor_v          = st.number_input("Motor voltage (V)", 12.0, 48.0, 24.0, 4.0)

    st.markdown("---")

    # ── Regenerate button ─────────────────────────────────────────────────────
    if st.button("⚙️ Regenerate All Geometry", type="primary", use_container_width=True):
        with st.spinner("Running CEM — physics deriving geometry..."):
            try:
                from .aria_cem import ARIAInputs, compute_aria, ARIAModule
                inputs = ARIAInputs(
                    max_arrest_force_kN     = max_arrest_kN,
                    min_hold_force_kN       = min_hold_kN,
                    fall_detection_v_m_s    = fall_detect_v,
                    max_fall_distance_m     = 1.0,
                    rope_diameter_mm        = rope_d_mm,
                    max_rope_capacity_m     = rope_cap_m,
                    slack_feed_speed_m_s    = feed_speed_ms,
                    max_retract_speed_m_s   = retract_speed_ms,
                    target_tension_N        = tension_N,
                    motor_voltage_V         = motor_v,
                    brake_drum_diameter_mm  = drum_d_mm,
                    rope_spool_hub_diameter_mm = spool_d_mm,
                    rope_spool_od_mm        = spool_od_mm,
                    housing_od_mm           = housing_od_mm,
                    wall_mount_bolt_pattern_mm = bolt_pattern_mm,
                    safety_factor_structural= SF_struct,
                    safety_factor_fatigue   = SF_fatigue,
                )
                geom = compute_aria(inputs)
                module = ARIAModule(inputs)
                module.geom = geom
                module.validate()

                st.session_state['aria_geom']   = geom
                st.session_state['aria_inputs'] = inputs
                st.session_state['aria_module'] = module
                log_cem_snapshot()
                st.success("Geometry computed ✅")

            except ImportError:
                st.error("aria_cem.py not found in path. Make sure it's in your repo root.")
                return
            except Exception as e:
                st.error(f"Regenerate failed: {e}")
                import traceback
                st.code(traceback.format_exc())
                return

    # ── Show results if computed ──────────────────────────────────────────────
    if 'aria_geom' not in st.session_state:
        st.info("Set parameters above and click Regenerate.")
        return

    geom    = st.session_state['aria_geom']
    inputs  = st.session_state['aria_inputs']
    module  = st.session_state['aria_module']

    # ── Validation ────────────────────────────────────────────────────────────
    st.markdown("### Validation")
    col_v1, col_v2 = st.columns(2)
    with col_v1:
        for msg in module.passed:
            st.success(msg.replace("OK: ",""))
    with col_v2:
        for msg in module.warnings:
            st.warning(msg.replace("WARNING: ",""))

    # ── Key dimensions ────────────────────────────────────────────────────────
    st.markdown("### Computed Dimensions")
    cols = st.columns(4)
    metrics = [
        ("Drum wall t", f"{geom.brake_drum.wall_thickness_mm:.2f} mm", f"SF={geom.brake_drum.safety_factor:.1f}"),
        ("Ratchet teeth", f"{geom.ratchet.n_teeth}", f"m=3, 26° PA"),
        ("Flyweight mass", f"{geom.clutch.flyweight_mass_g:.1f} g", f"×{geom.clutch.n_flyweights}"),
        ("Engage speed", f"{geom.clutch.engagement_v_m_s:.2f} m/s", f"{geom.clutch.safety_margin:.1f}× margin"),
        ("Spool capacity", f"{geom.spool.capacity_m:.1f} m", f"{geom.spool.layers} layers"),
        ("Gearbox ratio", f"{geom.motor.gearbox_ratio:.0f}:1", "one-way bearing req."),
        ("Arrest dist", f"{geom.predicted_arrest_distance_m:.3f} m", "ANSI ≤ 1.0m"),
        ("Peak force", f"{geom.predicted_peak_force_kN:.2f} kN", "ANSI ≤ 6.0kN"),
    ]
    for i, (label, val, delta) in enumerate(metrics):
        cols[i % 4].metric(label, val, delta)

    # ── ANSI compliance bar chart ─────────────────────────────────────────────
    st.markdown("### ANSI Performance")
    items   = ["Arrest dist (m)", "Peak force (kN)"]
    actuals = [geom.predicted_arrest_distance_m, geom.predicted_peak_force_kN]
    limits  = [1.0, 6.0]
    try:
        import plotly.graph_objects as go
        fig = go.Figure()
        colors  = ['#50fa7b' if a<=l else '#ff5555' for a,l in zip(actuals,limits)]
        fig.add_trace(go.Bar(x=items, y=actuals, marker_color=colors,
                              text=[f"{v:.3f}" for v in actuals],
                              textposition='outside', name='Actual'))
        for item, lim in zip(items, limits):
            fig.add_shape(type='line',
                          x0=items.index(item)-0.4, x1=items.index(item)+0.4,
                          y0=lim, y1=lim,
                          line=dict(color='red', width=2, dash='dash'))
        fig.update_layout(
            paper_bgcolor='#1a1a1a', plot_bgcolor='#1a1a1a',
            font_color='white', height=300,
            showlegend=False, margin=dict(t=20,b=20))
        st.plotly_chart(fig, use_container_width=True)
    except ImportError:
        st.bar_chart(pd.DataFrame({'Actual': actuals, 'ANSI Limit': limits},
                                   index=items))

    # ── CSV Downloads ─────────────────────────────────────────────────────────
    # ── Test data corrections (from Test Data tab) ────────────────────────────
    if 'cem_corrections' in st.session_state:
        c = st.session_state['cem_corrections']
        st.markdown("### Test Data vs CEM Prediction")
        st.caption(f"From test: **{c.get('test_id','unknown')}** ({c.get('test_date','')})")
        col_t1, col_t2, col_t3 = st.columns(3)

        if 'measured_arrest_distance_m' in c:
            predicted = float(st.session_state['aria_geom'].predicted_arrest_distance_m) if 'aria_geom' in st.session_state else None
            measured = float(c['measured_arrest_distance_m'])
            delta = f"{((measured - predicted) / predicted * 100):+.1f}% vs CEM" if predicted else "—"
            col_t1.metric("Arrest distance (measured)", f"{measured:.3f} m", delta)

        if 'measured_peak_force_kN' in c:
            predicted = float(st.session_state['aria_geom'].predicted_peak_force_kN) if 'aria_geom' in st.session_state else None
            measured = float(c['measured_peak_force_kN'])
            delta = f"{((measured - predicted) / predicted * 100):+.1f}% vs CEM" if predicted else "—"
            col_t2.metric("Peak force (measured)", f"{measured:.2f} kN", delta)

        if 'measured_catch_time_ms' in c:
            col_t3.metric("Catch time (measured)", f"{float(c['measured_catch_time_ms']):.1f} ms")

        if c.get('clutch_false_trigger'):
            st.warning("False clutch trigger detected — consider increasing fall_detection_v_m_s.")
        if c.get('ansi_fail'):
            st.error("Test FAILED ANSI Z359.14 limits — redesign required before next drop test.")
        if st.button("Clear test corrections", key="clear_corr_cem"):
            del st.session_state['cem_corrections']
            st.rerun()
        st.markdown("---")

    st.markdown("### Download Fusion 360 Import Files")
    st.caption("Each file: one ImportCSVPoints run. Filename tells you the operation and plane.")

    def make_csv_bytes(profiles):
        buf = io.StringIO()
        w   = csv.writer(buf)
        for i,pts in enumerate(profiles):
            for (x,z) in pts:
                w.writerow([f'{x:.4f}','0',f'{z:.4f}'])
            if i < len(profiles)-1:
                w.writerow([])
        return buf.getvalue().encode()

    def make_points_bytes(pts):
        buf = io.StringIO()
        w   = csv.writer(buf)
        for (x,z) in pts:
            w.writerow([f'{x:.4f}','0',f'{z:.4f}'])
        return buf.getvalue().encode()

    def circle(cx,cz,r,n=48):
        pts = [(cx+r*np.cos(a),cz+r*np.sin(a)) for a in np.linspace(0,2*np.pi,n,endpoint=False)]
        pts.append(pts[0]); return pts

    def rect(x0,z0,w,h):
        return [(x0,z0),(x0+w,z0),(x0+w,z0+h),(x0,z0+h),(x0,z0)]

    try:
        g = geom
        i = inputs

        # Precompute all profiles
        # Energy absorber
        F_act=2800; KE=0.5*100*i.fall_detection_v_m_s**2
        stroke=max((KE/F_act)*1000*3,80)
        Ro=30; t_w=max((8000*(Ro/1000))/(276e6/3)*1000,3)
        Ri=Ro-t_w; L=stroke+25; ft=8; fr=Ro+10
        absorber_shell=[(0,0),(0,Ri),(ft,Ri),(ft,fr),(ft+3,Ro),(L-ft-3,Ro),(L-ft,fr),(L,fr),(L,Ri),(L,0),(0,0)]

        # Motor mount
        T_m=i.min_hold_force_kN*1000*(g.spool.hub_diameter_mm/2/1000)/g.motor.gearbox_ratio
        tau=600e6*0.577/3
        Rbc=25; Fb=T_m/(4*Rbc/1000)
        db=max(np.sqrt(4*Fb/tau/np.pi)*1000,4)
        ds=max((16*T_m/(np.pi*tau))**(1/3)*1000,10)
        pr=(ds+0.5)/2; ph=Rbc+db*2+5
        mount_plate=rect(-ph,-ph,ph*2,ph*2)
        pilot_circ=circle(0,0,pr)
        bolt_pts=[(Rbc*np.cos(np.pi/4+k*np.pi/2),Rbc*np.sin(np.pi/4+k*np.pi/2)) for k in range(4)]

        # Bearing seat
        bm=max(np.ceil(i.target_tension_N*(g.spool.hub_diameter_mm/2/1000)/0.6/2)*2,12)
        bod=bm*1.5; bw=bm*2
        seat=[(0,0),(0,bm/2),(bw,bm/2),(bw,bod/2+1.5),(bw+3,bod/2+1.5),(bw+3,bod/2),(bw+8,bod/2),(bw+8,0),(0,0)]
        bear_ref=circle(bw/2,0,bod/2)

        # Rope guide
        Fg=i.min_hold_force_kN*1000; aL=60; ha=15
        wa=max(6*Fg*aL/1000/(276e6/3*(ha/1000)**2)*1000,6)
        gr=i.rope_diameter_mm/2+2; tcx=aL-gr; tcz=ha/2
        arm=[(0,0),(0,ha),(tcx-gr,ha)]+[(tcx+gr*np.cos(a),tcz+gr*np.sin(a)) for a in np.linspace(np.pi/2,-np.pi/2,20)]+[(tcx-gr,0),(0,0)]
        grv=circle(tcx,tcz,i.rope_diameter_mm/2+1)
        h1=circle(8,ha/2,4); h2=circle(20,ha/2,4)

        # Wall bracket
        Ft=g.total_mass_kg*9.81+i.min_hold_force_kN*1000*2
        ps=max(np.sqrt(Ft/5e6)*1000*1.5,150)
        gh=ps*0.7; Mg=Ft*0.10
        gt=max(Mg/(276e6/3*(gh/1000)**2/6)*1000,4)
        ad=max(np.sqrt(4*(Ft/4+Mg/(4*ps*0.35/1000))/(np.pi*600e6/3))*1000,8)
        ai=ad*2.5; Rd=i.wall_mount_bolt_pattern_mm/2
        backplate=rect(0,0,ps,ps)
        gusset=[(0,0),(gh,0),(0,gh),(0,0)]
        anchor_pts=[(ai,ai),(ps-ai,ai),(ai,ps-ai),(ps-ai,ps-ai)]
        dev_pts=[(ps/2+Rd*np.cos(np.pi/4+k*np.pi/2),ps/2+Rd*np.sin(np.pi/4+k*np.pi/2)) for k in range(4)]

        # ── Download buttons ──────────────────────────────────────────────────────
        st.markdown("**Energy Absorber**")
        col1, = st.columns(1)
        st.download_button(
            "absorber_REVOLVE_shell.csv — XZ plane, Fitted Splines, revolve 360° around X",
            make_csv_bytes([absorber_shell]),
            "absorber_REVOLVE_shell.csv", "text/csv", use_container_width=True)

        st.markdown(f"↳ L={L:.1f}mm, Ø{Ro*2:.0f}mm OD, stroke={stroke:.1f}mm, F_act=2.8kN")
        st.markdown("---")

        st.markdown("**Motor Mount**")
        st.download_button(
            "motormount_EXTRUDE_plate_bore.csv — XY, Fitted Splines, extrude plate, cut bore",
            make_csv_bytes([mount_plate, pilot_circ]),
            "motormount_EXTRUDE_plate_bore.csv", "text/csv", use_container_width=True)
        st.download_button(
            "motormount_POINTS_boltholes.csv — XY, Points style, draw circles for bolt holes",
            make_points_bytes(bolt_pts),
            "motormount_POINTS_boltholes.csv", "text/csv", use_container_width=True)
        st.markdown(f"↳ {ph*2:.0f}×{ph*2:.0f}mm plate, pilot Ø{pr*2:.1f}mm, {4}×M{db:.0f} on Ø{Rbc*2:.0f}mm BC")
        st.markdown("---")

        st.markdown("**One-Way Bearing Seat**")
        st.download_button(
            "bearingseat_REVOLVE_profile.csv — XZ, Fitted Splines, revolve bore profile 360°",
            make_csv_bytes([seat, bear_ref]),
            "bearingseat_REVOLVE_profile.csv", "text/csv", use_container_width=True)
        st.markdown(f"↳ bore Ø{bm:.0f}mm, bearing OD Ø{bod:.0f}mm, W={bw:.0f}mm → INA HFL{int(bm)}{int(bw)}")
        st.markdown("---")

        st.markdown("**Rope Guide**")
        st.download_button(
            "ropeguide_EXTRUDE_arm_groove_holes.csv — XY, Fitted Splines, extrude + cut",
            make_csv_bytes([arm, grv, h1, h2]),
            "ropeguide_EXTRUDE_arm_groove_holes.csv", "text/csv", use_container_width=True)
        st.markdown(f"↳ arm {aL:.0f}mm, {wa:.1f}×{ha:.0f}mm, groove R={gr:.1f}mm, 2× Ø8mm holes")
        st.markdown("---")

        st.markdown("**Wall Bracket**")
        st.download_button(
            "wallbracket_EXTRUDE_backplate.csv — XY, Fitted Splines, extrude plate",
            make_csv_bytes([backplate]),
            "wallbracket_EXTRUDE_backplate.csv", "text/csv", use_container_width=True)
        st.download_button(
            "wallbracket_EXTRUDE_gusset.csv — XZ offset from plate, extrude triangle",
            make_csv_bytes([gusset]),
            "wallbracket_EXTRUDE_gusset.csv", "text/csv", use_container_width=True)
        st.download_button(
            "wallbracket_POINTS_anchors.csv — XY, Points, concrete anchor holes",
            make_points_bytes(anchor_pts),
            "wallbracket_POINTS_anchors.csv", "text/csv", use_container_width=True)
        st.download_button(
            "wallbracket_POINTS_deviceholes.csv — XY, Points, ARIA housing bolt pattern",
            make_points_bytes(dev_pts),
            "wallbracket_POINTS_deviceholes.csv", "text/csv", use_container_width=True)
        st.markdown(f"↳ {ps:.0f}×{ps:.0f}mm plate, gusset {gh:.0f}mm, {4}×M{ad:.0f} anchors, device BC Ø{Rd*2:.0f}mm")
        st.markdown("---")

    except Exception as csv_err:
        st.error("CSV export failed — geometry may use a different structure than expected.")
        st.code(str(csv_err))
        import traceback
        with st.expander("Traceback"):
            st.code(traceback.format_exc())

    # ── Full dimensions table ─────────────────────────────────────────────────
    with st.expander("Full dimensions table"):
        rows = {
            "Brake drum diameter (mm)":     g.brake_drum.diameter_mm,
            "Brake drum wall (mm)":         g.brake_drum.wall_thickness_mm,
            "Brake drum SF":                g.brake_drum.safety_factor,
            "Ratchet teeth":                g.ratchet.n_teeth,
            "Ratchet face width (mm)":      g.ratchet.face_width_mm,
            "Flyweight mass (g)":           g.clutch.flyweight_mass_g,
            "Flyweight radius (mm)":        g.clutch.flyweight_radius_mm,
            "Spring preload (N)":           g.clutch.spring_preload_N,
            "Engagement speed (m/s)":       g.clutch.engagement_v_m_s,
            "Engagement RPM":               g.clutch.engagement_rpm,
            "Detection margin (×)":         g.clutch.safety_margin,
            "Spool hub dia (mm)":           g.spool.hub_diameter_mm,
            "Spool flange dia (mm)":        g.spool.flange_diameter_mm,
            "Spool width (mm)":             g.spool.width_mm,
            "Rope capacity (m)":            g.spool.capacity_m,
            "Gearbox ratio":                g.motor.gearbox_ratio,
            "Torque at spool (Nm)":         g.motor.motor_torque_Nm*g.motor.gearbox_ratio*0.85,
            "Housing OD (mm)":              g.housing.od_mm,
            "Housing wall (mm)":            g.housing.wall_thickness_mm,
            "Housing length (mm)":          g.housing.length_mm,
            "Arrest distance (m)":          g.predicted_arrest_distance_m,
            "Peak force (kN)":              g.predicted_peak_force_kN,
            "Catch time (ms)":              g.predicted_catch_time_ms,
            "Total mass (kg)":              g.total_mass_kg,
        }
        st.dataframe(pd.DataFrame.from_dict(rows, orient='index', columns=['Value'])
                     .style.format("{:.3f}"), use_container_width=True)

    # ── How to add to dashboard ───────────────────────────────────────────────
    with st.expander("How to wire this into aria_dashboard.py"):
        st.code("""
# In aria_dashboard.py, add to imports at top:
from aria_cem_tab import render_cem_tab

# Add to your setup list (wherever you define sidebar options):
setups = [
    ...existing setups...,
    "CEM Design (physics-derived geometry)",
]

# Add to your routing block:
elif setup.startswith("CEM Design"):
    render_cem_tab()
""", language="python")
