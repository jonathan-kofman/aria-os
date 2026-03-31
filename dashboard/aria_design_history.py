"""
aria_design_history.py — CEM Design History Log
=================================================
Tracks every CEM regeneration with a timestamped snapshot.
Drop this file in your repo root.

Usage in aria_cem_tab.py — after st.session_state['aria_geom'] is set:
    from aria_design_history import log_cem_snapshot, render_history_tab

Call log_cem_snapshot() any time you want to record the current state.
Call render_history_tab() to show the history browser in the dashboard.
"""

import json
import os
from datetime import datetime
from pathlib import Path
import streamlit as st

HISTORY_FILE = 'cem_design_history.json'
MAX_HISTORY_ENTRIES = 200


def _load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def _save_history(entries: list):
    # Keep only the most recent MAX_HISTORY_ENTRIES
    entries = entries[-MAX_HISTORY_ENTRIES:]
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        st.warning(f"Could not save design history: {e}")


def _geom_to_dict(geom, inputs) -> dict:
    """Convert CEM geometry and inputs to a serializable dict."""
    try:
        g = geom; i = inputs
        return {
            # Inputs
            'inputs': {
                'max_arrest_force_kN':    i.max_arrest_force_kN,
                'min_hold_force_kN':      i.min_hold_force_kN,
                'fall_detection_v_m_s':   i.fall_detection_v_m_s,
                'rope_diameter_mm':       i.rope_diameter_mm,
                'max_rope_capacity_m':    i.max_rope_capacity_m,
                'brake_drum_diameter_mm': i.brake_drum_diameter_mm,
                'rope_spool_hub_diameter_mm': i.rope_spool_hub_diameter_mm,
                'housing_od_mm':          i.housing_od_mm,
                'safety_factor_structural': i.safety_factor_structural,
                'safety_factor_fatigue':  i.safety_factor_fatigue,
                'target_tension_N':       i.target_tension_N,
                'motor_voltage_V':        i.motor_voltage_V,
            },
            # Key outputs
            'outputs': {
                'brake_drum_wall_mm':      round(g.brake_drum.wall_thickness_mm, 3),
                'brake_drum_sf':           round(g.brake_drum.safety_factor, 2),
                'ratchet_n_teeth':         g.ratchet.n_teeth,
                'ratchet_face_width_mm':   round(g.ratchet.face_width_mm, 2),
                'ratchet_sf':              round(g.ratchet.safety_factor, 2),
                'flyweight_mass_g':        round(g.clutch.flyweight_mass_g, 2),
                'flyweight_radius_mm':     round(g.clutch.flyweight_radius_mm, 2),
                'spring_preload_N':        round(g.clutch.spring_preload_N, 2),
                'engagement_v_ms':         round(g.clutch.engagement_v_m_s, 3),
                'detection_margin_x':      round(g.clutch.safety_margin, 2),
                'spool_hub_d_mm':          round(g.spool.hub_diameter_mm, 1),
                'spool_flange_d_mm':       round(g.spool.flange_diameter_mm, 1),
                'spool_width_mm':          round(g.spool.width_mm, 1),
                'rope_capacity_m':         round(g.spool.capacity_m, 1),
                'gearbox_ratio':           round(g.motor.gearbox_ratio, 1),
                'housing_od_mm':           round(g.housing.od_mm, 1),
                'housing_wall_mm':         round(g.housing.wall_thickness_mm, 2),
                'housing_length_mm':       round(g.housing.length_mm, 1),
                'arrest_distance_m':       round(g.predicted_arrest_distance_m, 4),
                'peak_force_kN':           round(g.predicted_peak_force_kN, 3),
                'catch_time_ms':           round(g.predicted_catch_time_ms, 1),
                'total_mass_kg':           round(g.total_mass_kg, 3),
            },
            # ANSI pass/fail
            'ansi': {
                'arrest_pass': g.predicted_arrest_distance_m <= 1.0,
                'force_pass':  g.predicted_peak_force_kN <= 6.0,
                'clutch_pass': g.clutch.safety_margin >= 3.0,
                'all_pass':    (g.predicted_arrest_distance_m <= 1.0 and
                                g.predicted_peak_force_kN <= 6.0 and
                                g.clutch.safety_margin >= 3.0),
            },
        }
    except Exception as e:
        return {'error': str(e)}


def log_cem_snapshot(label: str = ''):
    """
    Call this after every successful CEM regeneration to log the snapshot.
    Reads from st.session_state['aria_geom'] and st.session_state['aria_inputs'].
    """
    if 'aria_geom' not in st.session_state or 'aria_inputs' not in st.session_state:
        return

    geom   = st.session_state['aria_geom']
    inputs = st.session_state['aria_inputs']

    entry = {
        'timestamp': datetime.now().isoformat(),
        'label':     label.strip() or f"Run {datetime.now().strftime('%H:%M:%S')}",
        'data':      _geom_to_dict(geom, inputs),
    }

    history = _load_history()
    history.append(entry)
    _save_history(history)


def render_history_tab():
    """Render the design history browser as a Streamlit tab."""
    import pandas as pd

    st.markdown("## Design History")
    st.caption(
        "Every CEM regeneration is logged here automatically. "
        "Use this to track design evolution, compare iterations, and roll back if needed."
    )

    history = _load_history()

    if not history:
        st.info("No design history yet. Run the CEM Design tab and click Regenerate to start logging.")
        return

    # ── Summary table ─────────────────────────────────────────────────────────
    st.markdown(f"### {len(history)} logged runs")

    rows = []
    for idx, entry in enumerate(reversed(history)):
        d = entry.get('data', {})
        out = d.get('outputs', {})
        ansi = d.get('ansi', {})
        rows.append({
            '#':             len(history) - idx,
            'Time':          entry['timestamp'][:19].replace('T', ' '),
            'Label':         entry['label'],
            'Peak force (kN)': out.get('peak_force_kN', '—'),
            'Arrest dist (m)': out.get('arrest_distance_m', '—'),
            'Drum wall (mm)':  out.get('brake_drum_wall_mm', '—'),
            'Ratchet teeth':   out.get('ratchet_n_teeth', '—'),
            'Flyweight (g)':   out.get('flyweight_mass_g', '—'),
            'ANSI':            '✅' if ansi.get('all_pass') else '❌',
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # ── Compare two runs ──────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Compare two runs")

    run_labels = [f"#{len(history)-i}  {e['timestamp'][:19].replace('T',' ')}  {e['label']}"
                  for i, e in enumerate(reversed(history))]

    col1, col2 = st.columns(2)
    sel_a = col1.selectbox("Run A", run_labels, index=0,             key="hist_a")
    sel_b = col2.selectbox("Run B", run_labels, index=min(1, len(run_labels)-1), key="hist_b")

    idx_a = run_labels.index(sel_a)
    idx_b = run_labels.index(sel_b)
    entry_a = list(reversed(history))[idx_a]
    entry_b = list(reversed(history))[idx_b]

    out_a = entry_a.get('data', {}).get('outputs', {})
    out_b = entry_b.get('data', {}).get('outputs', {})

    compare_rows = []
    all_keys = sorted(set(list(out_a.keys()) + list(out_b.keys())))
    for key in all_keys:
        va = out_a.get(key, '—')
        vb = out_b.get(key, '—')
        try:
            diff = round(float(vb) - float(va), 4) if va != '—' and vb != '—' else '—'
            diff_str = f"+{diff}" if isinstance(diff, float) and diff > 0 else str(diff)
            changed = diff != 0 and diff != '—'
        except (TypeError, ValueError):
            diff_str = '—'; changed = va != vb
        compare_rows.append({
            'Parameter': key,
            f'A: {entry_a["label"][:20]}': va,
            f'B: {entry_b["label"][:20]}': vb,
            'Δ (B − A)': diff_str,
            'Changed': '⚠️' if changed else '',
        })

    compare_df = pd.DataFrame(compare_rows)
    # Highlight changed rows
    def highlight_changed(row):
        if row['Changed'] == '⚠️':
            return ['background-color: #2a1a00'] * len(row)
        return [''] * len(row)
    st.dataframe(
        compare_df.style.apply(highlight_changed, axis=1),
        use_container_width=True,
        hide_index=True
    )

    # ── Roll back ─────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Roll back to a previous run")
    st.caption("Loads a previous run's inputs back into session state. Click Regenerate in CEM Design to apply.")

    sel_rb = st.selectbox("Select run to roll back to", run_labels, key="hist_rb")
    idx_rb = run_labels.index(sel_rb)
    entry_rb = list(reversed(history))[idx_rb]

    with st.expander("Preview inputs from this run"):
        inp_data = entry_rb.get('data', {}).get('inputs', {})
        for k, v in inp_data.items():
            st.write(f"  {k}: {v}")

    if st.button("⏪ Load these inputs into CEM", type="primary"):
        inp_data = entry_rb.get('data', {}).get('inputs', {})
        if inp_data and 'aria_inputs' in st.session_state:
            inp = st.session_state['aria_inputs']
            for k, v in inp_data.items():
                if hasattr(inp, k):
                    try:
                        setattr(inp, k, type(getattr(inp, k))(v))
                    except Exception:
                        pass
            st.session_state['aria_inputs'] = inp
            st.success(f"Inputs loaded from run '{entry_rb['label']}'. Go to CEM Design and click Regenerate.")
        else:
            st.warning("No CEM inputs in session state. Open CEM Design tab first.")

    # ── Export ────────────────────────────────────────────────────────────────
    st.markdown("---")
    col_exp1, col_exp2 = st.columns(2)
    with col_exp1:
        if st.button("📥 Export full history as JSON"):
            st.download_button(
                "Download cem_design_history.json",
                json.dumps(history, indent=2).encode(),
                file_name="cem_design_history.json",
                mime="application/json",
            )
    with col_exp2:
        if st.button("🗑️ Clear history", type="secondary"):
            if st.session_state.get('confirm_clear_history'):
                _save_history([])
                st.session_state['confirm_clear_history'] = False
                st.success("History cleared.")
                st.rerun()
            else:
                st.session_state['confirm_clear_history'] = True
                st.warning("Click again to confirm clearing all history.")
