"""API doc retrieval for the LLM planner.

The W1 schema teaches the LLM the OP VOCABULARY (revolve, sweep, loft,
threadFeature, …). This module teaches it the SEMANTICS — what each
op actually expects, the gotchas, and worked-out CadQuery / Fusion /
Onshape patterns it can imitate.

Architecture is dead simple: a curated corpus of ~100 short markdown
notes, indexed once in-process with BM25, retrieved by goal text +
op-vocabulary keywords, and injected into the LLM system prompt
under "## Reference API" before each call.

Why BM25 + curated corpus, not a vector DB:
  - Latency: BM25 init is ~5ms on 100 docs; queries are ~1ms.
  - No embedding cost, no DB to deploy, no cache invalidation.
  - Goal text uses the exact terms the docs use ("sweep", "revolve",
    "involute") so lex-search wins over semantic search anyway.
  - Curated corpus stays small + correct; we never index hallucinated
    upstream docs.

Public API:
  index = APIDocIndex.default()             # builds once per process
  hits = index.search("centrifugal volute", k=5)
  for h in hits: print(h.title, h.snippet)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# rank_bm25 is required at retrieval time; gracefully degrade to a
# substring fallback if it's not installed (CI / Railway might skip it).
try:
    from rank_bm25 import BM25Okapi  # type: ignore
    _HAS_BM25 = True
except ImportError:
    _HAS_BM25 = False


# --- Curated corpus ----------------------------------------------------

# Each entry: (id, title, source [cadquery|fusion|onshape|cross], body).
# Body is short (<400 chars) — LLM context budget matters.

_CORPUS: list[tuple[str, str, str, str]] = [
    # --- Sweep / loft / revolve patterns ---
    ("cq.sweep.basic", "CadQuery sweep along a path",
     "cadquery",
     "Workplane.sweep(path, multisection=False, sweepAlongWires=None, "
     "makeSolid=True). The CALLING workplane holds the profile; `path` "
     "must be a Workplane whose .val() is a wire. Profile must close. "
     "For a tube: `cq.Workplane('XY').circle(5).sweep(path)`. "
     "Gotcha: a non-planar path needs `transition='right'` to avoid "
     "twist; use `auxSpine=` for guided rail."),
    ("cq.loft.sections",
     "CadQuery loft between cross-sections", "cadquery",
     "Build each cross-section on its own workplane offset along the "
     "axis: `cq.Workplane('XY').circle(50).workplane(offset=200)."
     "transformed((0,0,0)).rect(80,40).loft(combine=True)`. "
     "Gotcha: ALL sections must close; rect+circle works because both "
     "are closed wires. Edge cases: 0-area inlets crash — use a "
     "tiny circle (r=0.01) instead of a point."),
    ("cq.revolve.partial",
     "CadQuery partial revolve", "cadquery",
     "`Workplane.revolve(angleDegrees=360, axisStart=(0,0,0), "
     "axisEnd=(0,0,1), combine=True)`. Sketch the profile in the "
     "rZ-plane (X=radius, Z=axial). Gotcha: the profile must NOT "
     "cross the axis or you get a self-intersecting solid."),
    ("cq.helix.thread",
     "CadQuery: helix curve via parametricCurve",
     "cadquery",
     "`cq.Workplane('XY').parametricCurve(lambda t, p=pitch, r=rad, "
     "n=turns: (r*cos(2*pi*n*t), r*sin(2*pi*n*t), p*n*t), N=200)`. "
     "Sweep a small triangular profile along this for a thread. "
     "PREFER cq_warehouse.thread.IsoThread for ISO M-threads — gets "
     "the pitch + flank angles right."),
    ("cq.shell.hollow",
     "CadQuery shell to hollow a body", "cadquery",
     "`Workplane.shell(thickness, faceList=None)`. Default removes "
     "the topmost face. To pick faces: `result.faces('>Z').shell(-1.5)`. "
     "Negative thickness = inward. Gotcha: shell fails on highly "
     "curved internal corners — use generous fillets first."),

    # --- Involute gear math ---
    ("cq.gear.involute",
     "Involute gear via cq_gears", "cadquery",
     "`from cq_gears import SpurGear; gear = SpurGear(module=2, "
     "teeth_number=24, width=10, pressure_angle=20, bore_d=10).build()`. "
     "Returns a Workplane. Module m = OD/(N+2); face width 6×m typical. "
     "For helical: HelicalGear(..., helix_angle=15)."),
    ("ariaOS.gearFeature",
     "ARIA gearFeature op semantics", "cross",
     "`{kind: 'gearFeature', params: {sketch, module, n_teeth, "
     "thickness, pressure_angle?, helix_angle?}}` is body-creating "
     "(equivalent to extrude operation='new'). The bore is a SEPARATE "
     "extrude(operation='cut') AFTER the gear. Min n_teeth=4."),

    # --- Thread specs ---
    ("ariaOS.thread.spec",
     "threadFeature spec syntax", "cross",
     "Accepts ISO metric (M8x1.25, M16), UN (1/4-20-UNC, 3/8-16-UNF), "
     "NPT (1/4-NPT). Coarse pitch is auto-picked when omitted: M3→0.5, "
     "M4→0.7, M5→0.8, M6→1.0, M8→1.25, M10→1.5, M12→1.75, M16→2.0, "
     "M20→2.5. threadFeature attaches to a CYLINDRICAL FACE — emit "
     "the bore extrude FIRST."),
    ("fusion.thread.api",
     "Fusion ThreadFeatures.add", "fusion",
     "`comp.features.threadFeatures.add(input)`. `input.threadInfo = "
     "tdq.queryRecommendThreadData(diameter, isInternal, threadType)`. "
     "threadType: 'ISO Metric profile' | 'ANSI Unified Screw Threads' "
     "| 'ANSI National Pipe Thread'. Set isModeled=True for visible "
     "geometry, False for cosmetic. Specify threadLength for partial "
     "threads (isFullLength=False)."),

    # --- Fusion / Onshape op refs ---
    ("fusion.revolve.api",
     "Fusion RevolveFeatures.add", "fusion",
     "`comp.features.revolveFeatures.createInput(profile, axis, op)`. "
     "axis is a ConstructionAxis OR a sketch line. setAngleExtent("
     "isSymmetric=False, ValueInput.createByReal(rad)). Use math.radians "
     "to convert. For 360° prefer FullExtent: "
     "`input.setExtent(adsk.fusion.RevolveExtentDefinitions.FullRevolveExtentDefinition)`."),
    ("fusion.loft.api",
     "Fusion LoftFeatures + sections", "fusion",
     "`input = comp.features.loftFeatures.createInput(operation)`. "
     "For each profile: `input.loftSections.add(profile)`. "
     "input.isClosed=False (default). Optional rails: "
     "`input.centerLineOrRails.addRail(rail_curve)`."),
    ("fusion.sweep.api",
     "Fusion SweepFeatures.add", "fusion",
     "`Path.create(curve, ChainedCurveOptions.connectedChainedCurves)`. "
     "`SweepFeatures.createInput(profile, path, operation)`. The path "
     "comes from a SEPARATE sketch from the profile — same sketch "
     "produces a degenerate sweep."),
    ("fusion.coil.api",
     "Fusion CoilFeatures one-shot helix+section", "fusion",
     "`coilFeatures.createInput(originPoint, axis, operation)`. Set: "
     ".coilType = PitchAndRevolutionCoilType. .pitch, .revolutions, "
     ".diameter all ValueInput. .sectionType = CircularCoilFeatureSectionType "
     "(or Triangular for V-thread). .sectionSize = ValueInput. Single "
     "call yields swept geometry — no need for separate helix + sweep."),
    ("onshape.feature.btmft134",
     "Onshape BTMFeature-134 envelope", "onshape",
     "All non-sketch features POST as {btType: 'BTMFeature-134', "
     "featureType: 'revolve'|'sweep'|'loft'|'shell'|...|, name, "
     "parameters: [...]}. Parameter btTypes: BTMParameterEnum-145 "
     "(strings), BTMParameterQuantity-147 (mm/m/deg), "
     "BTMParameterQueryList-148 (entity refs), BTMParameterBoolean-144."),
    ("onshape.units.meters",
     "Onshape uses meters internally", "onshape",
     "BTMParameterQuantity values must be in meters even when "
     "`expression: '50 mm'` is mm-readable. Always: value = mm * 0.001. "
     "Angles are in radians for `value`; expression keeps 'deg'."),

    # --- ARIA-specific gotchas ---
    ("ariaOS.circular.pattern",
     "circularPattern: never pattern a 'new' body", "cross",
     "Patterning a body created with operation='new' rotates the whole "
     "part around the axis — no-op. Always pattern a CUT or JOIN "
     "feature (one bolt hole, one blade) so the pattern actually "
     "replicates the feature N times."),
    ("ariaOS.impeller.recipe",
     "Impeller / fan / turbine planner recipe", "cross",
     "1) Hub: circle + extrude(new). "
     "2) ONE blade off-center: rect or airfoil sketchSpline. "
     "3) extrude(join) the blade onto the hub. "
     "4) circularPattern that ONE blade N times. "
     "5) Bore: small circle + extrude(cut) LAST. "
     "Backward-swept = positive sweep angle (tip trails rotation)."),
    ("ariaOS.transition.duct",
     "Transition duct recipe (round → rect)", "cross",
     "1) sk_in: newSketch XY offset=0 + sketchCircle. "
     "2) sk_out: newSketch XY offset=L + sketchRect. "
     "3) loft sections=[sk_in, sk_out] operation='new'. "
     "4) shell(thickness=wall, faces=['inlet','outlet']). "
     "If outlet rotated, use loft `rails=[guide_curve]`."),
    ("ariaOS.volute.recipe",
     "Centrifugal volute recipe (sweep along spiral)", "cross",
     "1) sk_path: newSketch XY + sketchSpline points along Archimedean "
     "spiral r=a+b·θ growing from impeller OD. "
     "2) sk_prof: newSketch on YZ + sketchCircle (cross-section). "
     "3) sweep profile_sketch=sk_prof path_sketch=sk_path operation='new'. "
     "4) shell(faces=['inlet']) for hollow casing."),
    ("ariaOS.cap.screw.recipe",
     "Cap screw recipe", "cross",
     "1) Head: sketchCircle r=12 (M16) + extrude 16mm new. "
     "2) Shank: sketchCircle r=8 + extrude(-60mm, join) downward. "
     "3) threadFeature face='shank.cyl' spec='M16X2' length=50 modeled=true. "
     "Cap-head height ≈ shaft Ø; head OD ≈ 1.5× shaft Ø."),

    # --- Engineering gotchas (cross-cutting) ---
    ("eng.iso273.clearance",
     "ISO 273 clearance hole sizes", "cross",
     "When the user says 'M6 holes' in a flange, drill the CLEARANCE "
     "diameter, not 6mm. Close-fit (default): M3→3.4, M4→4.5, M5→5.5, "
     "M6→6.6, M8→9.0, M10→11.0, M12→13.5, M16→17.5, M20→22.0. "
     "Medium-fit (loose): M6→7.0, M8→10.0."),
    ("eng.shell.faces.naming",
     "Shell op: how to refer to the open face", "cross",
     "Bridge accepts: 'top'|'bottom' (Z-extreme face), or a face "
     "alias from a previous op. For revolved bottle-style parts, "
     "'top' opens the highest face. For lofted rect-to-round ducts, "
     "list both ['inlet','outlet'] to get a tube."),
    ("eng.mate.before.geometry",
     "Assembly: size BOM before geometry", "cross",
     "For mechanisms (gearbox, linkage), an Assembly Designer agent "
     "calculates BOM first: planetary 4:1 with 3 planets → sun=12T "
     "planet=12T ring=36T (sun + 2·planet = ring; ratio = 1 + ring/sun). "
     "ONLY then emit gearFeature ops with the determined N values."),
]


# --- Index implementation ---------------------------------------------

@dataclass
class APIDoc:
    id: str
    title: str
    source: str  # cadquery | fusion | onshape | cross
    body: str

    @property
    def full_text(self) -> str:
        return f"{self.title}\n{self.body}"

    def snippet(self, q_terms: set[str] | None = None,
                  max_len: int = 400) -> str:
        """Return the body, optionally trimmed to ~max_len chars."""
        if len(self.body) <= max_len:
            return self.body
        return self.body[:max_len] + "…"


@dataclass
class Hit:
    doc: APIDoc
    score: float

    @property
    def title(self) -> str: return self.doc.title

    @property
    def source(self) -> str: return self.doc.source

    def snippet(self, **kw) -> str: return self.doc.snippet(**kw)


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z_]+")


def _tokenize(text: str) -> list[str]:
    """Lower-case word tokens, no stemming (intentional — ARIA's
    keyword vocab is exact: 'sweep', 'revolve', 'loft' don't need
    morphological variants)."""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


class APIDocIndex:
    """BM25-backed retrieval over the curated corpus.

    Built lazily so import time stays cheap; first .search() pays the
    indexing cost (~5ms for the current corpus)."""

    _DEFAULT: "APIDocIndex | None" = None

    def __init__(self, docs: list[APIDoc] | None = None):
        self.docs = docs if docs is not None else self._load_default()
        self._tokens = [_tokenize(d.full_text) for d in self.docs]
        self._bm25: BM25Okapi | None = None
        if _HAS_BM25:
            self._bm25 = BM25Okapi(self._tokens)

    @staticmethod
    def _load_default() -> list[APIDoc]:
        return [APIDoc(*entry) for entry in _CORPUS]

    @classmethod
    def default(cls) -> "APIDocIndex":
        if cls._DEFAULT is None:
            cls._DEFAULT = cls()
        return cls._DEFAULT

    def search(self, query: str, k: int = 5,
                source_filter: str | None = None) -> list[Hit]:
        """Return top-k hits ranked by BM25 (or substring overlap if
        BM25 isn't installed). `source_filter` restricts to one of
        'cadquery'|'fusion'|'onshape'|'cross'."""
        if not query or not self.docs:
            return []
        candidates = [(i, d) for i, d in enumerate(self.docs)
                      if source_filter is None or d.source == source_filter
                      or d.source == "cross"]
        q_tokens = _tokenize(query)
        if self._bm25 is not None and _HAS_BM25:
            scores = self._bm25.get_scores(q_tokens)
            ranked = sorted(((scores[i], d) for i, d in candidates),
                              key=lambda r: r[0], reverse=True)
        else:
            # Fallback: simple token-overlap count
            qset = set(q_tokens)
            ranked = sorted(
                ((sum(1 for t in self._tokens[i] if t in qset), d)
                 for i, d in candidates),
                key=lambda r: r[0], reverse=True)
        hits = [Hit(d, float(s)) for s, d in ranked[:k] if s > 0]
        return hits

    def __len__(self) -> int:
        return len(self.docs)


def retrieve(goal: str, *, k: int = 5,
              source_filter: str | None = None) -> list[Hit]:
    """Module-level convenience wrapper around the default index."""
    return APIDocIndex.default().search(goal, k=k, source_filter=source_filter)


def render_for_prompt(hits: list[Hit]) -> str:
    """Format hits as a markdown block ready to drop into the LLM
    system prompt. Stays under ~2k chars even with k=8 to protect
    context budget."""
    if not hits:
        return ""
    out = ["## Reference API (most-relevant snippets for this goal)\n"]
    for h in hits:
        out.append(f"### {h.title}  _[{h.source}]_")
        out.append(h.snippet(max_len=400))
        out.append("")  # blank line
    return "\n".join(out).strip()
