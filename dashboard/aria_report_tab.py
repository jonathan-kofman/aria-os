"""
aria_report_tab.py — Report Generation Tab for ARIA Dashboard
One-click PDF download covering CEM results, ANSI compliance, and open items.
Works for both ARIA (auto belay) and LRE (rocket engine) CEM runs.

Add to aria_dashboard.py:
    from aria_report_tab import render_report_tab

SETUPS entry:
    "Report Generation": ["Generate PDF report"],

Routing:
    elif setup.startswith("Report Generation"):
        render_report_tab()
"""

import streamlit as st
import io
import os
import sys
import numpy as np
from datetime import datetime

sys.path.insert(0, '.')


def _make_aria_pdf(geom, inputs, title, author, notes):
    """Generate ARIA device PDF report using reportlab."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, HRFlowable, PageBreak)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            leftMargin=0.75*inch, rightMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch,
                            title=title, author=author)

    HEADER  = colors.HexColor('#0f3460')
    ALT     = colors.HexColor('#f0f4ff')
    BORDER  = colors.HexColor('#cccccc')
    GREEN   = colors.HexColor('#1b5e20')
    RED     = colors.HexColor('#b71c1c')

    base = getSampleStyleSheet()
    S = {
        'title':    ParagraphStyle('t', fontSize=20, fontName='Helvetica-Bold',
                                   textColor=HEADER, spaceAfter=4, alignment=TA_CENTER),
        'sub':      ParagraphStyle('s', fontSize=11, fontName='Helvetica',
                                   textColor=HEADER, spaceAfter=4, alignment=TA_CENTER),
        'h1':       ParagraphStyle('h1', fontSize=13, fontName='Helvetica-Bold',
                                   textColor=HEADER, spaceBefore=12, spaceAfter=6),
        'body':     ParagraphStyle('b', fontSize=9, fontName='Helvetica',
                                   spaceAfter=3, leading=13),
        'ok':       ParagraphStyle('ok', fontSize=9, fontName='Helvetica',
                                   textColor=GREEN, spaceAfter=2),
        'warn':     ParagraphStyle('w', fontSize=9, fontName='Helvetica-Bold',
                                   textColor=RED, spaceAfter=2),
        'footer':   ParagraphStyle('f', fontSize=7, fontName='Helvetica',
                                   textColor=colors.HexColor('#999999'),
                                   alignment=TA_CENTER),
    }

    def tbl(rows, widths=None):
        if widths is None:
            widths = [2.8*inch, 1.5*inch, 0.8*inch, 2.4*inch]
        style = TableStyle([
            ('BACKGROUND',   (0,0),(-1,0), HEADER),
            ('TEXTCOLOR',    (0,0),(-1,0), colors.white),
            ('FONTNAME',     (0,0),(-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',     (0,0),(-1,-1), 8),
            ('ALIGN',        (0,0),(-1,-1), 'LEFT'),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white, ALT]),
            ('GRID',         (0,0),(-1,-1), 0.5, BORDER),
            ('TOPPADDING',   (0,0),(-1,-1), 3),
            ('BOTTOMPADDING',(0,0),(-1,-1), 3),
            ('LEFTPADDING',  (0,0),(-1,-1), 5),
        ])
        return Table(rows, colWidths=widths, style=style, repeatRows=1)

    def sec(text):
        return [HRFlowable(width='100%', thickness=2, color=HEADER, spaceAfter=4),
                Paragraph(text, S['h1'])]

    story = []
    g = geom; i = inputs

    # Cover
    story.append(Spacer(1, 0.4*inch))
    story.append(Paragraph(title, S['title']))
    story.append(Paragraph("Computational Engineering Model — Device Report", S['sub']))
    story.append(Spacer(1, 0.15*inch))
    story.append(HRFlowable(width='80%', thickness=3, color=HEADER, hAlign='CENTER'))
    story.append(Spacer(1, 0.15*inch))
    cover = [
        ['Parameter', 'Value'],
        ['Max arrest force limit', f"{i.max_arrest_force_kN:.1f} kN (ANSI Z359.14)"],
        ['Min hold force',         f"{i.min_hold_force_kN:.1f} kN"],
        ['Fall detection speed',   f"{i.fall_detection_v_m_s:.2f} m/s"],
        ['Rope diameter',          f"{i.rope_diameter_mm:.0f} mm"],
        ['Rope capacity',          f"{i.max_rope_capacity_m:.0f} m"],
        ['Brake drum',             f"Ø{i.brake_drum_diameter_mm:.0f} mm"],
        ['Material (housing)',     i.material_housing],
        ['Material (ratchet)',     i.material_ratchet],
        ['Author',                 author],
        ['Generated',              datetime.now().strftime('%Y-%m-%d %H:%M')],
    ]
    story.append(tbl(cover, widths=[2.5*inch, 5.0*inch]))
    story.append(PageBreak())

    # Section 1: Brake drum
    story += sec("1. Brake Drum")
    story.append(Paragraph(
        "Drum wall thickness derived from hoop stress: t = P·r·SF / sigma_yield. "
        f"Material: {i.material_housing}. Safety factor: {i.safety_factor_structural:.1f}.",
        S['body']))
    drum_rows = [
        ['Parameter', 'Value', 'Unit', 'Notes'],
        ['Diameter',          f"{g.brake_drum.diameter_mm:.1f}",  'mm', 'Lead Solo reference'],
        ['Width',             f"{g.brake_drum.width_mm:.1f}",     'mm', 'Axial'],
        ['Wall thickness',    f"{g.brake_drum.wall_thickness_mm:.3f}", 'mm', 'Hoop stress derived'],
        ['Hoop stress',       f"{g.brake_drum.hoop_stress_MPa:.1f}",   'MPa', ''],
        ['Safety factor',     f"{g.brake_drum.safety_factor:.2f}",      '-', f"req ≥{i.safety_factor_structural:.1f}"],
        ['Mass (est.)',       f"{g.brake_drum.mass_kg:.3f}",             'kg', ''],
    ]
    story.append(tbl(drum_rows))

    # Section 2: Ratchet
    story += sec("2. Ratchet Wheel")
    story.append(Paragraph(
        f"Lewis formula tooth bending. Module m=3. Pressure angle {g.ratchet.pressure_angle_deg:.0f}°. "
        f"Material: {i.material_ratchet}. Fatigue SF: {i.safety_factor_fatigue:.1f}.",
        S['body']))
    ratchet_rows = [
        ['Parameter', 'Value', 'Unit', 'Notes'],
        ['Number of teeth',    str(g.ratchet.n_teeth),                  '-',  ''],
        ['Module',             '3.0',                                    'mm', 'Lewis formula'],
        ['Face width',         f"{g.ratchet.face_width_mm:.2f}",         'mm', 'Lewis bending'],
        ['Pressure angle',     f"{g.ratchet.pressure_angle_deg:.1f}",    'deg',''],
        ['Bending stress',     f"{g.ratchet.tooth_bending_stress_MPa:.1f}",'MPa',''],
        ['Safety factor',      f"{g.ratchet.safety_factor:.2f}",          '-', f"fatigue, req ≥{i.safety_factor_fatigue:.1f}"],
    ]
    story.append(tbl(ratchet_rows))
    story.append(PageBreak())

    # Section 3: Centrifugal Clutch
    story += sec("3. Centrifugal Clutch")
    story.append(Paragraph(
        "Flyweight mass derived from engagement speed requirement. "
        f"Detection margin = fall detection speed / normal climbing speed = {g.clutch.safety_margin:.1f}×. "
        "Must be ≥ 3.0× to avoid false triggers during normal climbing.",
        S['body']))
    clutch_rows = [
        ['Parameter', 'Value', 'Unit', 'Notes'],
        ['Flyweight count',    str(g.clutch.n_flyweights),               '-',  ''],
        ['Flyweight mass',     f"{g.clutch.flyweight_mass_g:.2f}",        'g',  'Each'],
        ['Flyweight radius',   f"{g.clutch.flyweight_radius_mm:.2f}",     'mm', 'Centroid'],
        ['Spring preload',     f"{g.clutch.spring_preload_N:.2f}",        'N',  'Per flyweight'],
        ['Engagement speed',   f"{g.clutch.engagement_v_m_s:.3f}",        'm/s',''],
        ['Engagement RPM',     f"{g.clutch.engagement_rpm:.1f}",          'rpm',''],
        ['Detection margin',   f"{g.clutch.safety_margin:.1f}",           '×',  'Fall/climb speed ratio'],
    ]
    story.append(tbl(clutch_rows))

    # Section 4: Spool + Motor
    story += sec("4. Rope Spool & Motor")
    spool_rows = [
        ['Parameter', 'Value', 'Unit', 'Notes'],
        ['Hub diameter',       f"{g.spool.hub_diameter_mm:.1f}",          'mm', ''],
        ['Flange diameter',    f"{g.spool.flange_diameter_mm:.1f}",       'mm', ''],
        ['Width',              f"{g.spool.width_mm:.1f}",                  'mm', ''],
        ['Layers',             str(g.spool.layers),                        '-',  ''],
        ['Rope capacity',      f"{g.spool.capacity_m:.1f}",               'm',  ''],
        ['Gearbox ratio',      f"{g.motor.gearbox_ratio:.0f}:1",          '-',  ''],
        ['Torque at spool',    f"{g.motor.motor_torque_Nm * g.motor.gearbox_ratio * 0.85:.2f}", 'Nm', ''],
        ['Back-drive note',    'One-way bearing required',                 '-',  'Planetary not self-locking'],
    ]
    story.append(tbl(spool_rows))
    story.append(PageBreak())

    # Section 5: Housing
    story += sec("5. Housing")
    housing_rows = [
        ['Parameter', 'Value', 'Unit', 'Notes'],
        ['Outer diameter',     f"{g.housing.od_mm:.1f}",                  'mm', ''],
        ['Wall thickness',     f"{g.housing.wall_thickness_mm:.2f}",      'mm', ''],
        ['Length',             f"{g.housing.length_mm:.1f}",              'mm', ''],
        ['Wall stress',        f"{g.housing.wall_stress_MPa:.1f}",        'MPa',''],
        ['Mass (est.)',        f"{g.housing.mass_kg:.3f}",                 'kg', ''],
        ['Wall mount bolts',   f"{g.housing.n_wall_bolts}× on Ø{g.housing.bolt_circle_mm:.0f}mm", '-', ''],
    ]
    story.append(tbl(housing_rows))

    # Section 6: ANSI Performance
    story += sec("6. ANSI Z359.14 Performance")
    ANSI_LIMIT_F  = 6.0
    ANSI_LIMIT_D  = 1.0
    perf_rows = [
        ['Check', 'Predicted', 'ANSI Limit', 'Status'],
        ['Arrest distance',
         f"{g.predicted_arrest_distance_m:.3f} m",
         f"{ANSI_LIMIT_D:.1f} m",
         'PASS' if g.predicted_arrest_distance_m <= ANSI_LIMIT_D else 'FAIL'],
        ['Peak force on climber',
         f"{g.predicted_peak_force_kN:.3f} kN",
         f"{ANSI_LIMIT_F:.1f} kN",
         'PASS' if g.predicted_peak_force_kN <= ANSI_LIMIT_F else 'FAIL'],
        ['Catch time',
         f"{g.predicted_catch_time_ms:.1f} ms",
         '—', '—'],
        ['Clutch detection margin',
         f"{g.clutch.safety_margin:.1f}×",
         '≥3.0×',
         'PASS' if g.clutch.safety_margin >= 3.0 else 'FAIL'],
        ['Brake drum SF',
         f"{g.brake_drum.safety_factor:.2f}",
         f"≥{i.safety_factor_structural:.1f}",
         'PASS' if g.brake_drum.safety_factor >= i.safety_factor_structural else 'FAIL'],
    ]
    style2 = TableStyle([
        ('BACKGROUND', (0,0),(-1,0), HEADER),
        ('TEXTCOLOR',  (0,0),(-1,0), colors.white),
        ('FONTNAME',   (0,0),(-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0,0),(-1,-1), 8),
        ('GRID',       (0,0),(-1,-1), 0.5, BORDER),
        ('TOPPADDING', (0,0),(-1,-1), 3),
        ('BOTTOMPADDING',(0,0),(-1,-1), 3),
        ('LEFTPADDING',(0,0),(-1,-1), 5),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white, ALT]),
    ])
    for idx, row in enumerate(perf_rows[1:], 1):
        if row[3] == 'PASS':
            style2.add('BACKGROUND', (3,idx),(3,idx), colors.HexColor('#e8f5e9'))
            style2.add('TEXTCOLOR',  (3,idx),(3,idx), GREEN)
        elif row[3] == 'FAIL':
            style2.add('BACKGROUND', (3,idx),(3,idx), colors.HexColor('#ffebee'))
            style2.add('TEXTCOLOR',  (3,idx),(3,idx), RED)
    story.append(Table(perf_rows,
                       colWidths=[2.5*inch, 1.5*inch, 1.5*inch, 1.5*inch],
                       style=style2))

    # Notes
    if notes.strip():
        story += sec("7. Notes")
        for line in notes.strip().split('\n'):
            if line.strip():
                story.append(Paragraph(f"• {line.strip()}", S['body']))

    # Footer
    story.append(Spacer(1, 0.3*inch))
    story.append(HRFlowable(width='100%', thickness=1, color=BORDER))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"Generated by ARIA CEM Dashboard — {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        "Physics derives geometry. Not a substitute for physical testing or certification.",
        S['footer']))

    doc.build(story)
    buf.seek(0)
    return buf.read()


def render_report_tab():
    st.markdown("## Report Generation")
    st.caption(
        "Generate a professional PDF report from your last CEM run. "
        "Suitable for advisor reviews, Intertek pre-submission, or manufacturing partners."
    )

    # ── Report type selector ──────────────────────────────────────────────────
    report_type = st.radio(
        "Report type",
        ["ARIA Auto Belay", "LRE Engine (coming soon)"],
        horizontal=True,
    )

    st.markdown("---")

    if report_type == "LRE Engine (coming soon)":
        st.info("LRE report generation will be available once CadQuery STEP export is wired in. "
                "For now use the standalone `cem_report.py` script.")
        return

    # ── Check if CEM has been run ─────────────────────────────────────────────
    if 'aria_geom' not in st.session_state:
        st.warning("No ARIA CEM data found. Go to **CEM Design** tab, set parameters, and click **Regenerate** first.")
        return

    geom   = st.session_state['aria_geom']
    inputs = st.session_state['aria_inputs']

    # ── Report metadata ───────────────────────────────────────────────────────
    st.markdown("### Report details")
    col1, col2 = st.columns(2)
    with col1:
        report_title  = st.text_input("Report title",
                                       value="ARIA Auto Belay — CEM Device Report")
        report_author = st.text_input("Author", value="Jonathan Kofman")
        report_purpose = st.selectbox("Intended audience",
            ["Personal / internal review",
             "Faculty / thesis advisor",
             "Intertek pre-certification meeting",
             "Manufacturing partner (McGillivray Motors)",
             "Investor / demo"])
    with col2:
        include_notes = st.text_area(
            "Additional notes (optional — appears in report)",
            height=120,
            placeholder="e.g. This is a Phase 1 POC prototype. "
                        "All dimensions subject to change after first drop test.",
        )

    # ── Quick summary before generating ──────────────────────────────────────
    st.markdown("### Report preview")
    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric("Arrest dist", f"{geom.predicted_arrest_distance_m:.3f} m",
                 "✅ PASS" if geom.predicted_arrest_distance_m <= 1.0 else "❌ FAIL")
    col_b.metric("Peak force",  f"{geom.predicted_peak_force_kN:.2f} kN",
                 "✅ PASS" if geom.predicted_peak_force_kN <= 6.0 else "❌ FAIL")
    col_c.metric("Clutch margin", f"{geom.clutch.safety_margin:.1f}×",
                 "✅ PASS" if geom.clutch.safety_margin >= 3.0 else "❌ FAIL")
    col_d.metric("Drum SF",     f"{geom.brake_drum.safety_factor:.2f}",
                 "✅ PASS" if geom.brake_drum.safety_factor >= inputs.safety_factor_structural else "❌ FAIL")

    all_pass = (geom.predicted_arrest_distance_m <= 1.0 and
                geom.predicted_peak_force_kN <= 6.0 and
                geom.clutch.safety_margin >= 3.0 and
                geom.brake_drum.safety_factor >= inputs.safety_factor_structural)
    if all_pass:
        st.success("All ANSI Z359.14 checks pass — report will show full compliance.")
    else:
        st.warning("Some checks fail — report will clearly flag these. Fix in CEM Design tab first if submitting to Intertek.")

    # ── Test corrections note ─────────────────────────────────────────────────
    if 'cem_corrections' in st.session_state:
        c = st.session_state['cem_corrections']
        st.info(f"Test corrections from **{c.get('test_id','unknown')}** will be noted in the report.")

    st.markdown("---")

    # ── Generate button ───────────────────────────────────────────────────────
    if st.button("📄 Generate PDF Report", type="primary", use_container_width=True):
        with st.spinner("Building PDF..."):
            try:
                full_notes = include_notes
                if 'cem_corrections' in st.session_state:
                    c = st.session_state['cem_corrections']
                    full_notes += (f"\n\nTest calibration data applied from: {c.get('test_id','unknown')} "
                                   f"({c.get('test_date','')}).")
                    if 'eta_cstar' in c:
                        full_notes += f" c* efficiency: {c['eta_cstar']:.4f}."

                pdf_bytes = _make_aria_pdf(
                    geom   = geom,
                    inputs = inputs,
                    title  = report_title,
                    author = report_author,
                    notes  = full_notes,
                )
                fname = (f"ARIA_CEM_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf")
                st.success(f"Report generated — {len(pdf_bytes)//1024} KB")
                st.download_button(
                    label    = f"⬇️ Download {fname}",
                    data     = pdf_bytes,
                    file_name= fname,
                    mime     = "application/pdf",
                    use_container_width=True,
                )
            except Exception as e:
                st.error(f"PDF generation failed: {e}")
                import traceback
                st.code(traceback.format_exc())

    # ── What's in the report ──────────────────────────────────────────────────
    with st.expander("What's included in the report"):
        st.markdown("""
**Cover page** — design requirements, materials, timestamp, author

**Section 1 — Brake Drum** — diameter, wall thickness, hoop stress, safety factor

**Section 2 — Ratchet Wheel** — Lewis formula results, face width, bending stress, fatigue SF

**Section 3 — Centrifugal Clutch** — flyweight mass/radius, spring preload, engagement speed, detection margin

**Section 4 — Rope Spool & Motor** — spool geometry, rope capacity, gearbox ratio, back-drive note

**Section 5 — Housing** — OD, wall thickness, wall stress, mount bolt pattern

**Section 6 — ANSI Z359.14 Performance** — arrest distance, peak force, catch time — all with pass/fail vs limits

**Section 7 — Notes** — your custom notes + any applied test corrections
        """)
