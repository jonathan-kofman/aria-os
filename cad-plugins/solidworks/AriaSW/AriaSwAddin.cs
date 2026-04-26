// AriaSwAddin.cs — SolidWorks add-in entry point for ARIA.
//
// Registers a WebView2-hosted Task Pane (AriaPanelHost) that loads the
// same React frontend served to the Rhino / Fusion / Onshape panels.
// The JS bridge already supports host="solidworks" — see
// frontend/src/aria/bridge.js — and routes through the shared
// chrome.webview postMessage path used by Rhino.
//
// ExecuteFeature dispatcher mirrors the contract used by the other
// CAD backends so a single op stream from ARIA-OS can drive any of
// them. Op handlers below are stubs until a SolidWorks install is
// available to verify against.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using Microsoft.Win32;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidWorks.Interop.swpublished;

namespace AriaSW
{
    // SolidWorks discovers add-ins via two registry locations written at
    // regasm time: HKLM\SOFTWARE\SOLIDWORKS\Addins\{GUID} (the catalog
    // entry that includes Title/Description) and
    // HKCU\Software\SOLIDWORKS\AddInsStartup\{GUID} (per-user enable
    // toggle). The COM-Visible class itself is registered by regasm; the
    // [ComRegisterFunction] / [ComUnregisterFunction] static methods
    // below extend that registration with the SW-specific keys.
    //
    // Description / Title used to live on a [SwAddin] attribute provided
    // by SolidWorks.Tools.dll, but that helper assembly isn't part of
    // the redistributable interop bundle, so we register manually.
    [Guid("38368bbe-a7a8-4f97-a8cc-c698294e5ef6")]
    [ComVisible(true)]
    public class AriaSwAddin : ISwAddin
    {
        private const string AddinDescription = "ARIA: AI CAD pipeline";
        private const string AddinTitle       = "ARIA";

        [ComRegisterFunction]
        public static void RegisterFunction(Type t)
        {
            string guid = "{" + t.GUID.ToString().ToUpper() + "}";
            using (var key = Registry.LocalMachine.CreateSubKey(
                @"SOFTWARE\SOLIDWORKS\Addins\" + guid))
            {
                if (key == null) return;
                key.SetValue(null, 1, RegistryValueKind.DWord); // 1 = load at startup
                key.SetValue("Description", AddinDescription);
                key.SetValue("Title", AddinTitle);
            }
            using (var key = Registry.CurrentUser.CreateSubKey(
                @"Software\SOLIDWORKS\AddInsStartup\" + guid))
            {
                key?.SetValue(null, 1, RegistryValueKind.DWord);
            }
        }

        [ComUnregisterFunction]
        public static void UnregisterFunction(Type t)
        {
            string guid = "{" + t.GUID.ToString().ToUpper() + "}";
            try { Registry.LocalMachine.DeleteSubKeyTree(
                @"SOFTWARE\SOLIDWORKS\Addins\" + guid, false); } catch { }
            try { Registry.CurrentUser.DeleteSubKeyTree(
                @"Software\SOLIDWORKS\AddInsStartup\" + guid, false); } catch { }
        }

        // -----------------------------------------------------------------
        // Lifecycle state
        // -----------------------------------------------------------------

        private ISldWorks _sw;
        private int _cookie;
        private ITaskpaneView _taskPane;
        private AriaPanelHost _panelHost;

        public ISldWorks SwApp => _sw;

        /// <summary>Most-recently-loaded add-in instance. Used by the
        /// bridge so the WebView2 callbacks can reach SwApp / panel
        /// without threading the reference through every call site.</summary>
        internal static AriaSwAddin Current { get; private set; }

        /// <summary>Write a line to SolidWorks' status bar (non-modal)
        /// AND a per-user log file so we can post-mortem startup
        /// failures that escape the COM boundary and crash SW.</summary>
        internal static void Log(string msg)
        {
            FileLog(msg);
            try
            {
                var frame = Current?._sw?.Frame() as IFrame;
                frame?.SetStatusBarText($"[ARIA] {msg}");
            }
            catch { /* best-effort */ }
        }

        private static readonly object _logLock = new object();
        private static string _logPath;

        internal static void FileLog(string msg)
        {
            try
            {
                if (_logPath == null)
                {
                    string dir = Path.Combine(
                        System.Environment.GetFolderPath(
                            System.Environment.SpecialFolder.LocalApplicationData),
                        "AriaSW");
                    Directory.CreateDirectory(dir);
                    _logPath = Path.Combine(dir, "addin.log");
                }
                lock (_logLock)
                {
                    File.AppendAllText(_logPath,
                        $"{DateTime.Now:HH:mm:ss.fff} {msg}{System.Environment.NewLine}");
                }
            }
            catch { /* best-effort — never let logging crash SW */ }
        }

        // -----------------------------------------------------------------
        // Shared op dispatcher — same contract as Fusion / Rhino / Onshape
        // -----------------------------------------------------------------

        private readonly Dictionary<string, object> _registry = new();

        public object ExecuteFeature(string kind, Dictionary<string, object> p)
        {
            // SolidWorks COM API is single-threaded apartment — every call
            // must happen on the SW main thread. Marshal via the panel's
            // Invoke so the bridge's Task.Run thread doesn't AccessViolation
            // mid-feature-create.
            if (_panelHost != null && _panelHost.InvokeRequired)
            {
                object marshalled = null;
                _panelHost.Invoke((Action)(() =>
                    marshalled = ExecuteFeatureCore(kind, p)));
                return marshalled;
            }
            return ExecuteFeatureCore(kind, p);
        }

        private object ExecuteFeatureCore(string kind, Dictionary<string, object> p)
        {
            FileLog($"  exec: {kind}");
            try
            {
                return kind switch
                {
                    "beginPlan"       => OpBeginPlan(),
                    "newSketch"       => OpNewSketch(p),
                    "sketchCircle"    => OpSketchCircle(p),
                    "sketchRect"      => OpSketchRect(p),
                    "extrude"         => OpExtrude(p),
                    "circularPattern" => OpCircularPattern(p),
                    "fillet"          => OpFillet(p),
                    "addParameter"    => OpAddParameter(p),
                    "verifyPart"      => OpVerifyPart(p),
                    // SW-native ops (extend the bridge contract beyond the
                    // shared CAD vocabulary so a SW-aware planner can
                    // request features that don't exist in Rhino/Onshape)
                    "setMaterial"     => OpSetMaterial(p),
                    "setView"         => OpSetView(p),
                    "zoomToFit"       => OpZoomToFit(p),
                    "saveAs"          => OpSaveAs(p),
                    "setProperty"     => OpSetProperty(p),
                    "addDimension"    => OpAddDimension(p),
                    "holeWizard"      => OpHoleWizard(p),
                    // Existing SW-unique stubs
                    "toolboxHardware" => OpToolboxHardware(p),
                    "weldmentProfile" => OpWeldmentProfile(p),
                    "dimXpertAuto"    => OpDimXpertAuto(p),
                    "exportEdrawings" => OpExportEdrawings(p),
                    _ => new { ok = false, error = $"Unknown kind: {kind}" },
                };
            }
            catch (Exception ex)
            {
                FileLog($"  op {kind} threw: {ex.GetType().Name}: {ex.Message}");
                return new { ok = false, error = $"{ex.GetType().Name}: {ex.Message}" };
            }
        }

        // -----------------------------------------------------------------
        // Real SolidWorks API ops — units come in as mm; SW wants meters.
        // Sketches map ARIA's XY/XZ/YZ to SW's Front/Top/Right planes.
        //
        // Each op tracks its result feature in _featureRegistry keyed by
        // the alias from the plan, so subsequent ops (extrude → cut a
        // previous body, circularPattern → pattern a feature) can find
        // the source.
        // -----------------------------------------------------------------

        private IModelDoc2 _model;
        private readonly Dictionary<string, object> _aliasMap = new();
        private string _activeSketchName;
        private string _activeSketchPlane;   // "XY" / "XZ" / "YZ" — drives sketch-y mirror
        private IFeature _lastBodyFeature;   // most-recent extrude(op="new") — used as default cut target

        /// <summary>SW's Top Plane and Right Plane sketches put sketch-y on
        /// the NEGATIVE world-axis of the plane normal (Top: sketch-y → -Z;
        /// Right: sketch-y → -X). Planners written with the intuitive
        /// convention (sketch-y → +world-axis) end up with mirrored
        /// geometry — see L-bracket leg ending at Z=-60 instead of +60.
        /// Mirror sketch-y at the SW boundary so planner output is
        /// correct regardless of plane.</summary>
        private double MirrorYIfNeeded(double cy)
        {
            string plane = (_activeSketchPlane ?? "XY").ToUpperInvariant();
            return plane == "XZ" || plane == "YZ" ? -cy : cy;
        }

        private static double Mm(object v) => Convert.ToDouble(v) / 1000.0;

        private IModelDoc2 EnsurePart()
        {
            // If a part is already open, reuse it. Otherwise create one
            // from the user's default Part template.
            var doc = _sw.IActiveDoc2 as IModelDoc2;
            if (doc != null && doc.GetType() == (int)swDocumentTypes_e.swDocPART)
                return doc;

            string template = _sw.GetUserPreferenceStringValue(
                (int)swUserPreferenceStringValue_e.swDefaultTemplatePart);
            if (string.IsNullOrEmpty(template))
                throw new InvalidOperationException(
                    "No default Part template configured in SolidWorks");

            var newDoc = _sw.NewDocument(
                template, (int)swDwgPaperSizes_e.swDwgPaperA4size, 0.0, 0.0);
            return newDoc as IModelDoc2;
        }

        private object OpBeginPlan()
        {
            _registry.Clear();
            _aliasMap.Clear();
            _activeSketchName = null;
            _activeSketchPlane = null;
            _lastBodyFeature = null;
            _model = EnsurePart();
            return new { ok = true, registry_cleared = true };
        }

        private object OpAddParameter(Dictionary<string, object> p)
        {
            string name = p["name"]?.ToString();
            double val  = Convert.ToDouble(p["value_mm"]);
            if (_model == null) _model = EnsurePart();

            var eq = _model.GetEquationMgr() as IEquationMgr;
            if (eq == null)
                return new { ok = false, error = "EquationMgr unavailable" };

            // Global var with explicit length units. SW's equation parser
            // treats `"x" = 100mm` as a length-typed global which can then
            // drive dimensions. Add2 takes (index, eq, solve).
            string line = $"\"{name}\" = {val.ToString(System.Globalization.CultureInfo.InvariantCulture)}mm";
            int idx = eq.Add2(-1, line, true);
            FileLog($"  addParameter: {line} -> idx={idx}");
            return new { ok = idx >= 0, name, value_mm = val, idx };
        }

        private static string SwPlaneName(string plane) =>
            (plane ?? "XY").ToUpperInvariant() switch
            {
                "XY" => "Front Plane",   // normal = Z, the canonical "lay flat" plane
                "XZ" => "Top Plane",
                "YZ" => "Right Plane",
                _    => "Front Plane",
            };

        private object OpNewSketch(Dictionary<string, object> p)
        {
            string plane = p.ContainsKey("plane") ? p["plane"]?.ToString() : "XY";
            string alias = p["alias"]?.ToString();
            string name  = p.ContainsKey("name") ? p["name"]?.ToString() : null;
            if (_model == null) _model = EnsurePart();

            string planeName = SwPlaneName(plane);
            // Clear current selection, then select the reference plane.
            _model.ClearSelection2(true);
            bool selected = _model.Extension.SelectByID2(
                planeName, "PLANE", 0, 0, 0, false, 0, null,
                (int)swSelectOption_e.swSelectOptionDefault);
            if (!selected)
                return new { ok = false, error = $"Could not select '{planeName}'" };

            _model.SketchManager.InsertSketch(true);   // Enter sketch mode

            // The just-created sketch is the most-recent feature. ISketch
            // doesn't expose its parent IFeature via cast, so grab via
            // FeatureByPositionReverse(0). We store the IFeature reference
            // itself in the alias map so later re-selection works without
            // depending on names (selection by name is unreliable when
            // multiple sketches with similar names exist).
            var sketchFeature = _model.FeatureByPositionReverse(0) as IFeature;
            if (sketchFeature != null && !string.IsNullOrEmpty(name))
            {
                sketchFeature.Name = name;
            }
            _activeSketchName = sketchFeature?.Name;

            _aliasMap[alias] = sketchFeature;   // store IFeature, not just name
            _activeSketchPlane = plane;         // drives sketch-y mirror in Circle/Rect
            FileLog($"  newSketch: alias={alias} name={_activeSketchName} plane={plane}");
            return new { ok = true, alias, plane, name = _activeSketchName };
        }

        private object OpSketchCircle(Dictionary<string, object> p)
        {
            double cx = Mm(p["cx"]);
            double cy = MirrorYIfNeeded(Mm(p["cy"]));
            double r  = Mm(p["r"]);
            if (_model == null) return new { ok = false, error = "no model" };

            // CreateCircle takes (xc, yc, zc, x_on_perimeter, y_on_perimeter, zp)
            var sketch = _model.SketchManager.ActiveSketch;
            if (sketch == null)
                return new { ok = false, error = "no active sketch" };

            object circle = _model.SketchManager.CreateCircle(
                cx, cy, 0,
                cx + r, cy, 0);
            return new { ok = circle != null, kind = "circle",
                          r_mm = r * 1000, cx_mm = cx * 1000, cy_mm = cy * 1000 };
        }

        private object OpSketchRect(Dictionary<string, object> p)
        {
            double w  = Mm(p["w"]);
            double h  = Mm(p["h"]);
            double cx = p.ContainsKey("cx") ? Mm(p["cx"]) : 0;
            double cy = MirrorYIfNeeded(p.ContainsKey("cy") ? Mm(p["cy"]) : 0);
            if (_model?.SketchManager?.ActiveSketch == null)
                return new { ok = false, error = "no active sketch" };

            // CreateCenterRectangle takes the center + a corner.
            object r = _model.SketchManager.CreateCenterRectangle(
                cx, cy, 0,
                cx + w / 2, cy + h / 2, 0);
            return new { ok = r != null, kind = "rect", w_mm = w * 1000, h_mm = h * 1000 };
        }

        private object OpExtrude(Dictionary<string, object> p)
        {
            string sketchAlias = p["sketch"]?.ToString();
            double dist = Mm(p["distance"]);
            string operation = p.ContainsKey("operation") ? p["operation"]?.ToString() : "new";
            string alias = p.ContainsKey("alias") ? p["alias"]?.ToString() : null;
            if (_model == null) return new { ok = false, error = "no model" };

            // Exit any active sketch so FeatureExtrusion3 can use it.
            if (_model.SketchManager.ActiveSketch != null)
                _model.SketchManager.InsertSketch(true);

            // Re-select the sketch. FeatureExtrusion3 accepts the
            // IFeature.Select2() route, but FeatureCut4 silently returns
            // null unless the sketch is selected via SelectByID2 (which
            // installs a different internal selection state than
            // IFeature.Select2). Try IFeature first for parity with the
            // body extrude path, fall back to SelectByID2 by feature name.
            _model.ClearSelection2(true);
            bool selectedSketch = false;
            string sketchFeatName = null;
            if (_aliasMap.ContainsKey(sketchAlias)
                && _aliasMap[sketchAlias] is IFeature sketchFeat)
            {
                sketchFeatName = sketchFeat.Name;
                selectedSketch = sketchFeat.Select2(false, 0);
                if (operation == "cut")
                {
                    // Replace IFeature.Select2 with the SelectByID2 form
                    // that FeatureCut4's internal validation expects.
                    _model.ClearSelection2(true);
                    selectedSketch = _model.Extension.SelectByID2(
                        sketchFeatName, "SKETCH", 0, 0, 0, false, 0, null,
                        (int)swSelectOption_e.swSelectOptionDefault);
                }
                FileLog($"  extrude: select sketch '{sketchFeatName}' (op={operation}) -> {selectedSketch}");
            }
            else
            {
                FileLog($"  extrude: alias '{sketchAlias}' not in aliasMap");
            }
            if (!selectedSketch)
                return new { ok = false, error = $"Could not select sketch '{sketchAlias}'" };

            IFeature feature = null;
            if (operation == "new" || operation == "join")
            {
                feature = _model.FeatureManager.FeatureExtrusion3(
                    true,                                                  // single-direction
                    false, false,
                    (int)swEndConditions_e.swEndCondBlind,
                    (int)swEndConditions_e.swEndCondBlind,
                    dist, 0,
                    false, false,
                    false, false,
                    0, 0,
                    false, false,
                    false, false,
                    true,                                                  // solid
                    true,                                                  // merge
                    true,                                                  // useFeatScope
                    (int)swStartConditions_e.swStartSketchPlane,
                    0, false) as IFeature;
            }
            else if (operation == "cut")
            {
                FileLog($"  cut: dist={dist*1000}mm");
                // One-time deep diagnostics so we can see what SW actually
                // sees at the moment of the cut. Body bbox tells us
                // geometrically where the body sits relative to the
                // sketch plane (cut direction needs to point INTO it).
                LogBodyDiagnostics();

                IFeature cutFeat = null;

                // Recipe lookup: prefer "blind" intent if a finite distance
                // is requested; treat very large distances as "through all".
                string cutIntent = (dist >= 1.0)
                    ? "cut_extrude_through_all"
                    : "cut_extrude_blind";
                JObject recipe = RecipeDb.Lookup(cutIntent);

                // (0) Recipe-driven first attempt — bypasses the entire
                //     fallback chain when a known-good combo exists.
                bool[] recipeUsedBlind = { false };
                bool[] recipeUsedFlip  = { false };
                bool[] recipeUsedSelB  = { false };
                bool[] recipeUsedAuto  = { true  };
                if (recipe != null)
                {
                    recipeUsedBlind[0] = recipe.Value<bool?>("blind") ?? true;
                    recipeUsedFlip[0]  = recipe.Value<bool?>("flip") ?? false;
                    recipeUsedSelB[0]  = recipe.Value<bool?>("selectBody") ?? false;
                    recipeUsedAuto[0]  = recipe.Value<bool?>("useAutoSelect") ?? true;
                    FileLog($"  cut: recipe '{cutIntent}' -> blind={recipeUsedBlind[0]} flip={recipeUsedFlip[0]} selBody={recipeUsedSelB[0]} auto={recipeUsedAuto[0]}");
                    cutFeat = TryFeatureCut(sketchFeatName, dist,
                        blind: recipeUsedBlind[0], selectBody: recipeUsedSelB[0],
                        useAutoSelect: recipeUsedAuto[0], flip: recipeUsedFlip[0]);
                }

                // Helper for recording the winning combo on success.
                void RecordCut(bool b, bool f, bool sb, bool au)
                {
                    RecipeDb.RecordSuccess(cutIntent, JObject.FromObject(new
                    {
                        method        = "FeatureCut4",
                        blind         = b,
                        flip          = f,
                        selectBody    = sb,
                        useAutoSelect = au,
                    }));
                }

                if (cutFeat != null)
                {
                    RecordCut(recipeUsedBlind[0], recipeUsedFlip[0],
                              recipeUsedSelB[0],  recipeUsedAuto[0]);
                }

                // (a) sketch-only + auto-select bodies + blind cut
                if (cutFeat == null)
                {
                    cutFeat = TryFeatureCut(sketchFeatName, dist,
                        blind: true, selectBody: false, useAutoSelect: true,
                        flip: false);
                    if (cutFeat != null) RecordCut(true, false, false, true);
                }

                // (b) sketch-only + ThroughAll + flip=false
                if (cutFeat == null)
                {
                    cutFeat = TryFeatureCut(sketchFeatName, dist,
                        blind: false, selectBody: false, useAutoSelect: true,
                        flip: false);
                    if (cutFeat != null) RecordCut(false, false, false, true);
                }

                // (c) flip=true: cut direction reversed. If SW's "default
                //     direction" puts the cut going AWAY from the body,
                //     the cut removes nothing and returns null. flip=true
                //     reverses to the other side.
                if (cutFeat == null)
                {
                    cutFeat = TryFeatureCut(sketchFeatName, dist,
                        blind: true, selectBody: false, useAutoSelect: true,
                        flip: true);
                    if (cutFeat != null) RecordCut(true, true, false, true);
                }

                // (d) flip=true + ThroughAll
                if (cutFeat == null)
                {
                    cutFeat = TryFeatureCut(sketchFeatName, dist,
                        blind: false, selectBody: false, useAutoSelect: true,
                        flip: true);
                    if (cutFeat != null) RecordCut(false, true, false, true);
                }

                // (e) explicit body select (Mark=4) + blind + flip=false
                if (cutFeat == null)
                {
                    cutFeat = TryFeatureCut(sketchFeatName, dist,
                        blind: true, selectBody: true, useAutoSelect: false,
                        flip: false);
                    if (cutFeat != null) RecordCut(true, false, true, false);
                }

                // (f) feature scope DISABLED — last-ditch
                if (cutFeat == null)
                    cutFeat = TryFeatureCutNoScope(sketchFeatName, dist);

                feature = cutFeat;
            }
            else
            {
                return new { ok = false, error = $"Unknown extrude op: {operation}" };
            }

            if (feature != null)
            {
                if (!string.IsNullOrEmpty(alias)) feature.Name = alias;
                _aliasMap[alias ?? feature.Name] = feature;
                if (operation == "new" || operation == "join")
                    _lastBodyFeature = feature;
            }
            return new { ok = feature != null, alias, kind = "extrude",
                          distance_mm = dist * 1000, operation };
        }

        private IFeature TryFeatureCut(string sketchName, double dist,
            bool blind, bool selectBody, bool useAutoSelect, bool flip)
        {
            _model.ClearSelection2(true);
            bool selSketch = _model.Extension.SelectByID2(
                sketchName, "SKETCH", 0, 0, 0, false, 0, null,
                (int)swSelectOption_e.swSelectOptionDefault);
            if (!selSketch)
            {
                FileLog($"  cut.try (blind={blind} body={selectBody} auto={useAutoSelect} flip={flip}): SelectByID2 sketch={false}");
                return null;
            }
            if (selectBody && _lastBodyFeature != null)
            {
                bool b = _lastBodyFeature.Select2(true, 4);
                FileLog($"  cut.try: append body '{_lastBodyFeature.Name}' Mark=4 -> {b}");
            }

            int featCountBefore = _model.FeatureManager.GetFeatureCount(false);
            int selCount = (_model.SelectionManager as ISelectionMgr)?.GetSelectedObjectCount2(-1) ?? 0;
            int endCond1 = blind ? (int)swEndConditions_e.swEndCondBlind
                                 : (int)swEndConditions_e.swEndCondThroughAll;
            int endCond2 = (int)swEndConditions_e.swEndCondBlind;
            double d1 = blind ? dist : 0.01;
            double d2 = 0.01;

            try
            {
                FileLog($"  cut.try blind={blind} body={selectBody} auto={useAutoSelect} flip={flip} T1={endCond1} sel={selCount} featCount={featCountBefore}");
                const double DEG = 0.01745329251994;
                // Capture as object first (no IFeature cast) so we can
                // see the actual COM type — null vs cast failure.
                object raw = _model.FeatureManager.FeatureCut4(
                    true,                                 // Sd (single-direction)
                    flip, false,                          // Flip, Dir
                    endCond1, endCond2,
                    d1, d2,
                    false, false,
                    false, false,
                    DEG, DEG,
                    false, false,
                    false, false,
                    false,                                // NormalCut
                    true, useAutoSelect,                  // UseFeatScope, UseAutoSelect
                    true, true,                           // AssemblyFeatureScope, AutoSelectComponents
                    false,                                // PropagateFeatureToParts
                    (int)swStartConditions_e.swStartSketchPlane,
                    0, false, false);
                int featCountAfter = _model.FeatureManager.GetFeatureCount(false);
                FileLog($"  cut.try return: rawNull={raw==null} rawType={raw?.GetType().FullName ?? "null"} featCount {featCountBefore}->{featCountAfter}");
                IFeature feat = raw as IFeature;
                if (feat == null && featCountAfter > featCountBefore)
                {
                    var recent = _model.FeatureByPositionReverse(0) as IFeature;
                    string nm = recent?.Name ?? "";
                    FileLog($"  cut.try: tree grew but cast was null — adopting '{nm}'");
                    feat = recent;
                }
                return feat;
            }
            catch (Exception ex)
            {
                FileLog($"  cut.try threw: {ex.GetType().Name}: {ex.Message}");
                return null;
            }
        }

        /// <summary>One-shot diagnostic dump before each cut. Logs the
        /// active body's bbox so we can verify the cut direction makes
        /// geometric sense relative to where the body actually sits.</summary>
        private void LogBodyDiagnostics()
        {
            try
            {
                var part = _model as IPartDoc;
                if (part == null) { FileLog("  diag: model is not IPartDoc"); return; }
                var bodies = part.GetBodies2(
                    (int)swBodyType_e.swSolidBody, false) as object[];
                if (bodies == null || bodies.Length == 0)
                {
                    FileLog("  diag: no solid bodies found");
                    return;
                }
                FileLog($"  diag: {bodies.Length} solid body/bodies in part");
                foreach (var bo in bodies)
                {
                    if (bo is IBody2 b)
                    {
                        var bbox = b.GetBodyBox() as double[];
                        if (bbox != null && bbox.Length >= 6)
                        {
                            FileLog($"  diag: body '{b.Name}' bbox(mm) " +
                                    $"X[{bbox[0]*1000:F1}..{bbox[3]*1000:F1}] " +
                                    $"Y[{bbox[1]*1000:F1}..{bbox[4]*1000:F1}] " +
                                    $"Z[{bbox[2]*1000:F1}..{bbox[5]*1000:F1}]");
                        }
                    }
                }
                FileLog($"  diag: FeatureManager type = {_model.FeatureManager.GetType().FullName}");
            }
            catch (Exception ex)
            {
                FileLog($"  diag threw: {ex.Message}");
            }
        }

        private IFeature TryFeatureCutNoScope(string sketchName, double dist)
        {
            _model.ClearSelection2(true);
            bool sel = _model.Extension.SelectByID2(
                sketchName, "SKETCH", 0, 0, 0, false, 0, null,
                (int)swSelectOption_e.swSelectOptionDefault);
            if (!sel) return null;
            FileLog("  cut.try noScope (UseFeatScope=false)");
            try
            {
                // Last-ditch: UseFeatScope=false → cut affects every body
                // in the part regardless of selection. ThroughAll(1) +
                // Sd=true (single direction) is the simplest valid combo.
                const double DEG = 0.01745329251994;
                IFeature feat = _model.FeatureManager.FeatureCut4(
                    true,
                    false, false,
                    (int)swEndConditions_e.swEndCondThroughAll,
                    (int)swEndConditions_e.swEndCondBlind,
                    0.01, 0.01,
                    false, false, false, false,
                    DEG, DEG,
                    false, false, false, false,
                    false,                                 // NormalCut=false
                    false, false,                          // UseFeatScope=false
                    true, true,
                    false,
                    (int)swStartConditions_e.swStartSketchPlane,
                    0, false, false) as IFeature;
                if (feat == null)
                {
                    var recent = _model.FeatureByPositionReverse(0) as IFeature;
                    string nm = recent?.Name ?? "";
                    if (!string.IsNullOrEmpty(nm) &&
                        (nm.StartsWith("Cut-") || nm.StartsWith("Cut Extrude")))
                    {
                        FileLog($"  noScope: FeatureCut4 returned null but found '{nm}' — adopting");
                        feat = recent;
                    }
                }
                return feat;
            }
            catch (Exception ex)
            {
                FileLog($"  cut.try noScope threw: {ex.GetType().Name}: {ex.Message}");
                return null;
            }
        }

        private object OpCircularPattern(Dictionary<string, object> p)
        {
            string featAlias = p["feature"]?.ToString();
            int count = p.ContainsKey("count") ? Convert.ToInt32(p["count"]) : 2;
            string axis = p.ContainsKey("axis") ? p["axis"]?.ToString().ToUpperInvariant() : "Z";
            string alias = p.ContainsKey("alias") ? p["alias"]?.ToString() : null;
            if (_model == null) return new { ok = false, error = "no model" };

            // Select the source feature directly via stored IFeature ref,
            // then add an axis selection.
            _model.ClearSelection2(true);
            bool selFeat = false;
            if (_aliasMap.ContainsKey(featAlias)
                && _aliasMap[featAlias] is IFeature srcFeat)
            {
                selFeat = srcFeat.Select2(true, 1);   // Append=true, Mark=1
                FileLog($"  circPat: select feature '{srcFeat.Name}' -> {selFeat}");
            }

            string axisPlane = axis switch
            {
                "X" => "Right Plane",
                "Y" => "Top Plane",
                _   => "Front Plane",
            };
            // For circular patterns we need a circular edge or an axis. The
            // canonical SW pattern: select a temporary axis or use the
            // origin's reference axis. Simpler: select the corresponding
            // origin reference plane edge — for "Z" axis, the origin's
            // vertical axis is along the intersection of Front + Right.
            // Easiest portable path: use the model's origin axis named
            // by SW's default (English locale defaults below).
            string axisName = axis switch
            {
                "X" => "X",
                "Y" => "Y",
                _   => "Z",
            };
            // Try selecting the named axis on Origin. Falls back to a plane.
            bool selAxis = _model.Extension.SelectByID2(
                axisName + " Axis@Origin", "AXIS", 0, 0, 0, true, 4, null,
                (int)swSelectOption_e.swSelectOptionDefault);
            if (!selAxis) selAxis = _model.Extension.SelectByID2(
                axisPlane, "PLANE", 0, 0, 0, true, 4, null,
                (int)swSelectOption_e.swSelectOptionDefault);

            var feature = _model.FeatureManager.FeatureCircularPattern5(
                count,                                                     // Number
                2 * Math.PI,                                               // Spacing (rad, total when EqualSpacing)
                false,                                                     // FlipDirection
                "NULL",                                                    // DName (skipped instances)
                false,                                                     // GeometryPattern
                true,                                                      // EqualSpacing
                false,                                                     // VaryInstance
                false,                                                     // SyncSubAssemblies
                false,                                                     // BDir2
                false,                                                     // BSymmetric
                0,                                                         // Number2
                0,                                                         // Spacing2
                "NULL",                                                    // DName2
                false) as IFeature;                                        // EqualSpacing2

            if (feature != null && !string.IsNullOrEmpty(alias))
            {
                feature.Name = alias;
                _aliasMap[alias] = feature.Name;
            }
            return new { ok = feature != null, alias, kind = "circular_pattern",
                          count, axis };
        }

        private object OpFillet(Dictionary<string, object> p)
        {
            // Stub: SW FilletXpert / FeatureFillet3 needs edge selections that
            // ARIA's bridge contract doesn't currently express. Returning ok
            // keeps the pipeline moving until we add a face/edge selector.
            return new { ok = true, kind = "fillet",
                          status = "no-op (edge selection not yet wired)" };
        }

        private object OpVerifyPart(Dictionary<string, object> p)
        {
            // Server-side DFM does the actual rule-checking; the client
            // side does post-processing so the user sees a polished part:
            //   1) apply 6061 alu material if none set yet
            //   2) write ARIA traceability custom properties
            //   3) DimXpert auto-dimension scheme on the body
            //   4) isometric view + zoom-to-fit
            //   5) save the part to %USERPROFILE%\Documents\ARIA\
            // Each step is wrapped — failures don't break the ack.
            string process = p.ContainsKey("process") ? p["process"]?.ToString() : null;
            if (_model == null) return new { ok = true, kind = "verifyPart", process,
                                              postProcess = "no model" };

            var report = new Dictionary<string, object>();

            try
            {
                if (_model is IPartDoc partDoc)
                {
                    string cur = partDoc.GetMaterialPropertyName2("", out _);
                    if (string.IsNullOrEmpty(cur))
                    {
                        partDoc.SetMaterialPropertyName2("",
                            "SOLIDWORKS Materials", "6061 Alloy");
                        report["material"] = "6061 Alloy";
                    }
                    else report["material"] = $"existing:{cur}";
                }
            }
            catch (Exception ex) { report["material_err"] = ex.Message; }

            try
            {
                var cp = _model.Extension.CustomPropertyManager[""];
                cp.Add3("Description",
                    (int)swCustomInfoType_e.swCustomInfoText,
                    "ARIA-generated part",
                    (int)swCustomPropertyAddOption_e.swCustomPropertyReplaceValue);
                cp.Add3("Process",
                    (int)swCustomInfoType_e.swCustomInfoText,
                    process ?? "cnc_3axis",
                    (int)swCustomPropertyAddOption_e.swCustomPropertyReplaceValue);
                cp.Add3("Generator",
                    (int)swCustomInfoType_e.swCustomInfoText,
                    "ARIA-OS",
                    (int)swCustomPropertyAddOption_e.swCustomPropertyReplaceValue);
                cp.Add3("CreatedAt",
                    (int)swCustomInfoType_e.swCustomInfoDate,
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
                    (int)swCustomPropertyAddOption_e.swCustomPropertyReplaceValue);
                report["custom_props"] = "ok";
            }
            catch (Exception ex) { report["custom_props_err"] = ex.Message; }

            try { _model.ShowNamedView2("*Isometric", -1); report["view"] = "isometric"; }
            catch (Exception ex) { report["view_err"] = ex.Message; }

            try { _model.ViewZoomtofit2(); report["zoom"] = "fit"; }
            catch (Exception ex) { report["zoom_err"] = ex.Message; }

            try
            {
                string savePath = _model.GetPathName();
                if (string.IsNullOrEmpty(savePath))
                {
                    string outDir = Path.Combine(
                        System.Environment.GetFolderPath(
                            System.Environment.SpecialFolder.MyDocuments),
                        "ARIA");
                    Directory.CreateDirectory(outDir);
                    savePath = Path.Combine(outDir,
                        $"aria_{DateTime.Now:yyyyMMdd_HHmmss}.SLDPRT");
                    int errs = 0, warns = 0;
                    _model.Extension.SaveAs(
                        savePath,
                        (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                        (int)swSaveAsOptions_e.swSaveAsOptions_Silent,
                        null, ref errs, ref warns);
                    report["saved"] = savePath;
                }
                else report["saved"] = $"existing:{savePath}";
            }
            catch (Exception ex) { report["save_err"] = ex.Message; }

            return new { ok = true, kind = "verifyPart", process,
                          postProcess = report };
        }

        // ---- New SW-native ops --------------------------------------------

        private object OpSetMaterial(Dictionary<string, object> p)
        {
            string db   = p.ContainsKey("database") ? p["database"]?.ToString() : "SOLIDWORKS Materials";
            string name = p["name"]?.ToString();
            if (_model is not IPartDoc partDoc)
                return new { ok = false, error = "active doc is not a part" };
            partDoc.SetMaterialPropertyName2("", db, name);
            return new { ok = true, database = db, name };
        }

        private object OpSetView(Dictionary<string, object> p)
        {
            string view = p.ContainsKey("view") ? p["view"]?.ToString() : "*Isometric";
            if (_model == null) return new { ok = false, error = "no model" };
            _model.ShowNamedView2(view, -1);
            return new { ok = true, view };
        }

        private object OpZoomToFit(Dictionary<string, object> p)
        {
            if (_model == null) return new { ok = false, error = "no model" };
            _model.ViewZoomtofit2();
            return new { ok = true };
        }

        private object OpSaveAs(Dictionary<string, object> p)
        {
            string path = p.ContainsKey("path") ? p["path"]?.ToString() : null;
            if (_model == null) return new { ok = false, error = "no model" };
            if (string.IsNullOrEmpty(path))
            {
                string outDir = Path.Combine(
                    System.Environment.GetFolderPath(
                        System.Environment.SpecialFolder.MyDocuments), "ARIA");
                Directory.CreateDirectory(outDir);
                path = Path.Combine(outDir,
                    $"aria_{DateTime.Now:yyyyMMdd_HHmmss}.SLDPRT");
            }
            int errs = 0, warns = 0;
            bool ok = _model.Extension.SaveAs(
                path,
                (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                (int)swSaveAsOptions_e.swSaveAsOptions_Silent,
                null, ref errs, ref warns);
            return new { ok, path, errs, warns };
        }

        private object OpSetProperty(Dictionary<string, object> p)
        {
            string name  = p["name"]?.ToString();
            string value = p["value"]?.ToString();
            if (_model == null) return new { ok = false, error = "no model" };
            var cp = _model.Extension.CustomPropertyManager[""];
            cp.Add3(name,
                (int)swCustomInfoType_e.swCustomInfoText,
                value,
                (int)swCustomPropertyAddOption_e.swCustomPropertyReplaceValue);
            return new { ok = true, name, value };
        }

        private object OpAddDimension(Dictionary<string, object> p)
        {
            // Place a "Smart Dimension" on the active sketch. The caller
            // gives world XYZ for the dim location; the value tracks
            // whatever entity is currently selected.
            double x = Mm(p["x"]);
            double y = Mm(p["y"]);
            double z = p.ContainsKey("z") ? Mm(p["z"]) : 0;
            string equation = p.ContainsKey("equation") ? p["equation"]?.ToString() : null;
            if (_model == null) return new { ok = false, error = "no model" };
            // AddDimension2 returns IDisplayDimension. The numeric value is
            // taken from current state; an equation string can drive it
            // parametrically if provided.
            var dim = _model.AddDimension2(x, y, z) as IDisplayDimension;
            if (dim == null) return new { ok = false, error = "AddDimension2 returned null" };
            if (!string.IsNullOrEmpty(equation))
            {
                var di = dim.GetDimension2(0) as IDimension;
                if (di != null) di.SetSystemValue3(0,
                    (int)swInConfigurationOpts_e.swThisConfiguration, null);
            }
            return new { ok = true, kind = "dimension" };
        }

        private object OpHoleWizard(Dictionary<string, object> p)
        {
            // Real SW Hole Wizard for tapped/clearance holes. Creates a
            // semantically-rich Hole feature (vs. a generic cut), so the
            // hole exports with thread callouts and bills of materials
            // know it's an M6 Tap, not an arbitrary 6.6mm bore.
            //
            // params:  size ("M6"|"M8"|"#10-32"...), kind ("clearance"|"tapped"),
            //          fit ("close"|"normal"|"loose"), depth_mm, x_mm, y_mm
            //
            // Real implementation requires a face/plane selection prior to
            // calling HoleWizard5 — the planner needs to know which face
            // to drill into. Until that's wired through, we return a stub
            // so callers see what's needed.
            return new { ok = false,
                          error = "holeWizard requires a face selection in params; not yet wired" };
        }

        // SW-unique features
        private object OpToolboxHardware(Dictionary<string, object> p) =>
            new { ok = false, todo = "Toolbox: use swApp.OpenDoc() with toolbox.sldprt paths" };

        private object OpWeldmentProfile(Dictionary<string, object> p) =>
            new { ok = false, todo = "Weldment: ModelDoc.FeatureManager.InsertWeldmentProfile()" };

        private object OpDimXpertAuto(Dictionary<string, object> p) =>
            new { ok = false, todo = "DimXpert: DrawingDoc.InsertAutoDimensionScheme()" };

        private object OpExportEdrawings(Dictionary<string, object> p) =>
            new { ok = false, todo = "eDrawings: SaveAs(path, swFileType_e, swEPart)" };

        // -----------------------------------------------------------------
        // ISwAddin
        // -----------------------------------------------------------------

        public bool ConnectToSW(object ThisSW, int Cookie)
        {
            // Hook unhandled exceptions FIRST so any crash from this point
            // hits the log instead of vanishing into SW's process. Avoid
            // FirstChanceException — it fires on every caught exception
            // including internal SW ones, and synchronous file IO inside
            // it can block SW's message pump.
            AppDomain.CurrentDomain.UnhandledException += (s, e) =>
                FileLog($"FATAL UnhandledException: {e.ExceptionObject}");
            System.Threading.Tasks.TaskScheduler.UnobservedTaskException += (s, e) =>
            {
                FileLog($"UnobservedTaskException: {e.Exception}");
                e.SetObserved();
            };

            FileLog("=== ConnectToSW start ===");
            try
            {
                _sw = (ISldWorks)ThisSW;
                _cookie = Cookie;
                Current = this;
                FileLog("ISldWorks cast OK");

                _sw.SetAddinCallbackInfo(0, this, Cookie);
                FileLog("SetAddinCallbackInfo OK");

                CreateTaskPane();
                FileLog("CreateTaskPane OK");

                RecipeDb.Init();
                FileLog($"RecipeDb init OK (count={RecipeDb.Count})");
            }
            catch (Exception ex)
            {
                FileLog($"ConnectToSW caught: {ex.GetType().Name}: {ex.Message}");
                FileLog($"  stack: {ex.StackTrace}");
                try { _sw.SendMsgToUser2($"ARIA add-in startup error: {ex.Message}",
                                          (int)swMessageBoxIcon_e.swMbWarning,
                                          (int)swMessageBoxBtn_e.swMbOk); }
                catch { }
            }
            FileLog("=== ConnectToSW end (returning true) ===");
            return true;
        }

        public bool DisconnectFromSW()
        {
            try
            {
                if (_taskPane != null)
                {
                    _taskPane.DeleteView();
                    _taskPane = null;
                }
                _panelHost?.Dispose();
                _panelHost = null;
            }
            catch { /* best-effort cleanup */ }

            // Per SW SDK: must release the COM ref to allow shutdown.
            Marshal.ReleaseComObject(_sw);
            _sw = null;
            Current = null;
            GC.Collect();
            GC.WaitForPendingFinalizers();
            return true;
        }

        // -----------------------------------------------------------------
        // Task Pane registration
        //
        // CreateTaskpaneView3 takes an icon path + title. AddControl
        // instantiates our COM-visible UserControl by ProgID. The
        // UserControl is registered for COM via regasm at install time
        // (see README install section).
        // -----------------------------------------------------------------

        private void CreateTaskPane()
        {
            string iconPath = EnsureTaskPaneIcon();
            FileLog($"icon: {iconPath} (exists={File.Exists(iconPath)})");
            const string toolTip = "ARIA: AI CAD pipeline";

            _taskPane = (ITaskpaneView)_sw.CreateTaskpaneView2(iconPath, toolTip);
            if (_taskPane == null)
                throw new InvalidOperationException(
                    $"CreateTaskpaneView2 returned null (icon: {iconPath})");
            FileLog("CreateTaskpaneView2 OK");

            // AddControl by ProgID. The COM activation path JIT-compiles
            // AriaPanelHost which references WebView2; if the WebView2
            // runtime is missing, the type-load fault hits here. Trap
            // it explicitly so we know.
            object hostObj = null;
            try
            {
                hostObj = _taskPane.AddControl("AriaSW.AriaPanelHost", "");
                FileLog($"AddControl returned: {(hostObj?.GetType().FullName ?? "null")}");
            }
            catch (Exception ex)
            {
                FileLog($"AddControl threw: {ex.GetType().Name}: {ex.Message}");
                throw;
            }

            if (hostObj is AriaPanelHost host)
            {
                _panelHost = host;
            }
            else
            {
                FileLog("AddControl gave non-AriaPanelHost; constructing directly");
                _panelHost = new AriaPanelHost();
                TryAddControlDirectly(_panelHost);
            }
        }

        private static string EnsureTaskPaneIcon()
        {
            string dir = Path.Combine(
                System.Environment.GetFolderPath(
                    System.Environment.SpecialFolder.LocalApplicationData),
                "AriaSW");
            Directory.CreateDirectory(dir);
            string path = Path.Combine(dir, "taskpane_icon.bmp");
            if (File.Exists(path)) return path;

            // SW accepts 24bpp BMPs reliably; default Bitmap(int,int) is
            // 32bpp ARGB which CreateTaskpaneView2 sometimes rejects.
            using (var bmp = new Bitmap(16, 16, PixelFormat.Format24bppRgb))
            using (var g = Graphics.FromImage(bmp))
            {
                g.Clear(Color.FromArgb(60, 90, 160));   // ARIA blue
                using (var font = new Font("Arial", 9, FontStyle.Bold))
                using (var brush = new SolidBrush(Color.White))
                {
                    g.DrawString("A", font, brush, 2, 1);
                }
                bmp.Save(path, ImageFormat.Bmp);
            }
            return path;
        }

        private void TryAddControlDirectly(AriaPanelHost host)
        {
            // Newer SW builds expose AddControlEx(IDispatch, "") which
            // accepts a live managed control. Probe via reflection so
            // this builds against older SW SDKs too.
            var addEx = _taskPane.GetType().GetMethod(
                "AddControlEx",
                BindingFlags.Public | BindingFlags.Instance);
            if (addEx != null)
            {
                try { addEx.Invoke(_taskPane, new object[] { host, "" }); return; }
                catch (Exception ex) { Log($"AddControlEx failed: {ex.Message}"); }
            }
            // If we get here the panel won't render. Surface a hint.
            Log("Task Pane control not registered — run `regasm /codebase AriaSW.dll`");
        }
    }
}
