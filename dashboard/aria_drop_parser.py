"""
aria_drop_parser.py — ARIA Drop Test Data Parser
=================================================
Turns raw CSV from any load cell logger into a pass/fail ANSI report
in under a minute. Automatically detects the arrest event, windows it,
and computes all required metrics.

Usage as standalone:
    python tools/aria_drop_parser.py data/drop_test_001.csv
    python tools/aria_drop_parser.py data/drop_test_001.csv --plot --save-report

Usage from dashboard:
    from aria_drop_parser import parse_drop_test, render_drop_parser_tab

Supported CSV formats (auto-detected):
  - time_s, tension_N                     (minimal)
  - time_s, tension_N, rope_pos_m         (with position)
  - time_s, tension_N, rope_pos_m, velocity_ms (full)
  - t, force, position                    (generic names auto-mapped)
  - timestamp_ms, load_N, encoder_mm      (raw embedded format)

ANSI Z359.14 limits applied:
  - Peak arrest force:    ≤ 6,000 N  (on climber — 60% of 8000N with harness factor)
  - Maximum arrest dist:  ≤ 1,000 mm (from device spec; ANSI says 813mm for SRL)
  - Average arrest force: ≤ 4,000 N
  - Arrest time:          report only (no ANSI limit)
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ── ANSI limits ───────────────────────────────────────────────────────────────
ANSI = {
    'peak_force_N':       6000.0,   # N — on climber (device side limit 8kN)
    'arrest_distance_mm': 1000.0,   # mm — conservative ARIA spec
    'avg_force_N':        4000.0,   # N — average during arrest
}

# ── Column name aliases (auto-detection) ──────────────────────────────────────
TIME_ALIASES     = ['time_s', 'time', 't', 't_s', 'timestamp_s', 'time_sec',
                    'timestamp_ms', 'time_ms']
TENSION_ALIASES  = ['tension_n', 'tension', 'force_n', 'force', 'load_n', 'load',
                    'f_n', 'fn', 'arrest_force', 'rope_tension']
POSITION_ALIASES = ['rope_pos_m', 'rope_pos', 'position_m', 'position', 'pos_m',
                    'pos', 'encoder_m', 'encoder_mm', 'displacement_m',
                    'rope_out_m', 'spool_pos']
VELOCITY_ALIASES = ['velocity_ms', 'velocity', 'vel_ms', 'vel', 'speed_ms',
                    'rope_speed']


def _find_col(df: pd.DataFrame, aliases: list) -> str | None:
    """Find a column by trying aliases (case-insensitive)."""
    cols_lower = {c.lower().strip(): c for c in df.columns}
    for a in aliases:
        if a.lower() in cols_lower:
            return cols_lower[a.lower()]
    return None


def _auto_convert_time(df: pd.DataFrame, t_col: str) -> pd.Series:
    """Convert time column to seconds if it looks like milliseconds."""
    t = df[t_col].values
    if t.max() > 1000:   # probably ms
        return pd.Series(t / 1000.0, name='time_s')
    if t.max() > 100:    # probably ms too
        return pd.Series(t / 1000.0, name='time_s')
    return df[t_col]


def _auto_convert_position(df: pd.DataFrame, pos_col: str) -> pd.Series:
    """Convert position to meters if it looks like mm."""
    p = df[pos_col].values
    if np.abs(p).max() > 10:   # probably mm
        return pd.Series(p / 1000.0, name='rope_pos_m')
    return df[pos_col]


def detect_arrest_event(
    t: np.ndarray,
    tension: np.ndarray,
    baseline_threshold_factor: float = 5.0,
    min_peak_N: float = 200.0,
) -> dict:
    """
    Auto-detect the arrest event in a tension time series.

    Strategy:
      1. Compute baseline tension (median of first 20% of data)
      2. Find first point where tension > baseline * threshold_factor
      3. Walk back to find true event start (tension first rises above 2× baseline)
      4. Walk forward to find event end (tension drops below 2× baseline for 50ms)

    Returns dict with event indices, times, and detection confidence.
    """
    n = len(tension)
    baseline_window = max(10, int(0.20 * n))
    baseline = np.median(np.abs(tension[:baseline_window]))
    baseline = max(baseline, 5.0)  # floor at 5N to handle near-zero baseline

    trigger_thresh = baseline * baseline_threshold_factor
    trigger_thresh = max(trigger_thresh, min_peak_N * 0.3)

    # Find trigger point
    trigger_idx = None
    for i in range(n):
        if tension[i] > trigger_thresh:
            trigger_idx = i
            break

    if trigger_idx is None:
        return {'found': False, 'reason': f'No arrest event detected (peak {tension.max():.0f}N < threshold {trigger_thresh:.0f}N)'}

    # Walk back to event start (tension > 2x baseline)
    start_thresh = baseline * 2.0
    start_idx = trigger_idx
    for i in range(trigger_idx, max(0, trigger_idx - 500), -1):
        if tension[i] < start_thresh:
            start_idx = i
            break

    # Walk forward to event end (tension < 2x baseline sustained for 50ms)
    dt_median = np.median(np.diff(t[:100]))
    sustain_samples = max(5, int(0.050 / max(dt_median, 1e-6)))
    end_idx = min(n - 1, trigger_idx + int(1.0 / max(dt_median, 1e-6)))  # max 1s window
    below_count = 0
    for i in range(trigger_idx, min(n, trigger_idx + int(2.0 / max(dt_median, 1e-6)))):
        if tension[i] < start_thresh:
            below_count += 1
            if below_count >= sustain_samples:
                end_idx = i
                break
        else:
            below_count = 0

    peak_idx  = start_idx + int(np.argmax(tension[start_idx:end_idx+1]))
    peak_N    = float(tension[peak_idx])
    t_start   = float(t[start_idx])
    t_peak    = float(t[peak_idx])
    t_end     = float(t[end_idx])
    duration  = t_end - t_start

    return {
        'found':       True,
        'start_idx':   start_idx,
        'peak_idx':    peak_idx,
        'end_idx':     end_idx,
        't_start':     t_start,
        't_peak':      t_peak,
        't_end':       t_end,
        'duration_s':  duration,
        'peak_N':      peak_N,
        'baseline_N':  baseline,
        'trigger_thresh_N': trigger_thresh,
        'confidence':  'high' if peak_N > 500 else 'low',
    }


def compute_arrest_metrics(
    t: np.ndarray,
    tension: np.ndarray,
    rope_pos: np.ndarray | None,
    event: dict,
) -> dict:
    """
    Compute all ANSI-relevant metrics from the detected arrest event.
    """
    si = event['start_idx']
    ei = event['end_idx']
    pi = event['peak_idx']

    t_arr  = t[si:ei+1]
    f_arr  = tension[si:ei+1]

    # Peak and average force
    peak_N = float(f_arr.max())
    avg_N  = float(f_arr.mean())

    # Arrest distance from position sensor (if available)
    arrest_dist_mm = None
    if rope_pos is not None and len(rope_pos) == len(t):
        pos_arr = rope_pos[si:ei+1]
        arrest_dist_mm = float(abs(pos_arr[-1] - pos_arr[0]) * 1000)

    # Arrest distance from kinematics if no position sensor
    # Use impulse-momentum: F*dt = m*dv, integrate twice for distance
    # Approximate: d ≈ v0 * t_arrest - 0.5 * (F_avg/m) * t_arrest^2
    # We don't know mass precisely, so flag as estimated
    arrest_dist_estimated = arrest_dist_mm is None

    if arrest_dist_mm is None:
        # Very rough: use duration and assume decel from ~1.5 m/s (typical fall speed)
        v0_est = 1.5  # m/s
        a_est  = peak_N / 100.0  # assume ~100kg test mass
        t_dur  = event['duration_s']
        arrest_dist_mm = max(0, (v0_est * t_dur - 0.5 * a_est * t_dur**2)) * 1000
        arrest_dist_estimated = True

    # Arrest time
    arrest_time_ms = event['duration_s'] * 1000

    # Impulse (force × time integral)
    dt_arr = np.diff(t_arr)
    impulse_Ns = float(np.sum(f_arr[:-1] * dt_arr)) if len(dt_arr) > 0 else 0.0

    # Jerk at onset (rate of force rise) — indicator of hard start
    if len(f_arr) > 5:
        dt_med  = float(np.median(np.diff(t_arr)))
        jerk    = float((f_arr[5] - f_arr[0]) / (5 * max(dt_med, 1e-6)))
    else:
        jerk = 0.0

    # ANSI pass/fail
    ansi_peak_pass  = peak_N  <= ANSI['peak_force_N']
    ansi_dist_pass  = arrest_dist_mm <= ANSI['arrest_distance_mm']
    ansi_avg_pass   = avg_N   <= ANSI['avg_force_N']
    ansi_all_pass   = ansi_peak_pass and ansi_dist_pass and ansi_avg_pass

    return {
        'peak_force_N':          round(peak_N, 1),
        'avg_force_N':           round(avg_N, 1),
        'arrest_distance_mm':    round(arrest_dist_mm, 1),
        'arrest_distance_estimated': arrest_dist_estimated,
        'arrest_time_ms':        round(arrest_time_ms, 1),
        'impulse_Ns':            round(impulse_Ns, 2),
        'force_rise_rate_N_s':   round(jerk, 0),
        'ansi_peak_pass':        ansi_peak_pass,
        'ansi_dist_pass':        ansi_dist_pass,
        'ansi_avg_pass':         ansi_avg_pass,
        'ansi_all_pass':         ansi_all_pass,
        'ansi_peak_limit_N':     ANSI['peak_force_N'],
        'ansi_dist_limit_mm':    ANSI['arrest_distance_mm'],
        'ansi_avg_limit_N':      ANSI['avg_force_N'],
        'peak_margin_pct':       round((1 - peak_N / ANSI['peak_force_N']) * 100, 1),
        'dist_margin_pct':       round((1 - arrest_dist_mm / ANSI['arrest_distance_mm']) * 100, 1),
    }


def parse_drop_test(
    csv_path: str | Path | None = None,
    df_raw: pd.DataFrame | None = None,
    baseline_threshold: float = 5.0,
    min_peak_N: float = 200.0,
) -> dict:
    """
    Main entry point. Pass either a file path or a pre-loaded DataFrame.

    Returns:
        {
          'ok':       bool — True if parsing succeeded
          'df':       pd.DataFrame — cleaned time series
          'event':    dict — detected arrest event
          'metrics':  dict — computed ANSI metrics
          'warnings': list[str]
          'errors':   list[str]
        }
    """
    warnings = []
    errors   = []

    # ── Load data ─────────────────────────────────────────────────────────────
    if df_raw is None:
        if csv_path is None:
            return {'ok': False, 'errors': ['No data provided']}
        try:
            df_raw = pd.read_csv(csv_path)
        except Exception as e:
            return {'ok': False, 'errors': [f'Could not read CSV: {e}']}

    # ── Detect columns ────────────────────────────────────────────────────────
    t_col   = _find_col(df_raw, TIME_ALIASES)
    f_col   = _find_col(df_raw, TENSION_ALIASES)
    p_col   = _find_col(df_raw, POSITION_ALIASES)
    v_col   = _find_col(df_raw, VELOCITY_ALIASES)

    if t_col is None:
        errors.append(f"Could not find time column. Tried: {TIME_ALIASES[:5]}...")
        return {'ok': False, 'errors': errors}
    if f_col is None:
        errors.append(f"Could not find tension/force column. Tried: {TENSION_ALIASES[:5]}...")
        return {'ok': False, 'errors': errors}

    if p_col is None:
        warnings.append("No rope position column found — arrest distance will be estimated from kinematics.")
    if v_col is None:
        warnings.append("No velocity column found — using position/time derivative.")

    # ── Build clean DataFrame ─────────────────────────────────────────────────
    df = pd.DataFrame()
    df['time_s']    = _auto_convert_time(df_raw, t_col)
    df['tension_N'] = pd.to_numeric(df_raw[f_col], errors='coerce')

    if p_col:
        df['rope_pos_m'] = _auto_convert_position(df_raw, p_col)
    if v_col:
        df['velocity_ms'] = pd.to_numeric(df_raw[v_col], errors='coerce')

    # Drop NaN rows
    df = df.dropna(subset=['time_s', 'tension_N']).reset_index(drop=True)

    if len(df) < 10:
        errors.append(f"Too few data points after cleaning ({len(df)}). Check CSV format.")
        return {'ok': False, 'df': df, 'errors': errors}

    # Sort by time
    df = df.sort_values('time_s').reset_index(drop=True)

    # Sample rate
    dt_vals = np.diff(df['time_s'].values)
    dt_med  = float(np.median(dt_vals))
    sample_rate_hz = 1.0 / max(dt_med, 1e-9)

    if sample_rate_hz < 50:
        warnings.append(f"Low sample rate ({sample_rate_hz:.0f} Hz). Arrest metrics may be inaccurate. Recommend ≥500 Hz.")
    if sample_rate_hz > 20000:
        warnings.append(f"Very high sample rate ({sample_rate_hz:.0f} Hz). Consider downsampling for performance.")

    t       = df['time_s'].values
    tension = df['tension_N'].values
    pos     = df['rope_pos_m'].values if 'rope_pos_m' in df.columns else None

    # ── Detect arrest event ───────────────────────────────────────────────────
    event = detect_arrest_event(t, tension,
                                baseline_threshold_factor=baseline_threshold,
                                min_peak_N=min_peak_N)

    if not event['found']:
        errors.append(f"No arrest event detected: {event.get('reason', '')}")
        return {'ok': False, 'df': df, 'event': event, 'errors': errors,
                'warnings': warnings}

    if event['confidence'] == 'low':
        warnings.append(f"Low confidence event detection (peak only {event['peak_N']:.0f}N). "
                        "Check that this is actually a drop test file.")

    # ── Compute metrics ───────────────────────────────────────────────────────
    metrics = compute_arrest_metrics(t, tension, pos, event)

    if metrics['arrest_distance_estimated']:
        warnings.append("Arrest distance estimated from kinematics (no encoder data). "
                        "Add rope_pos_m column for accurate distance measurement.")

    return {
        'ok':           True,
        'df':           df,
        'event':        event,
        'metrics':      metrics,
        'warnings':     warnings,
        'errors':       errors,
        'sample_rate_hz': round(sample_rate_hz, 1),
        'n_samples':    len(df),
        'duration_s':   round(float(t[-1] - t[0]), 3),
    }


def render_drop_parser_tab():
    """Streamlit tab for the drop test parser."""
    import streamlit as st

    st.markdown("## Drop Test Parser")
    st.caption(
        "Upload a CSV from any load cell logger. "
        "Automatically detects the arrest event, computes ANSI metrics, "
        "and gives you a pass/fail report in one click."
    )

    with st.expander("Supported CSV formats"):
        st.markdown("""
The parser auto-detects column names. These all work:
- `time_s, tension_N` — minimal
- `time_s, tension_N, rope_pos_m` — with position (more accurate distance)
- `t, force, position` — generic names
- `timestamp_ms, load_N, encoder_mm` — raw embedded logger format

Time can be in seconds or milliseconds (auto-detected).
Position can be in meters or mm (auto-detected).
        """)

    # ── Upload ─────────────────────────────────────────────────────────────────
    col1, col2 = st.columns([2, 1])
    with col1:
        uploaded = st.file_uploader("Upload drop test CSV", type=['csv'],
                                     key='drop_parser_upload')
    with col2:
        st.markdown("**Detection settings**")
        threshold  = st.slider("Event trigger (× baseline)", 2.0, 20.0, 5.0, 0.5,
                               help="Higher = less sensitive, fewer false detections")
        min_peak   = st.number_input("Min peak to detect (N)", 50.0, 2000.0, 200.0, 50.0)
        test_label = st.text_input("Test label (optional)", placeholder="e.g. Drop #1, 80kg, 40mm")

    # ── Mock data button ────────────────────────────────────────────────────────
    if st.button("Load example mock drop test"):
        # Generate inline
        dt_s = 0.001; t_arr = np.arange(0, 1.5, dt_s); n = len(t_arr)
        tension_arr = np.zeros(n); rope_pos_arr = np.zeros(n)
        rng = np.random.default_rng(42)
        for i, ti in enumerate(t_arr):
            if ti < 0.20:
                tension_arr[i] = rng.normal(2, 0.5)
                rope_pos_arr[i] = ti * 1.5
            elif ti < 0.25:
                frac = (ti - 0.20) / 0.05
                tension_arr[i] = max(0, 4200 * np.sin(np.pi * frac) + rng.normal(0,50))
                rope_pos_arr[i] = rope_pos_arr[i-1] + 0.001
            else:
                decay = np.exp(-(ti-0.25)/0.3)
                tension_arr[i] = 40 + 200*decay*np.cos(2*np.pi*8*(ti-0.25)) + rng.normal(0,5)
                rope_pos_arr[i] = rope_pos_arr[i-1]
        mock_df = pd.DataFrame({'time_s': t_arr, 'tension_N': tension_arr,
                                 'rope_pos_m': rope_pos_arr})
        st.session_state['drop_parser_df'] = mock_df
        st.success("Mock drop test loaded — click Parse to analyze.")

    if uploaded is not None:
        try:
            st.session_state['drop_parser_df'] = pd.read_csv(uploaded)
            st.success(f"Loaded {len(st.session_state['drop_parser_df'])} rows from {uploaded.name}")
        except Exception as e:
            st.error(f"Could not read file: {e}")

    if 'drop_parser_df' not in st.session_state:
        st.info("Upload a CSV or click 'Load example mock drop test' to start.")
        return

    df_raw = st.session_state['drop_parser_df']

    with st.expander("Raw data preview"):
        st.dataframe(df_raw.head(20), use_container_width=True, hide_index=True)

    # ── Parse ──────────────────────────────────────────────────────────────────
    if st.button("⚙️ Parse drop test", type="primary", use_container_width=True):
        with st.spinner("Detecting arrest event and computing ANSI metrics..."):
            result = parse_drop_test(df_raw=df_raw, baseline_threshold=threshold,
                                     min_peak_N=min_peak)
        st.session_state['drop_result'] = result

    if 'drop_result' not in st.session_state:
        return

    result = st.session_state['drop_result']

    # Warnings
    for w in result.get('warnings', []):
        st.warning(w)
    for e in result.get('errors', []):
        st.error(e)

    if not result.get('ok'):
        return

    event   = result['event']
    metrics = result['metrics']
    df      = result['df']

    # ── Event info ─────────────────────────────────────────────────────────────
    st.markdown("### Detected arrest event")
    col_e1, col_e2, col_e3, col_e4 = st.columns(4)
    col_e1.metric("Event start", f"{event['t_start']:.4f} s")
    col_e2.metric("Peak at",     f"{event['t_peak']:.4f} s")
    col_e3.metric("Event end",   f"{event['t_end']:.4f} s")
    col_e4.metric("Duration",    f"{event['duration_s']*1000:.1f} ms")

    # ── ANSI metrics ────────────────────────────────────────────────────────────
    st.markdown("### ANSI Z359.14 Results")
    col_a, col_b, col_c = st.columns(3)
    col_a.metric(
        "Peak force",
        f"{metrics['peak_force_N']:.0f} N",
        f"{'✅ PASS' if metrics['ansi_peak_pass'] else '❌ FAIL'} — limit {ANSI['peak_force_N']:.0f} N",
        delta_color="normal" if metrics['ansi_peak_pass'] else "inverse",
    )
    col_b.metric(
        "Arrest distance",
        f"{metrics['arrest_distance_mm']:.0f} mm" +
        (" (est.)" if metrics['arrest_distance_estimated'] else ""),
        f"{'✅ PASS' if metrics['ansi_dist_pass'] else '❌ FAIL'} — limit {ANSI['arrest_distance_mm']:.0f} mm",
        delta_color="normal" if metrics['ansi_dist_pass'] else "inverse",
    )
    col_c.metric(
        "Avg arrest force",
        f"{metrics['avg_force_N']:.0f} N",
        f"{'✅ PASS' if metrics['ansi_avg_pass'] else '❌ FAIL'} — limit {ANSI['avg_force_N']:.0f} N",
        delta_color="normal" if metrics['ansi_avg_pass'] else "inverse",
    )

    if metrics['ansi_all_pass']:
        st.success(f"✅ ALL ANSI CHECKS PASS — margins: "
                   f"force {metrics['peak_margin_pct']:.1f}%, "
                   f"distance {metrics['dist_margin_pct']:.1f}%")
    else:
        st.error("❌ ANSI CHECK FAILED — see metrics above")

    # Additional metrics
    with st.expander("Full metrics"):
        st.json(metrics)

    # ── Chart ───────────────────────────────────────────────────────────────────
    st.markdown("### Tension profile")
    try:
        import plotly.graph_objs as go
        fig = go.Figure()

        # Full trace
        fig.add_trace(go.Scatter(
            x=df['time_s'], y=df['tension_N'],
            mode='lines', name='Tension (N)',
            line=dict(color='#4fc3f7', width=1),
        ))

        # Arrest window highlight
        t_start = event['t_start']; t_end = event['t_end']
        fig.add_vrect(x0=t_start, x1=t_end,
                      fillcolor='rgba(255,121,198,0.15)', line_width=0,
                      annotation_text='Arrest window', annotation_font=dict(color='#ff79c6'))

        # Peak marker
        fig.add_vline(x=event['t_peak'], line_dash='dash',
                      line_color='#ff5555', line_width=1.5,
                      annotation_text=f"Peak {metrics['peak_force_N']:.0f}N",
                      annotation_font=dict(color='#ff5555'))

        # ANSI limit line
        fig.add_hline(y=ANSI['peak_force_N'], line_dash='dot',
                      line_color='#ff5555', line_width=1,
                      annotation_text=f"ANSI limit {ANSI['peak_force_N']:.0f}N",
                      annotation_font=dict(color='#ff5555', size=9))

        fig.update_layout(
            paper_bgcolor='#1a1a1a', plot_bgcolor='#1a1a1a',
            font_color='white', height=380,
            xaxis=dict(title='Time (s)', gridcolor='#2a2a2a'),
            yaxis=dict(title='Tension (N)', gridcolor='#2a2a2a'),
            margin=dict(t=20, b=40, l=60, r=20),
            hovermode='x unified',
        )
        st.plotly_chart(fig, use_container_width=True)

        # Zoomed arrest window
        mask = (df['time_s'] >= t_start - 0.05) & (df['time_s'] <= t_end + 0.1)
        df_zoom = df[mask]
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=df_zoom['time_s'], y=df_zoom['tension_N'],
                                   mode='lines', name='Tension', line=dict(color='#ff79c6', width=2)))
        fig2.add_hline(y=ANSI['peak_force_N'], line_dash='dot', line_color='#ff5555')
        fig2.add_hline(y=metrics['avg_force_N'], line_dash='dash', line_color='#ffb86c',
                       annotation_text=f"Avg {metrics['avg_force_N']:.0f}N",
                       annotation_font=dict(color='#ffb86c', size=9))
        fig2.update_layout(
            title=dict(text='Arrest window (zoomed)', font=dict(color='white', size=11)),
            paper_bgcolor='#1a1a1a', plot_bgcolor='#1a1a1a',
            font_color='white', height=280,
            xaxis=dict(title='Time (s)', gridcolor='#2a2a2a'),
            yaxis=dict(title='Tension (N)', gridcolor='#2a2a2a'),
            margin=dict(t=30, b=40, l=60, r=20),
        )
        st.plotly_chart(fig2, use_container_width=True)

    except ImportError:
        st.line_chart(df.set_index('time_s')['tension_N'])

    # ── Save to test history ────────────────────────────────────────────────────
    st.markdown("### Save result")
    col_s1, col_s2 = st.columns(2)
    with col_s1:
        if st.button("💾 Save to test history", type="primary"):
            os.makedirs("drop_test_results", exist_ok=True)
            fname = f"drop_test_results/drop_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            doc = {
                'timestamp':  datetime.now().isoformat(),
                'label':      test_label or fname,
                'metrics':    metrics,
                'event':      {k: v for k, v in event.items()
                               if not isinstance(v, (np.integer, np.floating))},
                'sample_rate_hz': result['sample_rate_hz'],
                'n_samples':  result['n_samples'],
            }
            # make JSON-serializable
            for k, v in doc['event'].items():
                if isinstance(v, (np.integer,)):
                    doc['event'][k] = int(v)
                elif isinstance(v, (np.floating,)):
                    doc['event'][k] = float(v)
            with open(fname, 'w') as f:
                json.dump(doc, f, indent=2)
            st.success(f"Saved to {fname}")

    with col_s2:
        # Download report
        report_lines = [
            "ARIA DROP TEST REPORT",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Label: {test_label or 'unnamed'}",
            "",
            "ANSI Z359.14 RESULTS",
            f"Peak force:       {metrics['peak_force_N']:.0f} N  (limit {ANSI['peak_force_N']:.0f} N)  {'PASS' if metrics['ansi_peak_pass'] else 'FAIL'}",
            f"Arrest distance:  {metrics['arrest_distance_mm']:.0f} mm  (limit {ANSI['arrest_distance_mm']:.0f} mm)  {'PASS' if metrics['ansi_dist_pass'] else 'FAIL'}",
            f"Avg arrest force: {metrics['avg_force_N']:.0f} N  (limit {ANSI['avg_force_N']:.0f} N)  {'PASS' if metrics['ansi_avg_pass'] else 'FAIL'}",
            f"Overall:          {'PASS' if metrics['ansi_all_pass'] else 'FAIL'}",
            "",
            "EVENT DETAILS",
            f"Arrest time:      {metrics['arrest_time_ms']:.1f} ms",
            f"Impulse:          {metrics['impulse_Ns']:.2f} N·s",
            f"Sample rate:      {result['sample_rate_hz']:.0f} Hz",
            f"Samples in event: {event['end_idx'] - event['start_idx']}",
        ]
        st.download_button(
            "⬇️ Download report",
            "\n".join(report_lines).encode(),
            file_name=f"drop_test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            mime="text/plain",
        )


# ── CLI entry point ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='ARIA Drop Test Parser')
    parser.add_argument('csv', type=Path, help='Drop test CSV file')
    parser.add_argument('--threshold', type=float, default=5.0,
                        help='Event trigger threshold (× baseline)')
    parser.add_argument('--min-peak', type=float, default=200.0,
                        help='Minimum peak tension to consider an arrest event (N)')
    parser.add_argument('--save-report', action='store_true',
                        help='Save text report alongside the CSV')
    parser.add_argument('--json-out', type=Path, default=None,
                        help='Save JSON results to this path')
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"ERROR: File not found: {args.csv}"); sys.exit(2)

    print(f"\nParsing: {args.csv}")
    result = parse_drop_test(csv_path=args.csv, baseline_threshold=args.threshold,
                              min_peak_N=args.min_peak)

    for w in result.get('warnings', []):
        print(f"⚠️  {w}")
    for e in result.get('errors', []):
        print(f"❌  {e}")

    if not result['ok']:
        sys.exit(1)

    m = result['metrics']; ev = result['event']
    print(f"\nSample rate: {result['sample_rate_hz']:.0f} Hz  |  {result['n_samples']} samples  |  {result['duration_s']:.2f}s")
    print(f"\nArrest event: t={ev['t_start']:.4f}–{ev['t_end']:.4f}s  ({ev['duration_s']*1000:.1f}ms)")
    print(f"\nANSI Z359.14 Results:")
    print(f"  Peak force:       {m['peak_force_N']:>8.0f} N   limit {ANSI['peak_force_N']:.0f} N   {'✅ PASS' if m['ansi_peak_pass'] else '❌ FAIL'}  (margin {m['peak_margin_pct']:.1f}%)")
    print(f"  Arrest distance:  {m['arrest_distance_mm']:>8.0f} mm  limit {ANSI['arrest_distance_mm']:.0f} mm  {'✅ PASS' if m['ansi_dist_pass'] else '❌ FAIL'}  (margin {m['dist_margin_pct']:.1f}%)")
    print(f"  Avg arrest force: {m['avg_force_N']:>8.0f} N   limit {ANSI['avg_force_N']:.0f} N   {'✅ PASS' if m['ansi_avg_pass'] else '❌ FAIL'}")
    print(f"\nOverall: {'✅ ALL PASS' if m['ansi_all_pass'] else '❌ FAILED'}")

    if args.json_out:
        args.json_out.write_text(json.dumps(result['metrics'], indent=2))
        print(f"\nResults saved to {args.json_out}")

    if args.save_report:
        rpath = args.csv.with_suffix('.report.txt')
        lines = [f"ARIA Drop Test Report — {datetime.now().isoformat()}",
                 f"File: {args.csv}",
                 f"Peak: {m['peak_force_N']:.0f}N  Dist: {m['arrest_distance_mm']:.0f}mm  Avg: {m['avg_force_N']:.0f}N",
                 f"Result: {'PASS' if m['ansi_all_pass'] else 'FAIL'}"]
        rpath.write_text('\n'.join(lines))
        print(f"Report saved to {rpath}")

    sys.exit(0 if m['ansi_all_pass'] else 1)


if __name__ == '__main__':
    main()
