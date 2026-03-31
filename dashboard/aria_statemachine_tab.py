"""
aria_statemachine_tab.py — ARIA State Machine Visualizer Tab
Replaces the integer line chart with a proper color-coded timeline.

Features:
  - Color-coded state bands (each state has a distinct color)
  - Tension profile overlaid on state timeline
  - Voice command markers as vertical annotations
  - Motor mode timeline as a separate band below states
  - Transition table: every state change with timestamp and trigger
  - Firmware sync check: flags any state sequence that shouldn't happen
  - Custom scenario builder: inject voice commands and tension at specific times

Add to aria_dashboard.py:
    from aria_statemachine_tab import render_statemachine_tab

SETUPS entry (replace or augment Setup 3):
    "State Machine Visualizer": ["Interactive timeline"],

Routing:
    elif setup.startswith("State Machine Visualizer"):
        render_statemachine_tab()
"""

import streamlit as st
import pandas as pd
import numpy as np
import sys
sys.path.insert(0, '.')


# ── State color palette ───────────────────────────────────────────────────────
STATE_COLORS = {
    'IDLE':        '#555555',
    'CLIMBING':    '#50fa7b',   # green
    'CLIPPING':    '#8be9fd',   # cyan
    'TAKE':        '#ff79c6',   # pink
    'REST':        '#ffb86c',   # orange
    'LOWER':       '#bd93f9',   # purple
    'WATCH_ME':    '#f1fa8c',   # yellow
    'UP':          '#ff5555',   # red
    'ESTOP':       '#ff0000',   # bright red
}

MOTOR_COLORS = {
    'OFF':          '#333333',
    'TENSION':      '#50fa7b',
    'RETRACT_HOLD': '#ff79c6',
    'PAYOUT_SLOW':  '#bd93f9',
    'PAYOUT_FAST':  '#8be9fd',
    'HOLD':         '#ffb86c',
    'TENSION_TIGHT':'#f1fa8c',
    'UP_DRIVE':     '#ff5555',
}

# Which transitions are valid per firmware logic
VALID_TRANSITIONS = {
    'IDLE':     ['CLIMBING'],
    'CLIMBING': ['CLIPPING', 'TAKE', 'REST', 'LOWER', 'WATCH_ME', 'UP', 'ESTOP'],
    'CLIPPING': ['CLIMBING', 'TAKE', 'ESTOP'],
    'TAKE':     ['CLIMBING', 'LOWER', 'ESTOP'],
    'REST':     ['CLIMBING', 'ESTOP'],
    'LOWER':    ['IDLE', 'ESTOP'],
    'WATCH_ME': ['CLIMBING', 'ESTOP'],
    'UP':       ['CLIMBING', 'ESTOP'],
    'ESTOP':    [],  # terminal — requires power cycle
}


def _run_simulation(scenario_steps, dt=0.1):
    """
    Run simulation from a list of scenario steps.
    Each step: {'t_start': float, 't_end': float, 'tension': float, 'voice': str, 'cv_clip': bool, 'estop': bool}
    Returns DataFrame with full time series.
    """
    from aria_models.state_machine import AriaStateMachine, Inputs

    # Build time array
    t_max = max(s['t_end'] for s in scenario_steps) + 0.5
    t_arr = np.arange(0, t_max, dt)
    sm    = AriaStateMachine()
    rows  = []

    for t in t_arr:
        # Find active step
        tension = 0.0; voice = ""; cv_clip = False; estop = False
        for step in scenario_steps:
            if step['t_start'] <= t < step['t_end']:
                tension  = step.get('tension', 0.0)
                voice    = step.get('voice', '')   if abs(t - step['t_start']) < dt * 1.5 else ''
                cv_clip  = step.get('cv_clip', False)
                estop    = step.get('estop', False)
                break

        inp = Inputs(voice=voice, tension_N=tension, cv_clip=cv_clip,
                     estop=estop, time_s=t, dt=dt)
        out = sm.step(inp)
        rows.append({
            'time_s':     round(t, 3),
            'state':      out.state.name,
            'motor_mode': out.motor_mode,
            'tension_N':  tension,
            'voice':      voice,
            'cv_clip':    cv_clip,
            'estop':      estop,
        })

    return pd.DataFrame(rows)


def _build_transitions(df):
    """Extract state transition events from time-series DataFrame."""
    transitions = []
    for i in range(1, len(df)):
        if df.iloc[i]['state'] != df.iloc[i-1]['state']:
            transitions.append({
                'time_s':    df.iloc[i]['time_s'],
                'from':      df.iloc[i-1]['state'],
                'to':        df.iloc[i]['state'],
                'trigger':   df.iloc[i]['voice'] if df.iloc[i]['voice'] else
                             ('cv_clip' if df.iloc[i]['cv_clip'] else
                              ('estop'  if df.iloc[i]['estop']   else
                               'sensor/timeout')),
                'tension_N': df.iloc[i]['tension_N'],
            })
    return pd.DataFrame(transitions) if transitions else pd.DataFrame(
        columns=['time_s','from','to','trigger','tension_N'])


def _check_firmware_sync(transitions_df):
    """Check all transitions against VALID_TRANSITIONS table."""
    issues = []
    for _, row in transitions_df.iterrows():
        from_s = row['from']; to_s = row['to']
        allowed = VALID_TRANSITIONS.get(from_s, [])
        if to_s not in allowed and to_s != from_s:
            issues.append(f"t={row['time_s']:.2f}s: {from_s} → {to_s} is NOT a valid firmware transition")
    return issues


def _plot_timeline(df, transitions_df, title="State Machine Timeline"):
    """Build the main Plotly timeline figure."""
    try:
        import plotly.graph_objs as go
    except ImportError:
        return None

    fig = go.Figure()
    t   = df['time_s'].values

    # ── Band 1: State color fill ──────────────────────────────────────────────
    # Draw colored rectangles for each state segment
    state_vals = df['state'].values
    i = 0
    while i < len(state_vals):
        s = state_vals[i]
        j = i
        while j < len(state_vals) and state_vals[j] == s:
            j += 1
        color = STATE_COLORS.get(s, '#888888')
        fig.add_vrect(
            x0=t[i], x1=t[min(j, len(t)-1)],
            fillcolor=color, opacity=0.25,
            line_width=0,
            annotation_text=s if (j - i) > 3 else '',
            annotation_position='top left',
            annotation_font=dict(size=9, color=color),
        )
        i = j

    # ── Band 2: Tension line ──────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=t, y=df['tension_N'].values,
        mode='lines', name='Tension (N)',
        line=dict(color='#ffffff', width=1.5),
        yaxis='y',
    ))

    # ── Band 3: Motor mode step line ──────────────────────────────────────────
    motor_vals = df['motor_mode'].values
    motor_numeric = [list(MOTOR_COLORS.keys()).index(m)
                     if m in MOTOR_COLORS else 0
                     for m in motor_vals]
    fig.add_trace(go.Scatter(
        x=t, y=motor_numeric,
        mode='lines', name='Motor mode',
        line=dict(color='#8be9fd', width=2, shape='hv'),
        yaxis='y2',
    ))

    # ── Voice command markers ─────────────────────────────────────────────────
    voice_rows = df[df['voice'] != '']
    for _, vrow in voice_rows.iterrows():
        fig.add_vline(
            x=vrow['time_s'],
            line_dash='dot', line_color='#f1fa8c', line_width=1.5,
            annotation_text=vrow['voice'],
            annotation_font=dict(size=9, color='#f1fa8c'),
            annotation_position='top right',
        )

    # ── Transition markers ────────────────────────────────────────────────────
    for _, tr in transitions_df.iterrows():
        color = STATE_COLORS.get(tr['to'], '#ffffff')
        fig.add_vline(
            x=tr['time_s'],
            line_dash='solid', line_color=color, line_width=1,
            opacity=0.6,
        )

    fig.update_layout(
        title=dict(text=title, font=dict(color='white', size=12)),
        paper_bgcolor='#1a1a1a',
        plot_bgcolor='#1a1a1a',
        font_color='white',
        height=420,
        xaxis=dict(title='Time (s)', gridcolor='#333', zeroline=False),
        yaxis=dict(title='Tension (N)', gridcolor='#222', side='left'),
        yaxis2=dict(
            title='Motor mode',
            overlaying='y', side='right',
            tickvals=list(range(len(MOTOR_COLORS))),
            ticktext=list(MOTOR_COLORS.keys()),
            gridcolor='#222',
        ),
        legend=dict(bgcolor='#222', bordercolor='#444', x=0.01, y=0.99),
        margin=dict(t=40, b=40, l=60, r=120),
        hovermode='x unified',
    )

    return fig


def render_statemachine_tab():
    st.markdown("## State Machine Visualizer")
    st.caption(
        "Color-coded timeline with voice markers, motor mode, and firmware sync validation. "
        "Use the scenario builder to inject faults, voice commands, or custom tension profiles."
    )

    tab_prebuilt, tab_custom, tab_transitions, tab_sync = st.tabs([
        "Standard Scenarios", "Custom Scenario Builder",
        "Transition Table", "Firmware Sync Check"
    ])

    # ════════════════════════════════════════════════════════════════
    # TAB 1 — Pre-built scenarios
    # ════════════════════════════════════════════════════════════════
    with tab_prebuilt:
        col1, col2 = st.columns([1, 2])
        with col1:
            scenario = st.selectbox("Scenario", [
                "Normal climb → TAKE → LOWER → IDLE",
                "Climb → clip detection → TAKE → LOWER",
                "Climb → REST → climb → LOWER",
                "Climb → WATCH ME → climb",
                "ESTOP during climb",
                "False TAKE (voice without load)",
                "Multiple cycles (5×)",
            ])
            n_cycles = st.slider("Cycles (multi-cycle scenarios)", 1, 10, 3)
            dt       = st.select_slider("Time resolution (s)", [0.05, 0.1, 0.2], value=0.1)

        # Build scenario steps
        if scenario == "Normal climb → TAKE → LOWER → IDLE":
            steps = [
                {'t_start': 0,   't_end': 1.0, 'tension': 0.0,   'voice': ''},
                {'t_start': 1.0, 't_end': 5.0, 'tension': 45.0,  'voice': ''},
                {'t_start': 5.0, 't_end': 5.1, 'tension': 45.0,  'voice': 'take'},
                {'t_start': 5.1, 't_end': 5.5, 'tension': 250.0, 'voice': ''},
                {'t_start': 5.5, 't_end': 8.0, 'tension': 300.0, 'voice': ''},
                {'t_start': 8.0, 't_end': 8.1, 'tension': 300.0, 'voice': 'lower'},
                {'t_start': 8.1, 't_end': 9.5, 'tension': 30.0,  'voice': ''},
                {'t_start': 9.5, 't_end': 11.0,'tension': 5.0,   'voice': ''},
            ]
        elif scenario == "Climb → clip detection → TAKE → LOWER":
            steps = [
                {'t_start': 0,   't_end': 1.0,  'tension': 0.0,   'voice': ''},
                {'t_start': 1.0, 't_end': 4.0,  'tension': 45.0,  'voice': ''},
                {'t_start': 4.0, 't_end': 4.3,  'tension': 45.0,  'voice': '', 'cv_clip': True},
                {'t_start': 4.3, 't_end': 7.0,  'tension': 45.0,  'voice': ''},
                {'t_start': 7.0, 't_end': 7.1,  'tension': 45.0,  'voice': 'take'},
                {'t_start': 7.1, 't_end': 7.5,  'tension': 260.0, 'voice': ''},
                {'t_start': 7.5, 't_end': 10.0, 'tension': 300.0, 'voice': ''},
                {'t_start': 10.0,'t_end': 10.1, 'tension': 300.0, 'voice': 'lower'},
                {'t_start': 10.1,'t_end': 12.0, 'tension': 10.0,  'voice': ''},
            ]
        elif scenario == "Climb → REST → climb → LOWER":
            steps = [
                {'t_start': 0,   't_end': 1.0,  'tension': 0.0,  'voice': ''},
                {'t_start': 1.0, 't_end': 5.0,  'tension': 45.0, 'voice': ''},
                {'t_start': 5.0, 't_end': 5.1,  'tension': 45.0, 'voice': 'rest'},
                {'t_start': 5.1, 't_end': 9.0,  'tension': 45.0, 'voice': ''},
                {'t_start': 9.0, 't_end': 9.1,  'tension': 45.0, 'voice': 'climbing'},
                {'t_start': 9.1, 't_end': 13.0, 'tension': 45.0, 'voice': ''},
                {'t_start':13.0, 't_end': 13.1, 'tension': 45.0, 'voice': 'lower'},
                {'t_start':13.1, 't_end': 15.0, 'tension': 5.0,  'voice': ''},
            ]
        elif scenario == "Climb → WATCH ME → climb":
            steps = [
                {'t_start': 0,   't_end': 1.0,  'tension': 0.0,  'voice': ''},
                {'t_start': 1.0, 't_end': 5.0,  'tension': 45.0, 'voice': ''},
                {'t_start': 5.0, 't_end': 5.1,  'tension': 45.0, 'voice': 'watch me'},
                {'t_start': 5.1, 't_end': 9.0,  'tension': 45.0, 'voice': ''},
                {'t_start': 9.0, 't_end': 9.1,  'tension': 45.0, 'voice': 'climbing'},
                {'t_start': 9.1, 't_end': 13.0, 'tension': 45.0, 'voice': ''},
                {'t_start':13.0, 't_end': 13.1, 'tension': 45.0, 'voice': 'lower'},
                {'t_start':13.1, 't_end': 15.0, 'tension': 5.0,  'voice': ''},
            ]
        elif scenario == "ESTOP during climb":
            steps = [
                {'t_start': 0,   't_end': 1.0, 'tension': 0.0,  'voice': ''},
                {'t_start': 1.0, 't_end': 5.0, 'tension': 45.0, 'voice': ''},
                {'t_start': 5.0, 't_end': 8.0, 'tension': 45.0, 'voice': '', 'estop': True},
            ]
        elif scenario == "False TAKE (voice without load)":
            steps = [
                {'t_start': 0,   't_end': 1.0, 'tension': 0.0,  'voice': ''},
                {'t_start': 1.0, 't_end': 5.0, 'tension': 45.0, 'voice': ''},
                {'t_start': 5.0, 't_end': 5.1, 'tension': 45.0, 'voice': 'take'},
                # Load never comes — window expires, stays CLIMBING
                {'t_start': 5.1, 't_end': 9.0, 'tension': 45.0, 'voice': ''},
                {'t_start': 9.0, 't_end': 9.1, 'tension': 45.0, 'voice': 'take'},
                {'t_start': 9.1, 't_end': 9.5, 'tension': 250.0,'voice': ''},
                {'t_start': 9.5, 't_end':12.0, 'tension': 300.0,'voice': ''},
                {'t_start':12.0, 't_end':12.1, 'tension': 300.0,'voice': 'lower'},
                {'t_start':12.1, 't_end':14.0, 'tension': 5.0,  'voice': ''},
            ]
        else:  # Multiple cycles
            steps = []
            t = 0.0
            cycle_len = 12.0
            for _ in range(n_cycles):
                steps += [
                    {'t_start': t,       't_end': t+1.0,  'tension': 0.0,   'voice': ''},
                    {'t_start': t+1.0,   't_end': t+5.0,  'tension': 45.0,  'voice': ''},
                    {'t_start': t+5.0,   't_end': t+5.1,  'tension': 45.0,  'voice': 'take'},
                    {'t_start': t+5.1,   't_end': t+5.5,  'tension': 250.0, 'voice': ''},
                    {'t_start': t+5.5,   't_end': t+8.0,  'tension': 300.0, 'voice': ''},
                    {'t_start': t+8.0,   't_end': t+8.1,  'tension': 300.0, 'voice': 'lower'},
                    {'t_start': t+8.1,   't_end': t+10.0, 'tension': 15.0,  'voice': ''},
                    {'t_start': t+10.0,  't_end': t+12.0, 'tension': 2.0,   'voice': ''},
                ]
                t += cycle_len

        with st.spinner("Simulating..."):
            df = _run_simulation(steps, dt=dt)

        transitions_df = _build_transitions(df)

        with col2:
            # State sequence summary
            states_visited = df['state'].unique().tolist()
            col2.markdown("**States visited:** " +
                          " → ".join(f":{['gray','green','cyan','red','orange','violet','yellow','red','red'][i % 9]}[{s}]"
                                     for i, s in enumerate(states_visited)))

        # ── Main timeline plot ────────────────────────────────────────────────
        try:
            import plotly.graph_objs as go

            fig = go.Figure()
            t_arr = df['time_s'].values

            # State colored background bands
            state_vals = df['state'].values
            i = 0
            while i < len(state_vals):
                s = state_vals[i]; j = i
                while j < len(state_vals) and state_vals[j] == s: j += 1
                color = STATE_COLORS.get(s, '#888')
                t0 = t_arr[i]; t1 = t_arr[min(j, len(t_arr)-1)]
                mid = (t0 + t1) / 2
                fig.add_vrect(x0=t0, x1=t1, fillcolor=color, opacity=0.20,
                              line_width=0)
                if (j - i) * dt >= 0.3:
                    fig.add_annotation(x=mid, y=1.02, yref='paper',
                                       text=s, showarrow=False,
                                       font=dict(color=color, size=8),
                                       xanchor='center')
                i = j

            # Tension profile
            fig.add_trace(go.Scatter(
                x=t_arr, y=df['tension_N'],
                mode='lines', name='Tension (N)',
                line=dict(color='white', width=1.5),
                hovertemplate='t=%{x:.2f}s<br>Tension=%{y:.0f}N<extra></extra>',
            ))

            # Motor mode as step line on secondary axis
            motor_keys = list(MOTOR_COLORS.keys())
            motor_num  = [motor_keys.index(m) if m in motor_keys else 0
                          for m in df['motor_mode']]
            fig.add_trace(go.Scatter(
                x=t_arr, y=motor_num,
                mode='lines', name='Motor mode',
                line=dict(color='#8be9fd', width=2, shape='hv'),
                yaxis='y2',
                hovertemplate='t=%{x:.2f}s<br>Motor=%{text}<extra></extra>',
                text=df['motor_mode'],
            ))

            # Voice markers
            voice_df = df[df['voice'] != '']
            for _, vr in voice_df.iterrows():
                fig.add_vline(x=vr['time_s'], line_dash='dot',
                              line_color='#f1fa8c', line_width=1.5,
                              annotation_text=f"'{vr['voice']}'",
                              annotation_font=dict(color='#f1fa8c', size=8),
                              annotation_position='top right')

            # Transition tick marks
            for _, tr in transitions_df.iterrows():
                fig.add_vline(x=tr['time_s'],
                              line_color=STATE_COLORS.get(tr['to'], '#fff'),
                              line_width=1, opacity=0.5)

            fig.update_layout(
                paper_bgcolor='#1a1a1a', plot_bgcolor='#1a1a1a',
                font_color='white', height=400,
                xaxis=dict(title='Time (s)', gridcolor='#2a2a2a', zeroline=False),
                yaxis=dict(title='Tension (N)', gridcolor='#222'),
                yaxis2=dict(
                    title='Motor mode',
                    overlaying='y', side='right',
                    tickvals=list(range(len(motor_keys))),
                    ticktext=motor_keys,
                    gridcolor='#222', showgrid=False,
                ),
                legend=dict(bgcolor='#222', bordercolor='#444',
                            x=0.01, y=0.99, font=dict(size=9)),
                margin=dict(t=30, b=40, l=60, r=140),
                hovermode='x unified',
            )
            st.plotly_chart(fig, use_container_width=True)

            # Legend for state colors
            st.markdown("**State color key:**")
            cols = st.columns(len(STATE_COLORS))
            for col, (state, color) in zip(cols, STATE_COLORS.items()):
                col.markdown(
                    f'<span style="background:{color};color:#000;'
                    f'padding:2px 6px;border-radius:3px;font-size:11px">'
                    f'{state}</span>', unsafe_allow_html=True)

        except ImportError:
            st.warning("Install plotly for the timeline chart.")
            st.line_chart(df.set_index('time_s')[['tension_N']])

    # ════════════════════════════════════════════════════════════════
    # TAB 2 — Custom scenario builder
    # ════════════════════════════════════════════════════════════════
    with tab_custom:
        st.markdown("### Build a custom scenario")
        st.caption("Add steps manually. Each step defines what inputs the state machine receives during that time window.")

        if 'custom_steps' not in st.session_state:
            st.session_state['custom_steps'] = [
                {'t_start': 0.0, 't_end': 1.0, 'tension': 0.0,  'voice': '', 'cv_clip': False, 'estop': False},
                {'t_start': 1.0, 't_end': 6.0, 'tension': 45.0, 'voice': '', 'cv_clip': False, 'estop': False},
            ]

        steps = st.session_state['custom_steps']

        # Edit existing steps
        st.markdown("**Steps:**")
        updated_steps = []
        for idx, step in enumerate(steps):
            with st.expander(f"Step {idx+1}: t={step['t_start']:.1f}–{step['t_end']:.1f}s  tension={step['tension']:.0f}N  voice='{step['voice']}'"):
                c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1.5, 1])
                t0  = c1.number_input("Start (s)", value=float(step['t_start']), step=0.5, key=f"t0_{idx}")
                t1  = c2.number_input("End (s)",   value=float(step['t_end']),   step=0.5, key=f"t1_{idx}")
                ten = c3.number_input("Tension (N)", value=float(step['tension']), step=5.0, key=f"ten_{idx}")
                vc  = c4.selectbox("Voice command", ['', 'take', 'lower', 'rest',
                                                      'watch me', 'up', 'climbing', 'slack'],
                                   index=['', 'take', 'lower', 'rest', 'watch me', 'up', 'climbing', 'slack'].index(step['voice'])
                                   if step['voice'] in ['', 'take', 'lower', 'rest', 'watch me', 'up', 'climbing', 'slack'] else 0,
                                   key=f"vc_{idx}")
                clip = c5.checkbox("CV clip", value=bool(step.get('cv_clip', False)), key=f"clip_{idx}")
                estop_cb = c5.checkbox("E-stop", value=bool(step.get('estop', False)), key=f"estop_{idx}")

                if not st.button(f"Delete step {idx+1}", key=f"del_{idx}"):
                    updated_steps.append({
                        't_start': t0, 't_end': t1, 'tension': ten,
                        'voice': vc, 'cv_clip': clip, 'estop': estop_cb
                    })

        if st.button("➕ Add step"):
            last_end = steps[-1]['t_end'] if steps else 0.0
            updated_steps.append({
                't_start': last_end, 't_end': last_end + 3.0,
                'tension': 45.0, 'voice': '', 'cv_clip': False, 'estop': False
            })

        st.session_state['custom_steps'] = updated_steps

        if st.button("▶️ Run custom scenario", type="primary") and updated_steps:
            with st.spinner("Simulating..."):
                df_custom = _run_simulation(updated_steps, dt=0.1)
            st.session_state['custom_df'] = df_custom
            st.success("Done — see results below.")

        if 'custom_df' in st.session_state:
            df_c = st.session_state['custom_df']
            tr_c = _build_transitions(df_c)

            # Quick metrics
            col_a, col_b, col_c = st.columns(3)
            col_a.metric("States visited", len(df_c['state'].unique()))
            col_b.metric("Transitions",    len(tr_c))
            col_c.metric("Duration",       f"{df_c['time_s'].max():.1f}s")

            try:
                import plotly.graph_objs as go
                fig2 = go.Figure()
                t2   = df_c['time_s'].values
                sv2  = df_c['state'].values

                i = 0
                while i < len(sv2):
                    s = sv2[i]; j = i
                    while j < len(sv2) and sv2[j] == s: j += 1
                    fig2.add_vrect(x0=t2[i], x1=t2[min(j,len(t2)-1)],
                                   fillcolor=STATE_COLORS.get(s,'#888'),
                                   opacity=0.25, line_width=0,
                                   annotation_text=s if (j-i)>2 else '',
                                   annotation_font=dict(color=STATE_COLORS.get(s,'#fff'), size=8))
                    i = j

                fig2.add_trace(go.Scatter(x=t2, y=df_c['tension_N'],
                                          mode='lines', name='Tension',
                                          line=dict(color='white', width=1.5)))
                voice2 = df_c[df_c['voice'] != '']
                for _, vr in voice2.iterrows():
                    fig2.add_vline(x=vr['time_s'], line_dash='dot',
                                   line_color='#f1fa8c', line_width=1.5,
                                   annotation_text=f"'{vr['voice']}'",
                                   annotation_font=dict(color='#f1fa8c', size=8))
                fig2.update_layout(paper_bgcolor='#1a1a1a', plot_bgcolor='#1a1a1a',
                                   font_color='white', height=320,
                                   xaxis=dict(title='Time (s)', gridcolor='#2a2a2a'),
                                   yaxis=dict(title='Tension (N)'),
                                   margin=dict(t=20,b=40,l=60,r=20),
                                   hovermode='x unified')
                st.plotly_chart(fig2, use_container_width=True)
            except ImportError:
                st.line_chart(df_c.set_index('time_s')[['tension_N']])

    # ════════════════════════════════════════════════════════════════
    # TAB 3 — Transition table
    # ════════════════════════════════════════════════════════════════
    with tab_transitions:
        st.markdown("### State transition log")
        st.caption("Every state change with timestamp, trigger, and tension at transition.")

        # Use whichever df was most recently simulated
        df_to_use = (st.session_state.get('custom_df')
                     if 'custom_df' in st.session_state else None)

        if df_to_use is None:
            st.info("Run a scenario in the Standard Scenarios or Custom Scenario Builder tab first.")
        else:
            tr = _build_transitions(df_to_use)
            if tr.empty:
                st.info("No transitions detected — check simulation parameters.")
            else:
                # Color the from/to cells
                def color_state(val):
                    c = STATE_COLORS.get(val, '#888')
                    return f'background-color: {c}22; color: {c}'
                st.dataframe(
                    tr.style.applymap(color_state, subset=['from', 'to']),
                    use_container_width=True,
                    hide_index=True,
                )
                st.metric("Total transitions", len(tr))
                st.metric("Unique states visited",
                          len(set(tr['from'].tolist() + tr['to'].tolist())))

    # ════════════════════════════════════════════════════════════════
    # TAB 4 — Firmware sync check
    # ════════════════════════════════════════════════════════════════
    with tab_sync:
        st.markdown("### Firmware sync check")
        st.caption(
            "Validates every state transition against the allowed transition table "
            "defined in the firmware. Flags any impossible transitions that would "
            "indicate a firmware bug or model mismatch."
        )

        df_to_use = (st.session_state.get('custom_df')
                     if 'custom_df' in st.session_state else None)

        if df_to_use is None:
            st.info("Run a scenario first.")
        else:
            tr = _build_transitions(df_to_use)
            issues = _check_firmware_sync(tr)

            if not issues:
                st.success("✅ All transitions are valid — model matches firmware logic.")
            else:
                st.error(f"⚠️ {len(issues)} invalid transition(s) detected:")
                for issue in issues:
                    st.markdown(f"- {issue}")

        # Show valid transition table for reference
        st.markdown("### Valid transition table (from firmware)")
        ref_rows = []
        for from_state, allowed in VALID_TRANSITIONS.items():
            ref_rows.append({
                'From state':    from_state,
                'Allowed transitions': ', '.join(allowed) if allowed else '(none — ESTOP is terminal)',
            })
        st.dataframe(pd.DataFrame(ref_rows), use_container_width=True, hide_index=True)
