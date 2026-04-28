r"""feature_taxonomy.py — every CAD feature class we want to support.

Source of truth for the cross-CAD feature support matrix. Each entry
classifies a feature by complexity tier, gives the API entry point per
CAD, and points at the test slug that exercises it.

Tiers (matches SW certifications + general CAD progression):
  T0_BASIC      - sketch primitives + simple extrude/revolve (CSWA)
  T1_CORE       - patterns, fillet/chamfer, shell, mate (CSWP)
  T2_ADVANCED   - sheet metal, surfaces, weldments, mold (CSWPA-*)
  T3_EXPERT     - configurations, design tables, equations, top-down (CSWE)
  T4_SPECIALIST - simulation, CAM toolpath, render, animation

Status per CAD:
  ok               - test passes geometry verify
  partial          - op runs, geometry suspect
  needs_workaround - op consistently fails, has a registered fallback
  unsupported      - CAD doesn't expose this feature
  untested         - nobody has run it yet

Use:
  from aria_os.native_planner.feature_taxonomy import TAXONOMY
  for entry in TAXONOMY:
      print(entry["id"], entry["tier"], entry["sw"]["status"])
"""
from __future__ import annotations

# Each entry is fully self-describing - no inheritance / DRY tricks.
# The verbosity is intentional: the matrix runner emits this same shape
# back into the ledger so we can diff feature support over time.
TAXONOMY: list[dict] = [

    # ===== T0 BASIC (CSWA) =====
    {"id": "sketch_circle", "tier": "T0_BASIC",
     "name": "Circle by center+radius",
     "category": "sketch",
     "test_slug": "sketch_circle",
     "sw":      {"api": "SketchManager.CreateCircleByRadius", "status": "ok"},
     "rhino":   {"api": "RhinoCommon.AddCircle (AriaBridge.cs)", "status": "ok"},
     "fusion":  {"api": "addByCenterRadius (line 385)",
                  "status": "ok"},
     "onshape": {"api": "skCircle FeatureScript", "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; dryrun mode pending bridge restart"},
     "autocad": {"api": "AcDbCircle / ezdxf.circle",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; ezdxf export pending bridge restart"}},

    {"id": "sketch_rect", "tier": "T0_BASIC",
     "name": "Rectangle by center+width+height",
     "category": "sketch",
     "test_slug": "sketch_rect",
     "sw":      {"api": "SketchManager.CreateCenterRectangle", "status": "ok"},
     "rhino":   {"api": "PolylineCurve (4 pts) in AriaBridge.cs",
                  "status": "ok"},
     "fusion":  {"api": "sketchLines.addTwoPointRectangle (line 402)",
                  "status": "ok"},
     "onshape": {"api": "skRectangle FeatureScript", "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "AcDbPolyline rectang", "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; ezdxf export pending bridge restart"}},

    {"id": "sketch_polyline", "tier": "T0_BASIC",
     "name": "Polyline (open or closed)",
     "category": "sketch",
     "test_slug": "sketch_polyline_hexagon",
     "sw":      {"api": "SketchManager.CreateLine repeated", "status": "ok"},
     "rhino":   {"api": "PolylineCurve in AriaBridge.cs", "status": "ok"},
     "fusion":  {"api": "sketchLines.addByTwoPoints (line 575)",
                  "status": "ok"},
     "onshape": {"api": "skLineSegment FeatureScript", "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "AcDbPolyline pline", "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; ezdxf export pending bridge restart"}},

    {"id": "sketch_spline", "tier": "T0_BASIC",
     "name": "Spline through control points",
     "category": "sketch",
     "test_slug": "sketch_spline_blob",
     "sw":      {"api": "SketchManager.CreateSpline (flat double[])",
                  "status": "needs_workaround"},
     "rhino":   {"api": "Curve.CreateInterpolatedCurve (AriaBridge.cs:1155)",
                  "status": "ok"},
     "fusion":  {"api": "sketchFittedSplines.add (line 566)",
                  "status": "ok"},
     "onshape": {"api": "skFitSpline FeatureScript",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "AcDbSpline / ezdxf.spline",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; ezdxf export pending bridge restart"}},

    {"id": "extrude_blind", "tier": "T0_BASIC",
     "name": "Extrude blind, single direction",
     "category": "feat",
     "test_slug": "extrude_blind",
     "sw":      {"api": "FeatureManager.FeatureExtrusion3 (29 args, "
                  "swEndCondBlind)",
                  "status": "ok"},
     "rhino":   {"api": "Brep.CreateFromOffsetFace + boolean (AriaBridge.cs:975)",
                  "status": "ok"},
     "fusion":  {"api": "extrudeFeatures.createInput + .add (line 445)",
                  "status": "ok"},
     "onshape": {"api": "opExtrude FeatureScript",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; synthetic STL export pending bridge restart"},
     "autocad": {"api": "ezdxf extrude simulation",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; synthetic 3D via ezdxf pending bridge restart"}},

    {"id": "extrude_cut", "tier": "T0_BASIC",
     "name": "Extruded cut (subtract through host)",
     "category": "feat",
     "test_slug": "extrude_cut",
     "sw":      {"api": "FeatureManager.FeatureCut4 (28 args, "
                  "useFeatScope+autoSelect)",
                  "status": "ok",
                  "workaround": "recipe DB caches winning combo per call site"},
     "rhino":   {"api": "Brep.CreateBooleanDifference (AriaBridge.cs:975)",
                  "status": "ok"},
     "fusion":  {"api": "extrudeFeatures.createInput "
                  "(operationType=cutFeatureOperation)",
                  "status": "untested"},
     "onshape": {"api": "opExtrude operationType=REMOVE",
                  "status": "untested"},
     "autocad": {"api": "subtract on 3dsolids",
                  "status": "untested"}},

    {"id": "revolve_full", "tier": "T0_BASIC",
     "name": "Revolve 360deg around centerline",
     "category": "feat",
     "test_slug": "revolve_full",
     "sw":      {"api": "FeatureManager.FeatureRevolve2 (20 args, "
                  "Type1=Blind angle=2pi)",
                  "status": "ok"},
     "rhino":   {"api": "RevSurface.Create + CreateFromRevSurface (AriaBridge.cs:1257)",
                  "status": "ok"},
     "fusion":  {"api": "revolveFeatures.createInput + .add (line 597)",
                  "status": "ok"},
     "onshape": {"api": "opRevolve FeatureScript",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; synthetic STEP/STL pending bridge restart"},
     "autocad": {"api": "revolve via param translation",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; synthetic geometry pending bridge restart"}},

    # ===== T1 CORE (CSWP) =====
    {"id": "fillet_constant", "tier": "T1_CORE",
     "name": "Constant-radius fillet on edges",
     "category": "feat",
     "test_slug": "fillet_constant",
     "sw":      {"api": "FeatureManager.FeatureFillet4 (constant radius)",
                  "status": "ok"},
     "rhino":   {"api": "Brep.CreateFilletEdges (AriaBridge.cs:1099)", "status": "ok"},
     "fusion":  {"api": "filletFeatures.addConstantRadiusEdgeSet (line 516)",
                  "status": "ok"},
     "onshape": {"api": "opFillet FeatureScript",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "fillet via param translation",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"}},

    {"id": "chamfer_distance", "tier": "T1_CORE",
     "name": "Chamfer by distance",
     "category": "feat",
     "test_slug": "chamfer_distance",
     "sw":      {"api": "FeatureManager.InsertFeatureChamfer",
                  "status": "ok"},
     "rhino":   {"api": "Brep.CreateChamferEdges",
                  "status": "untested"},
     "fusion":  {"api": "chamferFeatures.createInput.EqualDistanceType",
                  "status": "untested"},
     "onshape": {"api": "opChamfer FeatureScript",
                  "status": "untested"},
     "autocad": {"api": "CHAMFEREDGE 3dsolid",
                  "status": "untested"}},

    {"id": "shell_face_remove", "tier": "T1_CORE",
     "name": "Shell with one face removed",
     "category": "feat",
     "test_slug": "shell_face_remove",
     "sw":      {"api": "FeatureManager.InsertFeatureShell + face select",
                  "status": "ok"},
     "rhino":   {"api": "Brep.CreateShell (AriaBridge.cs:1457)",
                  "status": "ok"},
     "fusion":  {"api": "shellFeatures.createInput + .add (line 720)",
                  "status": "ok"},
     "onshape": {"api": "opShell FeatureScript",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "shell via param translation",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"}},

    {"id": "circular_pattern", "tier": "T1_CORE",
     "name": "Circular pattern of feature",
     "category": "pattern",
     "test_slug": "pattern_circular_6",
     "sw":      {"api": "FeatureCircularPattern5 (broken in IDispatch)",
                  "status": "needs_workaround",
                  "workaround": "expand to N explicit cuts at validator"},
     "rhino":   {"api": "Transform.Rotation + Boolean loop (AriaBridge.cs:1045)",
                  "status": "ok"},
     "fusion":  {"api": "circularPatternFeatures.add (line 493)",
                  "status": "ok"},
     "onshape": {"api": "opPattern FeatureScript (CIRCULAR type)",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "circularPattern via param translation",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"}},

    {"id": "linear_pattern", "tier": "T1_CORE",
     "name": "Linear pattern of feature (1D or 2D grid)",
     "category": "pattern",
     "test_slug": "pattern_linear_grid",
     "sw":      {"api": "FeatureLinearPattern - similar IDispatch issues",
                  "status": "needs_workaround",
                  "workaround": "expand to N explicit cuts at validator"},
     "rhino":   {"api": "Transform.Translation + duplicate",
                  "status": "untested"},
     "fusion":  {"api": "rectangularPatternFeatures.add",
                  "status": "untested"},
     "onshape": {"api": "opPattern FeatureScript (RECTANGULAR)",
                  "status": "untested"},
     "autocad": {"api": "ARRAYRECT",
                  "status": "untested"}},

    {"id": "mirror_about_plane", "tier": "T1_CORE",
     "name": "Mirror feature/body about reference plane",
     "category": "pattern",
     "test_slug": "adv_pattern_mirror_4holes",
     "sw":      {"api": "InsertMirrorFeature2 (IDispatch issues)",
                  "status": "needs_workaround"},
     "rhino":   {"api": "Transform.Mirror",
                  "status": "untested"},
     "fusion":  {"api": "mirrorFeatures.add",
                  "status": "untested"},
     "onshape": {"api": "opPattern FeatureScript (MIRROR)",
                  "status": "untested"},
     "autocad": {"api": "MIRROR3D",
                  "status": "untested"}},

    {"id": "hole_wizard_drill", "tier": "T1_CORE",
     "name": "Hole Wizard - simple drilled hole",
     "category": "feat",
     "test_slug": "hole_wizard_drill",
     "sw":      {"api": "FeatureManager.HoleWizard5 + face select",
                  "status": "needs_workaround",
                  "workaround": "needs face-id param threading"},
     "rhino":   {"api": "n/a - use Brep.CreateBooleanDifference with cylinder",
                  "status": "untested"},
     "fusion":  {"api": "holeFeatures.createSimpleInput",
                  "status": "untested"},
     "onshape": {"api": "opHole FeatureScript",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "holeWizard via param translation",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"}},

    {"id": "helix_constant", "tier": "T1_CORE",
     "name": "Helix - constant pitch + diameter",
     "category": "feat",
     "test_slug": "helix_constant",
     "sw":      {"api": "AddVariablePitchHelixFirstPitchAndDiameter "
                  "+ EndVariablePitchHelix",
                  "status": "ok"},
     "rhino":   {"api": "NurbsCurve.CreateSpiral (AriaBridge.cs:1345)",
                  "status": "ok"},
     "fusion":  {"api": "helixFeatures.createInput (line 658)",
                  "status": "ok"},
     "onshape": {"api": "opHelix FeatureScript",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "helix via param translation",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"}},

    {"id": "rib_open_profile", "tier": "T1_CORE",
     "name": "Rib from open sketch profile",
     "category": "feat",
     "test_slug": "rib_diagonal",
     "sw":      {"api": "FeatureManager.FeatureRib3",
                  "status": "needs_workaround",
                  "workaround": "sketch must be on perpendicular plane"},
     "rhino":   {"api": "n/a - construct manually",
                  "status": "untested"},
     "fusion":  {"api": "ribFeatures.createInput",
                  "status": "untested"},
     "onshape": {"api": "opRib FeatureScript",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "rib via param translation",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"}},

    {"id": "draft_neutral", "tier": "T1_CORE",
     "name": "Draft with neutral plane",
     "category": "feat",
     "test_slug": "draft_neutral",
     "sw":      {"api": "FeatureManager.InsertDraftDC2",
                  "status": "needs_workaround",
                  "workaround": "face hit-test misses; needs face-id"},
     "rhino":   {"api": "n/a - use surface offset + trim",
                  "status": "untested"},
     "fusion":  {"api": "draftFeatures.createInput + .add (line 803)",
                  "status": "ok"},
     "onshape": {"api": "opDraft FeatureScript",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "draft via param translation",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"}},

    # ===== T2 ADVANCED (CSWPA-*) =====
    # CSWPA-SU: Surfaces
    {"id": "surface_extrude_open", "tier": "T2_ADVANCED",
     "name": "Surface from open profile extrude",
     "category": "surf",
     "subcategory": "CSWPA-SU",
     "test_slug": "surface_knit_two",
     "sw":      {"api": "FeatureExtrusionThin2 (reflection-typed args)",
                  "status": "ok",
                  "verified_at": "2026-04-27T20:25"},
     "rhino":   {"api": "Surface.CreateExtrusion",
                  "status": "untested"},
     "fusion":  {"api": "extrudeFeatures (surface mode)",
                  "status": "ok"},
     "onshape": {"api": "opExtrude (surface mode)",
                  "status": "untested"},
     "autocad": {"api": "PLANESURF / SURFEXTRUDE",
                  "status": "untested"}},

    {"id": "surface_loft", "tier": "T2_ADVANCED",
     "name": "Loft surface between cross sections",
     "category": "surf", "subcategory": "CSWPA-SU",
     "test_slug": "loft_two_profiles",
     "sw":      {"api": "FeatureManager.InsertProtrusionBlend2 "
                  "(reflection-typed args + newSketch offset_mm support "
                  "for distinct profile planes)",
                  "status": "ok",
                  "verified_at": "2026-04-27T21:08"},
     "rhino":   {"api": "Brep.CreateFromLoftRefit",
                  "status": "untested"},
     "fusion":  {"api": "loftFeatures.createInput (line 640)",
                  "status": "ok"},
     "onshape": {"api": "opLoft FeatureScript",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"},
     "autocad": {"api": "loft via param translation",
                  "status": "partial",
                  "note": "cross-CAD handler added 2026-04-28; pending bridge restart"}},

    {"id": "surface_knit", "tier": "T2_ADVANCED",
     "name": "Knit / sew surfaces into a closed shell",
     "category": "surf", "subcategory": "CSWPA-SU",
     "test_slug": "knit_two_surfaces",
     "sw":      {"api": "FeatureManager.InsertSewing2",
                  "status": "untested"},
     "rhino":   {"api": "Brep.JoinBreps",
                  "status": "untested"},
     "fusion":  {"api": "stitchFeatures.add",
                  "status": "untested"},
     "onshape": {"api": "opThicken / opShell after merge",
                  "status": "untested"},
     "autocad": {"api": "SURFSCULPT / CONVTOSOLID",
                  "status": "untested"}},

    # CSWPA-SM: Sheet Metal
    {"id": "sheet_metal_base_flange", "tier": "T2_ADVANCED",
     "name": "Sheet Metal Base Flange",
     "category": "sm", "subcategory": "CSWPA-SM",
     "test_slug": "sm_base_flange_real",
     "sw":      {"api": "FeatureManager.InsertSheetMetalBaseFlange2",
                  "status": "ok",
                  "workaround": "validator transform: emit thin extrude "
                                "(operation=new, distance=thickness). "
                                "SM metadata preserved as sm_thickness_mm.",
                  "verified_at": "2026-04-27T20:19"},
     "rhino":   {"api": "n/a - no native sheet metal",
                  "status": "unsupported"},
     "fusion":  {"api": "n/a - sheet metal env required",
                  "status": "untested"},
     "onshape": {"api": "opSheetMetalStart + opThicken",
                  "status": "untested"},
     "autocad": {"api": "n/a",
                  "status": "unsupported"}},

    {"id": "sheet_metal_edge_flange", "tier": "T2_ADVANCED",
     "name": "Sheet Metal Edge Flange",
     "category": "sm", "subcategory": "CSWPA-SM",
     "test_slug": "sheet_metal_edge_flange",
     "sw":      {"api": "FeatureManager.InsertSheetMetalEdgeFlange2",
                  "status": "needs_workaround"},
     "rhino":   {"api": "unsupported", "status": "unsupported"},
     "fusion":  {"api": "sheet metal feature", "status": "untested"},
     "onshape": {"api": "opSheetMetalFlange", "status": "untested"},
     "autocad": {"api": "unsupported", "status": "unsupported"}},

    {"id": "sheet_metal_jog", "tier": "T2_ADVANCED",
     "name": "Sheet Metal Jog (Z-bend)",
     "category": "sm", "subcategory": "CSWPA-SM",
     "test_slug": "sheet_metal_jog",
     "sw":      {"api": "FeatureManager.InsertJogBend",
                  "status": "untested"},
     "rhino":   {"api": "unsupported", "status": "unsupported"},
     "fusion":  {"api": "n/a in sheet metal env", "status": "untested"},
     "onshape": {"api": "opSheetMetalJog", "status": "untested"},
     "autocad": {"api": "unsupported", "status": "unsupported"}},

    {"id": "sheet_metal_unfold", "tier": "T2_ADVANCED",
     "name": "Unfold / Flatten sheet metal",
     "category": "sm", "subcategory": "CSWPA-SM",
     "test_slug": "sheet_metal_unfold",
     "sw":      {"api": "FeatureManager.InsertSheetMetalUnfold "
                  "+ FlatPattern",
                  "status": "untested"},
     "rhino":   {"api": "unsupported", "status": "unsupported"},
     "fusion":  {"api": "FlatPatternFeature", "status": "untested"},
     "onshape": {"api": "opSheetMetalFlattenedView", "status": "untested"},
     "autocad": {"api": "unsupported", "status": "unsupported"}},

    # CSWPA-WD: Weldments
    {"id": "weldment_structural_member", "tier": "T2_ADVANCED",
     "name": "Weldment Structural Member (profile along path)",
     "category": "weldment", "subcategory": "CSWPA-WD",
     "test_slug": "weldment_struct_member",
     "sw":      {"api": "FeatureManager.InsertWeldmentMembers",
                  "status": "needs_workaround",
                  "workaround": "emulated via swept-extrude per leg"},
     "rhino":   {"api": "n/a - sweep along path", "status": "untested"},
     "fusion":  {"api": "n/a - manual modelling", "status": "untested"},
     "onshape": {"api": "opSweep with profile + path",
                  "status": "untested"},
     "autocad": {"api": "SWEEP", "status": "untested"}},

    {"id": "weldment_trim_extend", "tier": "T2_ADVANCED",
     "name": "Weldment trim/extend at corners",
     "category": "weldment", "subcategory": "CSWPA-WD",
     "test_slug": "weldment_trim_extend",
     "sw":      {"api": "FeatureManager.InsertTrimExtendCornerFeature",
                  "status": "untested"},
     "rhino":   {"api": "Brep boolean trim", "status": "untested"},
     "fusion":  {"api": "n/a", "status": "untested"},
     "onshape": {"api": "n/a", "status": "untested"},
     "autocad": {"api": "TRIM", "status": "untested"}},

    {"id": "weldment_end_cap", "tier": "T2_ADVANCED",
     "name": "Weldment End Cap on tube",
     "category": "weldment", "subcategory": "CSWPA-WD",
     "test_slug": "weldment_end_cap",
     "sw":      {"api": "FeatureManager.InsertEndCap",
                  "status": "untested"},
     "rhino":   {"api": "n/a", "status": "unsupported"},
     "fusion":  {"api": "n/a", "status": "untested"},
     "onshape": {"api": "n/a", "status": "untested"},
     "autocad": {"api": "n/a", "status": "unsupported"}},

    {"id": "weldment_cut_list", "tier": "T2_ADVANCED",
     "name": "Weldment Cut List BOM",
     "category": "weldment", "subcategory": "CSWPA-WD",
     "test_slug": "weldment_cut_list",
     "sw":      {"api": "Configuration.CutListItems",
                  "status": "untested"},
     "rhino":   {"api": "n/a", "status": "unsupported"},
     "fusion":  {"api": "n/a", "status": "untested"},
     "onshape": {"api": "n/a", "status": "untested"},
     "autocad": {"api": "n/a", "status": "unsupported"}},

    # CSWPA-MM: Mold Tools
    {"id": "mold_parting_line", "tier": "T2_ADVANCED",
     "name": "Mold Parting Line",
     "category": "mold", "subcategory": "CSWPA-MM",
     "test_slug": "mold_parting_line",
     "sw":      {"api": "FeatureManager.InsertMoldPartingLine",
                  "status": "untested"},
     "rhino":   {"api": "n/a", "status": "unsupported"},
     "fusion":  {"api": "moldDesign features", "status": "untested"},
     "onshape": {"api": "n/a", "status": "untested"},
     "autocad": {"api": "n/a", "status": "unsupported"}},

    {"id": "mold_tooling_split", "tier": "T2_ADVANCED",
     "name": "Tooling Split (core/cavity)",
     "category": "mold", "subcategory": "CSWPA-MM",
     "test_slug": "mold_tooling_split",
     "sw":      {"api": "FeatureManager.InsertToolingSplit2",
                  "status": "untested"},
     "rhino":   {"api": "n/a", "status": "unsupported"},
     "fusion":  {"api": "n/a", "status": "untested"},
     "onshape": {"api": "n/a", "status": "untested"},
     "autocad": {"api": "n/a", "status": "unsupported"}},

    # ===== T3 EXPERT (CSWE) =====
    {"id": "configuration_create", "tier": "T3_EXPERT",
     "name": "Create Configuration (named state)",
     "category": "config",
     "test_slug": "config_create_two",
     "sw":      {"api": "ConfigurationManager.AddConfiguration2 "
                  "(reflection-typed args; ShowConfiguration2 + "
                  "SetSuppression2 still no-op silently — see follow-up)",
                  "status": "ok",
                  "verified_at": "2026-04-27T20:46"},
     "rhino":   {"api": "n/a - use blocks/instances", "status": "unsupported"},
     "fusion":  {"api": "Design.activeConfiguration / "
                  "ConfigurationsManager",
                  "status": "untested"},
     "onshape": {"api": "Variable Studio / Configuration",
                  "status": "untested"},
     "autocad": {"api": "n/a - use Sheet Sets", "status": "unsupported"}},

    {"id": "design_table_excel", "tier": "T3_EXPERT",
     "name": "Design Table from Excel",
     "category": "config",
     "test_slug": "design_table_basic",
     "sw":      {"api": "ModelDoc2.InsertFamilyTableNew",
                  "status": "untested"},
     "rhino":   {"api": "n/a", "status": "unsupported"},
     "fusion":  {"api": "n/a (use parameters)", "status": "untested"},
     "onshape": {"api": "Configuration table",
                  "status": "untested"},
     "autocad": {"api": "n/a", "status": "unsupported"}},

    {"id": "equation_global_var", "tier": "T3_EXPERT",
     "name": "Global Variable driving dimension",
     "category": "equation",
     "test_slug": "equation_global_var",
     "sw":      {"api": "EquationMgr.Add2 (verified 2026-04-27 with bbox check)",
                  "status": "ok"},
     "rhino":   {"api": "n/a", "status": "unsupported"},
     "fusion":  {"api": "userParameters.add",
                  "status": "untested"},
     "onshape": {"api": "Variable Studio",
                  "status": "untested"},
     "autocad": {"api": "Parametric constraint", "status": "untested"}},

    {"id": "in_context_top_down", "tier": "T3_EXPERT",
     "name": "In-context (top-down) reference between parts",
     "category": "assembly",
     "test_slug": "top_down_ref",
     "sw":      {"api": "AssemblyDoc.EditPart3 + Convert Entities",
                  "status": "untested"},
     "rhino":   {"api": "Block instance reference", "status": "untested"},
     "fusion":  {"api": "rootComponent + parametric refs",
                  "status": "untested"},
     "onshape": {"api": "Master Part Studio + multi-derive",
                  "status": "untested"},
     "autocad": {"api": "Xref", "status": "untested"}},

    {"id": "master_model", "tier": "T3_EXPERT",
     "name": "Master model (skeleton-driven assembly)",
     "category": "assembly",
     "test_slug": "master_model_skeleton",
     "sw":      {"api": "Insert Part + Insert Body",
                  "status": "untested"},
     "rhino":   {"api": "Block + replace geometry",
                  "status": "untested"},
     "fusion":  {"api": "derived feature (Insert Derive)",
                  "status": "untested"},
     "onshape": {"api": "Multi Part Studio derivation",
                  "status": "untested"},
     "autocad": {"api": "n/a", "status": "unsupported"}},

    # ===== T4 SPECIALIST =====
    {"id": "fea_static_stress", "tier": "T4_SPECIALIST",
     "name": "Static stress analysis (FEA)",
     "category": "simulation",
     "test_slug": "fea_static_bracket",
     "sw":      {"api": "Simulation add-in (CWMesher / CWStudy)",
                  "status": "ok",
                  "workaround": "OpRunFEA implemented earlier"},
     "rhino":   {"api": "external (Karamba3D plugin)",
                  "status": "untested"},
     "fusion":  {"api": "simulationStudies", "status": "untested"},
     "onshape": {"api": "n/a (external)", "status": "unsupported"},
     "autocad": {"api": "n/a", "status": "unsupported"}},

    {"id": "cam_2d_pocket", "tier": "T4_SPECIALIST",
     "name": "CAM 2D pocket toolpath",
     "category": "cam",
     "test_slug": "cam_pocket",
     "sw":      {"api": "CAM add-in", "status": "untested"},
     "rhino":   {"api": "RhinoCAM external", "status": "untested"},
     "fusion":  {"api": "Manufacture workspace", "status": "untested"},
     "onshape": {"api": "Manufacturing app", "status": "untested"},
     "autocad": {"api": "n/a", "status": "unsupported"}},
]


def by_tier(tier: str) -> list[dict]:
    """Return all entries at a given tier."""
    return [e for e in TAXONOMY if e["tier"] == tier]


def by_cad_status(cad: str, status: str) -> list[dict]:
    """Return entries where the named CAD has the given status."""
    return [e for e in TAXONOMY
            if e.get(cad, {}).get("status") == status]


def coverage_by_cad(cad: str) -> dict[str, int]:
    """Count features by status for a given CAD."""
    counts: dict[str, int] = {}
    for e in TAXONOMY:
        s = e.get(cad, {}).get("status", "untested")
        counts[s] = counts.get(s, 0) + 1
    return counts


def render_summary() -> str:
    """Markdown overview - what we know about each CAD across all tiers."""
    out = ["# Cross-CAD Feature Taxonomy\n"]
    out.append(f"Total features tracked: **{len(TAXONOMY)}**\n")
    tiers = ["T0_BASIC", "T1_CORE", "T2_ADVANCED",
             "T3_EXPERT", "T4_SPECIALIST"]
    out.append("\n## By tier")
    for t in tiers:
        n = len(by_tier(t))
        out.append(f"- {t}: {n} features")

    cads = ["sw", "rhino", "fusion", "onshape", "autocad"]
    out.append("\n## Per-CAD coverage")
    out.append("| CAD | ok | needs_workaround | unsupported | untested |")
    out.append("|-----|----|------------------|-------------|----------|")
    for c in cads:
        cov = coverage_by_cad(c)
        out.append(f"| {c} | {cov.get('ok', 0)} | "
                    f"{cov.get('needs_workaround', 0)} | "
                    f"{cov.get('unsupported', 0)} | "
                    f"{cov.get('untested', 0)} |")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    print(render_summary())
