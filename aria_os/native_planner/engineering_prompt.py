"""Engineering conventions injected into every LLM planner prompt.

Instead of hardcoding ISO / ANSI / material / tolerance tables in Python,
we put them in the SYSTEM PROMPT so the LLM applies them consistently
to arbitrary parts. This scales better: any prompt, any standard, any
domain — the LLM handles it.

The content here is what an experienced mech/EE engineer would carry
in their head. Keeping it centralized here (vs scattered across
planners) means we update it once and every LLM plan benefits.
"""

def _build_engineering_prompt() -> str:
    """Assemble the engineering-knowledge system-prompt block by pulling
    tables from the `aria_os.engineering` library. This keeps the prompt
    and the Python lookups in sync — update one source of truth, both
    paths benefit."""
    try:
        from ..engineering.iso_273    import (ISO_CLEARANCE_CLOSE,
                                                ISO_CLEARANCE_MEDIUM,
                                                ISO_TAP_DRILL,
                                                ISO_COUNTERBORE_SHCS)
        from ..engineering.astm_mat   import (CNC_WALL_MIN_MM,
                                                FDM_WALL_MIN_MM)
        from ..engineering.iso_1302   import Ra_BY_FEATURE
        from ..engineering.iso_1101   import GDT_SYMBOLS, _FEATURE_GDT
        # Format compactly so we don't blow the context window
        def _dict_line(d, fmt="{k}={v}"):
            return "  " + ", ".join(fmt.format(k=k, v=v) for k, v in d.items())
        iso273_close = _dict_line(ISO_CLEARANCE_CLOSE)
        iso273_med   = _dict_line(ISO_CLEARANCE_MEDIUM)
        tap_drills   = _dict_line(ISO_TAP_DRILL)
        cbores       = _dict_line(ISO_COUNTERBORE_SHCS,
                                    fmt="{k}→{v[0]}×{v[1]}")
        walls_cnc    = _dict_line(CNC_WALL_MIN_MM, fmt="{k}≥{v}mm")
        walls_fdm    = _dict_line(FDM_WALL_MIN_MM, fmt="{k}≥{v}mm")
        ra_feat      = _dict_line(Ra_BY_FEATURE, fmt="{k}={v}µm")
        gdt_list     = ", ".join(
            f"{v['glyph']} {k}" for k, v in list(GDT_SYMBOLS.items())[:14])
        return "## Engineering conventions you MUST apply\n\n" + f"""
### Bolt holes — ISO 273 (the user says "M<n>", you drill clearance)
Close-fit (default):
{iso273_close}
Medium-fit (use when user says "loose" / "free fit"):
{iso273_med}
Tap drills (for tapped holes):
{tap_drills}

### Counterbores for socket-head cap screws (ISO 4762) — Ø × depth
{cbores}

### Wall thickness minimums (process-gated)
CNC machining:
{walls_cnc}
FDM 3D-printing:
{walls_fdm}
Rule of thumb: walls ≥ 2× the largest feature radius on them.

### Surface finish per ISO 1302 (Ra in µm by feature)
{ra_feat}
Default Ra 3.2µm for stock CNC, 12.5µm for FDM, 6.3µm for laser-cut.

### GD&T — apply to every drawing (ISO 1101 / ASME Y14.5)
Symbols available: {gdt_list}
Typical callouts:
  - Flat mating/sealing faces:  flatness 0.1, Ra 3.2
  - Primary datum face:         flatness 0.05, mark as -A-
  - Bolt hole pattern:          position Ø0.2 A|B|C, perpendicularity 0.1 to A
  - Bore for shaft fit:         cylindricity 0.05, perpendicularity 0.1 to A
  - Outer cylinder on shafts:   total runout 0.05 to A

### Tolerances — ISO 2768-m (default) unless specified
±tolerance band by nominal size: <3mm → ±0.1, 3-30 → ±0.2, 30-120 → ±0.3,
120-400 → ±0.5, 400-1000 → ±0.8. Use -f for precision, -c for coarse.

### Material callouts — always use the full ASTM/AMS reference
Examples: "AL 6061-T6 per ASTM B221", "SS 316L per ASTM A240",
"Steel 1018 CD per ASTM A108", "Carbon Steel A105 normalized per ASTM A105".

### Edge treatment (ISO 13715)
Every outer edge of a machined part gets a 0.5mm chamfer/radius by
default — "break all sharp edges". Reason: deburr + handling safety.

### Part conventions — apply per family
FLANGE
  - "PCD 80mm" means bolt circle DIAMETER 80mm → radius 40mm.
  - Straddle layout (first hole at 90° to +X) for even bolt counts.
  - In-line layout (first hole at +X) for odd counts.
  - Center bore gets cylindricity 0.05, perpendicularity 0.1 to back face.
  - Back face is primary datum A.
  - Pressure flange (ASME B16.5): hub + raised-face + gasket groove.
  - Structural (non-pressure): flat plate + chamfered edges, no hub.

IMPELLER / TURBINE
  - Hub first (cylinder, op="new"). NEVER pattern the whole hub.
  - ONE blade profile sketched OFF-CENTER, extrude op="join".
  - circularPattern that ONE joined blade N times.
  - "Backward-swept" = tip trails rotation direction (positive sweep angle).

BRACKET / L-BRACKET
  - Holes split across both legs when ≥ 4 (2 on base, 2 on leg).
  - Inside corner fillet R = wall_t/2 for stress relief.
  - Edge offset on holes = max(2× hole Ø, 8mm) from free edges.

SHAFT
  - Shoulder Ø ≥ 1.5× bearing journal Ø.
  - Keyway per ISO 6885.
  - Total runout on journals vs axis: 0.02mm.

GEAR
  - Module m = OD / (N + 2).
  - Face width ≈ 6 × module typical.
  - Total runout on pitch cylinder: 0.03 to axis.

HOUSING / ENCLOSURE
  - Walls per material (see above) — never below the process minimum.
  - Bosses for screws should have ≥ 1.5× Ø wall around the hole.
  - Gasket groove depth ≈ 0.6 × gasket Ø; width = gasket Ø × 1.1.

### ECAD (PCB) conventions
Trace widths: signal 0.15mm (6mil), 1A 0.5mm, 3A 1.2mm on 1oz Cu outer.
Via: 0.3mm drill × 0.6mm pad standard. Clearance: ≥0.2mm.
Decoupling: 100nF within 2mm of every IC power pin.
Ground pour: opposite layer from high-speed signals.

Apply these whenever the prompt doesn't override. When a user value
violates a minimum (wall 0.8mm on CNC aluminum), set it but LABEL the
feature with the violation: "Wall 0.8mm — below 1.5mm CNC min".
""".strip()
    except Exception as exc:
        # Fallback to static minimal prompt if the library imports break
        return ("## Apply ISO 273 clearance holes, ISO 2768-m tolerances, "
                "ISO 1302 Ra 3.2 on mating faces, ISO 13715 0.5mm edge "
                "chamfer, GD&T per ISO 1101.")


ENGINEERING_PRACTICE_PROMPT = _build_engineering_prompt()


# --- Lean variants ----------------------------------------------------
#
# When token budget is tight (free-tier Groq's 12K TPM is the binding
# constraint), drop the engineering prompt to a tiny core (~600 chars)
# plus only the part-family section that matches the goal. Saves
# ~3-3.5K chars per request, which is the difference between fitting
# 5 requests/min and 1.8 requests/min in Groq's TPM bucket.

_LEAN_CORE = (
    "## Engineering conventions\n"
    "- Bolt holes: drill ISO 273 CLEARANCE diameter, not nominal "
    "(M3→3.4, M4→4.5, M5→5.5, M6→6.6, M8→9.0, M10→11.0, M12→13.5, "
    "M16→17.5, M20→22.0).\n"
    "- Default tolerance ISO 2768-m. Default surface Ra 3.2µm CNC.\n"
    "- Edge break 0.5mm chamfer/radius on machined parts (ISO 13715).\n"
    "- 'PCD <D>' = bolt-circle DIAMETER → store as radius (D/2).\n"
    "- Material callouts use full ASTM ref (e.g. 'AL 6061-T6 per ASTM B221').\n"
)

# Per-family micro-prompt — added on top of _LEAN_CORE when the goal
# matches the family's keywords. Each entry < 300 chars.
_LEAN_FAMILIES: list[tuple[tuple[str, ...], str]] = [
    (("flange", "bolt circle", "pcd"),
     ("FLANGE: bolt holes on PCD radius. Even count → first hole at "
      "90°. Odd → at +X. Center bore datum A, cylindricity 0.05.")),
    (("impeller", "fan rotor", "blower"),
     ("IMPELLER: hub first (extrude new). ONE blade off-center "
      "(extrude join). circularPattern that ONE blade. Bore last (cut). "
      "'Backward-swept' = positive sweep angle (tip trails).")),
    (("bracket", "l-bracket", "gusset"),
     ("BRACKET: holes split across legs when ≥4 (2 base + 2 leg). "
      "Inside-corner fillet R = wall_t/2. Edge offset = max(2× hole Ø, 8mm).")),
    (("gear", "involute", "spur", "helical"),
     ("GEAR: prefer gearFeature one-liner. m = OD/(N+2). Face width "
      "≈ 6×m. Bore is a SEPARATE extrude(cut) AFTER gearFeature.")),
    (("shaft", "axle", "pin"),
     ("SHAFT: shoulder Ø ≥ 1.5× journal Ø. Keyway ISO 6885. "
      "Total runout 0.02mm on journals.")),
    (("housing", "enclosure", "case"),
     ("HOUSING: walls ≥ 1.5mm CNC AL / 2mm FDM. Bosses ≥ 1.5× hole Ø "
      "wall around. Gasket groove depth ≈ 0.6× gasket Ø.")),
    (("screw", "bolt", "fastener", "thread"),
     ("FASTENER: head + shank + threadFeature on the cylindrical "
      "shank face. M8x1.25 / 1/4-20-UNC / 1/4-NPT spec strings.")),
    (("planetary", "epicyclic", "gearbox"),
     ("PLANETARY: N_ring = N_sun + 2·N_planet. ratio = 1 + N_ring/N_sun. "
      "Equal spacing requires (N_sun+N_ring) % n_planets == 0.")),
    (("sheet metal", "louvered", "hem", "flange chain",
      "enclosure", "chassis panel"),
     ("SHEET METAL: use sheetMetalBase + sheetMetalFlange + "
      "sheetMetalBend (NOT extrude+shell). K-factor by material/"
      "thickness: AL-soft 0.33-0.45, AL-hard 0.42-0.50, steel-mild "
      "0.38-0.46, stainless 0.40-0.47. Min inside bend radius = "
      "1×t (steel/AL-soft) or 2.5×t (AL-hard 6061-T6). "
      "Bend allowance = (π·angle/180) × (R + K·t). "
      "Add bend relief slot (1.5·t wide × R+0.5·t long) at each "
      "interior bend to prevent tearing. Default 90° bends, R = t. "
      "End every plan with exportFlatPattern format='dxf' for laser-cut.")),
    (("gyroid", "lattice", "infill", "tpms", "porosity"),
     ("LATTICE: use implicitInfill operation='intersect' against the "
      "outer shell — clips lattice to outer surface, preserves holes.")),
    (("conformal cooling", "cooling channel"),
     ("CONFORMAL: implicitChannel target=block path=spline_sketch. "
      "Channel diameter typically 4-8mm.")),
    (("topology", "lightweighted", "minimum mass"),
     ("TOPOLOGY: implicitField with bounds covering the design "
      "envelope. operation='new' if standalone, 'intersect' to keep "
      "to outer envelope.")),
    (("drawing", "shop drawing", "blueprint", "gd&t", "gdt",
      "tolerance", "section view", "detail view", "callout"),
     ("DRAWING: beginDrawing → newSheet → addView (top/front/right/iso) "
      "→ autoDimension every view → gdtFrame for critical features. "
      "GD&T characteristic vocabulary: flatness ⏥, parallelism ⫽, "
      "perpendicularity ⊥, position ⌖, cylindricity ⌭, total_runout ⌰. "
      "Default callouts: flange back face → flatness 0.05 + parallelism "
      "0.1 to A. Bolt holes → position Ø0.2 A|B|C. Shaft journal → "
      "cylindricity 0.005 + total_runout 0.02 to A. Always close with "
      "addTitleBlock + revisionTable.")),
]


def _select_family_prompt(goal: str) -> str:
    """Return the part-family micro-prompt for `goal`, or empty if no
    family matches."""
    g = (goal or "").lower()
    out = []
    for keywords, snippet in _LEAN_FAMILIES:
        if any(k in g for k in keywords):
            out.append(snippet)
    return "\n".join(out)


def lean_engineering_prompt(goal: str) -> str:
    """Build a goal-targeted engineering prompt that's ~5× smaller
    than the full ENGINEERING_PRACTICE_PROMPT. Used by lean-mode
    eval / token-constrained provider runs."""
    fam = _select_family_prompt(goal)
    if fam:
        return _LEAN_CORE + "\n" + fam
    return _LEAN_CORE

_STATIC_FALLBACK = r"""
## Engineering conventions you MUST apply

### Bolt hole sizing (critical — engineers get this wrong most)
When the prompt says "M6 holes", "4x M8 mounting holes", "M4 clearance"
etc., the user means a CLEARANCE HOLE for that size bolt to pass
through, NOT a hole of the nominal thread diameter.

ISO 273 close-fit clearance holes (use these by default):
  M1.6 → 1.8   M2 → 2.4   M2.5 → 2.9   M3 → 3.4
  M4   → 4.5   M5 → 5.5   M6   → 6.6   M8  → 9.0
  M10  → 11.0  M12→ 13.5  M16  → 17.5  M20 → 22.0

ISO 273 medium-fit when the prompt says "loose" / "free fit":
  M3 → 3.6   M4 → 4.8   M5 → 5.8   M6 → 7.0   M8 → 10.0

Tap drill sizes (for "M6 tapped", "threaded M4", "M8 thread"):
  M3 → 2.5   M4 → 3.3   M5 → 4.2   M6 → 5.0   M8 → 6.8   M10 → 8.5

Counterbore Ø x depth for socket-head cap screws (ISO 4762):
  M3 → 6.5 × 3.4   M4 → 8.0 × 4.6   M5 → 10 × 5.7
  M6 → 11 × 6.8    M8 → 15 × 9      M10 → 18 × 11

### Wall thickness minimums by material + process
  Aluminium 6061 CNC:   1.5 mm min, 2.5 mm preferred
  Aluminium 6061 FDM:   2.0 mm min (printed orientation matters)
  Steel 1018 CNC:       1.0 mm min, 2.0 mm preferred
  ABS FDM:              1.5 mm min, 2.4 mm preferred (4 perimeters)
  PLA FDM:              1.2 mm min, 2.0 mm preferred
  Brass / Copper CNC:   1.5 mm min

### Fillet / chamfer defaults
  Any external edge on a machined part: 0.5mm chamfer min to deburr
  Stressed corners: R = 0.1 × wall thickness (stress concentration)
  FDM part corners: R = 1mm min (print quality)

### Tolerance conventions
  Default linear tolerance: ±0.1 mm (ISO 2768-m)
  Hole-to-edge distance: ≥ 2 × hole Ø for sheet metal
  Countersink depth: 0.5–1 × hole Ø

### Common part conventions
  FLANGE: bolt circle = PCD (pitch circle diameter). A "PCD 80mm"
    means bolt hole centers on an 80mm diameter circle = 40mm radius.
    Standard count 4 (uneven stress) or 6/8 (uniform).
  IMPELLER: blade angle convention — backward-swept blades have tips
    that trail rotation, forward-swept lead. Open-face = no front
    shroud. Blade count 4-12 typical for centrifugal fans.
  BRACKET / L-BRACKET: wall thickness ≥ 0.12 × max leg length for
    stiffness. Always chamfer/fillet inside corner for stress.
  SHAFT: shoulder Ø ≥ 1.5 × bore Ø. Keyway per ISO 6885.
  GEAR: module = OD / (N+2). Face width ≈ 6 × module.

### ECAD (PCB) conventions
  Trace width minimums:
    signal (≤100mA):  0.15 mm (6 mil)
    power (1A):       0.5 mm on 1oz Cu outer
    power (3A):       1.2 mm on 1oz Cu outer
  Via drill: 0.3mm hole, 0.6mm pad (standard 8mil/14mil)
  Clearance: 0.2mm minimum between traces (8 mil)
  Pad-to-edge: ≥ 0.3mm (mfg minimum is usually 0.25mm)
  Ground pour: always on the layer opposite high-speed signals
  Decoupling: 100nF ceramic within 2mm of every IC power pin

Apply these whenever the prompt doesn't override them. When the user
asks for non-standard dims, always respect their values — but call out
in the op label when the value violates a minimum (e.g. label says
"Wall 0.8mm — below 1.5mm CNC min; consider 2mm").
""".strip()
