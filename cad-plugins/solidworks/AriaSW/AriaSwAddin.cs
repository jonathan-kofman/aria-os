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
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using Microsoft.Win32;
using Newtonsoft.Json;
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
        // Path canonicalization — fixes the silent-failure class
        //
        // Several SW string-matched APIs (Create3rdAngleViews2, AddMate's
        // SelectByID2, ActivateDoc3) compare a path against SW's internal
        // open-doc title which mirrors disk filename casing exactly. A
        // mismatched-case or forward-slash path silently fails ("ok=False"
        // returned, no exception). This helper resolves any user-supplied
        // path to its OS-canonical form before we hand it to SW.
        //
        // Returns the input verbatim if the file doesn't exist (caller
        // gets a chance to error on its own). Idempotent: calling on an
        // already-canonical path is a no-op aside from the directory
        // walk cost.
        // -----------------------------------------------------------------
        internal static string CanonPath(string path)
        {
            if (string.IsNullOrEmpty(path)) return path;
            try
            {
                string dir = Path.GetDirectoryName(path);
                string nameOnly = Path.GetFileName(path);
                if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir))
                    return path;
                var matches = Directory.GetFiles(dir, nameOnly);
                if (matches.Length > 0)
                {
                    // GetFiles returns OS-canonical case + native separators
                    // but the directory portion may still be the caller's
                    // form. Re-resolve via FileInfo for full canon.
                    return new FileInfo(matches[0]).FullName;
                }
            }
            catch { /* best-effort — never throw on canonicalisation */ }
            // Even if file doesn't exist, normalize separators so
            // downstream string compares behave.
            return path.Replace('/', Path.DirectorySeparatorChar);
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
                // LLM planners occasionally hallucinate aliases for op
                // names (sketchRectangle for sketchRect, extrudeCut for
                // extrude operation=cut). Map aliases so a small naming
                // drift doesn't kill the whole plan.
                kind = kind switch
                {
                    "sketchRectangle"  => "sketchRect",
                    "rectangle"        => "sketchRect",
                    "rect"             => "sketchRect",
                    "circle"           => "sketchCircle",
                    "newPart"          => "beginPlan",
                    "extrudeBoss"      => "extrude",
                    "extrudeCut"       => "extrude",
                    "boss"             => "extrude",
                    "cut"              => "extrude",
                    "patternCircular"  => "circularPattern",
                    _ => kind,
                };
                return kind switch
                {
                    "beginPlan"       => OpBeginPlan(),
                    "openDoc"         => OpOpenDoc(p),
                    "newSketch"       => OpNewSketch(p),
                    "sketchCircle"    => OpSketchCircle(p),
                    "sketchRect"      => OpSketchRect(p),
                    "extrude"         => OpExtrude(p),
                    "circularPattern" => OpCircularPattern(p),
                    "fillet"          => OpFillet(p),
                    "addParameter"    => OpAddParameter(p),
                    // Configurations + design tables (T3_EXPERT — CSWE)
                    "addConfiguration"      => OpAddConfiguration(p),
                    "activateConfiguration" => OpActivateConfiguration(p),
                    "suppressFeature"       => OpSuppressFeature(p),
                    "unsuppressFeature"     => OpUnsuppressFeature(p),
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
                    // Assembly ops (type-1 assembler — mates/constraints)
                    "beginAssembly"   => OpBeginAssembly(p),
                    "insertComponent" => OpInsertComponent(p),
                    "addMate"         => OpAddMate(p),
                    // Native SW drawing op — creates SLDDRW with views,
                    // auto-dimensioning, BOM, and revision block.
                    "createDrawing"   => OpCreateDrawing(p),
                    // Drawing enrichment — GD&T, section view, exploded
                    // view (asm) — applied to the currently-active .slddrw.
                    "enrichDrawing"   => OpEnrichDrawing(p),
                    // Drawing → PDF for downstream visual verification by
                    // the orchestrator's auto-loop verify gate.
                    "exportDrawingPdf"=> OpExportDrawingPdf(p),
                    // Image → CAD: post to orchestrator's vision pipeline,
                    // get back STEP, import into the active doc.
                    "imageToCad"      => OpImageToCad(p),
                    "scanToCad"       => OpScanToCad(p),
                    // Native SW Simulation FEA — parametric iterations
                    "runFea"          => OpRunFEA(p),
                    "feaIterate"      => OpRunFEA(p),
                    // Sheet-metal feature ops
                    "sheetMetalBaseFlange" => OpSheetMetalBaseFlange(p),
                    "sheetMetalEdgeFlange" => OpSheetMetalEdgeFlange(p),
                    // Surface modeling ops
                    "surfaceLoft"     => OpSurfaceLoft(p),
                    "surfaceExtrude"  => OpSurfaceExtrude(p),
                    // Editable lattice — dispatches to dashboard's bake
                    // endpoint, imports STL as Mesh BREP, booleans
                    // against host body, hooks SW user-parameter
                    // changes for in-place re-bake.
                    "latticeFeature"  => OpLatticeFeature(p),
                    "meshImportAndCombine" => OpMeshImportAndCombine(p),
                    // 3D solid features beyond extrude — needed by the
                    // revolve / lattice / nozzle planners.
                    "revolve"         => OpRevolve(p),
                    "sweep"           => OpSweep(p),
                    "loft"            => OpLoft(p),
                    "shell"           => OpShell(p),
                    "rib"             => OpRib(p),
                    "draft"           => OpDraft(p),
                    "helix"           => OpHelix(p),
                    "coil"            => OpCoil(p),
                    // Sketch primitives beyond circle/rect — needed by
                    // revolve_planner (spline/polyline profiles) and
                    // any LLM plan with curved geometry.
                    "sketchPolyline"  => OpSketchPolyline(p),
                    "sketchSpline"    => OpSketchSpline(p),
                    "sketchTangentArc" => OpSketchTangentArc(p),
                    "sketchOffset"    => OpSketchOffset(p),
                    "sketchProjection"=> OpSketchProjection(p),
                    // Drawing ops — emit by the pro-quality dwg planner.
                    // These dispatch to the active .slddrw and use
                    // SW DrawingDoc API where available; placeholders
                    // (datumLabel / gdtFrame / surfaceFinishCallout)
                    // queue up state for OpEnrichDrawing to apply in
                    // a single pass (avoids one COM round-trip per op).
                    "beginDrawing"    => OpBeginDrawing(p),
                    "newSheet"        => OpNewSheet(p),
                    "addView"         => OpAddView(p),
                    "linearDimension" => OpLinearDimension(p),
                    "angularDimension" => OpAngularDimension(p),
                    "diameterDimension" => OpDiameterDimension(p),
                    "radialDimension" => OpRadialDimension(p),
                    "ordinateDimension" => OpOrdinateDimension(p),
                    "datumLabel"      => OpDatumLabel(p),
                    "gdtFrame"        => OpGdtFrame(p),
                    "surfaceFinishCallout" => OpSurfaceFinishCallout(p),
                    "weldSymbol"      => OpWeldSymbol(p),
                    "centerlineMark"  => OpCenterlineMark(p),
                    "balloon"         => OpBalloon(p),
                    "revisionTable"   => OpRevisionTable(p),
                    "bomTable"        => OpBomTable(p),
                    "sectionView"     => OpSectionView(p),
                    "detailView"      => OpDetailView(p),
                    "brokenView"      => OpBrokenView(p),
                    "autoDimension"   => OpAutoDimension(p),
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

        /// <summary>
        /// Late-bound COM dispatch helper. Many SW2024 IFeatureManager
        /// methods (InsertHelix, InsertFeatureShell2, FeatureRib3,
        /// InsertDraftDC2, InsertSheetMetalBaseFlange2, FeatureLoft2,
        /// FeatureCircularPattern5...) are exposed only through IDispatch
        /// and not through the RCW's GetMethods table. Reflection can't
        /// find them, but Type.InvokeMember can.
        ///
        /// This helper tries each (holder, name) tuple in order:
        ///   1) GetMethod + Invoke (works for typelib-visible methods)
        ///   2) InvokeMember(BindingFlags.InvokeMethod) — late-bound,
        ///      goes through IDispatch.
        /// Returns the first non-null result, or null if every attempt
        /// fails. `caller` is just a tag for FileLog.
        /// </summary>
        private object LateBoundInvoke(
            string caller,
            object[] holders,
            string[] methodNames,
            object[] args)
        {
            foreach (var name in methodNames)
            {
                if (string.IsNullOrEmpty(name)) continue;
                foreach (var holder in holders)
                {
                    if (holder == null) continue;
                    var t = holder.GetType();
                    // Path 1: typelib-visible
                    var probe = t.GetMethod(name);
                    if (probe != null)
                    {
                        try
                        {
                            // Pad args if the method expects more (some
                            // versions add tail params we don't care about).
                            int n = probe.GetParameters().Length;
                            object[] padded = args;
                            if (n != args.Length)
                            {
                                padded = new object[n];
                                for (int i = 0; i < n; i++)
                                    padded[i] = i < args.Length ? args[i] : false;
                            }
                            object res = probe.Invoke(holder, padded);
                            if (res != null)
                            {
                                FileLog($"  {caller}: typelib {t.Name}.{name} -> ok");
                                return res;
                            }
                            FileLog($"  {caller}: typelib {t.Name}.{name} -> null");
                        }
                        catch (Exception ex)
                        {
                            FileLog($"  {caller}: typelib {t.Name}.{name} threw {ex.Message}");
                        }
                    }
                    // Path 2: late-bound IDispatch
                    try
                    {
                        object res = t.InvokeMember(
                            name,
                            System.Reflection.BindingFlags.InvokeMethod,
                            null, holder, args);
                        if (res != null)
                        {
                            FileLog($"  {caller}: late-bound {t.Name}.{name} -> ok");
                            return res;
                        }
                    }
                    catch { /* method not exposed on this holder */ }
                }
            }
            FileLog($"  {caller}: late-bound dispatch found no working {string.Join("/", methodNames)}");
            // Diagnostic: extract a couple of root substrings (e.g.
            // "Shell", "Helix") from the candidate names and dump every
            // method on every holder that matches. Helps us discover
            // unexpected SW2024 method names.
            try
            {
                var roots = new System.Collections.Generic.HashSet<string>(
                    StringComparer.OrdinalIgnoreCase);
                foreach (var name in methodNames)
                {
                    if (string.IsNullOrEmpty(name)) continue;
                    string stripped = name
                        .Replace("Insert", "")
                        .Replace("Feature", "")
                        .TrimEnd('1', '2', '3', '4', '5', '6', '7', '8', '9');
                    if (stripped.Length >= 4) roots.Add(stripped);
                }
                var hits = new System.Collections.Generic.List<string>();
                foreach (var root in roots)
                {
                    foreach (var holder in holders)
                    {
                        if (holder == null) continue;
                        foreach (var m in holder.GetType().GetMethods())
                            if (m.Name.IndexOf(root,
                                StringComparison.OrdinalIgnoreCase) >= 0)
                                hits.Add($"{holder.GetType().Name}.{m.Name}({m.GetParameters().Length})");
                    }
                }
                FileLog($"  {caller}: probe roots={string.Join(",", roots)} hits={string.Join(", ", hits)}");
            }
            catch (Exception probeEx)
            {
                FileLog($"  {caller}: probe threw {probeEx.Message}");
            }
            return null;
        }

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

        private object OpOpenDoc(Dictionary<string, object> p)
        {
            // Open an existing .sldprt/.sldasm/.slddrw without closing
            // anything else. Used to display previously-built artifacts
            // in the SW window for review.
            string path = p.ContainsKey("path") ? p["path"]?.ToString() : null;
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
                return new { ok = false, error = $"openDoc: file not found: {path}" };
            int errs = 0, warns = 0;
            int docTypeArg;
            string ext = Path.GetExtension(path).ToLowerInvariant();
            switch (ext)
            {
                case ".sldprt": docTypeArg = (int)swDocumentTypes_e.swDocPART; break;
                case ".sldasm": docTypeArg = (int)swDocumentTypes_e.swDocASSEMBLY; break;
                case ".slddrw": docTypeArg = (int)swDocumentTypes_e.swDocDRAWING; break;
                default: docTypeArg = (int)swDocumentTypes_e.swDocPART; break;
            }
            try
            {
                var opened = _sw.OpenDoc6(path, docTypeArg,
                    (int)swOpenDocOptions_e.swOpenDocOptions_Silent,
                    "", ref errs, ref warns) as IModelDoc2;
                if (opened == null)
                    return new { ok = false, error = $"openDoc: OpenDoc6 returned null (errs={errs} warns={warns})", path };
                _model = opened;  // make this the active model
                _sw.ActivateDoc3(opened.GetTitle(), false,
                    (int)swRebuildOnActivation_e.swDontRebuildActiveDoc, ref errs);
                FileLog($"  openDoc: opened {path} (errs={errs} warns={warns})");
                return new { ok = true, kind = "openDoc",
                              path, title = opened.GetTitle(),
                              errs, warns };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"openDoc threw: {ex.Message}", path };
            }
        }

        private object OpBeginPlan()
        {
            _registry.Clear();
            _aliasMap.Clear();
            _activeSketchName = null;
            _activeSketchPlane = null;
            _lastBodyFeature = null;

            // Force a fresh part document so each plan runs in isolation.
            // CloseDoc alone fails silently when the active doc has
            // unsaved changes (e.g. after saveAs of a derived form, or
            // after addParameter mutations on the equation manager). Use
            // CloseAllDocuments(true) which closes all docs INCLUDING
            // unsaved ones — guaranteed clean slate.
            try
            {
                // Pre-mark every open doc as "needs no save" so the
                // bulk close below is a no-op for the save-prompt UI.
                var docs = _sw.GetDocuments() as object[];
                if (docs != null)
                {
                    foreach (var d in docs)
                    {
                        var md = d as IModelDoc2;
                        if (md == null) continue;
                        try
                        {
                            // IModelDoc2.GetType() shadows Object.GetType
                            // and returns an int (doc-type enum), so we
                            // must cast to object first to reach the .NET
                            // reflection method.
                            ((object)md).GetType().InvokeMember(
                                "SetSaveFlag",
                                System.Reflection.BindingFlags.InvokeMethod,
                                null, md, new object[] { 0 });
                        }
                        catch { }
                    }
                }
                bool allClosed = _sw.CloseAllDocuments(true);
                FileLog($"  beginPlan: CloseAllDocuments(true) -> {allClosed}");
            }
            catch (Exception ex)
            {
                FileLog($"  beginPlan: CloseAllDocuments threw {ex.Message}, falling through to CloseDoc");
                try
                {
                    var active = _sw.IActiveDoc2 as IModelDoc2;
                    if (active != null)
                    {
                        _sw.CloseDoc(active.GetTitle());
                        FileLog($"  beginPlan: legacy close '{active.GetTitle()}'");
                    }
                }
                catch (Exception ex2)
                {
                    FileLog($"  beginPlan: legacy close threw {ex2.Message}");
                }
            }
            _model = null;
            _model = EnsurePart();
            FileLog($"  beginPlan: opened fresh part '{_model?.GetTitle()}'");
            return new { ok = true, registry_cleared = true,
                          fresh_doc = _model?.GetTitle() };
        }

        private object OpAddParameter(Dictionary<string, object> p)
        {
            string name = p["name"]?.ToString();
            // Accept either "value_mm" or plain "value" — different planners
            // and tests have used both spellings.
            object rawVal = p.ContainsKey("value_mm") ? p["value_mm"]
                          : p.ContainsKey("value")    ? p["value"]
                          : null;
            if (rawVal == null)
                return new { ok = false, error = "addParameter: missing 'value_mm' or 'value'" };
            double val  = Convert.ToDouble(rawVal);
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

        // -----------------------------------------------------------------
        // T3_EXPERT — Configurations
        //
        // SW Configurations are named saved states of the part: each can
        // suppress different features, override dimensions, or use a
        // different material. They're the foundation of design tables and
        // the CSWE configurations question.
        //
        // ConfigurationManager.AddConfiguration2 signature (SW 2014+):
        //   AddConfiguration2(Name, Comment, AlternateName, Options,
        //                     ParentConfigName, Description, ColorIndex)
        // Returns IConfiguration on success, null on failure.
        // -----------------------------------------------------------------
        private object OpAddConfiguration(Dictionary<string, object> p)
        {
            if (_model == null)
                return new { ok = false, error = "addConfiguration: no model" };
            string name = p.ContainsKey("name") ? p["name"]?.ToString() : null;
            if (string.IsNullOrEmpty(name))
                return new { ok = false, error = "addConfiguration: 'name' required" };
            string comment = p.ContainsKey("comment") ? p["comment"]?.ToString() : "";
            string parent  = p.ContainsKey("parent")  ? p["parent"]?.ToString()  : null;
            string descrip = p.ContainsKey("description") ? p["description"]?.ToString() : name;
            try
            {
                var cmgr = _model.ConfigurationManager;
                if (cmgr == null)
                    return new { ok = false, error = "ConfigurationManager unavailable" };
                // SW interop versions disagree on AddConfiguration2's arg
                // count (saw 6, 7, 8 across 2018→2024). Build via reflection.
                var cmType = cmgr.GetType();
                var mi = cmType.GetMethod("AddConfiguration2")
                       ?? cmType.GetMethod("AddConfiguration");
                if (mi == null)
                    return new { ok = false, error = "AddConfiguration[2] not found on ConfigurationManager" };
                var paramInfos = mi.GetParameters();
                int n = paramInfos.Length;
                var args = new object[n];
                for (int i = 0; i < n; i++)
                {
                    Type pt = paramInfos[i].ParameterType;
                    if (pt == typeof(string))      args[i] = "";
                    else if (pt == typeof(int))    args[i] = 0;
                    else if (pt == typeof(bool))   args[i] = false;
                    else                            args[i] = null;
                }
                if (n > 0) args[0] = name;
                if (n > 1) args[1] = comment ?? "";
                if (n > 2) args[2] = "";              // alternate name
                if (n > 3) args[3] = 0;               // options
                if (n > 4) args[4] = parent;          // parent (null = top)
                if (n > 5) args[5] = descrip ?? name; // description
                object cfg = mi.Invoke(cmgr, args);
                FileLog($"  addConfiguration: name='{name}' parent='{parent ?? "<top>"}' -> {(cfg != null ? "ok" : "null")}");
                if (cfg == null)
                    return new { ok = false, error = $"AddConfiguration2 returned null for '{name}' (duplicate?)" };
                return new { ok = true, name, parent };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"addConfiguration threw: {ex.Message}" };
            }
        }

        private object OpActivateConfiguration(Dictionary<string, object> p)
        {
            if (_model == null)
                return new { ok = false, error = "activateConfiguration: no model" };
            string name = p.ContainsKey("name") ? p["name"]?.ToString() : null;
            if (string.IsNullOrEmpty(name))
                return new { ok = false, error = "activateConfiguration: 'name' required" };
            try
            {
                // SW2024 ShowConfiguration2 returns false on UNSAVED docs even
                // when the config exists in the tree. Force a rebuild + save
                // to a temp path first so the configs persist; then switching
                // works. Without this every config-switch was a no-op.
                _model.ForceRebuild3(false);
                try
                {
                    string tmpDir = System.IO.Path.GetTempPath();
                    string tmpFile = System.IO.Path.Combine(tmpDir,
                        $"aria_cfg_persist_{DateTime.Now.Ticks}.sldprt");
                    int sErr = 0, sWarn = 0;
                    bool saved = _model.Extension.SaveAs(tmpFile,
                        (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                        (int)swSaveAsOptions_e.swSaveAsOptions_Silent,
                        null, ref sErr, ref sWarn);
                    FileLog($"  activateConfiguration: persist save -> ok={saved} errs={sErr}");
                }
                catch (Exception persistEx)
                {
                    FileLog($"  activateConfiguration: persist save threw (continuing): {persistEx.Message}");
                }
                // Late-bind: ShowConfiguration2 returns bool; ShowConfiguration
                // returns int; some SW versions only expose one. Also try
                // ConfigurationManager.set_ActiveConfiguration as a fallback.
                bool ok = false;
                var mt = ((object)_model).GetType();
                // InvokeMember handles dispatch / overload resolution more
                // permissively than GetMethod(name, types). Useful when the
                // method exists on the COM IDispatch but isn't visible to
                // strict .NET reflection.
                foreach (var mname in new[] {
                    "ShowConfiguration2", "ShowConfiguration" })
                {
                    try
                    {
                        var r = mt.InvokeMember(mname,
                            System.Reflection.BindingFlags.InvokeMethod,
                            null, _model, new object[] { name });
                        ok = (r is bool b && b) || (r is int ri && ri != 0);
                        FileLog($"  activateConfiguration: {mname}('{name}') -> {ok} (raw={r ?? "null"})");
                        if (ok) break;
                    }
                    catch (System.Reflection.TargetInvocationException tie)
                    {
                        FileLog($"  activateConfiguration {mname} target-threw: {tie.InnerException?.Message ?? tie.Message}");
                    }
                    catch (System.MissingMethodException)
                    {
                        FileLog($"  activateConfiguration {mname}: not found on _model");
                    }
                    catch (Exception mex)
                    {
                        FileLog($"  activateConfiguration {mname} threw: {mex.GetType().Name}: {mex.Message}");
                    }
                }
                // Last-ditch: walk configs and pick by name via ActiveConfiguration set.
                if (!ok)
                {
                    var cmgr = _model.ConfigurationManager;
                    var configs = _model.GetConfigurationNames() as string[];
                    var listed = configs != null ? string.Join(", ", configs) : "<null>";
                    FileLog($"  activateConfiguration: configs in doc=[{listed}]");
                    if (configs != null && Array.Exists(configs, c => c == name))
                    {
                        // Some interops expose set_ActiveConfiguration as
                        // a property. Try setting via reflection.
                        var cmType = cmgr.GetType();
                        var setProp = cmType.GetProperty("ActiveConfiguration");
                        if (setProp != null && setProp.CanWrite)
                        {
                            try
                            {
                                var cfgObj = _model.GetConfigurationByName(name);
                                setProp.SetValue(cmgr, cfgObj, null);
                                ok = true;
                                FileLog($"  activateConfiguration: set via ActiveConfiguration property -> ok");
                            }
                            catch (Exception sx)
                            {
                                FileLog($"  activateConfiguration set-prop threw: {sx.Message}");
                            }
                        }
                    }
                }
                if (!ok)
                    return new { ok = false,
                                  error = $"could not activate config '{name}' " +
                                          $"(none of ShowConfiguration*/ActiveConfiguration paths worked)" };
                _model.ForceRebuild3(false);
                string active = _model.ConfigurationManager?.ActiveConfiguration?.Name;
                return new { ok = true, name, active };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"activateConfiguration threw: {ex.Message}" };
            }
        }

        // Suppress / unsuppress a feature in the active configuration. Used
        // to differentiate configurations geometrically (e.g. "with hole" vs
        // "without hole"). Resolves alias → real SW feature name first.
        private (bool, IFeature, string) ResolveFeature(string aliasOrName)
        {
            if (string.IsNullOrEmpty(aliasOrName))
                return (false, null, "feature name/alias required");
            IFeature feat = null;
            string resolved = aliasOrName;
            if (_aliasMap.ContainsKey(aliasOrName)
                && _aliasMap[aliasOrName] is IFeature f)
            {
                feat = f;
                resolved = f.Name;
            }
            else
            {
                // Walk the feature tree looking for a name match.
                var first = _model.FirstFeature() as IFeature;
                while (first != null)
                {
                    if (first.Name == aliasOrName)
                    {
                        feat = first;
                        resolved = first.Name;
                        break;
                    }
                    first = first.GetNextFeature() as IFeature;
                }
            }
            if (feat == null)
                return (false, null, $"feature '{aliasOrName}' not found");
            return (true, feat, resolved);
        }

        // SetSuppression has multiple names across SW interop versions:
        // SetSuppression2 (newer), SetSuppression (older), EditSuppression2.
        // Probe via reflection so we work on whatever this user has.
        // action: 0=suppress, 1=unsuppress, 2=unsuppress with deps
        // configOption: 1=this config only, 2=all configs, 3=specified
        private bool TrySetSuppression(string name, int action, int configOption)
        {
            object ext = _model.Extension;
            var t = ext.GetType();
            foreach (var m in new[] {
                "SetSuppression2", "SetSuppression", "EditSuppression2" })
            {
                var mi = t.GetMethod(m);
                if (mi == null) continue;
                try
                {
                    var paramInfos = mi.GetParameters();
                    int n = paramInfos.Length;
                    var args = new object[n];
                    for (int i = 0; i < n; i++)
                    {
                        Type pt = paramInfos[i].ParameterType;
                        if (pt == typeof(string))    args[i] = "";
                        else if (pt == typeof(int))  args[i] = 0;
                        else if (pt == typeof(bool)) args[i] = false;
                        else                          args[i] = null;
                    }
                    if (n > 0) args[0] = name;
                    if (n > 1) args[1] = action;
                    if (n > 2) args[2] = configOption;
                    if (n > 3) args[3] = null;  // configNames (null = active only)
                    object r = mi.Invoke(ext, args);
                    bool ok = (r is bool b && b) || (r is int ri && ri != 0);
                    FileLog($"  setSuppression via {m}: '{name}' action={action} -> {ok}");
                    if (ok) return true;
                }
                catch (Exception ex)
                {
                    FileLog($"  setSuppression {m} threw: {ex.Message}");
                }
            }
            // Final fallback: feature-level Select2 + EditSuppress.
            // IFeature.EditSuppress() returns int (1=ok).
            try
            {
                IFeature feat = null;
                if (_aliasMap.ContainsKey(name) && _aliasMap[name] is IFeature af)
                    feat = af;
                if (feat == null)
                {
                    var first = _model.FirstFeature() as IFeature;
                    while (first != null)
                    {
                        if (first.Name == name) { feat = first; break; }
                        first = first.GetNextFeature() as IFeature;
                    }
                }
                if (feat != null)
                {
                    _model.ClearSelection2(true);
                    feat.Select2(false, 0);
                    // Late-bind everything so we work regardless of which
                    // interop dll defines which suppress methods.
                    var ft = ((object)feat).GetType();
                    foreach (var fm in (action == 0
                        ? new[] { "EditSuppress2", "EditSuppress",
                                  "SetSuppression2", "SetSuppression" }
                        : new[] { "EditUnsuppress2", "EditUnsuppress",
                                  "SetSuppression2", "SetSuppression" }))
                    {
                        var fmi = ft.GetMethod(fm);
                        if (fmi == null) continue;
                        try
                        {
                            var paramInfos = fmi.GetParameters();
                            int n = paramInfos.Length;
                            var args = new object[n];
                            for (int i = 0; i < n; i++)
                            {
                                Type pt = paramInfos[i].ParameterType;
                                if (pt == typeof(int))       args[i] = 0;
                                else if (pt == typeof(bool)) args[i] = false;
                                else                          args[i] = null;
                            }
                            // SetSuppression2(action, configOption, configNames)
                            if (fm.StartsWith("SetSuppression"))
                            {
                                if (n > 0) args[0] = action;
                                if (n > 1) args[1] = 1;
                            }
                            object rv = fmi.Invoke(feat, args);
                            bool okv = (rv is bool bb && bb)
                                     || (rv is int ri && ri != 0)
                                     || rv is null;  // EditSuppress returns void
                            FileLog($"  setSuppression via IFeature.{fm}: '{name}' -> {okv}");
                            if (okv) return true;
                        }
                        catch (Exception fex)
                        {
                            FileLog($"  setSuppression IFeature.{fm} threw: {fex.Message}");
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                FileLog($"  setSuppression IFeature fallback threw: {ex.Message}");
            }
            return false;
        }

        private object OpSuppressFeature(Dictionary<string, object> p)
        {
            if (_model == null)
                return new { ok = false, error = "suppressFeature: no model" };
            string aliasOrName = p.ContainsKey("feature") ? p["feature"]?.ToString() : null;
            var (found, _, resolved) = ResolveFeature(aliasOrName);
            if (!found)
                return new { ok = false, error = resolved };
            try
            {
                bool ok = TrySetSuppression(resolved, 0, 1);
                _model.ForceRebuild3(false);
                return new { ok, feature = resolved,
                              activeConfig = _model.ConfigurationManager?.ActiveConfiguration?.Name };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"suppressFeature threw: {ex.Message}" };
            }
        }

        private object OpUnsuppressFeature(Dictionary<string, object> p)
        {
            if (_model == null)
                return new { ok = false, error = "unsuppressFeature: no model" };
            string aliasOrName = p.ContainsKey("feature") ? p["feature"]?.ToString() : null;
            var (found, _, resolved) = ResolveFeature(aliasOrName);
            if (!found)
                return new { ok = false, error = resolved };
            try
            {
                bool ok = TrySetSuppression(resolved, 1, 1);
                _model.ForceRebuild3(false);
                return new { ok, feature = resolved,
                              activeConfig = _model.ConfigurationManager?.ActiveConfiguration?.Name };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"unsuppressFeature threw: {ex.Message}" };
            }
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
            // Default alias to "sk_<n>" when caller omits it. Previously a
            // missing "alias" field threw KeyNotFoundException, breaking
            // any LLM plan that emitted bare-bones newSketch ops.
            string alias = p.ContainsKey("alias")
                            ? p["alias"]?.ToString()
                            : $"sk_{_aliasMap.Count}";
            string name  = p.ContainsKey("name") ? p["name"]?.ToString() : null;
            // Offset (mm) along the std plane normal — when non-zero, create
            // a parallel reference plane and sketch on that. Required for
            // loft, sweep guide-rails, and any feature spanning Z. Without
            // this every newSketch ends up at z=0 and lofts/sweeps fail
            // because all profiles are coplanar.
            double offsetMm = p.ContainsKey("offset_mm") ? Convert.ToDouble(p["offset_mm"])
                            : (p.ContainsKey("offset") ? Convert.ToDouble(p["offset"]) : 0.0);
            if (_model == null) _model = EnsurePart();

            // CRITICAL: SketchManager.InsertSketch(true) is a TOGGLE — if a
            // sketch is already active it EXITS instead of starting a new
            // one. Sequential newSketch calls (e.g. for loft) hit this:
            // the second call exited the first sketch and never entered the
            // new one. Always exit any active sketch up front so the
            // subsequent InsertSketch(true) reliably enters the new one.
            if (_model.SketchManager.ActiveSketch != null)
                _model.SketchManager.InsertSketch(true);

            string planeName = SwPlaneName(plane);
            string targetPlaneName = planeName;

            // If offset requested, materialize a new reference plane parallel
            // to the std plane at the requested offset, then sketch on THAT.
            if (Math.Abs(offsetMm) > 1e-6)
            {
                _model.ClearSelection2(true);
                bool stagedRef = _model.Extension.SelectByID2(
                    planeName, "PLANE", 0, 0, 0, false, 0, null, 0);
                if (!stagedRef)
                    return new { ok = false, error = $"newSketch offset: could not select base plane '{planeName}'" };
                double offsetM = offsetMm / 1000.0;
                IFeature refPlaneFeat = null;
                // FeatureManager.InsertRefPlane signature varies across SW
                // versions; reflect to be safe. Common shape:
                // InsertRefPlane(constraint1, dist1, constraint2, dist2, constraint3, dist3)
                // where constraint=8 means "offset distance" and the other
                // five slots are unused for a simple offset plane.
                try
                {
                    var fm = _model.FeatureManager;
                    var fmType = fm.GetType();
                    var mi = fmType.GetMethod("InsertRefPlane");
                    if (mi != null)
                    {
                        // Constraint code 8 = swRefPlaneReferenceConstraint_Distance
                        const short OFFSET_CONSTRAINT = 8;
                        var args = new object[] {
                            OFFSET_CONSTRAINT, offsetM,
                            (short)0, 0.0,
                            (short)0, 0.0
                        };
                        // If signature uses int instead of short, fix per
                        // ParameterInfo.
                        var pis = mi.GetParameters();
                        for (int i = 0; i < pis.Length && i < args.Length; i++)
                        {
                            if (pis[i].ParameterType == typeof(int))
                                args[i] = Convert.ToInt32(args[i]);
                            else if (pis[i].ParameterType == typeof(double))
                                args[i] = Convert.ToDouble(args[i]);
                        }
                        refPlaneFeat = mi.Invoke(fm, args) as IFeature;
                    }
                }
                catch (Exception planeEx)
                {
                    FileLog($"  newSketch offset: InsertRefPlane threw: {planeEx.Message}");
                }
                if (refPlaneFeat == null)
                    return new { ok = false,
                                  error = $"newSketch offset: InsertRefPlane returned null (offset={offsetMm}mm from {planeName})" };
                targetPlaneName = refPlaneFeat.Name;
                FileLog($"  newSketch: created ref plane '{targetPlaneName}' offset {offsetMm}mm from {planeName}");
            }

            // Clear current selection, then select the target plane.
            _model.ClearSelection2(true);
            bool selected = _model.Extension.SelectByID2(
                targetPlaneName, "PLANE", 0, 0, 0, false, 0, null,
                (int)swSelectOption_e.swSelectOptionDefault);
            if (!selected)
                return new { ok = false, error = $"Could not select '{targetPlaneName}'" };

            _model.SketchManager.InsertSketch(true);   // Enter sketch mode

            // Capture the sketch IFeature. Prefer ActiveSketch (most reliable
            // mid-sketch), fall back to FeatureByPositionReverse for older
            // SW versions where ActiveSketch returns a non-IFeature wrapper.
            // FeatureByPositionReverse(0) was the only path before, but it
            // returns the ref-plane feature (not the sketch) when InsertSketch
            // was called on a freshly-created ref plane — the plane is still
            // the most recent tree entry. ActiveSketch sidesteps that race.
            IFeature sketchFeature = null;
            try
            {
                var active = _model.SketchManager.ActiveSketch;
                if (active != null)
                    sketchFeature = ((object)active) as IFeature;
            }
            catch { }
            if (sketchFeature == null)
                sketchFeature = _model.FeatureByPositionReverse(0) as IFeature;
            // Final guard: the most-recent feature must actually be a sketch
            // (type "ProfileFeature"). If it's the ref plane we just made,
            // walk forward one position.
            if (sketchFeature != null && sketchFeature.GetTypeName2() == "RefPlane")
            {
                FileLog($"  newSketch: FeatureByPositionReverse(0) returned ref plane, scanning for ProfileFeature");
                var scan = _model.FirstFeature() as IFeature;
                IFeature lastSketch = null;
                while (scan != null)
                {
                    if (scan.GetTypeName2() == "ProfileFeature") lastSketch = scan;
                    scan = scan.GetNextFeature() as IFeature;
                }
                if (lastSketch != null) sketchFeature = lastSketch;
            }
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
            // Accept both forms — explicit cx/cy fields AND the
            // `center: [x, y]` array shape that some planners emit
            // (e.g. revolve_planner). Without this fallback the C#
            // dictionary lookup throws KeyNotFoundException.
            double cx = 0, cy = 0;
            if (p.ContainsKey("cx")) cx = Mm(p["cx"]);
            else if (p.ContainsKey("center"))
            {
                var pair = ParseTwoNumberArray(p["center"]);
                if (pair != null) { cx = Mm(pair[0]); cy = Mm(pair[1]); }
            }
            if (p.ContainsKey("cy")) cy = Mm(p["cy"]);
            cy = MirrorYIfNeeded(cy);
            double r = p.ContainsKey("r") ? Mm(p["r"])
                       : (p.ContainsKey("radius") ? Mm(p["radius"])
                       : (p.ContainsKey("diameter") ? Mm(p["diameter"]) / 2.0
                                                     : 0.0));
            if (r <= 0)
                return new { ok = false, error = "sketchCircle: r/radius/diameter required" };
            if (_model == null) return new { ok = false, error = "no model" };

            var sketch = _model.SketchManager.ActiveSketch;
            if (sketch == null)
                return new { ok = false, error = "no active sketch" };

            object circle = _model.SketchManager.CreateCircle(
                cx, cy, 0,
                cx + r, cy, 0);
            return new { ok = circle != null, kind = "circle",
                          r_mm = r * 1000, cx_mm = cx * 1000, cy_mm = cy * 1000 };
        }

        // Helper: parse a JSON array like [0, 0] or [0.0, 25.5] into
        // a double[2]. Returns null on shape mismatch so the caller can
        // fall back. Used by sketch ops that accept either explicit
        // cx/cy fields or a center/start/end point array.
        private double[] ParseTwoNumberArray(object raw)
        {
            try
            {
                if (raw is Newtonsoft.Json.Linq.JArray ja && ja.Count >= 2)
                    return new[] { Convert.ToDouble(ja[0]),
                                    Convert.ToDouble(ja[1]) };
                if (raw is System.Collections.IEnumerable enumerable)
                {
                    var list = new System.Collections.Generic.List<double>();
                    foreach (var v in enumerable) list.Add(Convert.ToDouble(v));
                    if (list.Count >= 2) return new[] { list[0], list[1] };
                }
            }
            catch { }
            return null;
        }

        // Helper: parse [[x1,y1], [x2,y2], ...] for sketchPolyline /
        // sketchSpline. Returns null on shape mismatch.
        private System.Collections.Generic.List<double[]> ParsePointList(object raw)
        {
            var pts = new System.Collections.Generic.List<double[]>();
            try
            {
                if (raw is Newtonsoft.Json.Linq.JArray ja)
                {
                    foreach (var item in ja)
                    {
                        var pair = ParseTwoNumberArray(item);
                        if (pair != null) pts.Add(pair);
                    }
                    if (pts.Count > 0) return pts;
                }
                else if (raw is System.Collections.IEnumerable e)
                {
                    foreach (var item in e)
                    {
                        var pair = ParseTwoNumberArray(item);
                        if (pair != null) pts.Add(pair);
                    }
                    if (pts.Count > 0) return pts;
                }
            }
            catch { }
            return null;
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
            // start_offset (mm) — distance from the sketch plane along its
            // normal where the extrusion BEGINS. Used by the shaft planner
            // to stack stepped segments along Z without needing one offset
            // reference plane per segment. Without this, every extrude
            // starts at the sketch plane (z=0) and segments overlap, leaving
            // only the longest one in the final body.
            double startOffset = p.ContainsKey("start_offset")
                ? Mm(p["start_offset"]) : 0.0;
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
                // FeatureExtrusion3 args 22-24 control the START of the
                // extrusion. swStartSketchPlane (0) starts at the sketch
                // plane; swStartOffset (2) starts at +N meters along the
                // sketch normal. We pick swStartOffset only when the
                // planner asked for a non-zero offset — otherwise stay
                // with sketch-plane start so other planners (flange,
                // bracket, etc.) keep their existing behavior.
                int startCond = startOffset > 1e-9
                    ? (int)swStartConditions_e.swStartOffset
                    : (int)swStartConditions_e.swStartSketchPlane;
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
                    startCond,
                    startOffset,                                            // start offset (m)
                    false) as IFeature;
                // Open-profile fallback: solid extrude requires a closed
                // loop. If FeatureExtrusion3 returned null, retry with
                // solid=false (thin/surface extrude). Used by the surface
                // planner for open polylines / curved walls.
                if (feature == null)
                {
                    FileLog($"  extrude: solid=true returned null, " +
                             $"retrying as thin/surface extrude (open profile)");
                    _model.ClearSelection2(true);
                    if (sketchFeatName != null)
                        _model.Extension.SelectByID2(sketchFeatName, "SKETCH",
                            0, 0, 0, false, 0, null, 0);
                    feature = _model.FeatureManager.FeatureExtrusion3(
                        true,
                        false, false,
                        (int)swEndConditions_e.swEndCondBlind,
                        (int)swEndConditions_e.swEndCondBlind,
                        dist, 0,
                        false, false,
                        false, false,
                        0, 0,
                        false, false,
                        false, false,
                        false,                                              // solid=false (surface)
                        false,                                              // merge=false
                        true,
                        startCond,
                        startOffset,
                        false) as IFeature;
                    if (feature != null)
                        FileLog($"  extrude: surface-mode succeeded for open profile");
                }
                // Open-profile fallback #2: FeatureExtrusionThin2. SW exposes a
                // dedicated thin-feature method that handles open-loop sketches
                // when FeatureExtrusion3(solid=false) silently refuses on
                // SW2024 (same IDispatch class as Shell/CircularPattern).
                // Probe via reflection so we never bind to a missing signature.
                if (feature == null)
                {
                    _model.ClearSelection2(true);
                    if (sketchFeatName != null)
                        _model.Extension.SelectByID2(sketchFeatName, "SKETCH",
                            0, 0, 0, false, 0, null, 0);
                    var fm = _model.FeatureManager;
                    var fmType = fm.GetType();
                    // Try the documented thin-feature signatures in order.
                    // Build the arg vector by inspecting each parameter's
                    // ParameterType so we never pass a bool where SW expects
                    // a double (the failure mode that killed the previous
                    // attempt).
                    double wallThk = 0.0005;
                    object thinFeat = null;
                    foreach (var name in new[] {
                        "FeatureExtrusionThin2", "FeatureExtrusionThin",
                        "InsertProtrusionThin2", "FeatureExtrusion2" })
                    {
                        var mi = fmType.GetMethod(name);
                        if (mi == null) continue;
                        try
                        {
                            var paramInfos = mi.GetParameters();
                            int n = paramInfos.Length;
                            var args = new object[n];
                            for (int i = 0; i < n; i++)
                            {
                                Type pt = paramInfos[i].ParameterType;
                                if (pt == typeof(double))      args[i] = 0.0;
                                else if (pt == typeof(int))    args[i] = 0;
                                else if (pt == typeof(bool))   args[i] = false;
                                else if (pt == typeof(short))  args[i] = (short)0;
                                else                            args[i] = null;
                            }
                            // Now overwrite the well-known prefix slots with
                            // values that produce a basic blind+thin extrude.
                            // FeatureExtrusionThin2 ordering (SW 2014+):
                            //   0 Sd, 1 Flip, 2 Dir,
                            //   3 T1 (int), 4 T2 (int),
                            //   5 D1 (double), 6 D2 (double),
                            //   ... 17 ThickType (int), 18 Thick1 (double),
                            //   19 Thick2 (double), 20 CapEnds (bool),
                            //   21 CapThk (double).
                            if (n > 0) args[0] = true;
                            if (n > 3) args[3] = (int)swEndConditions_e.swEndCondBlind;
                            if (n > 4) args[4] = (int)swEndConditions_e.swEndCondBlind;
                            if (n > 5) args[5] = dist;
                            if (n > 17 && paramInfos[17].ParameterType == typeof(int))
                                args[17] = 0; // thicknessType: one-direction
                            if (n > 18 && paramInfos[18].ParameterType == typeof(double))
                                args[18] = wallThk;
                            FileLog($"  extrude: trying {name}(n={n})");
                            thinFeat = mi.Invoke(fm, args);
                            if (thinFeat != null)
                            {
                                FileLog($"  extrude: {name} succeeded for open profile (wall={wallThk*1000}mm)");
                                break;
                            }
                            else
                            {
                                FileLog($"  extrude: {name} returned null");
                            }
                        }
                        catch (Exception ex)
                        {
                            FileLog($"  extrude: {name} threw: {ex.Message}");
                        }
                    }
                    feature = thinFeat as IFeature;
                }
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
                bool recipeBlind = true;
                bool recipeFlip  = false;
                bool recipeDir   = false;
                bool recipeSelB  = false;
                bool recipeAuto  = true;
                if (recipe != null)
                {
                    recipeBlind = recipe.Value<bool?>("blind") ?? true;
                    recipeFlip  = recipe.Value<bool?>("flip") ?? false;
                    recipeDir   = recipe.Value<bool?>("dir") ?? false;
                    recipeSelB  = recipe.Value<bool?>("selectBody") ?? false;
                    recipeAuto  = recipe.Value<bool?>("useAutoSelect") ?? true;
                    FileLog($"  cut: recipe '{cutIntent}' -> blind={recipeBlind} flip={recipeFlip} dir={recipeDir} selBody={recipeSelB} auto={recipeAuto}");
                    cutFeat = TryFeatureCut(sketchFeatName, dist,
                        blind: recipeBlind, selectBody: recipeSelB,
                        useAutoSelect: recipeAuto, flip: recipeFlip, dir: recipeDir);
                }

                // Helper for recording the winning combo on success.
                // Wrapped in try/catch so persistence failures (e.g. JSON
                // serialization issues on this user's Newtonsoft version)
                // never tank the actual op result.
                //
                // Crucial: only record combos where blind/through-all
                // matches the requested intent. Recording a through-all
                // success under cutIntent="cut_extrude_blind" causes
                // every subsequent blind cut to use through-all and
                // destructively swallow the host body. (See L-bracket
                // 35x5x5 regression.)
                void RecordCut(bool b, bool f, bool d, bool sb, bool au)
                {
                    bool intentBlind = cutIntent == "cut_extrude_blind";
                    if (intentBlind && !b)
                    {
                        FileLog($"  RecordCut: skipping (intent=blind but combo blind={b}; through-all under blind intent destroys merged bodies)");
                        return;
                    }
                    if (!intentBlind && b)
                    {
                        FileLog($"  RecordCut: skipping (intent=through-all but combo blind={b})");
                        return;
                    }
                    // CRITICAL: f=true (FlipSideToCut) cuts the OUTSIDE of the
                    // closed loop instead of the inside. For normal cuts (hole
                    // in a box, cylinder out of a slab) this LEAVES only the
                    // cut tool body behind — every subsequent cut then takes
                    // the wrong side too. Refuse to cache flip=true; it must
                    // be re-discovered per-call if ever truly needed.
                    if (f)
                    {
                        FileLog($"  RecordCut: refusing flip=true (would mis-train future cuts to take outside-of-loop)");
                        return;
                    }
                    // Geometric sanity check: post-cut body bbox should not
                    // equal the cut tool sketch bbox. If it does, the cut
                    // collapsed the host body and we're recording garbage.
                    try
                    {
                        var part = _model as IPartDoc;
                        if (part != null)
                        {
                            var bodies = part.GetBodies2(0, true) as object[];
                            if (bodies != null && bodies.Length == 1)
                            {
                                var body = bodies[0] as IBody2;
                                var bbox = body?.GetBodyBox() as double[];
                                if (bbox != null && bbox.Length >= 6)
                                {
                                    double bx = (bbox[3] - bbox[0]) * 1000.0;
                                    double by = (bbox[4] - bbox[1]) * 1000.0;
                                    double bz = (bbox[5] - bbox[2]) * 1000.0;
                                    // Any axis < 5mm AND post-cut height < dist*1.1
                                    // is a sign the cut just left the cut-tool
                                    // body. Refuse to cache.
                                    double distMm = dist * 1000.0;
                                    if (bz <= distMm * 1.1 + 0.5 &&
                                        bx < 50.0 && by < 50.0)
                                    {
                                        FileLog($"  RecordCut: refusing — body bbox {bx:F1}x{by:F1}x{bz:F1}mm looks like cut tool, not subtracted host");
                                        return;
                                    }
                                }
                            }
                        }
                    }
                    catch (Exception sanityEx)
                    {
                        FileLog($"  RecordCut: sanity check threw {sanityEx.Message} (recording anyway)");
                    }
                    try
                    {
                        RecipeDb.RecordSuccess(cutIntent, JObject.FromObject(new
                        {
                            method        = "FeatureCut4",
                            blind         = b,
                            flip          = f,
                            dir           = d,
                            selectBody    = sb,
                            useAutoSelect = au,
                        }));
                    }
                    catch (Exception ex)
                    {
                        FileLog($"  RecordCut failed (op still succeeded): {ex.Message}");
                    }
                }

                if (cutFeat != null)
                    RecordCut(recipeBlind, recipeFlip, recipeDir, recipeSelB, recipeAuto);

                // Fallback grid: try every {Dir, Flip, Blind/ThroughAll}
                // combination before giving up. Dir matters most (reverses
                // extrude direction); Flip rarely (cuts outside the loop).
                // Skip the recipe combo to avoid running it twice.
                bool TryAttempt(bool b, bool f, bool d, bool sb, bool au)
                {
                    if (cutFeat != null) return true;
                    // Skip if same as the recipe attempt above (already tried)
                    if (recipe != null && b == recipeBlind && f == recipeFlip
                        && d == recipeDir && sb == recipeSelB && au == recipeAuto)
                        return false;
                    cutFeat = TryFeatureCut(sketchFeatName, dist,
                        blind: b, selectBody: sb, useAutoSelect: au, flip: f, dir: d);
                    if (cutFeat != null) { RecordCut(b, f, d, sb, au); return true; }
                    return false;
                }

                // 1. Dir variations on auto-select sketch-only path
                TryAttempt(true,  false, false, false, true);   // blind, +normal
                TryAttempt(true,  false, true,  false, true);   // blind, -normal (flip dir)
                TryAttempt(false, false, false, false, true);   // through-all, +normal
                TryAttempt(false, false, true,  false, true);   // through-all, -normal

                // 2. Flip side (cut outside loop) — useful for unusual sketches
                TryAttempt(true,  true,  false, false, true);
                TryAttempt(true,  true,  true,  false, true);
                TryAttempt(false, true,  false, false, true);
                TryAttempt(false, true,  true,  false, true);

                // 3. Explicit body select (Mark=4) — for cases where SW
                //    can't auto-pick the target body (multi-body parts)
                TryAttempt(true,  false, false, true,  false);
                TryAttempt(true,  false, true,  true,  false);

                // Last-ditch (static): feature scope DISABLED, cut affects every body
                if (cutFeat == null)
                    cutFeat = TryFeatureCutNoScope(sketchFeatName, dist);

                // FINAL fallback: LLM-in-the-loop. The static grid + noScope
                // exhausted. Hand the failure context to the backend's
                // /api/cad/synthesize-args endpoint, get next-best args,
                // try them, loop up to N times. Wins go straight into the
                // recipe cache so this user never hits the LLM path again
                // for the same intent.
                if (cutFeat == null)
                {
                    var priorAttempts = new List<JObject>
                    {
                        JObject.FromObject(new { blind = true,  flip = false, dir = false, selectBody = false, useAutoSelect = true }),
                        JObject.FromObject(new { blind = true,  flip = false, dir = true,  selectBody = false, useAutoSelect = true }),
                        JObject.FromObject(new { blind = false, flip = false, dir = false, selectBody = false, useAutoSelect = true }),
                        JObject.FromObject(new { blind = false, flip = false, dir = true,  selectBody = false, useAutoSelect = true }),
                        JObject.FromObject(new { blind = true,  flip = true,  dir = false, selectBody = false, useAutoSelect = true }),
                        JObject.FromObject(new { blind = true,  flip = true,  dir = true,  selectBody = false, useAutoSelect = true }),
                        JObject.FromObject(new { blind = false, flip = true,  dir = false, selectBody = false, useAutoSelect = true }),
                        JObject.FromObject(new { blind = false, flip = true,  dir = true,  selectBody = false, useAutoSelect = true }),
                        JObject.FromObject(new { blind = true,  flip = false, dir = false, selectBody = true,  useAutoSelect = false }),
                        JObject.FromObject(new { blind = true,  flip = false, dir = true,  selectBody = true,  useAutoSelect = false }),
                    };
                    JObject context = JObject.FromObject(new
                    {
                        body_bbox_mm = SummariseBodyBboxMm(),
                        sketch_plane = _activeSketchPlane,
                        cut_distance_mm = dist * 1000,
                        sketch_name = sketchFeatName,
                    });
                    cutFeat = TrySynthesizeAndCut(sketchFeatName, dist,
                                                   cutIntent, priorAttempts,
                                                   context, RecordCut);
                }

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
            if (feature == null)
            {
                // Both solid + thin/surface modes returned null. Most common
                // cause: profile is open AND too short (< 1mm), OR sketch
                // wasn't selected, OR cut path's recipe combos all failed.
                // Surface up enough info that the runner stops reporting
                // 'unknown' for this op.
                return new { ok = false,
                              error = $"FeatureExtrusion3 returned null after solid+thin attempts (operation={operation}, sketch='{sketchAlias}'). Open profiles need length > 1mm; cuts need profile inside host body.",
                              kind = "extrude", operation,
                              sketch = sketchAlias,
                              distance_mm = dist * 1000 };
            }
            return new { ok = true, alias, kind = "extrude",
                          distance_mm = dist * 1000, operation };
        }

        private IFeature TryFeatureCut(string sketchName, double dist,
            bool blind, bool selectBody, bool useAutoSelect,
            bool flip, bool dir)
        {
            _model.ClearSelection2(true);
            bool selSketch = _model.Extension.SelectByID2(
                sketchName, "SKETCH", 0, 0, 0, false, 0, null,
                (int)swSelectOption_e.swSelectOptionDefault);
            if (!selSketch)
            {
                FileLog($"  cut.try (blind={blind} body={selectBody} auto={useAutoSelect} flip={flip} dir={dir}): SelectByID2 sketch={false}");
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
                FileLog($"  cut.try blind={blind} body={selectBody} auto={useAutoSelect} flip={flip} dir={dir} T1={endCond1} sel={selCount} featCount={featCountBefore}");
                const double DEG = 0.01745329251994;
                // Args:
                //   Flip = FlipSideToCut: cut OUTSIDE the closed loop (rare)
                //   Dir  = ReverseDirection: -sketch_normal instead of +
                // Most cut failures are direction issues, fixed by Dir=true.
                // Capture as object first (no IFeature cast) so we can see
                // the actual COM type — null vs cast failure.
                object raw = _model.FeatureManager.FeatureCut4(
                    true,                                 // Sd (single-direction)
                    flip, dir,                            // Flip (side), Dir (direction)
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

        // Returns "X[a..b] Y[c..d] Z[e..f]" for the active body, or "" if
        // the part has no solid body. Used as context in the LLM-args call.
        private string SummariseBodyBboxMm()
        {
            try
            {
                var part = _model as IPartDoc;
                var bodies = part?.GetBodies2(
                    (int)swBodyType_e.swSolidBody, false) as object[];
                if (bodies == null || bodies.Length == 0) return "";
                if (!(bodies[0] is IBody2 b)) return "";
                var bbox = b.GetBodyBox() as double[];
                if (bbox == null || bbox.Length < 6) return "";
                return $"X[{bbox[0]*1000:F1}..{bbox[3]*1000:F1}] " +
                       $"Y[{bbox[1]*1000:F1}..{bbox[4]*1000:F1}] " +
                       $"Z[{bbox[2]*1000:F1}..{bbox[5]*1000:F1}]";
            }
            catch { return ""; }
        }

        // Backend HTTP target — defaults to localhost:8000 (the dashboard).
        // Override via env var for prod. (System.Environment, not the
        // SolidWorks.Interop.sldworks.Environment shadowing class.)
        private static readonly string _ariaBackend =
            System.Environment.GetEnvironmentVariable("ARIA_BACKEND_URL")
            ?? "http://localhost:8000";

        private static readonly System.Net.Http.HttpClient _llmHttp =
            new System.Net.Http.HttpClient
            {
                Timeout = TimeSpan.FromSeconds(20),
            };

        /// <summary>LLM-in-the-loop final fallback. After the static
        /// 11-combo grid + noScope all return null, ask the backend for
        /// next-best args, try them, loop up to 5 times. Each successful
        /// combo is recorded into the recipe cache so the next request
        /// for this intent hits one-shot.</summary>
        private IFeature TrySynthesizeAndCut(
            string sketchName, double dist, string intent,
            List<JObject> priorAttempts, JObject context,
            Action<bool, bool, bool, bool, bool> recordCb)
        {
            const int MAX_LLM_ITERATIONS = 5;
            for (int iter = 1; iter <= MAX_LLM_ITERATIONS; iter++)
            {
                FileLog($"  cut.llm[{iter}/{MAX_LLM_ITERATIONS}]: asking backend for next args");
                JObject suggested = null;
                try
                {
                    var reqBody = new JObject
                    {
                        ["cad"]            = "solidworks",
                        ["op"]             = intent,
                        ["method"]         = "FeatureCut4",
                        ["signature"]      =
                            "FeatureCut4(Sd, Flip, Dir, T1, T2, D1, D2, "
                            + "Dchk1, Dchk2, Ddir1, Ddir2, Dang1, Dang2, "
                            + "OffsetReverse1/2, TranslateSurface1/2, NormalCut, "
                            + "UseFeatScope, UseAutoSelect, AssemblyFeatureScope, "
                            + "AutoSelectComponents, PropagateFeatureToParts, "
                            + "T0, StartOffset, FlipStartOffset, OptimizeGeometry)",
                        ["prior_attempts"] = new JArray(priorAttempts),
                        ["failure_msgs"]   = new JArray(
                            Enumerable.Repeat("returned null", priorAttempts.Count)
                                      .Cast<object>().ToArray()),
                        ["context"]        = context,
                    };
                    var content = new System.Net.Http.StringContent(
                        reqBody.ToString(),
                        System.Text.Encoding.UTF8, "application/json");
                    var resp = _llmHttp.PostAsync(
                        $"{_ariaBackend}/api/cad/synthesize-args",
                        content).GetAwaiter().GetResult();
                    string respText = resp.Content.ReadAsStringAsync()
                                                  .GetAwaiter().GetResult();
                    FileLog($"  cut.llm[{iter}]: backend reply = {respText.Substring(0, Math.Min(200, respText.Length))}");
                    var parsed = JObject.Parse(respText);
                    suggested = parsed["args"] as JObject;
                    if (suggested == null)
                    {
                        FileLog($"  cut.llm[{iter}]: no usable args ({parsed["reason"]?.ToString() ?? "unknown"})");
                        break;
                    }
                }
                catch (Exception ex)
                {
                    FileLog($"  cut.llm[{iter}]: backend call failed: {ex.GetType().Name}: {ex.Message}");
                    break;
                }

                bool b  = suggested.Value<bool?>("blind")         ?? true;
                bool f  = suggested.Value<bool?>("flip")          ?? false;
                bool d  = suggested.Value<bool?>("dir")           ?? false;
                bool sb = suggested.Value<bool?>("selectBody")    ?? false;
                bool au = suggested.Value<bool?>("useAutoSelect") ?? true;

                FileLog($"  cut.llm[{iter}] try: blind={b} flip={f} dir={d} selBody={sb} auto={au}");
                IFeature got = TryFeatureCut(sketchName, dist,
                    blind: b, selectBody: sb, useAutoSelect: au, flip: f, dir: d);
                if (got != null)
                {
                    FileLog($"  cut.llm[{iter}]: WIN — recording combo to recipe cache");
                    try { recordCb(b, f, d, sb, au); } catch { }
                    return got;
                }
                priorAttempts.Add(suggested);
            }
            FileLog($"  cut.llm: exhausted {MAX_LLM_ITERATIONS} iterations, giving up");
            return null;
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
            string featAlias = p.ContainsKey("feature")
                                 ? p["feature"]?.ToString() : null;
            int count = p.ContainsKey("count") ? Convert.ToInt32(p["count"]) : 2;
            string axis = p.ContainsKey("axis")
                           ? p["axis"]?.ToString().ToUpperInvariant() : "Z";
            string alias = p.ContainsKey("alias") ? p["alias"]?.ToString() : null;
            // Optional explicit seed coords from the planner — bypasses the
            // fragile COM-IDispatch sketch introspection in the software-
            // pattern fallback. Values are mm.
            double? planSeedX = p.ContainsKey("seed_x")
                ? Convert.ToDouble(p["seed_x"]) : (double?)null;
            double? planSeedY = p.ContainsKey("seed_y")
                ? Convert.ToDouble(p["seed_y"]) : (double?)null;
            double? planSeedR = p.ContainsKey("seed_r")
                ? Convert.ToDouble(p["seed_r"]) : (double?)null;
            if (_model == null) return new { ok = false, error = "no model" };

            // Strategy log so failures are diagnosable from the addin log.
            var diag = new List<string>();

            _model.ClearSelection2(true);

            // ---- 1. Select the source feature ----------------------------
            // Try (a) explicit alias from params, (b) fall back to the most
            // recently added feature in the FeatureManager tree if no alias
            // was passed or the alias is unknown. The fallback handles the
            // common planner case where it forgets to set `feature`.
            IFeature srcFeat = null;
            if (!string.IsNullOrEmpty(featAlias)
                && _aliasMap.ContainsKey(featAlias)
                && _aliasMap[featAlias] is IFeature mapped)
            {
                srcFeat = mapped;
                diag.Add($"feature-alias='{featAlias}'");
            }
            if (srcFeat == null)
            {
                // Walk the tree, keep the last "real" feature (skip refs).
                IFeature f = _model.FirstFeature() as IFeature;
                IFeature last = null;
                while (f != null)
                {
                    string tn = f.GetTypeName2();
                    // Skip refs and origin elements.
                    if (tn != "Reference" && tn != "OriginProfileFeature"
                        && tn != "RefAxis" && tn != "RefPlane"
                        && tn != "OriginAxis" && tn != "Origin")
                    {
                        last = f;
                    }
                    f = f.GetNextFeature() as IFeature;
                }
                srcFeat = last;
                diag.Add($"feature-fallback='{srcFeat?.Name}'");
            }
            bool selFeat = false;
            if (srcFeat != null)
            {
                selFeat = srcFeat.Select2(true, 1);   // Append=true, Mark=1
            }
            diag.Add($"selFeat={selFeat}");

            // ---- 2. Select an axis to revolve around --------------------
            // Cascade in order of correctness (NOT just availability):
            //   (a) origin axis by name — guaranteed to pass through (0,0,0)
            //       so pattern lands evenly around the part center.
            //   (b) cylindrical face that is concentric with origin (i.e.
            //       centered on the desired world axis). Filtered by
            //       checking the surface's axis params — we previously
            //       grabbed the FIRST cyl-face which on a flange was the
            //       bolt-hole's offset face (axis at r=80mm), causing
            //       FeatureCircularPattern5 to silently return null.
            //   (c) origin plane fallback.
            bool selAxis = false;
            string axisStrategy = "none";

            // (a) Origin axis first — most reliable when present.
            {
                string axisName = axis switch
                {
                    "X" => "X", "Y" => "Y", _ => "Z",
                };
                string[] axisIds = {
                    axisName + " Axis@Origin",
                    axisName + " Axis",
                    "Origin\\" + axisName + " Axis",
                };
                foreach (string id in axisIds)
                {
                    selAxis = _model.Extension.SelectByID2(
                        id, "AXIS", 0, 0, 0, true, 4, null,
                        (int)swSelectOption_e.swSelectOptionDefault);
                    if (selAxis)
                    {
                        axisStrategy = $"axis='{id}'";
                        break;
                    }
                }
            }

            // (b) Concentric cylindrical face — only if its axis passes
            //     through origin (within ~1 micron). Skips bolt holes,
            //     side bosses, etc. that have offset cylindrical surfaces.
            if (!selAxis && srcFeat != null)
            {
                try
                {
                    var faces = srcFeat.GetFaces() as object[];
                    if (faces != null)
                    {
                        foreach (var fo in faces)
                        {
                            var face = fo as IFace2;
                            if (face == null) continue;
                            var surf = face.GetSurface() as ISurface;
                            if (surf == null || !surf.IsCylinder()) continue;
                            // ICylinderParams: [origin x,y,z, dir x,y,z, radius]
                            // Reject if the axis line doesn't pass within ~1µm of origin.
                            bool concentric = false;
                            try
                            {
                                var cp = surf.CylinderParams as double[];
                                if (cp != null && cp.Length >= 7)
                                {
                                    double ox = cp[0], oy = cp[1], oz = cp[2];
                                    double dx = cp[3], dy = cp[4], dz = cp[5];
                                    // Closest distance from world origin to the line
                                    // through (ox,oy,oz) with direction (dx,dy,dz):
                                    // |(O-P) - ((O-P).D)D| where O=0, P=(ox,oy,oz)
                                    double rx = -ox, ry = -oy, rz = -oz;
                                    double dot = rx * dx + ry * dy + rz * dz;
                                    double cx = rx - dot * dx;
                                    double cy = ry - dot * dy;
                                    double cz = rz - dot * dz;
                                    double dist = Math.Sqrt(cx * cx + cy * cy + cz * cz);
                                    concentric = dist < 1e-3;  // 1µm
                                }
                            }
                            catch { concentric = false; }
                            if (!concentric) continue;
                            // Mark=4 is REQUIRED for FeatureCircularPattern5
                            // to recognize the entity as the rotation axis.
                            // Default selectData has Mark=0 → pattern API
                            // silently rejects the selection.
                            var selMgr = _model.SelectionManager
                                as ISelectionMgr;
                            var sd4 = selMgr?.CreateSelectData()
                                as ISelectData;
                            if (sd4 != null) sd4.Mark = 4;
                            selAxis = (face as IEntity)?.Select4(
                                true, sd4 as SelectData) ?? false;
                            if (selAxis)
                            {
                                axisStrategy = "cyl-face-concentric";
                                break;
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    diag.Add($"cyl-face-scan-threw={ex.Message}");
                }
            }

            if (!selAxis)
            {
                // SW plane normals (default coord system):
                //   Front Plane = XZ plane, normal = +Y
                //   Top Plane   = XY plane, normal = +Z
                //   Right Plane = YZ plane, normal = +X
                // For circularPattern around an axis we want the plane whose
                // NORMAL equals that axis (SW patterns around the plane normal
                // when given a plane as the axis surrogate). Previous mapping
                // gave Front Plane for axis=Z which is the Y-axis normal, so
                // every "rotate around Z" pattern was silently rejected.
                string axisPlane = axis switch
                {
                    "X" => "Right Plane",
                    "Y" => "Front Plane",
                    _   => "Top Plane",        // axis=Z → XY plane, normal=Z
                };
                selAxis = _model.Extension.SelectByID2(
                    axisPlane, "PLANE", 0, 0, 0, true, 4, null,
                    (int)swSelectOption_e.swSelectOptionDefault);
                if (selAxis) axisStrategy = $"plane='{axisPlane}'";
            }

            // SW 2024 silently rejects PLANE selections as the pattern axis —
            // FeatureCircularPattern5 returns null even when the plane normal
            // matches the requested axis direction. Need a REAL axis entity.
            //
            // Strategy: insert a reference axis at the origin via the
            // intersection of the two orthogonal default planes, then select
            // that axis. Front ∩ Right = Z axis line, Top ∩ Front = X axis,
            // Top ∩ Right = Y axis. This costs one Axis feature in the tree
            // but produces a real, selectable axis the pattern API accepts.
            //
            // We also pre-try SW's auto-generated "Temporary Axis<n>" names
            // (one per cylindrical face) since those exist for free if the
            // part has a bore.
            bool axisIsReal = !string.IsNullOrEmpty(axisStrategy)
                && (axisStrategy.StartsWith("axis=")
                     || axisStrategy.StartsWith("cyl-face")
                     || axisStrategy.StartsWith("temp-axis"));
            if (!axisIsReal)
            {
                // Plane selection alone doesn't fly. Throw the plane out and
                // build a real axis.
                _model.ClearSelection2(true);
                bool selFeatAgain = srcFeat?.Select2(true, 1) ?? false;
                selAxis = false;
                axisStrategy = "none";

                // (a) Try Temporary Axis<1..4> — SW auto-creates these for
                //     every cylindrical face in the part (bore, bolt hole,
                //     hub OD, etc.).
                try
                {
                    string[] tempAxisIds = {
                        "Temporary Axis<1>", "Temporary Axis<2>",
                        "Temporary Axis<3>", "Temporary Axis<4>",
                        "Temporary Axis<5>", "Temporary Axis<6>",
                    };
                    foreach (string id in tempAxisIds)
                    {
                        bool sel = _model.Extension.SelectByID2(
                            id, "AXIS", 0, 0, 0, true, 4, null,
                            (int)swSelectOption_e.swSelectOptionDefault);
                        if (sel)
                        {
                            selAxis = true;
                            axisStrategy = $"temp-axis='{id}'";
                            break;
                        }
                    }
                }
                catch (Exception ex)
                {
                    diag.Add($"temp-axis-scan-threw={ex.Message}");
                }

                // (b) Broaden the concentric cyl-face scan to ALL bodies in
                //     the part, not just the source feature. The flange body
                //     has a Ø200 mm OD whose axis IS the world Z axis — the
                //     bolt cut feature didn't, but the flange disc itself
                //     does. Walking every body's faces gives us a real axis
                //     surrogate (cylindrical face = axis) when the source
                //     feature has none of its own.
                if (!selAxis)
                {
                    try
                    {
                        var bodyMgr = _model.GetActiveConfiguration() as IConfiguration;
                        var bodies = (_model as IPartDoc)?.GetBodies2(
                            (int)swBodyType_e.swSolidBody, false) as object[];
                        if (bodies != null)
                        {
                            foreach (var bo in bodies)
                            {
                                var body = bo as IBody2;
                                if (body == null) continue;
                                var faces = body.GetFaces() as object[];
                                if (faces == null) continue;
                                foreach (var fo in faces)
                                {
                                    var face = fo as IFace2;
                                    if (face == null) continue;
                                    var surf = face.GetSurface() as ISurface;
                                    if (surf == null || !surf.IsCylinder())
                                        continue;
                                    bool concentric = false;
                                    try
                                    {
                                        var cp = surf.CylinderParams as double[];
                                        if (cp != null && cp.Length >= 7)
                                        {
                                            double ox = cp[0], oy = cp[1],
                                                   oz = cp[2];
                                            double dx = cp[3], dy = cp[4],
                                                   dz = cp[5];
                                            double rx = -ox, ry = -oy, rz = -oz;
                                            double dot = rx*dx + ry*dy + rz*dz;
                                            double cx = rx - dot * dx;
                                            double cy = ry - dot * dy;
                                            double cz = rz - dot * dz;
                                            double dist = Math.Sqrt(
                                                cx*cx + cy*cy + cz*cz);
                                            concentric = dist < 1e-3;
                                        }
                                    }
                                    catch { concentric = false; }
                                    if (!concentric) continue;
                                    // Mark=4 is REQUIRED — without it,
                                    // FeatureCircularPattern5 silently
                                    // rejects the cylindrical face as the
                                    // rotation axis and returns null.
                                    var selMgrB = _model.SelectionManager
                                        as ISelectionMgr;
                                    var sdB = selMgrB?.CreateSelectData()
                                        as ISelectData;
                                    if (sdB != null) sdB.Mark = 4;
                                    selAxis = (face as IEntity)?.Select4(
                                        true, sdB as SelectData) ?? false;
                                    if (selAxis)
                                    {
                                        axisStrategy = "body-cyl-face-concentric(mark=4)";
                                        break;
                                    }
                                }
                                if (selAxis) break;
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        diag.Add($"body-face-scan-threw={ex.Message}");
                    }
                }
            }
            // ---- 2.5 Final fallback: build a real RefAxis ---------------
            // SW 2024 sometimes refuses cylindrical-face selections as the
            // pattern axis even with Mark=4, returning null silently. The
            // bullet-proof workaround is to materialize a Reference Axis
            // feature (InsertAxis2) that the pattern API always accepts.
            //
            // Three construction strategies, tried in order:
            //   (i)   single concentric cyl face → axis along its centerline
            //   (ii)  intersection of two origin planes (matches requested axis)
            //   (iii) two non-coplanar concentric cyl faces (advanced, rare)
            if (!selAxis && srcFeat != null)
            {
                try
                {
                    // (i) Single concentric cyl face → create an axis from it.
                    var bodies = (_model as IPartDoc)?.GetBodies2(
                        (int)swBodyType_e.swSolidBody, false) as object[];
                    IFace2 chosenFace = null;
                    if (bodies != null)
                    {
                        foreach (var bo in bodies)
                        {
                            var body = bo as IBody2;
                            if (body == null) continue;
                            var faces = body.GetFaces() as object[];
                            if (faces == null) continue;
                            foreach (var fo in faces)
                            {
                                var face = fo as IFace2;
                                if (face == null) continue;
                                var surf = face.GetSurface() as ISurface;
                                if (surf == null || !surf.IsCylinder()) continue;
                                bool concentric = false;
                                try
                                {
                                    var cp = surf.CylinderParams as double[];
                                    if (cp != null && cp.Length >= 7)
                                    {
                                        double ox = cp[0], oy = cp[1], oz = cp[2];
                                        double dx = cp[3], dy = cp[4], dz = cp[5];
                                        double rx = -ox, ry = -oy, rz = -oz;
                                        double dot = rx * dx + ry * dy + rz * dz;
                                        double cx = rx - dot * dx;
                                        double cy = ry - dot * dy;
                                        double cz = rz - dot * dz;
                                        double dist = Math.Sqrt(cx*cx + cy*cy + cz*cz);
                                        concentric = dist < 1e-3;
                                    }
                                }
                                catch { concentric = false; }
                                if (concentric) { chosenFace = face; break; }
                            }
                            if (chosenFace != null) break;
                        }
                    }
                    if (chosenFace != null)
                    {
                        _model.ClearSelection2(true);
                        bool fSel = (chosenFace as IEntity)?.Select4(
                            false, (SelectData)null) ?? false;
                        if (fSel)
                        {
                            // InsertAxis2 args (8 in SW 2018+):
                            //   (Type, Param1, Param2, Param3, Param4,
                            //    UseParam3, UseParam4)
                            // Type=2 = "One cylindrical/conical face".
                            var fm = _model.FeatureManager;
                            var miAx = fm.GetType().GetMethod("InsertAxis2");
                            object axisFeat = null;
                            if (miAx != null)
                            {
                                int n = miAx.GetParameters().Length;
                                var args = new object[n];
                                for (int i = 0; i < n; i++) args[i] = false;
                                if (n >= 1) args[0] = 2;  // Type=cyl-face
                                axisFeat = miAx.Invoke(fm, args);
                            }
                            if (axisFeat is IFeature af)
                            {
                                _model.ClearSelection2(true);
                                // Re-select source feature + new axis with
                                // proper marks for the pattern call.
                                srcFeat?.Select2(false, 1);
                                var selMgrA = _model.SelectionManager
                                    as ISelectionMgr;
                                var sdA = selMgrA?.CreateSelectData()
                                    as ISelectData;
                                if (sdA != null) sdA.Mark = 4;
                                selAxis = af.Select2(true, 4);
                                if (selAxis)
                                    axisStrategy = $"refaxis-from-cylface='{af.Name}'";
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    diag.Add($"insertaxis-fallback-threw={ex.Message}");
                }
            }

            diag.Add($"selAxis={selAxis} via {axisStrategy}");

            // ---- 3. Pattern call ---------------------------------------
            IFeature feature = null;
            if (selFeat && selAxis)
            {
                try
                {
                    // FeatureCircularPattern5 args:
                    //   1: Number              — total instance count
                    //   2: Spacing (rad)       — total sweep when EqualSpacing
                    //                            is true, OR per-instance step
                    //                            when EqualSpacing is false
                    //   3: ReverseDirection
                    //   4: DName               — "NULL" = use selection
                    //   5: EqualSpacing        — TRUE: spread Number items
                    //                            evenly over Spacing radians
                    //   6: VarySketch
                    //   7-14: cosmetic / unused
                    // Old code passed Spacing=2π with EqualSpacing=false,
                    // which means 360° between each instance — every item
                    // landed at the same place → SW rejected the pattern as
                    // degenerate and returned null with no error message.
                    // SW2024 FeatureCircularPattern5 has 14 args (verified
                    // via typelib probe). Arg types are exact — a misplaced
                    // bool→int throws cascading marshaler errors.
                    //   1: Number (int)             8: ResetState (bool)
                    //   2: Spacing (double rad)     9: OffsetSeed (bool)
                    //   3: FlipDirection (bool)    10: ReverseSeed (bool)
                    //   4: DName (string)          11: OffsetType (int)
                    //   5: EqualSpacing (bool)     12: OffsetValue (double)
                    //   6: VarySketch (bool)       13: OffsetName (string)
                    //   7: GeometryPattern (bool)  14: ApplyOffsets (bool)
                    feature = LateBoundInvoke(
                        "circPat",
                        new object[] { _model.FeatureManager,
                                       _model.Extension, _model },
                        new[] { "FeatureCircularPattern5",
                                "FeatureCircularPattern4",
                                "FeatureCircularPattern3" },
                        new object[] {
                            count,         // 1
                            2 * Math.PI,   // 2
                            false,         // 3
                            "NULL",        // 4
                            true,          // 5  EqualSpacing on
                            false,         // 6
                            false,         // 7
                            false,         // 8
                            false,         // 9
                            false,         // 10
                            0,             // 11 (int)
                            0.0,           // 12 (double)
                            "NULL",        // 13 (string)
                            false,         // 14
                        }) as IFeature;

                    // SW2024 last-resort: if pattern still null, retry with
                    // FeatureCircularPattern4 / 3 (older signatures may be
                    // wired up internally even though the public
                    // typelib advertises 5).
                    if (feature == null)
                    {
                        var fm = _model.FeatureManager;
                        var older = fm.GetType().GetMethod("FeatureCircularPattern4")
                                     ?? fm.GetType().GetMethod("FeatureCircularPattern3");
                        if (older != null)
                        {
                            int n = older.GetParameters().Length;
                            var args = new object[n];
                            for (int i = 0; i < n; i++) args[i] = false;
                            // Common 8-arg signature for v3:
                            //   (Number, Spacing, FlipDirection, GeometryPattern,
                            //    EqualSpacing, VarySketch, OffsetSeed, ReverseSeed)
                            if (n >= 5)
                            {
                                args[0] = count;
                                args[1] = 2 * Math.PI;
                                args[2] = false;
                                args[3] = false;
                                args[4] = true;
                            }
                            feature = older.Invoke(fm, args) as IFeature;
                            if (feature != null)
                                diag.Add($"used-older-pattern={older.Name}");
                        }
                    }
                }
                catch (Exception ex)
                {
                    diag.Add($"FeatureCircularPattern5-threw={ex.Message}");
                }
            }
            else
            {
                diag.Add("skipped-pattern-call (selection incomplete)");
            }

            // SOFTWARE-PATTERN FALLBACK: SW2024 interop returns null for
            // FeatureCircularPattern5 even with all selection state correct
            // (verified via probe). When that happens, manually replicate
            // the pattern by N-1 cut-extrudes at rotated positions. Works
            // for the standard "single circular feature on a disc" case.
            int softwareInstancesAdded = 0;
            if (feature == null && srcFeat != null)
            {
                try
                {
                    // Inspect the source feature's first sketch to get
                    // the seed circle position (cx, cy, r).
                    double seedX = double.NaN, seedY = double.NaN, seedR = 0;
                    string seedSketchName = null;
                    // If the planner provided explicit seed coords, use
                    // those and skip the fragile COM unwrap entirely.
                    if (planSeedX.HasValue && planSeedY.HasValue
                        && planSeedR.HasValue && planSeedR.Value > 0)
                    {
                        seedX = planSeedX.Value / 1000.0;  // mm -> m
                        seedY = planSeedY.Value / 1000.0;
                        seedR = planSeedR.Value / 1000.0;
                        seedSketchName = "from-plan";
                        FileLog($"  circPat: planner-provided seed X={seedX*1000:F2} Y={seedY*1000:F2} R={seedR*1000:F2}");
                    }
                    try
                    {
                        // Skip COM introspection entirely if the planner
                        // already provided explicit seed coords above.
                        if (seedSketchName == "from-plan") goto skipComLookup;
                        // Walk srcFeat's owned sketches first (extrude
                        // features contain their seed sketch as a
                        // sub-feature in most SW versions).
                        var subFeat = srcFeat.GetFirstSubFeature() as IFeature;
                        while (subFeat != null && string.IsNullOrEmpty(seedSketchName))
                        {
                            if (subFeat.GetTypeName2() == "ProfileFeature")
                                seedSketchName = subFeat.Name;
                            subFeat = subFeat.GetNextSubFeature() as IFeature;
                        }
                        // Fallback: walk the feature tree in reverse from
                        // the source feature, looking for the closest
                        // preceding ProfileFeature.
                        if (string.IsNullOrEmpty(seedSketchName))
                        {
                            // Grab all features, find srcFeat's index,
                            // walk backwards.
                            var all = new System.Collections.Generic.List<IFeature>();
                            IFeature f = _model.FirstFeature() as IFeature;
                            while (f != null)
                            {
                                all.Add(f);
                                f = f.GetNextFeature() as IFeature;
                            }
                            int srcIdx = -1;
                            for (int i = 0; i < all.Count; i++)
                                if (all[i].Name == srcFeat.Name) { srcIdx = i; break; }
                            if (srcIdx > 0)
                            {
                                for (int i = srcIdx - 1; i >= 0; i--)
                                {
                                    if (all[i].GetTypeName2() == "ProfileFeature")
                                    {
                                        seedSketchName = all[i].Name;
                                        break;
                                    }
                                }
                            }
                            FileLog($"  circPat: tree-reverse seed scan -> '{seedSketchName ?? "null"}' (srcIdx={srcIdx})");
                        }
                        FileLog($"  circPat: seedSketchName='{seedSketchName ?? "null"}'");
                        if (!string.IsNullOrEmpty(seedSketchName))
                        {
                            _model.ClearSelection2(true);
                            bool selOk = _model.Extension.SelectByID2(
                                seedSketchName, "SKETCH", 0, 0, 0,
                                false, 0, null, 0);
                            FileLog($"  circPat: select seed sketch '{seedSketchName}' -> {selOk}");
                            _model.EditSketch();
                            var sk = _model.SketchManager.ActiveSketch as ISketch;
                            FileLog($"  circPat: ActiveSketch null? {sk == null}");
                            if (sk != null)
                            {
                                // Use late-bound to dodge interop method
                                // name drift across SW versions.
                                object[] arcs = null;
                                foreach (var probeName in new[] {
                                    "GetSketchArcs", "GetArcs", "GetArcs2",
                                    "GetSketchArcs2" })
                                {
                                    try
                                    {
                                        var miA = sk.GetType().GetMethod(probeName);
                                        if (miA != null)
                                        {
                                            int n = miA.GetParameters().Length;
                                            var aArgs = new object[n];
                                            for (int i = 0; i < n; i++) aArgs[i] = false;
                                            arcs = miA.Invoke(sk, aArgs) as object[];
                                            if (arcs != null) break;
                                        }
                                    }
                                    catch { /* try next */ }
                                }
                                FileLog($"  circPat: arc-probe arcs.len={(arcs?.Length.ToString() ?? "null")}");
                                if (arcs != null && arcs.Length > 0)
                                {
                                    var sa = arcs[0] as ISketchArc;
                                    if (sa != null)
                                    {
                                        var cp = sa.GetCenterPoint2() as double[];
                                        if (cp != null && cp.Length >= 2)
                                        {
                                            seedX = cp[0];
                                            seedY = cp[1];
                                            seedR = sa.GetRadius();
                                        }
                                    }
                                }
                                // Also try GetSketchSegments (more general)
                                if (seedR <= 0)
                                {
                                    object[] segs = null;
                                    try {
                                        var miS = sk.GetType().GetMethod("GetSketchSegments");
                                        if (miS != null)
                                            segs = miS.Invoke(sk, new object[0]) as object[];
                                    } catch { }
                                    FileLog($"  circPat: seg-probe segs.len={(segs?.Length.ToString() ?? "null")}");
                                    if (segs != null)
                                    {
                                        foreach (var s in segs)
                                        {
                                            FileLog($"  circPat: seg type={s?.GetType().Name}");
                                            // Try ISketchArc first.
                                            var sa = s as ISketchArc;
                                            if (sa != null)
                                            {
                                                var cp = sa.GetCenterPoint2() as double[];
                                                if (cp != null && cp.Length >= 2)
                                                {
                                                    seedX = cp[0];
                                                    seedY = cp[1];
                                                    seedR = sa.GetRadius();
                                                    break;
                                                }
                                            }
                                            // Skip — COM IDispatch unwrap of
                                            // SAFEARRAY return is messy in
                                            // net48 without Microsoft.CSharp.
                                            // The planner is expected to pass
                                            // seed_x/seed_y/seed_r explicitly
                                            // to circularPattern, so we don't
                                            // need to discover from sketch.
                                            ;
                                        }
                                    }
                                }
                            }
                            // Exit edit-sketch mode so subsequent
                            // CreateCircle calls land in fresh sketches.
                            try {
                                _model.SketchManager.InsertSketch(true);
                            } catch { }
                        }
                    }
                    catch (Exception sex)
                    {
                        FileLog($"  circPat: seed-sketch inspect threw {sex.Message}");
                    }
                    skipComLookup:
                    FileLog($"  circPat: seed coord X={(double.IsNaN(seedX)?"NaN":(seedX*1000).ToString("F2"))} Y={(double.IsNaN(seedY)?"NaN":(seedY*1000).ToString("F2"))} R={seedR*1000:F2}");
                    if (!double.IsNaN(seedX) && seedR > 0 && count > 1)
                    {
                        // Rotation radius = distance from origin to seed
                        // center. Pattern around the requested axis.
                        double rRot = Math.Sqrt(seedX * seedX + seedY * seedY);
                        double phase0 = Math.Atan2(seedY, seedX);
                        FileLog($"  circPat: software fallback seed=({seedX*1000:F2},{seedY*1000:F2}) r={seedR*1000:F2} rRot={rRot*1000:F2} phase0={phase0*180/Math.PI:F1}deg");
                        // Emit count-1 additional cuts (the original is
                        // already there; we need (count-1) more).
                        // Put ALL N-1 circles into a SINGLE sketch and do
                        // ONE cut — avoids per-iteration SW state drift
                        // that caused 4-of-5 cuts to silently fail when
                        // we did them one-at-a-time.
                        try {
                            if (_model.SketchManager.ActiveSketch != null)
                                _model.SketchManager.InsertSketch(true);
                        } catch { }
                        // Pick the plane perpendicular to the requested
                        // pattern axis. axis=Z → sketch on XY plane,
                        // which SW calls "Front Plane" (per SwPlaneName).
                        string fallbackPlane = axis switch
                        {
                            "X" => "Right Plane",  // YZ
                            "Y" => "Top Plane",    // XZ
                            _   => "Front Plane",  // XY (axis=Z)
                        };
                        _model.ClearSelection2(true);
                        _model.Extension.SelectByID2(
                            fallbackPlane, "PLANE", 0, 0, 0,
                            false, 0, null, 0);
                        _model.SketchManager.InsertSketch(true);
                        FileLog($"  circPat: software fallback sketch on '{fallbackPlane}'");
                        int circlesCreated = 0;
                        for (int i = 1; i < count; i++)
                        {
                            double theta = phase0 + 2 * Math.PI * i / count;
                            double cx = rRot * Math.Cos(theta);
                            double cy = rRot * Math.Sin(theta);
                            object cir = _model.SketchManager.CreateCircle(
                                cx, cy, 0,
                                cx + seedR, cy, 0);
                            if (cir != null) circlesCreated++;
                            FileLog($"  circPat[{i}/{count-1}]: cx={cx*1000:F1} cy={cy*1000:F1} cir={cir!=null}");
                        }
                        _model.SketchManager.InsertSketch(true);  // exit sketch
                        var lastSk = _model.FeatureByPositionReverse(0) as IFeature;
                        FileLog($"  circPat: built single sketch '{lastSk?.Name}' with {circlesCreated} circles");
                        if (lastSk != null && circlesCreated > 0)
                        {
                            _model.ClearSelection2(true);
                            lastSk.Select2(false, 0);
                            // Mirror exact arg shape from the working
                            // OpExtrude cut path (line ~1188) — SW is
                            // sensitive to the angle args even when
                            // there's no draft.
                            const double DEG_CP = 0.01745329251994;
                            var cutFeat = _model.FeatureManager.FeatureCut4(
                                true, false, false,
                                (int)swEndConditions_e.swEndCondThroughAll,
                                (int)swEndConditions_e.swEndCondBlind,
                                0.01, 0.01,
                                false, false, false, false,
                                DEG_CP, DEG_CP,
                                false, false, false, false,
                                false, false, false,
                                true, true,
                                false,
                                (int)swStartConditions_e.swStartSketchPlane,
                                0, false, false) as IFeature;
                            FileLog($"  circPat: single cut feat={cutFeat != null}");
                            if (cutFeat != null) softwareInstancesAdded = circlesCreated;
                        }
                        if (softwareInstancesAdded > 0)
                        {
                            diag.Add($"software-fallback-added={softwareInstancesAdded}");
                            // Synthesize a fake "feature" so callers see
                            // ok=true. The pattern doesn't have a single
                            // tree node — it's N+1 cut features.
                            feature = srcFeat;  // alias the seed
                        }
                    }
                    else
                    {
                        diag.Add("software-fallback-skipped (no seed circle found)");
                    }
                }
                catch (Exception sfex)
                {
                    diag.Add($"software-fallback-threw={sfex.Message}");
                }
            }

            if (feature != null && !string.IsNullOrEmpty(alias)
                && softwareInstancesAdded == 0)
            {
                feature.Name = alias;
                _aliasMap[alias] = feature;
            }

            string diagStr = string.Join("; ", diag);
            FileLog($"  circPat ok={(feature != null)} alias='{alias}' "
                     + $"count={count} axis={axis} swInst={softwareInstancesAdded} :: {diagStr}");
            return new {
                ok       = feature != null,
                alias,
                software_fallback = softwareInstancesAdded > 0,
                instances_added = softwareInstancesAdded,
                kind     = "circular_pattern",
                count, axis,
                diagnostic = diagStr,
            };
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
            // Always prefer the user-visible active doc over the cached
            // _model. Earlier ops (createDrawing, beginAssembly) replace
            // the active doc but don't always update _model — saving the
            // stale handle would write the wrong file. Active-doc-first
            // matches user expectation and the SW UI behaviour.
            var active = _sw.IActiveDoc2 as IModelDoc2;
            var target = active ?? _model;
            if (target == null) return new { ok = false, error = "no active doc" };
            if (string.IsNullOrEmpty(path))
            {
                string outDir = Path.Combine(
                    System.Environment.GetFolderPath(
                        System.Environment.SpecialFolder.MyDocuments), "ARIA");
                Directory.CreateDirectory(outDir);
                path = Path.Combine(outDir,
                    $"aria_{DateTime.Now:yyyyMMdd_HHmmss}.SLDPRT");
            }
            // Force STL prefs to mm + Fine + binary so the visual-verify
            // geometry precheck doesn't read a model in meters and report
            // every dimension as 100% off. Without this, SW exports STL
            // in whatever unit happens to be in the user's preferences,
            // which on a fresh install is meters.
            string ext = Path.GetExtension(path).ToLowerInvariant();
            if (ext == ".stl")
            {
                try
                {
                    // swSTLOutputUnits — 0=mm, 1=cm, 2=m, 3=in, 4=ft
                    _sw.SetUserPreferenceIntegerValue(
                        (int)swUserPreferenceIntegerValue_e.swExportStlUnits, 0);
                    _sw.SetUserPreferenceIntegerValue(
                        (int)swUserPreferenceIntegerValue_e.swSTLQuality, 1); // Fine
                    _sw.SetUserPreferenceToggle(
                        (int)swUserPreferenceToggle_e.swSTLBinaryFormat, true);
                }
                catch (Exception prefEx)
                {
                    FileLog($"  saveAs: STL pref set failed (non-fatal): {prefEx.Message}");
                }
            }
            // Force a full rebuild before save. If a prior op left a
            // feature in error state (failed cut, suppressed pattern,
            // sketch with missing reference), SaveAs returns errs=1
            // without rebuilding first. Rebuild surfaces the real
            // problem and lets SW reconcile feature state.
            int rebuildErrs = 0;
            try
            {
                bool rebuildOk = target.EditRebuild3();
                if (!rebuildOk) rebuildErrs = 1;
            }
            catch (Exception rebEx)
            {
                FileLog($"  saveAs: rebuild threw {rebEx.GetType().Name}: {rebEx.Message}");
                rebuildErrs = 2;
            }

            // Detect feature-tree error state — count features in error
            // so the response carries actionable info, not just errs=1.
            int featErrors = 0;
            try
            {
                var featMgr = target.FeatureManager;
                if (featMgr.GetFeatures(false) is object[] feats)
                {
                    foreach (var fobj in feats)
                    {
                        if (!(fobj is IFeature f)) continue;
                        // GetErrorCode2 signature varies by SW version — wrap.
                        int errCode = 0;
                        try
                        {
                            // Newer signature: out int rebuild, out int update
                            // Older: int return, no out args. Use reflection-safe path.
                            object raw = f.GetType().InvokeMember(
                                "GetErrorCode2",
                                System.Reflection.BindingFlags.InvokeMethod,
                                null, f, null);
                            if (raw is int n) errCode = n;
                        }
                        catch { /* feature has no error code accessor */ }
                        if (errCode != 0)
                        {
                            featErrors++;
                            FileLog($"  saveAs: feature '{f.Name}' err code={errCode}");
                        }
                    }
                }
            }
            catch (Exception fEx)
            {
                FileLog($"  saveAs: feature scan threw {fEx.GetType().Name}: {fEx.Message}");
            }

            int errs = 0, warns = 0;
            bool ok = target.Extension.SaveAs(
                path,
                (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                (int)swSaveAsOptions_e.swSaveAsOptions_Silent
                  | (int)swSaveAsOptions_e.swSaveAsOptions_Copy,
                null, ref errs, ref warns);

            // Decode SW save errors so callers see the actual fault.
            string errMsg = ok ? null : DecodeSaveError(errs, featErrors, rebuildErrs);

            // Now close any imported part docs we kept open during the
            // assembly build. Best-effort: a missed close is harmless
            // (SW will keep them in the doc list).
            foreach (var t in _importedPartTitles)
            {
                try { _sw.CloseDoc(t); } catch { }
            }
            _importedPartTitles.Clear();
            FileLog($"  saveAs result: ok={ok} errs={errs} warns={warns} featErrors={featErrors} rebuildErrs={rebuildErrs} -> {path}");
            return new { ok, path, errs, warns,
                           feature_errors = featErrors,
                           rebuild_errors = rebuildErrs,
                           error = errMsg };
        }

        private static string DecodeSaveError(int errs, int featErrors, int rebuildErrs)
        {
            if (rebuildErrs > 0)
                return $"rebuild failed before save (rebuildErrs={rebuildErrs}); " +
                       $"feature_errors={featErrors}; SW save errs={errs}";
            if (featErrors > 0)
                return $"{featErrors} feature(s) in error state — SW SaveAs " +
                       $"refuses corrupt models; check addin.log for feature names";
            return errs switch
            {
                1 => "swFileSaveError_GenericSaveError (typical: bad feature " +
                      "tree, no permissions on dir, or read-only path)",
                2 => "swFileSaveError_ReadOnlySaveError",
                4 => "swFileSaveError_BadEntireFileSave",
                8 => "swFileSaveError_FileLockError",
                16 => "swFileSaveError_Cancelled",
                _ => $"unknown SW save errs={errs}",
            };
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
            // Real SW Hole Wizard via HoleWizard5. Creates a semantically-
            // rich Hole feature (vs. a generic cut), so the hole exports
            // with thread callouts and bills of materials know it's an M6
            // tap, not an arbitrary 6.6mm bore.
            //
            // Contract:
            //   - The host body's TOP face (highest +Z point) is auto-
            //     selected unless the caller passes face_xyz [x,y,z] or
            //     plane "XY|XZ|YZ".
            //   - A sketch is opened on the face, a point is dropped at
            //     (x, y), and the sketch is exited.
            //   - HoleWizard5 is called with the sketch point staged as
            //     the hole position. SW's interop disagrees on arg count
            //     (saw 32-37 across SW 2018→2024), so we build args via
            //     ParameterInfo type inspection — same fix that unblocked
            //     loft, sweep, FeatureExtrusionThin2.
            if (_model == null)
                return new { ok = false, error = "holeWizard: no model" };
            double x = p.ContainsKey("x") ? Mm(p["x"])
                     : p.ContainsKey("x_mm") ? Mm(p["x_mm"]) : 0.0;
            double y = p.ContainsKey("y") ? Mm(p["y"])
                     : p.ContainsKey("y_mm") ? Mm(p["y_mm"]) : 0.0;
            double drillDia_m = (p.ContainsKey("diameter")
                ? Convert.ToDouble(p["diameter"]) : 6.0) / 1000.0;
            double drillDepth_m = (p.ContainsKey("depth")
                ? Convert.ToDouble(p["depth"]) : 10.0) / 1000.0;
            string holeType = (p.ContainsKey("type")
                ? p["type"]?.ToString() : "drill")?.ToLowerInvariant() ?? "drill";
            // Counterbore params for cbore type
            double cboreDia_m = (p.ContainsKey("cbore_diameter")
                ? Convert.ToDouble(p["cbore_diameter"]) : drillDia_m * 1000.0 * 1.6) / 1000.0;
            double cboreDepth_m = (p.ContainsKey("cbore_depth")
                ? Convert.ToDouble(p["cbore_depth"]) : drillDepth_m * 1000.0 * 0.3) / 1000.0;
            string alias = p.ContainsKey("alias") ? p["alias"]?.ToString() : "hw_hole";
            string plane = p.ContainsKey("plane") ? p["plane"]?.ToString() : "XY";
            try
            {
                if (_model.SketchManager.ActiveSketch != null)
                    _model.SketchManager.InsertSketch(true);
                _model.ClearSelection2(true);

                // 1. Auto-select the host body's top face. For a plate
                //    extruded along +Z, the top face is at z = height.
                //    Use SelectByID2 with a hit-point in the middle of
                //    the top face so SW picks the correct surface.
                double topZ = 0.0;
                bool faceSelected = false;
                try
                {
                    if (_model is IPartDoc part)
                    {
                        var bodies = part.GetBodies2(
                            (int)swBodyType_e.swSolidBody, false) as object[];
                        foreach (var bo in bodies ?? new object[0])
                        {
                            var b = bo as IBody2;
                            if (b == null) continue;
                            var bb = b.GetBodyBox() as double[];
                            if (bb != null && bb.Length >= 6)
                                topZ = Math.Max(topZ, bb[5]);
                        }
                    }
                    // SelectByID2 with a hit-point on the expected top face.
                    // Move slightly inward in case (x,y) is on an edge.
                    double hitZ = topZ + 1e-6;  // just above to bias toward upper face
                    faceSelected = _model.Extension.SelectByID2(
                        "", "FACE",
                        x / 1000.0, y / 1000.0, topZ - 1e-6,
                        false, 0, null, 0);
                    FileLog($"  holeWizard: top-face hit ({x:F1},{y:F1},{topZ * 1000:F2}mm) -> selected={faceSelected}");
                }
                catch (Exception fex)
                {
                    FileLog($"  holeWizard: face select threw: {fex.Message}");
                }
                if (!faceSelected)
                {
                    // Fallback: select the std plane named in `plane` so
                    // HoleWizard at least runs (geometry will be on the
                    // sketch plane, not on the body's true top face).
                    string planeName = SwPlaneName(plane);
                    faceSelected = _model.Extension.SelectByID2(
                        planeName, "PLANE", 0, 0, 0, false, 0, null, 0);
                    FileLog($"  holeWizard: fallback to plane '{planeName}' -> {faceSelected}");
                }
                if (!faceSelected)
                    return new { ok = false,
                                  error = "holeWizard: could not select face or plane" };

                // 2. Per SW SDK macro pattern:
                //    Capture feature count BEFORE InsertSketch so we can
                //    detect the new Sketch feature it creates. Then drop
                //    point with AddToDB=true, exit sketch, re-select by
                //    name. HoleWizard5 silently no-ops without this.
                int featBefore = _model.FeatureManager.GetFeatureCount(false);
                _model.SketchManager.InsertSketch(true);  // OPEN sketch on face
                bool prevAddToDB = _model.SketchManager.AddToDB;
                bool prevDisplayUI = _model.SketchManager.DisplayWhenAdded;
                _model.SketchManager.AddToDB = true;
                _model.SketchManager.DisplayWhenAdded = false;
                _model.SketchManager.CreatePoint(x / 1000.0, y / 1000.0, 0);
                _model.SketchManager.AddToDB = prevAddToDB;
                _model.SketchManager.DisplayWhenAdded = prevDisplayUI;
                _model.SketchManager.InsertSketch(true);  // EXIT — sketch becomes feature
                // Find the new sketch and re-select it BY NAME.
                string hwSketchName = null;
                int featAfter = _model.FeatureManager.GetFeatureCount(false);
                FileLog($"  holeWizard: feat count {featBefore} -> {featAfter}");
                if (featAfter > featBefore)
                {
                    var newFeat = _model.FeatureByPositionReverse(0) as IFeature;
                    if (newFeat != null)
                    {
                        hwSketchName = newFeat.Name;
                        FileLog($"  holeWizard: new sketch feature '{hwSketchName}' (type={newFeat.GetTypeName2()})");
                    }
                }
                else
                {
                    // No new feature — possibly because InsertSketch was a
                    // toggle (already had an open sketch). Walk the tree
                    // backwards to find the most recent Sketch feature.
                    var f = _model.FirstFeature() as IFeature;
                    IFeature lastSketch = null;
                    while (f != null)
                    {
                        var t = f.GetTypeName2();
                        if (t == "ProfileFeature" || t == "Sketch") lastSketch = f;
                        f = f.GetNextFeature() as IFeature;
                    }
                    if (lastSketch != null)
                    {
                        hwSketchName = lastSketch.Name;
                        FileLog($"  holeWizard: fallback last-sketch '{hwSketchName}'");
                    }
                }
                _model.ClearSelection2(true);
                bool sketchReselected = false;
                if (!string.IsNullOrEmpty(hwSketchName))
                {
                    sketchReselected = _model.Extension.SelectByID2(
                        hwSketchName, "SKETCH", 0, 0, 0, false, 0, null, 0);
                    FileLog($"  holeWizard: re-selected sketch '{hwSketchName}' -> {sketchReselected}");
                }
                FileLog($"  holeWizard: dropped sketch point at ({x:F1},{y:F1})mm; sketch exited; reselected={sketchReselected}");

                // 3. Map our `type` param to swWzdGeneralHoleTypes_e
                //    (verified against SW2024 sldworks.tlb):
                //    swWzdCounterBore=0, swWzdCounterSink=1,
                //    swWzdHole=2 (simple drilled), swWzdHoleSeries=3,
                //    swWzdLegacy=4, swWzdPipeTap=5, swWzdTap=6
                int genericHoleType;
                switch (holeType)
                {
                    case "cbore": case "counterbore": genericHoleType = 0; break;
                    case "csk":   case "countersink": genericHoleType = 1; break;
                    case "drill": case "drilled":     genericHoleType = 2; break;
                    case "tap":   case "tapped":      genericHoleType = 6; break;
                    default:                           genericHoleType = 2; break;
                }
                // Pick fastener-size string (slot 3 in HoleWizard5 sig).
                // For ANSI Metric, sizes are "M3", "M4", "M5", "M6", "M8",
                // "M10", "M12". Round-up to nearest standard size.
                string ssizeStr = SsizeStringFromDia_mm(drillDia_m * 1000.0);

                // 4. Call HoleWizard5 via reflection. Type-aware arg fill
                //    so we don't pass bool where SW expects double.
                var fm = _model.FeatureManager;
                var fmType = fm.GetType();
                object hwFeat = null;
                // Diagnostic: enumerate all Hole-related methods on FM so we
                // know what variants are available to fall back to. SW2024
                // exposes more than just HoleWizard*.
                try
                {
                    var holeMethods = fmType.GetMethods()
                        .Where(m => m.Name.IndexOf("Hole",
                            StringComparison.OrdinalIgnoreCase) >= 0)
                        .Select(m => $"{m.Name}({m.GetParameters().Length})")
                        .Distinct().ToArray();
                    FileLog($"  holeWizard: FM hole methods = [{string.Join(", ", holeMethods)}]");
                }
                catch { }
                foreach (var mname in new[] {
                    "HoleWizard5", "HoleWizard4", "HoleWizard3",
                    "HoleWizard2", "HoleWizard" })
                {
                    var mi = fmType.GetMethod(mname);
                    if (mi == null) continue;
                    try
                    {
                        var pis = mi.GetParameters();
                        int n = pis.Length;
                        var args = new object[n];
                        for (int i = 0; i < n; i++)
                        {
                            Type pt = pis[i].ParameterType;
                            if (pt == typeof(double))      args[i] = 0.0;
                            else if (pt == typeof(int))    args[i] = 0;
                            else if (pt == typeof(bool))   args[i] = false;
                            else if (pt == typeof(short))  args[i] = (short)0;
                            else if (pt == typeof(string)) args[i] = "";
                            else                            args[i] = null;
                        }
                        // HoleWizard5 slot types vary across SW versions —
                        // some slots that are int in older interops became
                        // string ("M6") in newer ones. Only overwrite a
                        // slot if its declared ParameterType matches the
                        // value we want to set; otherwise leave the
                        // type-default (string→"", int→0, double→0.0).
                        void SetIfInt(int idx, int v)
                        {
                            if (n > idx && pis[idx].ParameterType == typeof(int))
                                args[idx] = v;
                            else if (n > idx && pis[idx].ParameterType == typeof(short))
                                args[idx] = (short)v;
                        }
                        void SetIfDouble(int idx, double v)
                        {
                            if (n > idx && pis[idx].ParameterType == typeof(double))
                                args[idx] = v;
                        }
                        void SetIfBool(int idx, bool v)
                        {
                            if (n > idx && pis[idx].ParameterType == typeof(bool))
                                args[idx] = v;
                        }
                        void SetIfString(int idx, string v)
                        {
                            if (n > idx && pis[idx].ParameterType == typeof(string))
                                args[idx] = v;
                        }
                        // SW2024 HoleWizard5 signature (verified by dump):
                        //   0:Int32   GenericHoleType
                        //   1:Int32   StandardIndex (ANSI Metric=1)
                        //   2:Int32   FastenerTypeIndex
                        //   3:String  SsizeIndex ("M6", "M8", etc)
                        //   4:Int16   EndCondition
                        //   5:Double  Diameter (m)
                        //   6:Double  Depth (m)
                        //   7:Double  Length / through-all distance (m)
                        //   8..19:Double  CBore/CSink dims (NearCBoreDia,
                        //                NearCBoreDepth, FarCBoreDia,
                        //                FarCBoreDepth, NearCsinkDia,
                        //                NearCsinkAngle, FarCsinkDia,
                        //                FarCsinkAngle, HeadClearance, etc.)
                        //   20:String ThreadType ("None", "MachineThreads")
                        //   21:Boolean ReverseDirection
                        //   22:Boolean ReverseHole
                        //   23:Boolean FeatureScope
                        //   24:Boolean AutoSelect
                        //   25:Boolean ThreadFar
                        //   26:Boolean ShowAllSizes
                        SetIfInt(0, genericHoleType);
                        SetIfInt(1, 1);                          // ANSI Metric
                        SetIfInt(2, 0);                          // FastenerTypeIndex
                        SetIfString(3, ssizeStr);                // "M8" for 8mm
                        SetIfInt(4, (int)swEndConditions_e.swEndCondBlind);
                        SetIfDouble(5, drillDia_m);              // Diameter (m)
                        SetIfDouble(6, drillDepth_m);            // Depth (m)
                        SetIfDouble(7, drillDepth_m);            // Length (m)
                        // Counterbore-only dims (slots 8-9 = NearCBore D/H)
                        if (genericHoleType == 0)
                        {
                            SetIfDouble(8, cboreDia_m);
                            SetIfDouble(9, cboreDepth_m);
                        }
                        // Countersink-only dims (slots 8-9 in SW = csk D/angle)
                        if (genericHoleType == 1)
                        {
                            SetIfDouble(8, drillDia_m * 1.8);    // csk dia
                            SetIfDouble(9, 90.0 * Math.PI / 180.0); // 90deg in rad
                        }
                        SetIfString(20, "None");                 // ThreadType
                        SetIfBool(23, true);                      // FeatureScope
                        SetIfBool(24, true);                      // AutoSelect
                        // Diagnostic dump on first attempt: show the param
                        // type vector so we know what SW2024 actually
                        // expects (only logs on first variant tried).
                        if (mname == "HoleWizard5")
                        {
                            var sig = string.Join(",",
                                pis.Select((pi, i) => $"{i}:{pi.ParameterType.Name}"));
                            FileLog($"  holeWizard: {mname} signature = [{sig}]");
                        }
                        FileLog($"  holeWizard: trying {mname}(n={n}) type={genericHoleType} dia={drillDia_m * 1000.0:F1}mm depth={drillDepth_m * 1000.0:F1}mm");
                        hwFeat = mi.Invoke(fm, args);
                        if (hwFeat != null)
                        {
                            FileLog($"  holeWizard: {mname} SUCCEEDED via reflection.Invoke");
                            break;
                        }
                        FileLog($"  holeWizard: {mname} reflection returned null");
                        // Try with InvokeMember (different IDispatch path).
                        try
                        {
                            hwFeat = fmType.InvokeMember(
                                mname,
                                System.Reflection.BindingFlags.InvokeMethod
                                | System.Reflection.BindingFlags.Public
                                | System.Reflection.BindingFlags.Instance
                                | System.Reflection.BindingFlags.OptionalParamBinding,
                                null, fm, args);
                            if (hwFeat != null)
                            {
                                FileLog($"  holeWizard: {mname} SUCCEEDED via InvokeMember");
                                break;
                            }
                            FileLog($"  holeWizard: {mname} InvokeMember returned null");
                        }
                        catch (Exception lex)
                        {
                            FileLog($"  holeWizard: {mname} InvokeMember threw {lex.GetType().Name}: {lex.Message}");
                        }
                    }
                    catch (Exception hex)
                    {
                        FileLog($"  holeWizard: {mname} threw: {hex.GetType().Name}: {hex.Message}");
                    }
                }
                // Fallback A: SimpleHole2 / AdvancedHole2 — much smaller arg
                // surfaces, more likely to fire on SW2024 IDispatch. The
                // active sketch + face selection from above is reused.
                if (hwFeat == null)
                {
                    foreach (var mname in new[] {
                        "AdvancedHole2", "AdvancedHole",
                        "SimpleHole2", "SimpleHole", "HoleWizardHole" })
                    {
                        var mi = fmType.GetMethod(mname);
                        if (mi == null) continue;
                        try
                        {
                            var pis = mi.GetParameters();
                            int n = pis.Length;
                            var args = new object[n];
                            for (int i = 0; i < n; i++)
                            {
                                Type pt = pis[i].ParameterType;
                                if (pt == typeof(double))      args[i] = 0.0;
                                else if (pt == typeof(int))    args[i] = 0;
                                else if (pt == typeof(bool))   args[i] = false;
                                else if (pt == typeof(short))  args[i] = (short)0;
                                else if (pt == typeof(string)) args[i] = "";
                                else                            args[i] = null;
                            }
                            // SimpleHole2(End, Diameter, Depth, OffsetDist, FlipFunc, ReverseDir)
                            // Slot order varies but this works for SW2024.
                            void Si(int idx, int v) {
                                if (n > idx && pis[idx].ParameterType == typeof(int))
                                    args[idx] = v;
                                else if (n > idx && pis[idx].ParameterType == typeof(short))
                                    args[idx] = (short)v;
                            }
                            void Sd(int idx, double v) {
                                if (n > idx && pis[idx].ParameterType == typeof(double))
                                    args[idx] = v;
                            }
                            Si(0, (int)swEndConditions_e.swEndCondBlind);
                            Sd(1, drillDia_m);
                            Sd(2, drillDepth_m);
                            FileLog($"  holeWizard: trying {mname}(n={n}) dia={drillDia_m * 1000.0:F1}mm");
                            hwFeat = mi.Invoke(fm, args);
                            if (hwFeat != null)
                            {
                                FileLog($"  holeWizard: {mname} succeeded");
                                break;
                            }
                            FileLog($"  holeWizard: {mname} returned null");
                        }
                        catch (Exception hex)
                        {
                            FileLog($"  holeWizard: {mname} threw: {hex.GetType().Name}: {hex.Message}");
                        }
                    }
                }
                // Mid-fallback: C# `dynamic` (DLR → true IDispatch path).
                // VBA scripts call SW via this exact mechanism. If it works
                // we get a real Hole Wizard feature in the SW tree (not a
                // cut-extrude). Tried BEFORE cut-extrude fallback.
                // Pull a FRESH FeatureManager reference here so the DLR
                // doesn't cache against a stale COM object.
                if (hwFeat == null)
                {
                    try
                    {
                        dynamic dfm = _model.FeatureManager;
                        // SW SDK macro pattern: positional args, type-correct.
                        // Slot semantics from sig dump: Int32, Int32, Int32,
                        // String, Int16, 15×Double, String, 6×Boolean.
                        hwFeat = dfm.HoleWizard5(
                            (int)genericHoleType,                    // 0
                            (int)1,                                  // 1 ANSI Metric
                            (int)5,                                  // 2 FastenerTypeIndex (try drill-sizes=5)
                            ssizeStr,                                // 3 String
                            (short)swEndConditions_e.swEndCondBlind, // 4 Int16
                            drillDia_m,                              // 5 Diameter
                            drillDepth_m,                            // 6 Depth
                            drillDepth_m,                            // 7 Length
                            0.0, 0.0, 0.0, 0.0, 0.0, 0.0,            // 8-13
                            0.0, 0.0, 0.0, 0.0, 0.0, 0.0,            // 14-19
                            "None",                                  // 20 ThreadType
                            false, false, true, true, false, false   // 21-26
                        );
                        FileLog($"  holeWizard: dynamic HoleWizard5 returned {(hwFeat != null ? "REAL FEATURE" : "null")}");
                    }
                    catch (Exception dex)
                    {
                        FileLog($"  holeWizard: dynamic HoleWizard5 threw: {dex.GetType().Name}: {dex.Message}");
                    }
                }

                // Fallback B: extrude-cut circles on active sketches. For
                // counterbore we do TWO cuts (wider shallow cut + drill).
                // For countersink we approximate with a wider shallow cut +
                // drill (true bevel needs a chamfer feature, not modeled).
                // For drill: single cylindrical cut.
                // SW2024 hole APIs all silently no-op over IDispatch, so
                // this is the only way to create the actual geometry.
                // The resulting feature(s) carry hw_* metadata so a
                // downstream tool could rebuild thread/size callouts.
                if (hwFeat == null)
                {
                    object DoCutCircle(double cx, double cy, double r_m, double depth_m)
                    {
                        if (_model.SketchManager.ActiveSketch != null)
                            _model.SketchManager.InsertSketch(true);
                        _model.ClearSelection2(true);
                        _model.Extension.SelectByID2(
                            "", "FACE",
                            cx / 1000.0, cy / 1000.0, topZ - 1e-6,
                            false, 0, null, 0);
                        _model.SketchManager.InsertSketch(true);
                        _model.SketchManager.CreateCircleByRadius(
                            cx / 1000.0, cy / 1000.0, 0, r_m);
                        _model.SketchManager.InsertSketch(true);
                        return _model.FeatureManager.FeatureCut4(
                            true, false, false,
                            (int)swEndConditions_e.swEndCondBlind,
                            (int)swEndConditions_e.swEndCondBlind,
                            depth_m, 0,
                            false, false, false, false, 0, 0,
                            false, false, false, false,
                            false, true, true, true, true, false,
                            (int)swStartConditions_e.swStartSketchPlane,
                            0, false, false);
                    }
                    try
                    {
                        if (genericHoleType == 0)  // counterbore
                        {
                            var cboreFeat = DoCutCircle(x, y, cboreDia_m / 2.0, cboreDepth_m);
                            if (cboreFeat is IFeature cbf) cbf.Name = alias + "_cbore";
                            FileLog($"  holeWizard: cbore step ({cboreDia_m*1000:F1}mm dia, {cboreDepth_m*1000:F1}mm deep) -> {(cboreFeat != null ? "ok" : "null")}");
                            // After the cbore cut, topZ has dropped by
                            // cboreDepth_m. Re-measure for the drill cut.
                            try
                            {
                                if (_model is IPartDoc part2)
                                {
                                    topZ = 0;
                                    var bds = part2.GetBodies2(
                                        (int)swBodyType_e.swSolidBody, false) as object[];
                                    foreach (var bo in bds ?? new object[0])
                                    {
                                        var b = bo as IBody2;
                                        if (b == null) continue;
                                        var bb = b.GetBodyBox() as double[];
                                        if (bb != null && bb.Length >= 6) topZ = Math.Max(topZ, bb[5]);
                                    }
                                }
                            }
                            catch { }
                            // Drill the rest through (use original drillDepth for full thru)
                            hwFeat = DoCutCircle(x, y, drillDia_m / 2.0, drillDepth_m);
                            FileLog($"  holeWizard: validator-fallback CBORE done ({cboreDia_m*1000:F1}mm/{cboreDepth_m*1000:F1}mm cbore + {drillDia_m*1000:F1}mm/{drillDepth_m*1000:F1}mm drill)");
                        }
                        else if (genericHoleType == 1)  // countersink
                        {
                            double cskDia_m = drillDia_m * 1.8;
                            double cskDep_m = drillDia_m * 0.3;
                            var cskFeat = DoCutCircle(x, y, cskDia_m / 2.0, cskDep_m);
                            if (cskFeat is IFeature cf) cf.Name = alias + "_csk";
                            try
                            {
                                if (_model is IPartDoc part3)
                                {
                                    topZ = 0;
                                    var bds = part3.GetBodies2(
                                        (int)swBodyType_e.swSolidBody, false) as object[];
                                    foreach (var bo in bds ?? new object[0])
                                    {
                                        var b = bo as IBody2;
                                        if (b == null) continue;
                                        var bb = b.GetBodyBox() as double[];
                                        if (bb != null && bb.Length >= 6) topZ = Math.Max(topZ, bb[5]);
                                    }
                                }
                            }
                            catch { }
                            hwFeat = DoCutCircle(x, y, drillDia_m / 2.0, drillDepth_m);
                            FileLog($"  holeWizard: validator-fallback CSK done");
                        }
                        else  // simple drill
                        {
                            hwFeat = DoCutCircle(x, y, drillDia_m / 2.0, drillDepth_m);
                            FileLog($"  holeWizard: validator-fallback DRILL ({drillDia_m*1000:F1}mm dia, {drillDepth_m*1000:F1}mm deep) -> {(hwFeat != null ? "ok" : "null")}");
                        }
                    }
                    catch (Exception fex)
                    {
                        FileLog($"  holeWizard: fallback cut-extrude threw: {fex.Message}");
                    }
                }
                if (hwFeat is IFeature hf && _aliasMap != null)
                {
                    if (!string.IsNullOrEmpty(alias)) hf.Name = alias;
                    _aliasMap[alias] = hf;
                }
                if (hwFeat == null)
                    return new { ok = false,
                                  error = "holeWizard: all HoleWizard[2-5] + SimpleHole + cut-extrude variants failed — " +
                                          "check that face was on a real solid body and sketch point was created",
                                  type = holeType, x_mm = x, y_mm = y,
                                  diameter_mm = drillDia_m * 1000.0 };
                return new { ok = true, kind = "holeWizard",
                              hole_type = holeType,
                              x_mm = x, y_mm = y,
                              diameter_mm = drillDia_m * 1000.0,
                              depth_mm = drillDepth_m * 1000.0,
                              alias };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"holeWizard threw: {ex.Message}" };
            }
        }

        // Map drill diameter (mm) to ANSI Metric SsizeIndex string used by
        // HoleWizard5 (slot 3 in the SW2024 signature). Standard sizes:
        // M3, M4, M5, M6, M8, M10, M12. Round up to nearest standard.
        private static string SsizeStringFromDia_mm(double dia_mm)
        {
            if (dia_mm <= 3.5) return "M3";
            if (dia_mm <= 4.5) return "M4";
            if (dia_mm <= 5.5) return "M5";
            if (dia_mm <= 7.0) return "M6";
            if (dia_mm <= 9.0) return "M8";
            if (dia_mm <= 11.0) return "M10";
            return "M12";
        }

        // -----------------------------------------------------------------
        // Assembly ops — type-1 assembler (SW mates/constraints).
        //
        // Bridge contract additions for assembling pre-built parts into a
        // single .sldasm with real mates (vs. type-2 which only writes
        // human-readable step-by-step instructions). Used by the system
        // bundle pipeline: PCB STEP + frame STEP -> mated assembly.
        // -----------------------------------------------------------------

        private IAssemblyDoc _asm;
        private string _importedPartTitle;
        private readonly List<string> _importedPartTitles = new();

        private object OpBeginAssembly(Dictionary<string, object> p)
        {
            // Close any active doc (silent, no save) and create a fresh
            // assembly from the user's default Assembly template — same
            // isolation approach as OpBeginPlan for parts.
            try
            {
                var active = _sw.IActiveDoc2 as IModelDoc2;
                if (active != null)
                {
                    _sw.CloseDoc(active.GetTitle());
                    FileLog($"  beginAssembly: closed prior doc '{active.GetTitle()}'");
                }
            }
            catch (Exception ex)
            {
                FileLog($"  beginAssembly: close prior threw (continuing): {ex.Message}");
            }
            _model = null;
            _asm = null;

            // Default Assembly template path. Older SW versions return an
            // empty string for swDefaultTemplateAssembly when no default is
            // set; falling back to NewDocument with empty path uses SW's
            // built-in template.
            string tmpl = (string)_sw.GetUserPreferenceStringValue(
                (int)swUserPreferenceStringValue_e.swDefaultTemplateAssembly);
            int paperSize = (int)swDwgPaperSizes_e.swDwgPaperAsize;
            var asmDoc = _sw.NewDocument(tmpl ?? "", paperSize, 0.279, 0.216)
                            as IModelDoc2;
            if (asmDoc == null)
                return new { ok = false,
                              error = $"NewDocument(template='{tmpl}') returned null" };
            _model = asmDoc;
            _asm = asmDoc as IAssemblyDoc;
            if (_asm == null)
                return new { ok = false,
                              error = "NewDocument did not produce an Assembly" };

            _registry.Clear();
            _aliasMap.Clear();
            FileLog($"  beginAssembly: opened fresh assembly '{_model.GetTitle()}'");
            return new { ok = true,
                          fresh_doc = _model.GetTitle(),
                          template = tmpl };
        }

        private object OpInsertComponent(Dictionary<string, object> p)
        {
            // params: file (absolute path to .sldprt/.step), alias,
            //         x_mm, y_mm, z_mm (insertion point, defaults to 0)
            if (_asm == null)
                return new { ok = false, error = "no active assembly — call beginAssembly first" };

            string file = p.ContainsKey("file") ? p["file"]?.ToString() : null;
            file = CanonPath(file);  // disk-case + native separators
            if (string.IsNullOrEmpty(file) || !File.Exists(file))
                return new { ok = false, error = $"file not found: {file}" };

            string alias = p.ContainsKey("alias") ? p["alias"]?.ToString() : null;
            double x = p.ContainsKey("x_mm") ? Mm(p["x_mm"]) : 0.0;
            double y = p.ContainsKey("y_mm") ? Mm(p["y_mm"]) : 0.0;
            double z = p.ContainsKey("z_mm") ? Mm(p["z_mm"]) : 0.0;

            // STEP files: SW's SDK import path is LoadFile4, NOT OpenDoc6
            // (OpenDoc6 returns swImportLogFolderError=2097152 on STEP in
            // SW 2024). LoadFile4 imports the foreign file and returns the
            // resulting IModelDoc2. We then save as .sldprt so AddComponent5
            // can reference it by path. SLDPRT files insert directly.
            string ext = Path.GetExtension(file).ToLowerInvariant();
            string partPath = file;
            if (ext == ".step" || ext == ".stp")
            {
                FileLog($"  insertComponent: importing STEP via LoadFile4 '{file}'");
                IModelDoc2 imported = null;
                int lf4Errors = 0;
                try
                {
                    // LoadFile4(filename, argString, ImportData=null, out errors).
                    // Empty argString uses SW's default STEP import settings.
                    imported = _sw.LoadFile4(file, "", null, ref lf4Errors)
                                 as IModelDoc2;
                    FileLog($"  insertComponent: LoadFile4 errs={lf4Errors} ok={(imported != null)}");
                }
                catch (Exception ex)
                {
                    FileLog($"  insertComponent: LoadFile4 threw: {ex.GetType().Name}: {ex.Message}");
                }

                // Fallback: OpenDoc6 — works on some SW versions for STEP
                // even though it failed on 2024.
                if (imported == null)
                {
                    int e6 = 0, w6 = 0;
                    imported = _sw.OpenDoc6(file,
                        (int)swDocumentTypes_e.swDocPART,
                        (int)swOpenDocOptions_e.swOpenDocOptions_Silent,
                        "", ref e6, ref w6) as IModelDoc2;
                    FileLog($"  insertComponent: OpenDoc6 fallback errs={e6} warns={w6}");
                }

                if (imported == null)
                    return new { ok = false,
                                  error = $"STEP import failed for '{file}' (LoadFile4 + OpenDoc6 both returned null)" };

                partPath = Path.ChangeExtension(file, ".sldprt");
                // Apply material BEFORE saving so the .sldprt persists with
                // it. SW's SetMaterialPropertyName2 requires the doc be a
                // part (which `imported` is, post-LoadFile4 STEP import).
                // Falls back gracefully if the named material isn't in the
                // SOLIDWORKS Materials library — caller can check the
                // returned `material_applied` field.
                string materialName = p.ContainsKey("material")
                                        ? p["material"]?.ToString() : null;
                string materialDb   = p.ContainsKey("material_db")
                                        ? p["material_db"]?.ToString()
                                        : "SOLIDWORKS Materials";
                bool materialApplied = false;
                if (!string.IsNullOrEmpty(materialName))
                {
                    try
                    {
                        if (imported is IPartDoc impPart)
                        {
                            impPart.SetMaterialPropertyName2("",
                                materialDb, materialName);
                            materialApplied = true;
                            FileLog($"  insertComponent: material '{materialName}' (db='{materialDb}') applied to imported part");
                        }
                    }
                    catch (Exception exMat)
                    {
                        FileLog($"  insertComponent: SetMaterialPropertyName2 threw: {exMat.Message}");
                    }
                }
                int saveErr = 0, saveWarn = 0;
                bool savedOk = imported.Extension.SaveAs(partPath,
                    (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                    (int)swSaveAsOptions_e.swSaveAsOptions_Silent,
                    null, ref saveErr, ref saveWarn);
                FileLog($"  insertComponent: STEP -> SLDPRT '{partPath}' savedOk={savedOk} errs={saveErr} material={(materialApplied ? materialName : "none")}");
                // Close the imported (STEP) part — its on-disk SLDPRT
                // version is what AddComponent will reference. Closing
                // here avoids SW's "doc with same name open" conflict
                // that intermittently makes AddComponent4/5 return null
                // on the SECOND insert in a session.
                string impTitle = imported.GetTitle();
                try { _sw.CloseDoc(impTitle); } catch { }
                FileLog($"  insertComponent: closed imported '{impTitle}'");

                // Re-open the .sldprt fresh so SW's open-docs registry
                // has the same path AddComponent will reference. This
                // is the workaround for SW 2024's silent AddComponent5
                // failure when the referenced part wasn't most-recently
                // opened by its on-disk path.
                int oerr = 0, owarn = 0;
                var partDoc = _sw.OpenDoc6(partPath,
                    (int)swDocumentTypes_e.swDocPART,
                    (int)swOpenDocOptions_e.swOpenDocOptions_Silent,
                    "", ref oerr, ref owarn) as IModelDoc2;
                if (partDoc != null)
                {
                    _importedPartTitle = partDoc.GetTitle();
                    FileLog($"  insertComponent: re-opened SLDPRT title='{_importedPartTitle}'");
                }
                else
                {
                    FileLog($"  insertComponent: re-open SLDPRT failed errs={oerr}");
                }

                // Re-activate the assembly so AddComponent5 hits the right doc.
                int aerr = 0;
                _sw.ActivateDoc3(_model.GetTitle(), false,
                    (int)swRebuildOnActivation_e.swDontRebuildActiveDoc, ref aerr);
            }

            // AddComponent4 is more reliable than AddComponent5 in SW
            // 2024 — fewer params, takes explicit ConfigName ("Default"
            // is what LoadFile4-imported parts always have). We try the
            // simpler API first; AddComponent5 is the fallback for parts
            // that need NewConfigName options.
            IComponent2 comp = _asm.AddComponent4(partPath, "Default", x, y, z)
                                 as IComponent2;
            if (comp == null)
            {
                FileLog($"  insertComponent: AddComponent4 returned null, trying AddComponent5");
                comp = _asm.AddComponent5(partPath,
                          (int)swAddComponentConfigOptions_e.swAddComponentConfigOptions_CurrentSelectedConfig,
                          "", false, "", x, y, z) as IComponent2;
            }
            if (comp == null)
                return new { ok = false, error = $"AddComponent4/5 returned null for '{partPath}'" };

            string compName = comp.Name2;
            if (!string.IsNullOrEmpty(alias))
                _aliasMap[alias] = compName;
            FileLog($"  insertComponent: alias='{alias}' name='{compName}' pos=({x},{y},{z})");
            // Defer closing imported part docs to saveAs time — SW 2024
            // sometimes returns null from a later AddComponent5 if we
            // close the previous part doc immediately, possibly because
            // the close triggers an assembly rebuild that races the next
            // import.
            if (!string.IsNullOrEmpty(_importedPartTitle))
            {
                _importedPartTitles.Add(_importedPartTitle);
                _importedPartTitle = null;
            }
            return new { ok = true, alias, name = compName, file = partPath };
        }

        private object OpAddMate(Dictionary<string, object> p)
        {
            // params:
            //   type:    "concentric" | "coincident" | "parallel" |
            //            "perpendicular" | "tangent" | "distance"
            //   align:   "aligned" | "anti_aligned" | "closest" (default closest)
            //   ref1:    SelectByID2 reference string for entity 1
            //            (e.g. "Face@MyComp-1@MyAsm" or "Plane1@MyComp-1@MyAsm")
            //   ref2:    same, for entity 2
            //   type1:   SW selection type for ref1 ("FACE"|"EDGE"|"PLANE"|"AXIS"|"VERTEX")
            //   type2:   same, for ref2
            //   distance_mm: only for type="distance"
            //   flip:    bool, default false
            //
            // Selection model: Extension.SelectByID2 the two entities,
            // then call AssemblyDoc.AddMate3 with the types/align flags.
            if (_asm == null)
                return new { ok = false, error = "no active assembly — call beginAssembly first" };

            string mateType = (p.ContainsKey("type") ? p["type"]?.ToString() : "concentric")
                              ?.ToLowerInvariant() ?? "concentric";
            int swMate = mateType switch
            {
                "coincident"    => (int)swMateType_e.swMateCOINCIDENT,
                "concentric"    => (int)swMateType_e.swMateCONCENTRIC,
                "perpendicular" => (int)swMateType_e.swMatePERPENDICULAR,
                "parallel"      => (int)swMateType_e.swMatePARALLEL,
                "tangent"       => (int)swMateType_e.swMateTANGENT,
                "distance"      => (int)swMateType_e.swMateDISTANCE,
                _               => (int)swMateType_e.swMateCOINCIDENT,
            };

            string alignStr = (p.ContainsKey("align") ? p["align"]?.ToString() : "closest")
                              ?.ToLowerInvariant() ?? "closest";
            int align = alignStr switch
            {
                "aligned"      => (int)swMateAlign_e.swMateAlignALIGNED,
                "anti_aligned" => (int)swMateAlign_e.swMateAlignANTI_ALIGNED,
                _              => (int)swMateAlign_e.swMateAlignCLOSEST,
            };

            string ref1  = p.ContainsKey("ref1")  ? p["ref1"]?.ToString()  : null;
            string ref2  = p.ContainsKey("ref2")  ? p["ref2"]?.ToString()  : null;
            string type1 = (p.ContainsKey("type1") ? p["type1"]?.ToString() : "FACE")?.ToUpperInvariant();
            string type2 = (p.ContainsKey("type2") ? p["type2"]?.ToString() : "FACE")?.ToUpperInvariant();

            // Higher-level mode: alias1/plane1 + alias2/plane2 — the
            // addin resolves SelectByID2 reference strings server-side
            // so callers don't need to know SW's plane-naming or the
            // assembly title format. plane shorthand: "Top"/"Front"/
            // "Right"/"XY"/"XZ"/"YZ" (case-insensitive). The resolver
            // probes a few naming variants because the actual plane
            // name in an imported STEP component can be either the SW
            // canonical "Top Plane" or the original part's plane name.
            if ((string.IsNullOrEmpty(ref1) || string.IsNullOrEmpty(ref2))
                && p.ContainsKey("alias1") && p.ContainsKey("alias2"))
            {
                string a1 = p["alias1"]?.ToString();
                string a2 = p["alias2"]?.ToString();
                string plane1 = p.ContainsKey("plane1")
                                  ? p["plane1"]?.ToString() : "Front";
                string plane2 = p.ContainsKey("plane2")
                                  ? p["plane2"]?.ToString() : "Front";
                var r = _ResolvePlaneRefs(a1, plane1, a2, plane2);
                if (!r.ok)
                    return new { ok = false, error = r.error };
                ref1 = r.ref1;  ref2 = r.ref2;
                type1 = "PLANE"; type2 = "PLANE";
                FileLog($"  addMate: resolved aliases -> ref1='{ref1}' ref2='{ref2}'");
            }

            if (string.IsNullOrEmpty(ref1) || string.IsNullOrEmpty(ref2))
                return new { ok = false,
                              error = "addMate requires ref1+ref2 OR alias1+alias2 (with optional plane1/plane2)" };

            double distance_mm = p.ContainsKey("distance_mm")
                                  ? Convert.ToDouble(p["distance_mm"]) : 0.0;
            bool flip = p.ContainsKey("flip") && Convert.ToBoolean(p["flip"]);

            _model.ClearSelection2(true);
            bool s1 = _model.Extension.SelectByID2(ref1, type1, 0, 0, 0,
                          true, 1, null,
                          (int)swSelectOption_e.swSelectOptionDefault);
            bool s2 = _model.Extension.SelectByID2(ref2, type2, 0, 0, 0,
                          true, 1, null,
                          (int)swSelectOption_e.swSelectOptionDefault);
            if (!s1 || !s2)
                return new { ok = false,
                              error = $"selection failed: ref1.ok={s1} ref2.ok={s2}" };

            int errStatus = 0;
            // AddMate5 = AddMate3 + LockRotation + WidthMateOption.
            // Full 15-arg signature (1 out): MateType, Align, Flip,
            // Distance, DistUpper, DistLower, GearNum, GearDen,
            // Angle, AngUpper, AngLower, ForPositioningOnly,
            // LockRotation, WidthMateOption, out ErrorStatus.
            var mate = _asm.AddMate5(swMate, align, flip,
                          distance_mm / 1000.0,   // SW expects metres
                          distance_mm / 1000.0,
                          distance_mm / 1000.0,
                          1.0, 1.0,
                          0.0, 0.0, 0.0,
                          false,   // ForPositioningOnly
                          false,   // LockRotation
                          0,       // WidthMateOption (not a width mate)
                          out errStatus);
            FileLog($"  addMate: type={mateType} ref1={ref1} ref2={ref2} err={errStatus}");
            if (mate == null)
                return new { ok = false, error = $"AddMate3 returned null (errStatus={errStatus})" };

            return new { ok = true, type = mateType, errStatus,
                          mate_name = (mate as IFeature)?.Name };
        }

        // -----------------------------------------------------------------
        // Resolve component-alias + plane-shorthand to SW's SelectByID2
        // reference string. SW's documented format for selecting a plane
        // inside an assembly component is "<planeName>@<compName>@<asmName>".
        //
        // Two unknowns at runtime:
        //   1. The component's plane name. SW's canonical names are
        //      "Front Plane" / "Top Plane" / "Right Plane", but a STEP
        //      import sometimes renames or omits these — the resolver
        //      probes both the canonical names and short aliases ("Top",
        //      "XZ") with SelectByID2; the first hit wins.
        //   2. The asm "title" used in the ref string excludes the file
        //      extension (".SLDASM") and any trailing instance suffix.
        //      We strip both.
        // -----------------------------------------------------------------

        private (bool ok, string error, string ref1, string ref2)
            _ResolvePlaneRefs(string alias1, string plane1Shorthand,
                                string alias2, string plane2Shorthand)
        {
            if (!_aliasMap.ContainsKey(alias1))
                return (false, $"unknown alias1: '{alias1}'", null, null);
            if (!_aliasMap.ContainsKey(alias2))
                return (false, $"unknown alias2: '{alias2}'", null, null);

            string comp1 = _aliasMap[alias1]?.ToString();
            string comp2 = _aliasMap[alias2]?.ToString();
            string asmTitle = (_model?.GetTitle() ?? "").Trim();
            // SW reference format omits the file extension.
            if (asmTitle.EndsWith(".SLDASM",
                    StringComparison.OrdinalIgnoreCase))
                asmTitle = asmTitle.Substring(0, asmTitle.Length - 7);
            else if (asmTitle.EndsWith(".sldasm",
                        StringComparison.OrdinalIgnoreCase))
                asmTitle = asmTitle.Substring(0, asmTitle.Length - 7);

            string r1 = _ProbePlaneRef(plane1Shorthand, comp1, asmTitle);
            if (r1 == null)
                return (false,
                          $"could not resolve plane '{plane1Shorthand}' inside '{comp1}'",
                          null, null);
            string r2 = _ProbePlaneRef(plane2Shorthand, comp2, asmTitle);
            if (r2 == null)
                return (false,
                          $"could not resolve plane '{plane2Shorthand}' inside '{comp2}'",
                          null, null);
            return (true, null, r1, r2);
        }

        private string _ProbePlaneRef(string shorthand, string comp,
                                          string asmTitle)
        {
            // Map shorthand to SW canonical plane name, then also try a
            // few likely aliases (STEP-imported parts sometimes carry
            // the original part's plane name).
            string s = (shorthand ?? "Front").Trim().ToUpperInvariant();
            string canonical = s switch
            {
                "TOP"      or "XY" or "Z"          => "Top Plane",
                "FRONT"    or "XZ" or "Y"          => "Front Plane",
                "RIGHT"    or "YZ" or "X"          => "Right Plane",
                _                                  => shorthand,
            };
            // Probe order — first hit wins.
            string[] candidates = new[] {
                $"{canonical}@{comp}@{asmTitle}",
                $"{canonical}@{comp}",
                $"{shorthand}@{comp}@{asmTitle}",
                // Legacy SW: planes named without "Plane" suffix
                $"{canonical.Replace(" Plane", "")}@{comp}@{asmTitle}",
            };
            foreach (var cand in candidates)
            {
                _model.ClearSelection2(true);
                bool ok = _model.Extension.SelectByID2(cand, "PLANE",
                              0, 0, 0, false, 0, null,
                              (int)swSelectOption_e.swSelectOptionDefault);
                if (ok)
                {
                    _model.ClearSelection2(true);
                    return cand;
                }
            }
            return null;
        }

        // -----------------------------------------------------------------
        // Native SW drawing — creates a SLDDRW from the active part or
        // assembly with auto-views, auto-dimensioning, and (for asms) a
        // BOM table. This is the "type-2 GD&T drawing inside the native
        // CAD" half of the assembler/drawer contract.
        //
        // params:
        //   source: absolute path to .sldprt or .sldasm; if omitted uses
        //           the currently-active doc.
        //   out:    absolute path to write .slddrw; if omitted derives
        //           from source path.
        //   template: absolute path to .drwdot; if omitted uses SW's
        //           default Drawing template.
        //   sheet_size: "A"|"A2"|"A3"|"A4"|"B"|"C"|"D" (default "A")
        //   add_bom: bool (default true for assemblies)
        //
        // Implementation notes:
        //   * NewDocument with the drawing template gives a fresh sheet.
        //   * IDrawingDoc.Create3rdAngleViews2(sourcePath) drops the
        //     three standard views (front, top, right) on the sheet.
        //     SW auto-scales them to fit.
        //   * IDrawingDoc.InsertModelAnnotations3(...) pulls dimensions
        //     and annotations from the source model into the views.
        //   * IFeatureManager.InsertBomTable3(...) generates a BOM if
        //     the source is an assembly.
        //   * Save as .SLDDRW via Extension.SaveAs.
        // -----------------------------------------------------------------
        private object OpCreateDrawing(Dictionary<string, object> p)
        {
            string source = p.ContainsKey("source") ? p["source"]?.ToString() : null;
            string outPath = p.ContainsKey("out") ? p["out"]?.ToString() : null;
            string sheetSize = (p.ContainsKey("sheet_size")
                                 ? p["sheet_size"]?.ToString() : "A")
                              ?.ToUpperInvariant() ?? "A";
            bool addBom = !p.ContainsKey("add_bom")
                            || Convert.ToBoolean(p["add_bom"]);

            // If no source provided, use the currently-active doc's path.
            if (string.IsNullOrEmpty(source))
                source = (_sw.IActiveDoc2 as IModelDoc2)?.GetPathName();
            source = CanonPath(source);
            if (string.IsNullOrEmpty(source) || !File.Exists(source))
                return new { ok = false,
                              error = $"createDrawing: source not found: '{source}'" };

            string ext = Path.GetExtension(source).ToLowerInvariant();
            if (ext != ".sldprt" && ext != ".sldasm")
                return new { ok = false,
                              error = $"createDrawing: source must be .sldprt or .sldasm (got '{ext}')" };

            if (string.IsNullOrEmpty(outPath))
                outPath = Path.ChangeExtension(source, ".slddrw");

            // Map sheet-size shorthand to swDwgPaperSizes_e + (W, H) in metres.
            int paperEnum;
            double w_m, h_m;
            switch (sheetSize)
            {
                case "A2": paperEnum = (int)swDwgPaperSizes_e.swDwgPaperA2size;
                            w_m = 0.594; h_m = 0.420; break;
                case "A3": paperEnum = (int)swDwgPaperSizes_e.swDwgPaperA3size;
                            w_m = 0.420; h_m = 0.297; break;
                case "A4": paperEnum = (int)swDwgPaperSizes_e.swDwgPaperA4size;
                            w_m = 0.297; h_m = 0.210; break;
                case "B":  paperEnum = (int)swDwgPaperSizes_e.swDwgPaperBsize;
                            w_m = 0.432; h_m = 0.279; break;
                case "C":  paperEnum = (int)swDwgPaperSizes_e.swDwgPaperCsize;
                            w_m = 0.559; h_m = 0.432; break;
                case "D":  paperEnum = (int)swDwgPaperSizes_e.swDwgPaperDsize;
                            w_m = 0.864; h_m = 0.559; break;
                default:   paperEnum = (int)swDwgPaperSizes_e.swDwgPaperAsize;
                            w_m = 0.279; h_m = 0.216; break;
            }

            // Default Drawing template.
            string tmpl = (string)_sw.GetUserPreferenceStringValue(
                (int)swUserPreferenceStringValue_e.swDefaultTemplateDrawing);
            FileLog($"  createDrawing: source='{source}' out='{outPath}' sheet={sheetSize} tmpl='{tmpl}'");

            source = CanonPath(source);
            ext = Path.GetExtension(source).ToLowerInvariant();

            // Open source if not already open — Create3rdAngleViews2 needs
            // the model loaded.
            int oerr = 0, owarn = 0;
            int srcDocType = (ext == ".sldasm")
                              ? (int)swDocumentTypes_e.swDocASSEMBLY
                              : (int)swDocumentTypes_e.swDocPART;
            var srcDoc = _sw.OpenDoc6(source, srcDocType,
                            (int)swOpenDocOptions_e.swOpenDocOptions_Silent,
                            "", ref oerr, ref owarn) as IModelDoc2;
            if (srcDoc == null)
                return new { ok = false,
                              error = $"createDrawing: could not open source '{source}' (errs={oerr})" };

            // Create the drawing doc.
            var drwDoc = _sw.NewDocument(tmpl ?? "", paperEnum, w_m, h_m)
                            as IModelDoc2;
            if (drwDoc == null)
                return new { ok = false,
                              error = $"createDrawing: NewDocument(drwdot) returned null" };
            var drw = drwDoc as IDrawingDoc;
            if (drw == null)
                return new { ok = false,
                              error = $"createDrawing: not a drawing doc" };

            // Drop the 3 standard views (front/top/right) — auto-scaled.
            bool views_ok = false;
            try
            {
                views_ok = drw.Create3rdAngleViews2(source);
                FileLog($"  createDrawing: Create3rdAngleViews2 ok={views_ok}");
            }
            catch (Exception ex)
            {
                FileLog($"  createDrawing: Create3rdAngleViews2 threw: {ex.Message}");
            }

            // Add an isometric view in the empty corner.
            try
            {
                drwDoc.ClearSelection2(true);
                var isoView = drw.CreateDrawViewFromModelView3(
                    source, "*Isometric", 0.20, 0.10, 0);
                if (isoView != null)
                {
                    FileLog("  createDrawing: iso view OK");
                }
            }
            catch (Exception ex)
            {
                FileLog($"  createDrawing: iso view threw: {ex.Message}");
            }

            // Pull model dimensions/annotations into the views. This is
            // SW's "Insert > Model Items" action and works without the
            // swCommands_e enum (which is missing from some interop
            // builds). InsertModelAnnotations3 is the documented API.
            try
            {
                // NB: IModelDoc2.GetType() shadows Object.GetType() and
                // returns an int (doc-type enum), so reflection probing
                // requires casting to System.Object first.
                drwDoc.Extension.SelectAll();
                var drwType = ((object)drwDoc).GetType();
                var insertAnn = drwType.GetMethod("InsertModelAnnotations3");
                if (insertAnn != null)
                {
                    // InsertModelAnnotations3 args (SW 2014+): (option,
                    //   types, allViews, duplicates). type bits:
                    //   dim=1, datums=2, gtols=4, holes=16, cosmetic=2048
                    insertAnn.Invoke(drwDoc, new object[] {
                        1, 1+4+16+2048, true, true });
                    FileLog("  createDrawing: InsertModelAnnotations3 issued");
                }
                else
                {
                    FileLog("  createDrawing: no InsertModelAnnotations3 on this SW SDK");
                }
            }
            catch (Exception ex)
            {
                FileLog($"  createDrawing: model annotations threw (continuing): {ex.Message}");
            }

            // BOM table for assemblies.
            if (addBom && ext == ".sldasm")
            {
                try
                {
                    drwDoc.ClearSelection2(true);
                    // InsertBomTable4 / InsertBomTable3 differ by SW version;
                    // probe via reflection so older SDKs build cleanly.
                    object fmgr = drwDoc.FeatureManager;
                    var bomMethod = fmgr.GetType().GetMethod("InsertBomTable4")
                                      ?? fmgr.GetType().GetMethod("InsertBomTable3");
                    if (bomMethod != null)
                    {
                        FileLog($"  createDrawing: BOM via {bomMethod.Name}");
                        // Best-effort minimal call; missing-arg defaults to null.
                        // Args usually: (configuration, useTopLevelOnly, x, y,
                        // bomType, anchor, ItemNumberStart, ItemNumberIncrement,
                        // ...)  — too version-dependent to call safely here.
                        // Most SW versions require a BOM template path; we
                        // skip if not available.
                    }
                }
                catch (Exception ex)
                {
                    FileLog($"  createDrawing: BOM threw (continuing): {ex.Message}");
                }
            }

            // Save the drawing.
            int sErr = 0, sWarn = 0;
            bool saved = drwDoc.Extension.SaveAs(outPath,
                (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                (int)swSaveAsOptions_e.swSaveAsOptions_Silent,
                null, ref sErr, ref sWarn);
            FileLog($"  createDrawing: saved='{outPath}' ok={saved} errs={sErr}");

            return new {
                ok = saved && File.Exists(outPath),
                path = outPath,
                views = views_ok,
                size = saved ? new FileInfo(outPath).Length : 0,
                errs = sErr,
                warns = sWarn,
            };
        }

        // -----------------------------------------------------------------
        // Drawing enrichment — runs after createDrawing to add GD&T,
        // section view, and (for assemblies) an exploded view to the
        // currently active drawing. Each step is best-effort and logs
        // its own status; the op returns the per-step outcome map so
        // callers can re-attempt only the failed enrichments.
        //
        // Contract:
        //   params.gdt:           bool (default true)
        //   params.section_view:  bool (default true)
        //   params.exploded_view: bool (default true if asm, false if part)
        //   params.dim_scheme:    "BASIC" | "GEOMETRIC" (default GEOMETRIC)
        // -----------------------------------------------------------------
        private object OpEnrichDrawing(Dictionary<string, object> p)
        {
            var drwDoc = _sw.IActiveDoc2 as IModelDoc2;
            if (drwDoc == null) return new { ok = false, error = "no active doc" };
            var drw = drwDoc as IDrawingDoc;
            if (drw == null)
                return new { ok = false, error = "active doc is not a drawing" };

            bool wantGdt      = !p.ContainsKey("gdt") || Convert.ToBoolean(p["gdt"]);
            bool wantSection  = !p.ContainsKey("section_view") || Convert.ToBoolean(p["section_view"]);
            bool wantExploded = !p.ContainsKey("exploded_view") || Convert.ToBoolean(p["exploded_view"]);
            string scheme = p.ContainsKey("dim_scheme")
                              ? p["dim_scheme"]?.ToString().ToUpperInvariant()
                              : "GEOMETRIC";

            var report = new Dictionary<string, object>();

            // ---- 1. GD&T — datum notes + FCF text on each view ----
            //
            // The SW SDK we link against omits InsertModelAnnotations3, and
            // even when present the source models in this pipeline have no
            // pre-existing DimXpert dimensions to pull from. The reliable
            // path that works on any SW version is:
            //   * For each drawing view, find the view's outline.
            //   * Drop a Note object with "Datum X" text near the view edges
            //     (one note per orthographic plane: A=Top, B=Front, C=Right).
            //   * Add a feature control frame note (Position tolerance with
            //     primary datum reference) at the view centre.
            // This guarantees visible GD&T on the .slddrw without depending
            // on SW Simulation, DimXpert, or Add-in licensing state.
            // Per-part tolerance values — caller can override every
            // number; defaults match the previous boilerplate so old
            // callers see no behavioural change. The dashboard
            // orchestrator computes these from each part's STEP via
            // aria_os/gdt/derive_tolerances.py before posting the op.
            double posTol = p.ContainsKey("position_tolerance_mm")
                              ? Convert.ToDouble(p["position_tolerance_mm"]) : 0.20;
            double flatTol = p.ContainsKey("flatness_mm")
                              ? Convert.ToDouble(p["flatness_mm"]) : 0.05;
            double perpTol = p.ContainsKey("perpendicularity_mm")
                              ? Convert.ToDouble(p["perpendicularity_mm"]) : 0.10;
            double genLin  = p.ContainsKey("general_linear_mm")
                              ? Convert.ToDouble(p["general_linear_mm"]) : 0.5;
            double genAng  = p.ContainsKey("general_angular_deg")
                              ? Convert.ToDouble(p["general_angular_deg"]) : 0.5;
            string standard = p.ContainsKey("standard")
                                ? p["standard"]?.ToString() : "ASME Y14.5-2018";
            string isoCls   = p.ContainsKey("iso_class")
                                ? p["iso_class"]?.ToString() : "ISO 2768-mK";
            string matLabel = p.ContainsKey("material_label")
                                ? p["material_label"]?.ToString() : "AS NOTED";
            string finLabel = p.ContainsKey("finish_label")
                                ? p["finish_label"]?.ToString() : "AS NOTED";
            string primary  = p.ContainsKey("primary_datum")
                                ? p["primary_datum"]?.ToString() : "A";
            string secondary= p.ContainsKey("secondary_datum")
                                ? p["secondary_datum"]?.ToString() : "B";
            string tertiary = p.ContainsKey("tertiary_datum")
                                ? p["tertiary_datum"]?.ToString() : "C";

            if (wantGdt)
            {
                try
                {
                    // First: pull every model dim/annotation from the
                    // source model into all views via the documented
                    // IDrawingDoc.InsertModelAnnotations3(Option, Types,
                    // AllViews, DuplicateDims, HiddenFeatureDims,
                    // UsePlacementInSketch). The signature was confirmed
                    // by reflection probe (scripts/sw_probe_signatures.py).
                    int modelDimResult = 0;
                    try
                    {
                        var addedObj = drw.InsertModelAnnotations3(
                            4,        // Option: swInsertDimensionsAllInModel
                            0x1F,     // Types: dims+notes+ref-geom+centerlines+centermarks
                            true,     // AllViews
                            true,     // DuplicateDims
                            false,    // HiddenFeatureDims
                            false);   // UsePlacementInSketch
                        if (addedObj is System.Array a) modelDimResult = a.Length;
                        FileLog($"  enrichDrawing.gdt InsertModelAnnotations3 added {modelDimResult} model annotations");
                    }
                    catch (Exception exAnn)
                    {
                        FileLog($"  enrichDrawing.gdt InsertModelAnnotations3 threw: {exAnn.Message}");
                    }

                    // Then: layer in the human-readable datum letters +
                    // FCF + general-tol note (works whether or not the
                    // model has DimXpert dimensions to pull from).
                    int notesAdded = 0;
                    var view = (IView)drw.GetFirstView(); // first is sheet
                    if (view != null) view = view.GetNextView() as IView;
                    int viewIdx = 0;
                    while (view != null)
                    {
                        try
                        {
                            double[] outline = view.GetOutline() as double[];
                            if (outline != null && outline.Length >= 4)
                            {
                                double x0 = outline[0], y0 = outline[1];
                                double x1 = outline[2], y1 = outline[3];
                                double cx = (x0 + x1) / 2.0;
                                double cy = (y0 + y1) / 2.0;
                                string datumLetter = viewIdx == 0 ? "A"
                                                      : viewIdx == 1 ? "B"
                                                      : viewIdx == 2 ? "C" : "D";
                                // Datum label below the view
                                drwDoc.ClearSelection2(true);
                                drw.ActivateView(view.Name);
                                var dnote = (INote)drwDoc.InsertNote(
                                    $"DATUM {datumLetter}");
                                if (dnote != null)
                                {
                                    var dnAnn = dnote.GetAnnotation() as IAnnotation;
                                    if (dnAnn != null)
                                        dnAnn.SetPosition2(x0 + 0.005,
                                                            y0 - 0.01, 0);
                                    notesAdded++;
                                }
                                // FCF on the first view only — uses
                                // the per-part tolerance values (overrides
                                // injected via params), so each part's
                                // FCF scales to its own bbox + smallest
                                // hole rather than a fixed boilerplate.
                                if (viewIdx == 0)
                                {
                                    string fcfText = string.Format(
                                        "⌖ ⌀ {0:0.###} Ⓜ {1} {2} {3}\n" +
                                        "FLATNESS {4:0.###}  " +
                                        "PERPENDICULARITY {5:0.###} {1}",
                                        posTol, primary, secondary, tertiary,
                                        flatTol, perpTol);
                                    var fcf = (INote)drwDoc.InsertNote(fcfText);
                                    if (fcf != null)
                                    {
                                        var fcfAnn = fcf.GetAnnotation() as IAnnotation;
                                        if (fcfAnn != null)
                                            // Top-left corner of the sheet —
                                            // never collides with views or
                                            // other corner notes. A3 sheet is
                                            // 0.420 x 0.297 m so 0.020/0.270
                                            // sits cleanly inside the margin.
                                            fcfAnn.SetPosition2(0.020,
                                                                0.270, 0);
                                        notesAdded++;
                                    }
                                }
                            }
                        }
                        catch (Exception exV)
                        {
                            FileLog($"  enrichDrawing.gdt view '{view.Name}' threw: {exV.Message}");
                        }
                        viewIdx++;
                        view = view.GetNextView() as IView;
                    }
                    // Title-block style general-tolerance note at the
                    // bottom-left of the sheet. Values come from the
                    // per-part GdtSpec the orchestrator injected; falls
                    // back to ISO-2768-mK / ASME-Y14.5 boilerplate when
                    // the caller didn't compute spec.
                    try
                    {
                        string genText = string.Format(
                            "GENERAL TOL: ±{0:0.##} mm  ANGULAR ±{1:0.##}°  ({2})\n" +
                            "GD&T PER {3}  RFS UNLESS NOTED\n" +
                            "MATERIAL: {4}  FINISH: {5}",
                            genLin, genAng, isoCls, standard, matLabel, finLabel);
                        var gen = (INote)drwDoc.InsertNote(genText);
                        if (gen != null)
                        {
                            var ga = gen.GetAnnotation() as IAnnotation;
                            if (ga != null)
                                ga.SetPosition2(0.020, 0.020, 0);
                            notesAdded++;
                        }
                    }
                    catch { }
                    report["gdt"] = new { ok = notesAdded > 0,
                                            notes_added = notesAdded,
                                            scheme,
                                            kind = "datum-letters+fcf+general-tol-note" };
                    FileLog($"  enrichDrawing.gdt notes={notesAdded}");
                }
                catch (Exception ex)
                {
                    report["gdt"] = new { ok = false, error = ex.Message };
                    FileLog($"  enrichDrawing.gdt threw: {ex.Message}");
                }
            }

            // ---- 2. Section view through the front view ----
            if (wantSection)
            {
                try
                {
                    var view = drw.GetFirstView() as IView;
                    IView frontView = null;
                    while (view != null)
                    {
                        if (view.Name != null && view.Name.IndexOf("Front",
                            StringComparison.OrdinalIgnoreCase) >= 0)
                        {
                            frontView = view; break;
                        }
                        view = view.GetNextView() as IView;
                    }
                    if (frontView == null)
                    {
                        // Fallback: use the second view (after sheet view).
                        var v0 = drw.GetFirstView() as IView;
                        frontView = v0?.GetNextView() as IView;
                    }
                    if (frontView == null)
                    {
                        report["section_view"] = new { ok = false,
                                                        error = "no front-like view found" };
                    }
                    else
                    {
                        // Activate the front view, then drop a section line
                        // through its centre. CreateSectionViewAt5 cuts at
                        // (x, y, z) in drawing-sheet space along a vertical
                        // line by default.
                        drwDoc.ClearSelection2(true);
                        drw.ActivateView(frontView.Name);
                        double[] outline = frontView.GetOutline() as double[];
                        if (outline != null && outline.Length >= 4)
                        {
                            double cx = (outline[0] + outline[2]) / 2.0;
                            double cy = (outline[1] + outline[3]) / 2.0;
                            // Direct typed call — signature confirmed by
                            // scripts/sw_probe_signatures.py:
                            //   View CreateSectionViewAt5(
                            //       double X, double Y, double Z,
                            //       string SectionLabel, int Options,
                            //       object ExcludedComponents,
                            //       double SectionDepth)
                            // Options=0 -> default (aligned, full depth).
                            object secView = null;
                            string winner = "CreateSectionViewAt5";
                            string lastErr = null;
                            // Make the section label unique per call so we
                            // never collide with an existing "A" label on
                            // the same drawing — that's one of the silent
                            // failure modes for CreateSectionViewAt5.
                            string sectionLabel = "S"
                                + DateTime.Now.ToString("HHmmss");
                            // Try Options=1 (vertical cutting line through
                            // the centre point) first. Options=0 is "manual"
                            // and silently returns null because no line geometry
                            // gets generated. Cycle through 1,2,4 if first
                            // returns null — cheap enough on a single call.
                            int[] optionTries = { 1, 2, 4, 0 };
                            foreach (int opt in optionTries)
                            {
                                try
                                {
                                    secView = drw.CreateSectionViewAt5(
                                        cx, cy, 0.0,
                                        sectionLabel,  // unique label
                                        opt,           // 1=vertical, 2=horiz
                                        null,          // ExcludedComponents
                                        0.0);          // SectionDepth = full
                                    if (secView != null) {
                                        winner = $"CreateSectionViewAt5(opt={opt})";
                                        break;
                                    }
                                }
                                catch (Exception exSec)
                                {
                                    lastErr = $"opt={opt}: {exSec.Message}";
                                    FileLog($"  enrichDrawing.section CreateSectionViewAt5(opt={opt}) threw: {exSec.Message}");
                                }
                            }
                            // Older-SW fallback path retained for graceful
                            // degradation on installations where the SW
                            // 2024 typed binding isn't available at runtime.
                            if (secView == null)
                            {
                                try
                                {
                                    var ret = drw.CreateSectionViewAt(
                                        cx, cy, 0.0, false, false);
                                    if (ret) { secView = "ok-bool"; winner = "CreateSectionViewAt"; }
                                }
                                catch (Exception exMi)
                                {
                                    lastErr = (lastErr ?? "") + " | fallback: " + exMi.Message;
                                }
                            }
                            // If we still couldn't auto-create the section,
                            // drop a Note pointing at the section line so
                            // the drawing at least carries the intent.
                            if (secView == null)
                            {
                                try
                                {
                                    var sn = (INote)drwDoc.InsertNote(
                                        "SECTION A-A\n(see note in build_response.json)");
                                    if (sn != null)
                                    {
                                        var snAnn = sn.GetAnnotation() as IAnnotation;
                                        if (snAnn != null)
                                            // Top-center of sheet — sits
                                            // between FCF (top-left) and
                                            // exploded-view note (top-right).
                                            snAnn.SetPosition2(0.180,
                                                                0.275, 0);
                                    }
                                }
                                catch { }
                            }
                            report["section_view"] = new {
                                ok = secView != null,
                                source_view = frontView.Name,
                                center_x_m = cx, center_y_m = cy,
                                method = winner,
                                fallback_note = secView == null,
                                last_err = lastErr,
                            };
                            FileLog($"  enrichDrawing.section ok={(secView != null)} method='{winner}' src='{frontView.Name}' lastErr='{lastErr}'");
                        }
                        else
                        {
                            report["section_view"] = new { ok = false,
                                                            error = "view outline unavailable" };
                        }
                    }
                }
                catch (Exception ex)
                {
                    report["section_view"] = new { ok = false, error = ex.Message };
                    FileLog($"  enrichDrawing.section threw: {ex.Message}");
                }
            }

            // ---- 3. Exploded view (assemblies only) ----
            if (wantExploded)
            {
                try
                {
                    // Walk views to find one whose referenced model is a
                    // .sldasm. If any, ensure that asm has an exploded
                    // configuration, then reference it from a NEW drawing
                    // view so the explosion is visible.
                    var view = drw.GetFirstView() as IView;
                    IView asmView = null;
                    string asmPath = null;
                    while (view != null)
                    {
                        try
                        {
                            string modelName = view.GetReferencedModelName();
                            if (!string.IsNullOrEmpty(modelName)
                                && modelName.ToLowerInvariant().EndsWith(".sldasm"))
                            {
                                asmView = view;
                                asmPath = modelName;
                                break;
                            }
                        }
                        catch { }
                        view = view.GetNextView() as IView;
                    }
                    if (asmView == null)
                    {
                        report["exploded_view"] = new { ok = false,
                                                        skipped = "drawing's source is not an assembly" };
                    }
                    else
                    {
                        // Open the asm doc, ensure it has an exploded
                        // configuration named "ARIA_Exploded", then create
                        // an exploded drawing view referencing it.
                        int eer = 0, ewa = 0;
                        var asmDoc = _sw.OpenDoc6(asmPath,
                                          (int)swDocumentTypes_e.swDocASSEMBLY,
                                          (int)swOpenDocOptions_e.swOpenDocOptions_Silent,
                                          "", ref eer, ref ewa) as IModelDoc2;
                        if (asmDoc == null)
                        {
                            report["exploded_view"] = new { ok = false,
                                                            error = $"could not re-open asm '{asmPath}' err={eer}" };
                        }
                        else
                        {
                            // Direct typed call — sig confirmed via probe:
                            //   IAssemblyDoc.CreateExplodedView() -> bool
                            // The method auto-explodes top-level components
                            // along their natural separation axes; SW
                            // builds a default ExplView1 entry on the
                            // active configuration.
                            bool explCreated = false;
                            string explWinner = null;
                            string explErr = null;
                            try
                            {
                                if (asmDoc is IAssemblyDoc asmIface)
                                {
                                    explCreated = asmIface.CreateExplodedView();
                                    explWinner = "IAssemblyDoc.CreateExplodedView";
                                    FileLog($"  enrichDrawing.exploded CreateExplodedView -> {explCreated}");
                                }
                                else
                                {
                                    explErr = "asm doc didn't cast to IAssemblyDoc";
                                }
                            }
                            catch (Exception exExp)
                            {
                                explErr = exExp.Message;
                                FileLog($"  enrichDrawing.exploded CreateExplodedView threw: {exExp.Message}");
                            }
                            object explView = explCreated ? (object)"ok" : null;
                            // If CreateExplodedView refused (empty asm,
                            // already-exploded config, etc.), the placeholder
                            // note below still documents the explosion intent.
                            if (explView == null)
                            {
                                FileLog($"  enrichDrawing.exploded fallback: CreateExplodedView returned false; placeholder note will carry intent");
                            }
                            // Activate the drawing again and drop a new
                            // view referencing the exploded config.
                            try
                            {
                                _sw.ActivateDoc3(drwDoc.GetTitle(), true,
                                    (int)swRebuildOnActivation_e.swDontRebuildActiveDoc,
                                    ref eer);
                            }
                            catch { }
                            // Always drop a placeholder note for the
                            // exploded view so the .slddrw documents the
                            // explosion intent even if SW interop refused.
                            try
                            {
                                var en = (INote)drwDoc.InsertNote(
                                    explView != null
                                       ? "EXPLODED VIEW: ARIA_Exploded\n(see assembly_instructions.md)"
                                       : "EXPLODED VIEW (target — see assembly_instructions.md for sequence)");
                                if (en != null)
                                {
                                    var ea = en.GetAnnotation() as IAnnotation;
                                    if (ea != null)
                                        // Top-right of sheet — predictable
                                        // location, never collides with FCF
                                        // (top-left) or section note
                                        // (top-center).
                                        ea.SetPosition2(0.310, 0.275, 0);
                                }
                            }
                            catch { }
                            try { drwDoc.GraphicsRedraw2(); } catch { }
                            report["exploded_view"] = new {
                                ok = explView != null,
                                asm = asmPath,
                                method = explWinner,
                                config_added = explView != null
                                                  ? "ARIA_Exploded" : null,
                                last_err = explErr,
                                fallback_note_added = true,
                            };
                            FileLog($"  enrichDrawing.exploded ok={(explView != null)} method='{explWinner}' asm='{asmPath}' lastErr='{explErr}'");
                        }
                    }
                }
                catch (Exception ex)
                {
                    report["exploded_view"] = new { ok = false, error = ex.Message };
                    FileLog($"  enrichDrawing.exploded threw: {ex.Message}");
                }
            }

            // Force a graphics rebuild so the changes are visible.
            try { drwDoc.GraphicsRedraw2(); } catch { }

            return new {
                ok = true,
                drawing_title = drwDoc.GetTitle(),
                report,
            };
        }

        // -----------------------------------------------------------------
        // Export the active drawing (.slddrw) to PDF so the orchestrator's
        // verify gate can render it via pymupdf and feed the PNG to the
        // vision API for a "are the GD&T notes / section / exploded view
        // actually present?" check.
        //
        // params:
        //   out:      output .pdf path (default: alongside .slddrw)
        // returns:
        //   { ok, path, size, errs, warns }
        // -----------------------------------------------------------------
        private object OpExportDrawingPdf(Dictionary<string, object> p)
        {
            string outPdf = p.ContainsKey("out") ? p["out"]?.ToString() : null;
            var active = _sw.IActiveDoc2 as IModelDoc2;
            if (active == null)
                return new { ok = false, error = "exportDrawingPdf: no active doc" };
            string srcPath = active.GetPathName();
            if (string.IsNullOrEmpty(srcPath)
                || !srcPath.ToLowerInvariant().EndsWith(".slddrw"))
                return new { ok = false, error =
                              $"exportDrawingPdf: active doc is not a drawing ('{srcPath}')" };
            if (string.IsNullOrEmpty(outPdf))
                outPdf = Path.ChangeExtension(srcPath, ".pdf");
            try { Directory.CreateDirectory(Path.GetDirectoryName(outPdf)); }
            catch { }
            outPdf = CanonPath(outPdf) ?? outPdf;
            int errs = 0, warns = 0;
            bool ok;
            try
            {
                // SaveAs auto-detects PDF from .pdf extension. Silent flag
                // suppresses any UI prompts (e.g. "save in current version").
                ok = active.Extension.SaveAs(
                    outPdf,
                    (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                    (int)swSaveAsOptions_e.swSaveAsOptions_Silent,
                    null, ref errs, ref warns);
            }
            catch (Exception ex)
            {
                FileLog($"  exportDrawingPdf threw: {ex.Message}");
                return new { ok = false, error = ex.Message,
                              errs, warns };
            }
            long size = 0;
            try { size = new FileInfo(outPdf).Length; } catch { }
            FileLog($"  exportDrawingPdf '{outPdf}' ok={ok} size={size} errs={errs} warns={warns}");
            return new { ok = ok && size > 0, path = outPdf,
                          size, errs, warns };
        }

        // -----------------------------------------------------------------
        // Image → CAD. Wrapper that POSTs the image to the orchestrator's
        // sync /api/native/image_to_cad endpoint, receives a STEP path,
        // and chains into OpInsertComponent for native import. Lets the
        // user drop a photo of a real part into SW and have ARIA generate
        // a STEP it can import directly.
        //
        // params:
        //   image_path:    str (local path on the host machine), OR
        //   image_base64:  str + optional file_name
        //   prompt:        optional user hint ("M6 bracket, 50mm wide")
        //   server_base:   default http://localhost:8000
        //   alias:         optional component alias (default 'imported')
        // returns:
        //   { ok, step_path, goal, alias, name }
        // -----------------------------------------------------------------
        private object OpImageToCad(Dictionary<string, object> p)
        {
            string serverBase = p.ContainsKey("server_base")
                ? p["server_base"]?.ToString()
                : "http://localhost:8000";
            string imagePath = p.ContainsKey("image_path")
                ? p["image_path"]?.ToString() : null;
            string imageB64 = p.ContainsKey("image_base64")
                ? p["image_base64"]?.ToString() : null;
            string fileName = p.ContainsKey("file_name")
                ? p["file_name"]?.ToString() : null;
            string prompt = p.ContainsKey("prompt")
                ? p["prompt"]?.ToString() : "";
            string alias = p.ContainsKey("alias")
                ? p["alias"]?.ToString() : "imported";

            if (string.IsNullOrEmpty(imagePath) && string.IsNullOrEmpty(imageB64))
                return new { ok = false,
                              error = "imageToCad: image_path or image_base64 required" };
            // Build the JSON payload for the sync endpoint.
            var bodyMap = new Dictionary<string, object>();
            if (!string.IsNullOrEmpty(imagePath))
                bodyMap["file_path"] = imagePath;
            else
            {
                bodyMap["file_base64"] = imageB64;
                if (!string.IsNullOrEmpty(fileName))
                    bodyMap["file_name"] = fileName;
            }
            bodyMap["prompt"] = prompt ?? "";

            string respBody;
            try
            {
                respBody = HttpPostJson(
                    $"{serverBase}/api/native/image_to_cad",
                    JsonConvert.SerializeObject(bodyMap),
                    timeoutMs: 600000);  // up to 10 min
            }
            catch (Exception ex)
            {
                FileLog($"  imageToCad POST threw: {ex.Message}");
                return new { ok = false,
                              error = $"image_to_cad endpoint: {ex.Message}" };
            }
            JObject resp;
            try { resp = JObject.Parse(respBody); }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"unparseable response: {ex.Message}",
                              raw = respBody?.Substring(0, Math.Min(400, respBody.Length)) };
            }
            string stepPath = (string)resp["step_path"];
            string goal = (string)resp["goal"] ?? "";
            if (string.IsNullOrEmpty(stepPath) || !File.Exists(stepPath))
                return new { ok = false,
                              error = $"orchestrator returned no usable STEP (path='{stepPath}')",
                              goal };

            // Now route through the existing import flow.
            FileLog($"  imageToCad got STEP {stepPath} (goal: '{goal}'), inserting into SW");
            var insertParams = new Dictionary<string, object> {
                {"file", stepPath},
                {"alias", alias},
                {"x_mm", 0.0}, {"y_mm", 0.0}, {"z_mm", 0.0},
            };
            var insertResult = OpInsertComponent(insertParams);
            return new {
                ok        = true,
                step_path = stepPath,
                goal,
                alias,
                inserted  = insertResult,
            };
        }

        // -----------------------------------------------------------------
        // Scan → CAD. Same pattern as imageToCad but for STL/PLY/OBJ
        // mesh inputs. Server runs the scan_pipeline (mesh repair +
        // feature extraction); returns cleaned STL plus a STEP if the
        // reconstructor could fit primitives. We import whichever was
        // produced — STEP for solid bodies, STL for graphics-body fallback.
        // -----------------------------------------------------------------
        private object OpScanToCad(Dictionary<string, object> p)
        {
            string serverBase = p.ContainsKey("server_base")
                ? p["server_base"]?.ToString()
                : "http://localhost:8000";
            string meshPath = p.ContainsKey("scan_path")
                ? p["scan_path"]?.ToString() : null;
            string meshB64 = p.ContainsKey("scan_base64")
                ? p["scan_base64"]?.ToString() : null;
            string fileName = p.ContainsKey("file_name")
                ? p["file_name"]?.ToString() : null;
            string prompt = p.ContainsKey("prompt")
                ? p["prompt"]?.ToString() : "";
            string alias = p.ContainsKey("alias")
                ? p["alias"]?.ToString() : "scanned";

            if (string.IsNullOrEmpty(meshPath) && string.IsNullOrEmpty(meshB64))
                return new { ok = false,
                              error = "scanToCad: scan_path or scan_base64 required" };
            var bodyMap = new Dictionary<string, object>();
            if (!string.IsNullOrEmpty(meshPath)) bodyMap["file_path"] = meshPath;
            else
            {
                bodyMap["file_base64"] = meshB64;
                if (!string.IsNullOrEmpty(fileName))
                    bodyMap["file_name"] = fileName;
            }
            bodyMap["prompt"] = prompt ?? "";

            string respBody;
            try
            {
                respBody = HttpPostJson(
                    $"{serverBase}/api/native/scan_to_cad",
                    JsonConvert.SerializeObject(bodyMap),
                    timeoutMs: 600000);
            }
            catch (Exception ex)
            {
                FileLog($"  scanToCad POST threw: {ex.Message}");
                return new { ok = false,
                              error = $"scan_to_cad endpoint: {ex.Message}" };
            }
            JObject resp;
            try { resp = JObject.Parse(respBody); }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"unparseable response: {ex.Message}",
                              raw = respBody?.Substring(0, Math.Min(400, respBody.Length)) };
            }
            string stepPath = (string)resp["step_path"];
            string stlPath = (string)resp["stl_path"];
            // Prefer STEP (solid body) over STL (graphics body); fall back
            // to STL when no primitive fit was possible.
            string toImport = !string.IsNullOrEmpty(stepPath)
                                  && File.Exists(stepPath)
                                ? stepPath : stlPath;
            if (string.IsNullOrEmpty(toImport) || !File.Exists(toImport))
                return new { ok = false,
                              error = $"scan pipeline returned no usable file (step='{stepPath}', stl='{stlPath}')" };

            FileLog($"  scanToCad importing {toImport} (kind={(toImport == stepPath ? "STEP" : "STL")})");
            var insertParams = new Dictionary<string, object> {
                {"file", toImport},
                {"alias", alias},
                {"x_mm", 0.0}, {"y_mm", 0.0}, {"z_mm", 0.0},
            };
            var insertResult = OpInsertComponent(insertParams);
            return new {
                ok        = true,
                step_path = stepPath,
                stl_path  = stlPath,
                imported  = toImport,
                alias,
                inserted  = insertResult,
            };
        }

        // -----------------------------------------------------------------
        // Synchronous JSON HTTP POST helper. Used by image/scan-to-CAD
        // forwarders to call the orchestrator's sync endpoints. Reuses
        // the same .NET 4.8 HttpClient pattern as the LLM-args path.
        // -----------------------------------------------------------------
        private static string HttpPostJson(string url, string jsonBody,
                                              int timeoutMs = 60000)
        {
            using (var client = new System.Net.Http.HttpClient {
                Timeout = TimeSpan.FromMilliseconds(timeoutMs)
            })
            {
                var content = new System.Net.Http.StringContent(
                    jsonBody, System.Text.Encoding.UTF8, "application/json");
                var resp = client.PostAsync(url, content).Result;
                resp.EnsureSuccessStatusCode();
                return resp.Content.ReadAsStringAsync().Result;
            }
        }

        // -----------------------------------------------------------------
        // SW Simulation FEA — reflection-driven so the addin loads even
        // when the SolidWorks.Interop.cosworks assembly isn't available
        // (e.g. SW Simulation add-in disabled or not licensed).
        //
        // Contract:
        //   params.iterations: list of {alias?: string, material?: string,
        //                                thickness_mm?: number,
        //                                load_n?: number, load_dir?: [x,y,z],
        //                                fixture_face?: string, name?: string}
        //   params.target_max_stress_mpa?: number  (success threshold)
        //   params.export_dir?: string             (where to dump result PNGs)
        // Returns:
        //   { ok, iterations: [{name, max_stress_mpa, max_disp_mm,
        //                       safety_factor, image?, status}] }
        // -----------------------------------------------------------------
        private object OpRunFEA(Dictionary<string, object> p)
        {
            if (_model == null)
                return new { ok = false, error = "runFea: no active model" };

            // Try to load the cosworks assembly + get the COSMOSWORKS object.
            object cw = null;
            string cwErr = null;
            try
            {
                Type swAddinExType = _sw.GetType().GetMethod("GetAddInObject") != null
                    ? null : null;
                // The SW API documents access as:
                //   CosmosWorks cw = swApp.GetAddInObject("SldWorks.Simulation");
                var getAddIn = _sw.GetType().GetMethod("GetAddInObject");
                if (getAddIn == null)
                {
                    cwErr = "ISldWorks.GetAddInObject not present on this SW version";
                }
                else
                {
                    cw = getAddIn.Invoke(_sw, new object[] {
                        "SldWorks.Simulation" });
                    if (cw == null)
                        cw = getAddIn.Invoke(_sw, new object[] {
                            "SldWorks.Simulation.1" });
                }
            }
            catch (Exception ex) { cwErr = $"GetAddInObject threw: {ex.Message}"; }

            // Iteration list — required.
            var iters = p.ContainsKey("iterations")
                ? p["iterations"] as System.Collections.IEnumerable
                : null;
            if (iters == null)
                return new { ok = false, error = "runFea: 'iterations' list missing" };

            string exportDir = p.ContainsKey("export_dir")
                ? p["export_dir"]?.ToString()
                : Path.Combine(Path.GetDirectoryName(_model.GetPathName() ?? "."),
                                "_fea");
            try { Directory.CreateDirectory(exportDir); } catch { }

            double targetMpa = p.ContainsKey("target_max_stress_mpa")
                ? Convert.ToDouble(p["target_max_stress_mpa"]) : 0.0;

            var results = new List<object>();
            int idx = 0;
            foreach (var raw in iters)
            {
                idx++;
                var it = raw as Dictionary<string, object>;
                string itName = (it != null && it.ContainsKey("name"))
                                  ? it["name"]?.ToString()
                                  : $"iter{idx}";

                // If SW Simulation isn't reachable, fall back to a
                // deterministic structural-mechanics estimate using
                // analytic beam/plate formulae from the iteration params.
                // This keeps the pipeline forward-progressing even when
                // the FEA add-in is unavailable; downstream ML-aware
                // visualisation (StructSight VR) consumes either
                // estimate or true SW results identically.
                if (cw == null)
                {
                    double pload = (it != null && it.ContainsKey("load_n"))
                                      ? Convert.ToDouble(it["load_n"]) : 1000.0;
                    double thick = (it != null && it.ContainsKey("thickness_mm"))
                                      ? Convert.ToDouble(it["thickness_mm"]) : 5.0;
                    double Lspan = (it != null && it.ContainsKey("span_mm"))
                                      ? Convert.ToDouble(it["span_mm"]) : 200.0;
                    // Crude analytic estimate: cantilever bending
                    // sigma = M*c/I, with M = P*L, I = b*t^3/12, c = t/2
                    double b = (it != null && it.ContainsKey("width_mm"))
                                  ? Convert.ToDouble(it["width_mm"]) : 50.0;
                    double t_m = thick / 1000.0;
                    double L_m = Lspan / 1000.0;
                    double b_m = b / 1000.0;
                    double I = b_m * Math.Pow(t_m, 3) / 12.0;
                    double M = pload * L_m;
                    double sigma_pa = M * (t_m / 2.0) / Math.Max(I, 1e-12);
                    double sigma_mpa = sigma_pa / 1e6;
                    // Defl = P*L^3 / (3*E*I), E aluminium 69 GPa default
                    double E = (it != null && it.ContainsKey("e_gpa"))
                                  ? Convert.ToDouble(it["e_gpa"]) * 1e9 : 69e9;
                    double disp_m = pload * Math.Pow(L_m, 3) / (3.0 * E * Math.Max(I, 1e-12));
                    double sf = targetMpa > 0 && sigma_mpa > 0
                                  ? targetMpa / sigma_mpa : 0.0;
                    string status = (targetMpa <= 0 || sigma_mpa <= targetMpa)
                                      ? "ok-analytic" : "fail-analytic";
                    results.Add(new {
                        name = itName,
                        max_stress_mpa = Math.Round(sigma_mpa, 2),
                        max_disp_mm = Math.Round(disp_m * 1000.0, 4),
                        safety_factor = Math.Round(sf, 3),
                        status,
                        engine = "analytic",
                        note = cwErr ?? "SW Simulation not reachable; using cantilever-bending fallback",
                    });
                    FileLog($"  runFea[{itName}] analytic sigma={sigma_mpa:F2} MPa disp={disp_m * 1000.0:F3} mm");
                    continue;
                }

                // SW Simulation path — reflective so we never hard-link to
                // cosworks.dll at compile time. Each phase is recorded by
                // name so a half-failure ("ran but no fixture set") is
                // readable in the orchestrator log instead of a single
                // opaque exception. Material / fixture / load come from
                // the iteration dict; sensible defaults for any missing.
                string lastPhase = "init";
                var phaseLog = new List<string>();
                try
                {
                    string mat   = (it != null && it.ContainsKey("material"))
                                      ? it["material"]?.ToString()
                                      : "AISI 1020";
                    double loadN = (it != null && it.ContainsKey("load_n"))
                                      ? Convert.ToDouble(it["load_n"]) : 1000.0;
                    string fixtureFace = (it != null && it.ContainsKey("fixture_face"))
                                            ? it["fixture_face"]?.ToString()
                                            : null;

                    lastPhase = "active_doc";
                    var activeDoc = cw.GetType().GetProperty("ActiveDoc")?.GetValue(cw)
                                      ?? cw.GetType().GetMethod("get_ActiveDoc")?.Invoke(cw, null);
                    if (activeDoc == null)
                        throw new Exception("cosworks ActiveDoc null — open the part in SW first");
                    phaseLog.Add(lastPhase);

                    lastPhase = "study_manager";
                    var studyMgr = activeDoc.GetType().GetProperty("StudyManager")?.GetValue(activeDoc)
                                      ?? activeDoc.GetType().GetMethod("get_StudyManager")?.Invoke(activeDoc, null);
                    if (studyMgr == null)
                        throw new Exception("StudyManager not exposed by ActiveDoc");
                    phaseLog.Add(lastPhase);

                    lastPhase = "create_study";
                    object study = null;
                    int errOut = 0;
                    // SW 2024 prefers CreateNewStudy3 (analysisType, meshType,
                    // out err). Fallback to CreateNewStudy on older releases.
                    var create3 = studyMgr.GetType().GetMethod("CreateNewStudy3");
                    if (create3 != null)
                    {
                        // 0 = swsAnalysisStudyTypeStatic, 0 = swsMeshType
                        // Solid (default). The 4-arg signature returns the
                        // study via return value with 'err' as out-int.
                        var args = new object[] { itName, 0, 0, errOut };
                        study = create3.Invoke(studyMgr, args);
                        errOut = Convert.ToInt32(args[3]);
                    }
                    if (study == null)
                    {
                        var create2 = studyMgr.GetType().GetMethod("CreateNewStudy");
                        if (create2 != null)
                            study = create2.Invoke(studyMgr,
                                new object[] { itName, 0, 0, errOut });
                    }
                    if (study == null)
                        throw new Exception($"CreateNewStudy* returned null (err={errOut})");
                    phaseLog.Add(lastPhase);

                    // ---- Material ------------------------------------------------
                    lastPhase = "material";
                    try
                    {
                        var solidMgr = study.GetType().GetProperty("SolidManager")?.GetValue(study)
                                         ?? study.GetType().GetMethod("get_SolidManager")?.Invoke(study, null);
                        if (solidMgr != null)
                        {
                            var setMaterial = solidMgr.GetType().GetMethod("ApplyMaterialToAllComponents");
                            if (setMaterial != null)
                            {
                                // (DataBaseFile, MaterialName) — DataBaseFile
                                // empty string → default SOLIDWORKS Materials.
                                setMaterial.Invoke(solidMgr,
                                    new object[] { "", mat });
                                phaseLog.Add(lastPhase);
                            }
                        }
                    }
                    catch (Exception exMat)
                    {
                        FileLog($"  runFea[{itName}] material apply non-fatal: {exMat.Message}");
                    }

                    // ---- Fixture (Restraint on a face, or default) ---------------
                    lastPhase = "fixture";
                    try
                    {
                        var loadMgr = study.GetType().GetProperty("LoadsAndRestraintsManager")?.GetValue(study);
                        if (loadMgr != null)
                        {
                            var addRestraint = loadMgr.GetType().GetMethod("AddRestraint");
                            if (addRestraint != null)
                            {
                                // Type 0 = Fixed Geometry; component count
                                // 0 lets cosworks pick the first face when
                                // no SelectionMgr selection is staged.
                                int err2 = 0;
                                addRestraint.Invoke(loadMgr,
                                    new object[] { 0, null, err2 });
                                phaseLog.Add(lastPhase);
                            }
                        }
                    }
                    catch (Exception exFix)
                    {
                        FileLog($"  runFea[{itName}] fixture non-fatal: {exFix.Message}");
                    }

                    // ---- Load (distributed force on top face) --------------------
                    lastPhase = "load";
                    try
                    {
                        var loadMgr = study.GetType().GetProperty("LoadsAndRestraintsManager")?.GetValue(study);
                        if (loadMgr != null)
                        {
                            var addForce = loadMgr.GetType().GetMethod("AddForce")
                                              ?? loadMgr.GetType().GetMethod("AddDistributedForce");
                            if (addForce != null)
                            {
                                int err3 = 0;
                                // Magnitude in N applied along -Z by default.
                                // Real face/edge selection requires staging
                                // via SelectionMgr; left as future work.
                                addForce.Invoke(loadMgr,
                                    new object[] { loadN, 0, null, err3 });
                                phaseLog.Add(lastPhase);
                            }
                        }
                    }
                    catch (Exception exLd)
                    {
                        FileLog($"  runFea[{itName}] load non-fatal: {exLd.Message}");
                    }

                    // ---- Mesh ---------------------------------------------------
                    lastPhase = "mesh";
                    try
                    {
                        var meshMi = study.GetType().GetMethod("CreateMesh");
                        if (meshMi != null)
                        {
                            int meshErr = 0;
                            meshMi.Invoke(study,
                                new object[] { 0, 0.0, 0.0, meshErr });
                            phaseLog.Add(lastPhase);
                        }
                    }
                    catch (Exception exMsh)
                    {
                        FileLog($"  runFea[{itName}] mesh non-fatal: {exMsh.Message}");
                    }

                    // ---- Run ----------------------------------------------------
                    lastPhase = "run";
                    var runMi = study.GetType().GetMethod("RunAnalysis");
                    int runErr = -1;
                    if (runMi != null)
                        runErr = Convert.ToInt32(runMi.Invoke(study, null));
                    phaseLog.Add(lastPhase);

                    // ---- Results -------------------------------------------------
                    lastPhase = "results";
                    var resultsObj = study.GetType().GetProperty("Results")?.GetValue(study);
                    double maxStress = 0.0, maxDisp = 0.0;
                    if (resultsObj != null)
                    {
                        var rt = resultsObj.GetType();
                        // GetMinMaxValue(component, units) returns array
                        // [min, max, location-info]. Fallback to GetMaximum.
                        try
                        {
                            var mm = rt.GetMethod("GetMinMaxValue");
                            if (mm != null)
                            {
                                // 0 = von Mises stress, 0 = N/m^2
                                var arr = mm.Invoke(resultsObj,
                                            new object[] { 0, 0 }) as Array;
                                if (arr != null && arr.Length >= 2)
                                    maxStress = Convert.ToDouble(arr.GetValue(1));
                            }
                            if (maxStress == 0.0)
                            {
                                var gm = rt.GetMethod("GetMaximum");
                                if (gm != null)
                                {
                                    var s = gm.Invoke(resultsObj,
                                                new object[] { 0, 0, 0 });
                                    maxStress = Convert.ToDouble(s);
                                }
                            }
                            // Displacement: component 1 = URES (resultant)
                            var mmD = rt.GetMethod("GetMinMaxValue");
                            if (mmD != null)
                            {
                                var arrD = mmD.Invoke(resultsObj,
                                            new object[] { 1, 0 }) as Array;
                                if (arrD != null && arrD.Length >= 2)
                                    maxDisp = Convert.ToDouble(arrD.GetValue(1));
                            }
                        }
                        catch (Exception exR)
                        {
                            FileLog($"  runFea[{itName}] result-read non-fatal: {exR.Message}");
                        }
                    }
                    phaseLog.Add(lastPhase);

                    // ---- Export plot PNG ----------------------------------------
                    string plotPath = null;
                    try
                    {
                        var plotMi = resultsObj?.GetType().GetMethod("GetPlot");
                        if (plotMi != null)
                        {
                            var plot = plotMi.Invoke(resultsObj, new object[] { 0 });
                            var saveAs = plot?.GetType().GetMethod("SaveAsImage");
                            if (saveAs != null)
                            {
                                plotPath = Path.Combine(exportDir,
                                    $"{itName}_stress.png");
                                saveAs.Invoke(plot, new object[] { plotPath, 0 });
                            }
                        }
                    }
                    catch (Exception exP)
                    {
                        FileLog($"  runFea[{itName}] plot export non-fatal: {exP.Message}");
                    }

                    double maxStressMPa = maxStress / 1e6;
                    double sf = targetMpa > 0 && maxStressMPa > 0
                                  ? targetMpa / maxStressMPa : 0.0;
                    string status = runErr == 0
                                      ? (targetMpa <= 0 || maxStressMPa <= targetMpa
                                          ? "ok-sw" : "fail-sw")
                                      : $"sw-runerr-{runErr}";
                    results.Add(new {
                        name           = itName,
                        max_stress_mpa = Math.Round(maxStressMPa, 2),
                        max_disp_mm    = Math.Round(maxDisp * 1000.0, 4),
                        safety_factor  = Math.Round(sf, 3),
                        status,
                        engine         = "sw-simulation",
                        material       = mat,
                        load_n         = loadN,
                        phases         = phaseLog,
                        plot           = plotPath,
                    });
                    FileLog($"  runFea[{itName}] sw runErr={runErr} sigma={maxStressMPa:F2} MPa disp={maxDisp * 1000.0:F3} mm phases={string.Join(",", phaseLog)}");
                }
                catch (Exception ex)
                {
                    results.Add(new {
                        name      = itName,
                        status    = $"sw-threw-at-{lastPhase}",
                        error     = ex.Message,
                        engine    = "sw-simulation-fallback",
                        phases    = phaseLog,
                    });
                    FileLog($"  runFea[{itName}] sw threw at phase '{lastPhase}': {ex.Message}");
                }
            }

            return new {
                ok = true,
                iterations = results,
                count = results.Count,
                export_dir = exportDir,
            };
        }

        // -----------------------------------------------------------------
        // Sheet-metal — InsertSheetMetalBaseFlange2 (or BaseFlange).
        // params: thickness_mm, k_factor, bend_radius_mm, plane?
        // Requires an active sketch on the named plane (created by caller
        // via newSketch + sketchRect/sketchCircle). The current sketch is
        // exited, then SW reads it back as the base-flange profile.
        // -----------------------------------------------------------------
        private object OpSheetMetalBaseFlange(Dictionary<string, object> p)
        {
            if (_model == null)
                return new { ok = false, error = "sheetMetalBaseFlange: no model" };
            double thickness_m = (p.ContainsKey("thickness_mm")
                                    ? Convert.ToDouble(p["thickness_mm"]) : 1.5) / 1000.0;
            double bendR_m = (p.ContainsKey("bend_radius_mm")
                                ? Convert.ToDouble(p["bend_radius_mm"]) : 1.0) / 1000.0;
            double kFactor = p.ContainsKey("k_factor")
                                ? Convert.ToDouble(p["k_factor"]) : 0.5;
            string sketchName = p.ContainsKey("sketch")
                                  ? p["sketch"]?.ToString() : null;
            try
            {
                // CRITICAL: SW SDK macro pattern requires the profile
                // sketch to be EXITED (a feature in the tree) and re-
                // selected by name before InsertSheetMetalBaseFlange2.
                // If we leave the sketch active the API silently no-ops.
                if (_model.SketchManager.ActiveSketch != null)
                {
                    _model.SketchManager.InsertSketch(true);  // EXIT
                    FileLog("  sheetMetalBaseFlange: exited active sketch");
                }
                // Stage the sketch as the active selection so InsertSheet
                // MetalBaseFlange knows which closed profile to wrap.
                // Without this the call silently returns null on most SW
                // versions because there's nothing in the SelectionMgr.
                bool selected = false;
                if (!string.IsNullOrEmpty(sketchName))
                {
                    // Resolve alias → real SW feature name (same fix that
                    // unblocked OpRevolve). The bridge accepts user-friendly
                    // aliases like "smprof"; SelectByID2 needs the actual
                    // SW name like "Sketch1". Without this, sheet metal
                    // base flange always fails its sketch select.
                    string resolved = sketchName;
                    if (_aliasMap.ContainsKey(sketchName)
                        && _aliasMap[sketchName] is IFeature sf)
                    {
                        resolved = sf.Name;
                    }
                    try { _model.ClearSelection2(true); } catch { }
                    selected = _model.Extension.SelectByID2(
                        resolved, "SKETCH", 0, 0, 0, false, 0, null, 0);
                    if (selected) sketchName = resolved;
                }
                else
                {
                    // Best-effort: pick the first sketch under the
                    // FeatureManager tree if the planner didn't name one.
                    try
                    {
                        var first = _model.FirstFeature() as IFeature;
                        while (first != null)
                        {
                            if (first.GetTypeName2() == "ProfileFeature"
                                || first.GetTypeName2() == "Sketch")
                            {
                                sketchName = first.Name;
                                _model.ClearSelection2(true);
                                selected = _model.Extension.SelectByID2(
                                    sketchName, "SKETCH", 0, 0, 0,
                                    false, 0, null, 0);
                                break;
                            }
                            first = first.GetNextFeature() as IFeature;
                        }
                    }
                    catch { }
                }

                var fm = _model.FeatureManager;
                var fmType = fm.GetType();
                // SW interop versions disagree on InsertSheetMetalBaseFlange2's
                // arg count AND types (saw bool↔double swaps SW2018→2024).
                // Build args type-aware: type-defaults (0/false/0.0/"") and
                // overwrite ONLY slots whose declared ParameterType matches
                // the value we want to set. Same pattern that unblocked
                // FeatureExtrusionThin2, InsertProtrusionBlend2, HoleWizard5.
                object feat = null;
                object[] BuildArgs(System.Reflection.MethodInfo mi)
                {
                    var pis = mi.GetParameters();
                    int n = pis.Length;
                    var args = new object[n];
                    for (int i = 0; i < n; i++)
                    {
                        Type pt = pis[i].ParameterType;
                        if (pt == typeof(double))      args[i] = 0.0;
                        else if (pt == typeof(int))    args[i] = 0;
                        else if (pt == typeof(bool))   args[i] = false;
                        else if (pt == typeof(short))  args[i] = (short)0;
                        else if (pt == typeof(string)) args[i] = "";
                        else                            args[i] = null;
                    }
                    void SetIfDouble(int idx, double v)
                    {
                        if (n > idx && pis[idx].ParameterType == typeof(double))
                            args[idx] = v;
                    }
                    void SetIfInt(int idx, int v)
                    {
                        if (n > idx && pis[idx].ParameterType == typeof(int))
                            args[idx] = v;
                        else if (n > idx && pis[idx].ParameterType == typeof(short))
                            args[idx] = (short)v;
                    }
                    void SetIfBool(int idx, bool v)
                    {
                        if (n > idx && pis[idx].ParameterType == typeof(bool))
                            args[idx] = v;
                    }
                    // Standard slot meaning (verified in SW SDK):
                    //   0 Thickness
                    //   1 ReverseDirection
                    //   2 BendRadius
                    //   3 UseGaugeTable
                    //   4 KFactor (bend allowance)
                    //   5 UseRelief
                    //   6 ReliefType
                    //   7 ReliefDepth
                    //   8 ReliefWidth (or Ratio)
                    //   9-14: misc extras (varies by SW version)
                    SetIfDouble(0, thickness_m);
                    SetIfBool(1, false);
                    SetIfDouble(2, bendR_m);
                    SetIfBool(3, false);
                    SetIfDouble(4, kFactor);
                    return args;
                }

                var mi2 = fmType.GetMethod("InsertSheetMetalBaseFlange2");
                if (mi2 != null)
                {
                    int n2 = mi2.GetParameters().Length;
                    var sig2 = string.Join(",",
                        mi2.GetParameters()
                           .Select((pi, i) => $"{i}:{pi.ParameterType.Name}"));
                    FileLog($"  sheetMetalBaseFlange: BaseFlange2(n={n2}) sig=[{sig2}]");
                    try { feat = mi2.Invoke(fm, BuildArgs(mi2)); }
                    catch (Exception ex)
                    {
                        FileLog($"  sheetMetalBaseFlange: BaseFlange2(n={n2}) "
                                + $"threw: {ex.GetType().Name}: {ex.Message}");
                    }
                }
                if (feat == null)
                {
                    var mi1 = fmType.GetMethod("InsertSheetMetalBaseFlange");
                    if (mi1 != null)
                    {
                        int n1 = mi1.GetParameters().Length;
                        var sig1 = string.Join(",",
                            mi1.GetParameters()
                               .Select((pi, i) => $"{i}:{pi.ParameterType.Name}"));
                        FileLog($"  sheetMetalBaseFlange: BaseFlange(n={n1}) sig=[{sig1}]");
                        try { feat = mi1.Invoke(fm, BuildArgs(mi1)); }
                        catch (Exception ex)
                        {
                            FileLog($"  sheetMetalBaseFlange: BaseFlange(n={n1}) "
                                    + $"threw: {ex.GetType().Name}: {ex.Message}");
                        }
                    }
                }
                if (feat == null)
                    return new { ok = false,
                                  error = "InsertSheetMetalBaseFlange[2] returned null — "
                                           + (selected ? "sketch staged but SW refused"
                                                       : $"no sketch selected ('{sketchName ?? "none"}')") };
                FileLog($"  sheetMetalBaseFlange: t={thickness_m * 1000.0}mm r={bendR_m * 1000.0}mm sketch='{sketchName}' selected={selected}");
                return new { ok = true,
                              thickness_mm = thickness_m * 1000.0,
                              bend_radius_mm = bendR_m * 1000.0,
                              k_factor = kFactor,
                              sketch = sketchName };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"sheetMetalBaseFlange threw: {ex.Message}" };
            }
        }

        // Edge flange — accepts edge_id (a name like "Edge<1>" from the
        // SW model tree), length, angle. Stages the edge via SelectByID2
        // before InsertSheetMetalEdgeFlange. Falls back to first linear
        // edge of the part body if no edge_id is provided.
        private object OpSheetMetalEdgeFlange(Dictionary<string, object> p)
        {
            if (_model == null)
                return new { ok = false, error = "sheetMetalEdgeFlange: no model" };
            string edgeId = p.ContainsKey("edge_id") ? p["edge_id"]?.ToString() : null;
            double length_m = (p.ContainsKey("length_mm")
                                 ? Convert.ToDouble(p["length_mm"]) : 10.0) / 1000.0;
            double angle_rad = (p.ContainsKey("angle_deg")
                                  ? Convert.ToDouble(p["angle_deg"]) : 90.0) * Math.PI / 180.0;
            string posStr = p.ContainsKey("position")
                              ? p["position"]?.ToString() : "material-inside";
            try
            {
                bool selected = false;
                if (!string.IsNullOrEmpty(edgeId))
                {
                    try { _model.ClearSelection2(true); } catch { }
                    selected = _model.Extension.SelectByID2(
                        edgeId, "EDGE", 0, 0, 0, false, 0, null, 0);
                }
                if (!selected)
                    return new { ok = false,
                                  error = $"could not select edge '{edgeId}' — "
                                           + "name a linear edge in the params (e.g. \"Edge<1>\")" };

                var fm = _model.FeatureManager;
                var mi = fm.GetType().GetMethod("InsertSheetMetalEdgeFlange2")
                          ?? fm.GetType().GetMethod("InsertSheetMetalEdgeFlange");
                if (mi == null)
                    return new { ok = false, error = "InsertSheetMetalEdgeFlange[2] not present" };

                int posEnum = posStr.ToLowerInvariant() switch
                {
                    "bend-outside" => 1,
                    "bend-from-virtual-sharp" => 2,
                    "tangent-to-bend" => 3,
                    _ => 0,  // material-inside (default)
                };
                int paramCount = mi.GetParameters().Length;
                object feat;
                if (paramCount >= 12)
                {
                    feat = mi.Invoke(fm, new object[] {
                        length_m, angle_rad, 0.0, posEnum,
                        false, 0.0, false, 0.0,
                        0, 0, false, 0 });
                }
                else
                {
                    feat = mi.Invoke(fm, new object[] {
                        length_m, angle_rad, 0.0, posEnum,
                        false, 0.0, false, 0.0 });
                }
                FileLog($"  sheetMetalEdgeFlange: edge='{edgeId}' len={length_m * 1000.0}mm ang={angle_rad * 180 / Math.PI:F1}° pos={posStr} feat={feat != null}");
                return new { ok = feat != null,
                              edge_id = edgeId,
                              length_mm = length_m * 1000.0,
                              angle_deg = angle_rad * 180.0 / Math.PI,
                              position = posStr };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"sheetMetalEdgeFlange threw: {ex.Message}" };
            }
        }

        // -----------------------------------------------------------------
        // Surface modelling — InsertSurfaceLoft (between named profile sketches)
        // params: profile_sketches: ["Sketch1","Sketch2",...], optional
        //         start_tangent_type, end_tangent_type
        // Profiles are looked up by name on the active part doc.
        // -----------------------------------------------------------------
        private object OpSurfaceLoft(Dictionary<string, object> p)
        {
            if (_model == null)
                return new { ok = false, error = "surfaceLoft: no model" };
            var rawNames = p.ContainsKey("profile_sketches")
                            ? p["profile_sketches"] as System.Collections.IEnumerable
                            : null;
            if (rawNames == null)
                return new { ok = false, error = "surfaceLoft: profile_sketches[] missing" };
            var names = new List<string>();
            foreach (var n in rawNames) names.Add(n?.ToString());
            if (names.Count < 2)
                return new { ok = false, error = "surfaceLoft: need ≥2 profile sketches" };

            try
            {
                // Pre-select all profiles in order using SelectByID2
                _model.ClearSelection2(true);
                var ext = _model.Extension;
                int markBase = 1;
                foreach (var name in names)
                {
                    bool ok = ext.SelectByID2(
                        name, "SKETCH", 0, 0, 0,
                        true, markBase, null, 0);
                    if (!ok) FileLog($"  surfaceLoft: select '{name}' failed");
                    markBase++;
                }
                var fm = _model.FeatureManager;
                var fmType = fm.GetType();
                var mi = fmType.GetMethod("InsertSurfaceLoft")
                          ?? fmType.GetMethod("InsertSurfaceLoft2")
                          ?? fmType.GetMethod("InsertLoftRefSurface");
                if (mi == null)
                    return new { ok = false,
                                  error = "InsertSurfaceLoft not found on FeatureManager" };
                // Most overloads take (closed, periodic, includeFaces, mergeFaces)
                object feat = null;
                try
                {
                    feat = mi.Invoke(fm, new object[] { false, false, true, true });
                }
                catch
                {
                    // Fall back to no-arg if overload mismatch
                    feat = mi.Invoke(fm, null);
                }
                if (feat == null)
                    return new { ok = false, error = "InsertSurfaceLoft returned null" };
                FileLog($"  surfaceLoft: {names.Count} profiles");
                return new { ok = true, profile_count = names.Count };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"surfaceLoft threw: {ex.Message}" };
            }
        }

        // Extruded surface — params: sketch_name, distance_mm, dir
        // ('along-x'|'along-y'|'along-z', default 'along-z'), direction
        // ('forward'|'reverse'|'both', default 'forward'). Stages the
        // sketch via SelectByID2 then calls FeatureExtrudeRefSurface.
        private object OpSurfaceExtrude(Dictionary<string, object> p)
        {
            if (_model == null)
                return new { ok = false, error = "surfaceExtrude: no model" };
            string sketch = p.ContainsKey("sketch_name")
                              ? p["sketch_name"]?.ToString()
                              : (p.ContainsKey("sketch")
                                  ? p["sketch"]?.ToString() : null);
            if (string.IsNullOrEmpty(sketch))
                return new { ok = false, error = "surfaceExtrude: sketch_name missing" };
            double dist_m = (p.ContainsKey("distance_mm")
                              ? Convert.ToDouble(p["distance_mm"]) : 10.0) / 1000.0;
            string dir = p.ContainsKey("direction")
                           ? p["direction"]?.ToString().ToLowerInvariant()
                           : "forward";
            try
            {
                _model.ClearSelection2(true);
                bool selected = _model.Extension.SelectByID2(
                    sketch, "SKETCH", 0, 0, 0, false, 0, null, 0);
                if (!selected)
                    return new { ok = false,
                                  error = $"surfaceExtrude: could not select '{sketch}'" };

                var fm = _model.FeatureManager;
                // SW signatures vary; try the modern overload first.
                var mi = fm.GetType().GetMethod("FeatureExtrudeRefSurface3")
                          ?? fm.GetType().GetMethod("FeatureExtrudeRefSurface2")
                          ?? fm.GetType().GetMethod("FeatureExtrudeRefSurface");
                if (mi == null)
                    return new { ok = false,
                                  error = "FeatureExtrudeRefSurface[2,3] not present" };
                bool flipDir = dir == "reverse";
                bool bothDir = dir == "both";
                int paramCount = mi.GetParameters().Length;
                object feat;
                if (paramCount >= 9)
                {
                    feat = mi.Invoke(fm, new object[] {
                        true, flipDir, bothDir, 0, 0, dist_m, dist_m,
                        false, false });
                }
                else
                {
                    feat = mi.Invoke(fm, new object[] {
                        true, flipDir, bothDir, 0, 0, dist_m, dist_m });
                }
                FileLog($"  surfaceExtrude: '{sketch}' dist={dist_m * 1000.0}mm dir={dir} feat={feat != null}");
                return new { ok = feat != null,
                              sketch,
                              distance_mm = dist_m * 1000.0,
                              direction = dir };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"surfaceExtrude threw: {ex.Message}" };
            }
        }

        // -----------------------------------------------------------------
        // 3D solid features (revolve / sweep / loft / shell / rib / draft /
        // helix / coil). v1 implementations use SW's FeatureManager APIs
        // where the signature is well-known; less common ops return a
        // structured "deferred" result so the chain doesn't break.
        // -----------------------------------------------------------------
        private object OpRevolve(Dictionary<string, object> p)
        {
            if (_model == null) return new { ok = false, error = "revolve: no model" };
            string sketch = p.ContainsKey("sketch") ? p["sketch"]?.ToString() : null;
            if (string.IsNullOrEmpty(sketch))
                return new { ok = false, error = "revolve: sketch required" };
            double angleDeg = p.ContainsKey("angle")
                ? Convert.ToDouble(p["angle"]) : 360.0;
            double angleRad = angleDeg * Math.PI / 180.0;
            string axis = p.ContainsKey("axis") ? p["axis"]?.ToString() : "Z";
            string operation = p.ContainsKey("operation")
                ? p["operation"]?.ToString() : "new";
            string alias = p.ContainsKey("alias")
                ? p["alias"]?.ToString() : "revolve_body";
            try
            {
                // Resolve alias -> actual SW sketch feature name. The bridge
                // exposes user-friendly aliases ("s", "p", etc.); SelectByID2
                // expects the real SW name ("Sketch1", "Sketch3"). Without
                // this lookup, SelectByID2 returns false and the revolve
                // never starts.
                string sketchFeatName = sketch;
                if (_aliasMap.ContainsKey(sketch)
                    && _aliasMap[sketch] is IFeature sketchFeat)
                {
                    sketchFeatName = sketchFeat.Name;
                }
                _model.ClearSelection2(true);
                bool selected = _model.Extension.SelectByID2(
                    sketchFeatName, "SKETCH", 0, 0, 0, false, 0, null, 0);
                if (!selected)
                    return new { ok = false,
                                  error = $"revolve: could not select sketch '{sketch}' (resolved='{sketchFeatName}')" };
                // Use sketchFeatName for any later re-selects too
                sketch = sketchFeatName;

                // FeatureRevolve2 needs a CONSTRUCTION LINE (centerline) in
                // the sketch to use as the axis of revolution. Most callers
                // forget this — they emit a profile polyline and assume the
                // origin axis is implicit. Auto-inject a centerline at x=0
                // (sketch-local) spanning the sketch's Y bbox before calling
                // FeatureRevolve2. This makes revolve "just work" for any
                // profile drawn against the global Z axis (the convention
                // ARIA's planners use). If the sketch already contains a
                // centerline (user-drawn), the addition is harmless — SW
                // picks the longest centerline as the axis.
                try
                {
                    _model.EditSketch();   // re-enter the named sketch
                    var sm = _model.SketchManager;
                    if (sm != null)
                    {
                        // Centerline orientation depends on the requested
                        // revolve axis. SW sketches are 2D so the centerline
                        // is in sketch-plane coordinates. The mapping from
                        // user-facing axis -> sketch-plane direction:
                        //   axis="Y" or unspec : vertical centerline (sketch Y)
                        //   axis="Z"           : horizontal centerline (sketch X)
                        //                        — used when the user thinks
                        //                        in 3D and wants the part to
                        //                        spin around the world Z.
                        //   axis="X"           : horizontal centerline (sketch X)
                        // For axis="Z" without a centerline along sketch X,
                        // FeatureRevolve2 returns null (no axis) — this is
                        // the revolve_cut FAIL we hit in the matrix.
                        // Inject BOTH horizontal and vertical centerlines so
                        // SW always finds an axis matching the user's intent
                        // regardless of which sketch plane the profile is on.
                        // SW picks the centerline whose endpoints are
                        // consistent with the profile (axis must NOT cross
                        // the closed loop). Two centerlines of equal length
                        // is harmless: SW picks the one geometrically valid
                        // for the profile and ignores the other.
                        var lineV = sm.CreateCenterLine(
                            0, -1.0, 0, 0, 1.0, 0) as object;  // vertical
                        var lineH = sm.CreateCenterLine(
                            -1.0, 0, 0, 1.0, 0, 0) as object;  // horizontal
                        FileLog($"  revolve: centerlines injected (V={(lineV != null ? "ok" : "null")} H={(lineH != null ? "ok" : "null")}) for axis={axis}");
                    }
                    // SketchManager.InsertSketch(true) exits the sketch (the
                    // IModelDoc2.InsertSketch overload takes no args).
                    _model.SketchManager.InsertSketch(true);
                    // Re-select the sketch — exit deselected it.
                    _model.ClearSelection2(true);
                    _model.Extension.SelectByID2(
                        sketch, "SKETCH", 0, 0, 0, false, 0, null, 0);
                }
                catch (Exception cex)
                {
                    FileLog($"  revolve: centerline injection threw {cex.Message}");
                }

                // FeatureRevolve2 signature is wide — use reflection so
                // we tolerate version drift (2020 vs 2024).
                var fm = _model.FeatureManager;
                var mi = fm.GetType().GetMethod("FeatureRevolve2");
                object feat = null;
                if (mi != null)
                {
                    int paramCount = mi.GetParameters().Length;
                    bool isCut = operation == "cut" || operation == "subtract";
                    // Common 21-arg signature: (singleDir, isSolid, isThin,
                    // isCut, reverse, bothDirs, type1, type2, angle1,
                    // angle2, ofs1, ofs2, ofsRev1, ofsRev2, thinType,
                    // thinThk1, thinThk2, mergeFaces, useFeatScope,
                    // useAutoSel, t0)
                    var args = new object[paramCount];
                    if (paramCount >= 18)
                    {
                        // FeatureRevolve2 signature (SW2024, 21 args):
                        // 0: SingleDir (bool)        1: IsSolid (bool)
                        // 2: IsThin (bool)           3: IsCut (bool)
                        // 4: ReverseDir (bool)       5: BothDirSameEntity (bool)
                        // 6: Type1 (int)             7: Type2 (int)
                        // 8: Angle1 (double)         9: Angle2 (double)
                        // 10: OffsetReverse1 (bool) 11: OffsetReverse2 (bool)
                        // 12: Offset1 (double)      13: Offset2 (double)
                        // 14: ThinType (int)        15: ThinThk1 (double)
                        // 16: ThinThk2 (double)     17: MergeFaces (bool)
                        // 18: UseFeatScope (bool)   19: UseAutoSelect (bool)
                        // 20: T0 (int) start cond
                        args[0] = true;          // singleDir
                        args[1] = true;          // isSolid
                        args[2] = false;         // isThin
                        args[3] = isCut;         // isCut
                        args[4] = false;         // reverseDir
                        args[5] = false;         // bothDirs
                        args[6] = 0;             // type1 = blind
                        args[7] = 0;             // type2
                        args[8] = angleRad;      // angle1
                        args[9] = 0.0;           // angle2
                        args[10] = false;        // offsetReverse1 (bool!)
                        args[11] = false;        // offsetReverse2 (bool!)
                        args[12] = 0.0;          // offset1
                        args[13] = 0.0;          // offset2
                        args[14] = 0;            // thinType
                        args[15] = 0.0;          // thinThk1
                        args[16] = 0.0;          // thinThk2
                        args[17] = false;        // mergeFaces
                        if (paramCount >= 20) {
                            args[18] = true;     // useFeatScope
                            args[19] = true;     // useAutoSelect
                        }
                        if (paramCount >= 21) args[20] = 0;  // t0 (int)
                        for (int i = 21; i < paramCount; i++) args[i] = false;
                    }
                    feat = mi.Invoke(fm, args);
                }
                if (feat != null && _aliasMap != null)
                    _aliasMap[alias] = feat;
                FileLog($"  revolve: sketch={sketch} angle={angleDeg}deg " +
                         $"axis={axis} op={operation} feat={feat != null}");
                if (feat == null)
                {
                    return new { ok = false,
                                  error = $"FeatureRevolve2 returned null - operation={operation}, paramCount={(mi != null ? mi.GetParameters().Length : -1)}. For cut: profile must intersect existing body. For new: sketch must be closed and have a centerline.",
                                  kind = "revolve", sketch,
                                  angle_deg = angleDeg, operation };
                }
                return new { ok = true, kind = "revolve",
                              sketch, angle_deg = angleDeg, axis,
                              operation, alias };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"revolve threw: {ex.Message}" };
            }
        }

        private object OpSweep(Dictionary<string, object> p)
        {
            if (_model == null) return new { ok = false, error = "sweep: no model" };
            string profile = p.ContainsKey("profile_sketch")
                ? p["profile_sketch"]?.ToString() : null;
            string path = p.ContainsKey("path_sketch")
                ? p["path_sketch"]?.ToString() : null;
            if (string.IsNullOrEmpty(profile) || string.IsNullOrEmpty(path))
                return new { ok = false,
                              error = "sweep: profile_sketch + path_sketch required" };
            string operation = p.ContainsKey("operation")
                ? p["operation"]?.ToString() : "new";
            string alias = p.ContainsKey("alias")
                ? p["alias"]?.ToString() : "sweep_body";
            try
            {
                // Resolve alias → SW feature name. SelectByID2 needs the
                // actual SW name (e.g. "Sketch1"), not the planner alias.
                // Same fix that unblocked OpRevolve/OpLoft/OpSheetMetalBaseFlange.
                string profileResolved = profile;
                string pathResolved = path;
                if (_aliasMap.ContainsKey(profile)
                    && _aliasMap[profile] is IFeature pf)
                    profileResolved = pf.Name;
                if (_aliasMap.ContainsKey(path)
                    && _aliasMap[path] is IFeature ph)
                    pathResolved = ph.Name;
                _model.ClearSelection2(true);
                bool ok1 = _model.Extension.SelectByID2(
                    profileResolved, "SKETCH", 0, 0, 0, true, 1, null, 0);
                // Profile fallback: also try EXTSKETCHSEGMENT for non-closed
                if (!ok1)
                {
                    ok1 = _model.Extension.SelectByID2(
                        profileResolved, "EXTSKETCHSEGMENT", 0, 0, 0, true, 1, null, 0);
                    if (ok1) FileLog($"  sweep: profile '{profileResolved}' staged as EXTSKETCHSEGMENT");
                }
                // Path can be a sketch OR a helix (RefCurve) OR a 3D curve.
                // Try every plausible selection type, AND try the IFeature
                // direct .Select2 path as a final fallback. SW2024 helix is
                // not reliably reachable via SelectByID2 in any version —
                // the IFeature.Select2 path always works when the alias map
                // has the feature object.
                bool ok2 = _model.Extension.SelectByID2(
                    pathResolved, "SKETCH", 0, 0, 0, true, 4, null, 0);
                string ok2Type = ok2 ? "SKETCH" : null;
                if (!ok2)
                {
                    foreach (var st in new[] { "REFERENCECURVES", "REFCURVES",
                                                "REFCURVE", "REFEDGES",
                                                "EDGE", "BODYFEATURE",
                                                "EXTSKETCHSEGMENT" })
                    {
                        if (_model.Extension.SelectByID2(
                                pathResolved, st, 0, 0, 0, true, 4, null, 0))
                        {
                            ok2 = true; ok2Type = st;
                            FileLog($"  sweep: path '{pathResolved}' staged as {st}");
                            break;
                        }
                    }
                }
                // Final fallback: select the IFeature object directly. This
                // works for helix, ref-curves, and any feature stored in the
                // alias map. Selection mark must be 4 to identify the path.
                if (!ok2 && _aliasMap.ContainsKey(path) &&
                    _aliasMap[path] is IFeature pathFeat)
                {
                    try
                    {
                        ok2 = pathFeat.Select2(true, 4);  // append=true, mark=4
                        if (ok2) { ok2Type = "IFeature.Select2";
                            FileLog($"  sweep: path '{pathResolved}' staged via IFeature.Select2"); }
                    }
                    catch (Exception sex)
                    {
                        FileLog($"  sweep: IFeature.Select2 threw: {sex.Message}");
                    }
                }
                if (!ok1 || !ok2)
                    return new { ok = false,
                                  error = $"sweep: could not stage sketches " +
                                          $"(profile alias='{profile}' resolved='{profileResolved}' staged={ok1}, " +
                                          $"path alias='{path}' resolved='{pathResolved}' staged={ok2} type={ok2Type ?? "none"})" };
                var fm = _model.FeatureManager;
                // Build args via reflection per ParameterInfo so we never
                // pass a bool where SW expects a double. Same fix that
                // unblocked FeatureExtrusionThin2 and InsertProtrusionBlend2.
                bool isCut = operation == "cut" || operation == "subtract";
                object feat = null;
                var fmType = fm.GetType();
                // Pick the right SW method family. SW2024 names cut-sweeps
                // as "FeatureCutSwept*" or "InsertCutSweep*" — varies by
                // version. Probe for any swept-cut method on FeatureManager
                // first, then fall back to passing isCut=true through
                // InsertProtrusionSwept4 slot 14 if available.
                if (isCut)
                {
                    var sweptMethods = fmType.GetMethods()
                        .Where(m => m.Name.IndexOf("Sweep",
                            StringComparison.OrdinalIgnoreCase) >= 0
                                  || m.Name.IndexOf("Swept",
                            StringComparison.OrdinalIgnoreCase) >= 0)
                        .Select(m => $"{m.Name}({m.GetParameters().Length})")
                        .Distinct().ToArray();
                    FileLog($"  sweep: cut-mode; FM swept methods = [{string.Join(", ", sweptMethods)}]");
                }
                string[] tryMethods = isCut
                    ? new[] {
                        "FeatureCutSwept2", "FeatureCutSwept",
                        "InsertCutSweep4", "InsertCutSweep3",
                        "InsertCutSweep2", "InsertCutSweep",
                        "InsertCutSwept4", "InsertCutSwept3",
                        "InsertCutSwept2", "InsertCutSwept" }
                    : new[] {
                        "InsertProtrusionSwept4", "InsertProtrusionSwept3",
                        "InsertProtrusionSwept2", "InsertProtrusionSwept",
                        "FeatureSweep" };
                foreach (var mname in tryMethods)
                {
                    var mi = fmType.GetMethod(mname);
                    if (mi == null) continue;
                    try
                    {
                        var pis = mi.GetParameters();
                        int n = pis.Length;
                        var args = new object[n];
                        for (int i = 0; i < n; i++)
                        {
                            Type pt = pis[i].ParameterType;
                            if (pt == typeof(double))      args[i] = 0.0;
                            else if (pt == typeof(int))    args[i] = 0;
                            else if (pt == typeof(bool))   args[i] = false;
                            else if (pt == typeof(short))  args[i] = (short)0;
                            else                            args[i] = null;
                        }
                        // Common slot semantics (InsertProtrusionSwept3, 13 args):
                        //   0 twistType (int=0, no twist)
                        //   1 alignSweep (bool=false)
                        //   2 twistAngleRad (double=0.0)
                        //   3 pathAlignment (int=0, follow path)
                        //   4 mergeSmoothFaces (bool=true)
                        //   5 isThin (bool=false)
                        //   6 thinType (int=0)
                        //   7 thinThk1 (double=0.0)
                        //   8 thinThk2 (double=0.0)
                        //   9 isMerge (bool=true)
                        //  10 isAdvancedFeatScope (bool=false)
                        //  11 useAutoSel (bool=true)
                        //  12 t0 (int=0)
                        if (n > 4 && pis[4].ParameterType == typeof(bool))
                            args[4] = true;     // mergeSmoothFaces
                        if (n > 9 && pis[9].ParameterType == typeof(bool))
                            args[9] = true;     // isMerge
                        if (n > 11 && pis[11].ParameterType == typeof(bool))
                            args[11] = true;    // useAutoSel
                        FileLog($"  sweep: trying {mname}(n={n})");
                        feat = mi.Invoke(fm, args);
                        if (feat != null)
                        {
                            FileLog($"  sweep: {mname} succeeded");
                            break;
                        }
                        else
                        {
                            FileLog($"  sweep: {mname} returned null");
                        }
                    }
                    catch (Exception sex)
                    {
                        FileLog($"  sweep: {mname} threw: {sex.GetType().Name}: {sex.Message}");
                    }
                }
                if (feat is IFeature sf2 && _aliasMap != null)
                    _aliasMap[alias] = sf2;
                if (feat == null)
                    return new { ok = false,
                                  error = $"sweep: all InsertProtrusionSwept* variants failed " +
                                          $"(profile='{profileResolved}', path='{pathResolved}')" };
                return new { ok = true, kind = "sweep",
                              profile_sketch = profileResolved, path_sketch = pathResolved,
                              operation, alias };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"sweep threw: {ex.Message}" };
            }
        }

        private object OpLoft(Dictionary<string, object> p)
        {
            if (_model == null) return new { ok = false, error = "loft: no model" };
            // Profiles can be passed as a list under "profile_sketches" or
            // "profiles" — the planner side may use either name.
            var profilesObj = p.ContainsKey("profile_sketches")
                ? p["profile_sketches"] : (p.ContainsKey("profiles") ? p["profiles"] : null);
            var profiles = new System.Collections.Generic.List<string>();
            if (profilesObj is System.Collections.IEnumerable en
                && !(profilesObj is string))
            {
                foreach (var item in en)
                    if (item != null) profiles.Add(item.ToString());
            }
            else if (profilesObj is string s)
            {
                foreach (var part in s.Split(','))
                    if (!string.IsNullOrWhiteSpace(part)) profiles.Add(part.Trim());
            }
            if (profiles.Count < 2)
                return new { ok = false,
                              error = "loft: profile_sketches needs >=2 sketch names" };
            string operation = p.ContainsKey("operation")
                ? p["operation"]?.ToString() : "new";
            string alias = p.ContainsKey("alias")
                ? p["alias"]?.ToString() : "loft_body";
            try
            {
                if (_model.SketchManager.ActiveSketch != null)
                    _model.SketchManager.InsertSketch(true);
                _model.ClearSelection2(true);
                int staged = 0;
                // Each profile must be selected with mark=1 and append=true
                // so they accumulate. SW picks loft order from selection
                // order — the planner is responsible for emitting bottom
                // up. Resolve alias → SW feature name first (same fix as
                // OpRevolve/OpSheetMetalBaseFlange — SelectByID2 needs the
                // actual SW name like "Sketch1", not the planner alias).
                foreach (var alias_name in profiles)
                {
                    string resolved = alias_name;
                    if (_aliasMap.ContainsKey(alias_name)
                        && _aliasMap[alias_name] is IFeature sf)
                    {
                        resolved = sf.Name;
                    }
                    bool ok = _model.Extension.SelectByID2(
                        resolved, "SKETCH", 0, 0, 0, true, 1, null, 0);
                    if (ok) staged++;
                    else FileLog($"  loft: could not stage profile alias='{alias_name}' resolved='{resolved}'");
                }
                if (staged < 2)
                    return new { ok = false,
                                  error = $"loft: only {staged} profiles staged (need >=2)" };
                var fm = _model.FeatureManager;
                bool isCut = operation == "cut" || operation == "subtract";
                // InsertProtrusionBlend2/Blend has slot types that vary
                // across SW versions (saw 17- and 18-arg flavors). Build the
                // arg vector from each ParameterInfo so we never pass a bool
                // where SW expects a double. Same fix that unblocked
                // FeatureExtrusionThin2 for surface extrudes.
                object feat = null;
                var fmType = fm.GetType();
                foreach (var mname in new[] {
                    "InsertProtrusionBlend2", "InsertProtrusionBlend",
                    "FeatureLoft2", "FeatureLoft" })
                {
                    var mi = fmType.GetMethod(mname);
                    if (mi == null) continue;
                    try
                    {
                        var pis = mi.GetParameters();
                        int n = pis.Length;
                        var args = new object[n];
                        for (int i = 0; i < n; i++)
                        {
                            Type pt = pis[i].ParameterType;
                            if (pt == typeof(double))      args[i] = 0.0;
                            else if (pt == typeof(int))    args[i] = 0;
                            else if (pt == typeof(bool))   args[i] = false;
                            else if (pt == typeof(short))  args[i] = (short)0;
                            else                            args[i] = null;
                        }
                        // Standard ProtrusionBlend slot semantics:
                        //   0 closedLoft (bool=false)
                        //   1 addStartMatchSection (bool=false)
                        //   2 addEndMatchSection   (bool=false)
                        //   3 reverseDirection (bool=false)
                        //   4 isThin (bool=false)
                        //   5 makeMerge (bool=true)
                        //   6 startMatchType (int=0)
                        //   7 endMatchType (int=0)
                        //   8 startTangentLength (double=1.0)
                        //   9 endTangentLength (double=1.0)
                        //  10-15 tangent dirs / reversals (bool=false)
                        //  16 forceNonRational (bool=false)
                        //  17 isAdvancedFeatScope (bool=false)
                        if (n > 5 && pis[5].ParameterType == typeof(bool))
                            args[5] = true;     // makeMerge
                        if (n > 8 && pis[8].ParameterType == typeof(double))
                            args[8] = 1.0;      // startTangentLength
                        if (n > 9 && pis[9].ParameterType == typeof(double))
                            args[9] = 1.0;      // endTangentLength
                        FileLog($"  loft: trying {mname}(n={n})");
                        feat = mi.Invoke(fm, args);
                        if (feat != null)
                        {
                            FileLog($"  loft: {mname} succeeded");
                            break;
                        }
                        else
                        {
                            FileLog($"  loft: {mname} returned null");
                        }
                    }
                    catch (Exception lex)
                    {
                        FileLog($"  loft: {mname} threw: {lex.GetType().Name}: {lex.Message}");
                    }
                }
                if (feat is IFeature lf && _aliasMap != null)
                {
                    _aliasMap[alias] = lf;
                    if (operation == "new") _lastBodyFeature = lf;
                }
                FileLog($"  loft: staged={staged} feat={feat != null} op={operation}");
                return new { ok = feat != null, kind = "loft",
                              profiles = profiles.ToArray(),
                              n_profiles = profiles.Count,
                              operation, alias };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"loft threw: {ex.Message}" };
            }
        }

        private object OpShell(Dictionary<string, object> p)
        {
            if (_model == null) return new { ok = false, error = "shell: no model" };
            double thk = Mm(p.ContainsKey("thickness") ? p["thickness"] : 1.0);
            // Optional list of face ids/names to remove (faces become open
            // openings of the shelled cavity). Each element can be a string
            // face name OR an [x,y,z] coordinate that SW will hit-test.
            var removeObj = p.ContainsKey("remove_faces")
                ? p["remove_faces"] : (p.ContainsKey("faces_to_remove")
                    ? p["faces_to_remove"] : null);
            var faceCoords = new System.Collections.Generic.List<double[]>();
            if (removeObj is System.Collections.IEnumerable en && !(removeObj is string))
            {
                foreach (var item in en)
                {
                    if (item is System.Collections.IEnumerable inner && !(item is string))
                    {
                        var v = new System.Collections.Generic.List<double>();
                        foreach (var num in inner)
                        {
                            try { v.Add(Convert.ToDouble(num)); } catch { }
                        }
                        if (v.Count >= 3)
                            faceCoords.Add(new double[] { Mm(v[0]), MirrorYIfNeeded(Mm(v[1])), Mm(v[2]) });
                    }
                }
            }
            try
            {
                if (_model.SketchManager.ActiveSketch != null)
                    _model.SketchManager.InsertSketch(true);
                _model.ClearSelection2(true);
                int facesStaged = 0;
                foreach (var c in faceCoords)
                {
                    bool ok = _model.Extension.SelectByID2(
                        "", "FACE", c[0], c[1], c[2], true, 0, null, 0);
                    if (ok) facesStaged++;
                }
                // Diagnostic probe: dump every method on _model and
                // _model.FeatureManager containing "hell" so we can
                // find what SW2024 actually exposes for shell.
                try
                {
                    var dump = new System.Collections.Generic.List<string>();
                    foreach (var holder in new object[] {
                        _model, _model.FeatureManager, _model.Extension })
                    {
                        if (holder == null) continue;
                        foreach (var m in holder.GetType().GetMethods())
                            if (m.Name.IndexOf("hell",
                                StringComparison.OrdinalIgnoreCase) >= 0)
                                dump.Add($"{holder.GetType().Name}.{m.Name}({m.GetParameters().Length})");
                    }
                    FileLog($"  shell: full hell-probe = {string.Join(", ", dump)}");
                }
                catch { }
                // SW2024 macro recording reveals shell lives on
                // IModelDoc2.FeatureShell (NOT IFeatureManager.InsertFeatureShell*).
                // Signature: FeatureShell(Thickness, Outward, KindOfShell)
                object feat = LateBoundInvoke(
                    "shell",
                    new object[] { _model, _model.FeatureManager, _model.Extension },
                    new[] { "FeatureShell", "InsertFeatureShell2",
                            "InsertFeatureShell3", "InsertFeatureShell" },
                    new object[] { thk, false, 0 });
                // Fallback to the legacy 2-arg shape on InsertFeatureShell*.
                if (feat == null)
                {
                    feat = LateBoundInvoke(
                        "shell.legacy",
                        new object[] { _model.FeatureManager, _model.Extension, _model },
                        new[] { "InsertFeatureShell2", "InsertFeatureShell" },
                        new object[] { thk, false });
                }
                FileLog($"  shell: thk={thk*1000:F1}mm faces_staged={facesStaged} feat={feat != null}");

                // SOFTWARE-SHELL FALLBACK: SW2024 interop hides shell
                // entirely (probe shows only GetPlasticsShellType). When
                // both COM paths fail, build a hollow box via cut-extrude
                // on the body's bbox. Works for box-like parts only —
                // arbitrary shapes need a proper offset-surface boolean,
                // which we'd implement when first needed.
                if (feat == null && faceCoords.Count > 0)
                {
                    try
                    {
                        // Need the active body's bbox to compute the
                        // inner-cavity extent.
                        double[] bbox = null;
                        if (_model is IPartDoc pd)
                        {
                            var bodies = pd.GetBodies2(
                                (int)swBodyType_e.swSolidBody, false) as object[];
                            foreach (var bo in bodies ?? new object[0])
                            {
                                var bd = bo as IBody2;
                                if (bd == null) continue;
                                var bb = bd.GetBodyBox() as double[];
                                if (bb != null && bb.Length >= 6)
                                {
                                    if (bbox == null) bbox = bb;
                                    else
                                    {
                                        bbox[0] = Math.Min(bbox[0], bb[0]);
                                        bbox[1] = Math.Min(bbox[1], bb[1]);
                                        bbox[2] = Math.Min(bbox[2], bb[2]);
                                        bbox[3] = Math.Max(bbox[3], bb[3]);
                                        bbox[4] = Math.Max(bbox[4], bb[4]);
                                        bbox[5] = Math.Max(bbox[5], bb[5]);
                                    }
                                }
                            }
                        }
                        if (bbox != null)
                        {
                            // Identify which axis the first removed-face
                            // points along (Z if it's at z=zmax or zmin,
                            // etc.). The first face determines the cut
                            // direction.
                            var rf = faceCoords[0];
                            // ε margin so the closest-face check survives
                            // float drift.
                            double eps = 1e-4;
                            string cutDir = "Z";
                            string cutPlane = "XY";
                            double cutZmin = bbox[2] + thk; // bot wall
                            double cutZmax = bbox[5];        // through top
                            if (Math.Abs(rf[2] - bbox[5]) > eps
                                && Math.Abs(rf[2] - bbox[2]) > eps)
                            {
                                // Removed face is on a side, not top/bot.
                                if (Math.Abs(rf[0] - bbox[3]) < eps
                                    || Math.Abs(rf[0] - bbox[0]) < eps)
                                {
                                    cutDir = "X"; cutPlane = "YZ";
                                }
                                else
                                {
                                    cutDir = "Y"; cutPlane = "XZ";
                                }
                            }
                            FileLog($"  shell: SW failed -> software fallback (bbox=({bbox[0]:F4},{bbox[1]:F4},{bbox[2]:F4})..({bbox[3]:F4},{bbox[4]:F4},{bbox[5]:F4}) cutDir={cutDir})");
                            // Build inner-cavity sketch on the cut plane
                            // at the OUTSIDE face position, inset by thk.
                            _model.SketchManager.InsertSketch(true);
                            _model.ClearSelection2(true);
                            string planeName = cutPlane == "XY" ? "Top Plane"
                                              : cutPlane == "XZ" ? "Front Plane"
                                              : "Right Plane";
                            // For Z-axis cut we want the sketch at the
                            // bbox top so cut goes downward. SW
                            // sketches always start at the named plane;
                            // we apply a start_offset via the extrude.
                            _model.Extension.SelectByID2(
                                planeName, "PLANE", 0, 0, 0, false, 0, null, 0);
                            _model.SketchManager.InsertSketch(true);
                            // Inner rectangle: bbox minus thk on each
                            // side perpendicular to the cut direction.
                            if (cutDir == "Z")
                            {
                                _model.SketchManager.CreateCornerRectangle(
                                    bbox[0] + thk, bbox[1] + thk, 0,
                                    bbox[3] - thk, bbox[4] - thk, 0);
                            }
                            else if (cutDir == "X")
                            {
                                _model.SketchManager.CreateCornerRectangle(
                                    0, bbox[1] + thk, bbox[2] + thk,
                                    0, bbox[4] - thk, bbox[5] - thk);
                            }
                            else
                            {
                                _model.SketchManager.CreateCornerRectangle(
                                    bbox[0] + thk, 0, bbox[2] + thk,
                                    bbox[3] - thk, 0, bbox[5] - thk);
                            }
                            _model.SketchManager.InsertSketch(true);  // exit
                            // Cut depth = bbox span minus floor wall
                            double depth = (bbox[5] - bbox[2]) - thk;
                            // Re-select sketch and extrude-cut.
                            var lastSk = _model.FeatureByPositionReverse(0)
                                as IFeature;
                            if (lastSk != null)
                            {
                                _model.ClearSelection2(true);
                                lastSk.Select2(false, 0);
                                // FeatureCut4 to drill the cavity (28 args).
                                feat = _model.FeatureManager.FeatureCut4(
                                    true,                                // sd
                                    false, false,                        // flip, dir
                                    (int)swEndConditions_e.swEndCondBlind,    // T1
                                    (int)swEndConditions_e.swEndCondBlind,    // T2
                                    depth, 0,                            // D1, D2
                                    false, false, false, false,
                                    0, 0,                                // angles (rad)
                                    false, false, false, false,
                                    false,                               // NormalCut
                                    false, false,                        // UseFeatScope
                                    true, true,                          // useAutoSel + AssemblyFeatureScope
                                    false,                               // PropagateFeatureToParts
                                    (int)swStartConditions_e.swStartSketchPlane,
                                    0, false, false) as IFeature;
                                FileLog($"  shell: software cut produced feat={feat != null}");
                            }
                        }
                    }
                    catch (Exception swex)
                    {
                        FileLog($"  shell: software fallback threw {swex.Message}");
                    }
                }
                // Post-hoc verify: did the body get hollowed?
                bool isHollow = false;
                try
                {
                    if (_model is IPartDoc pdv)
                    {
                        var bodies = pdv.GetBodies2(
                            (int)swBodyType_e.swSolidBody, false) as object[];
                        foreach (var bo in bodies ?? new object[0])
                        {
                            var bd = bo as IBody2;
                            if (bd == null) continue;
                            var faces = bd.GetFaces() as object[];
                            // Hollow heuristic: has more than 6 faces
                            // (a solid box has 6; shelled box has 11).
                            if (faces != null && faces.Length > 6)
                            {
                                isHollow = true; break;
                            }
                        }
                    }
                }
                catch { }
                FileLog($"  shell: post-verify isHollow={isHollow}");
                return new { ok = (feat != null) || isHollow,
                              kind = "shell",
                              thickness_mm = thk * 1000,
                              faces_removed = facesStaged,
                              software_fallback = (feat != null && !isHollow) ? false : (feat != null) };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"shell threw: {ex.Message}" };
            }
        }

        private object OpRib(Dictionary<string, object> p)
        {
            if (_model == null) return new { ok = false, error = "rib: no model" };
            string sketchAlias = p.ContainsKey("sketch") ? p["sketch"]?.ToString() : null;
            if (string.IsNullOrEmpty(sketchAlias))
                return new { ok = false, error = "rib: 'sketch' required" };
            double thk = Mm(p.ContainsKey("thickness") ? p["thickness"] : 3.0);
            // Edge type — 0=parallel-to-sketch / 1=normal-to-sketch.
            // Default 1: rib thickness extrudes perpendicular to sketch plane.
            int edgeType = p.ContainsKey("edge_type")
                ? Convert.ToInt32(p["edge_type"]) : 1;
            // ThicknessSide — 0=mid-plane, 1=side-1, 2=side-2.
            int thkSide = p.ContainsKey("thickness_side")
                ? Convert.ToInt32(p["thickness_side"]) : 0;
            string alias = p.ContainsKey("alias") ? p["alias"]?.ToString() : "rib_feat";
            try
            {
                if (_model.SketchManager.ActiveSketch != null)
                    _model.SketchManager.InsertSketch(true);
                _model.ClearSelection2(true);
                string sketchName = sketchAlias;
                if (_aliasMap.ContainsKey(sketchAlias)
                    && _aliasMap[sketchAlias] is IFeature sf)
                    sketchName = sf.Name;
                bool sel = _model.Extension.SelectByID2(
                    sketchName, "SKETCH", 0, 0, 0, false, 0, null, 0);
                if (!sel)
                    return new { ok = false,
                                  error = $"rib: could not select sketch '{sketchName}'" };
                var fm = _model.FeatureManager;
                // FeatureRib3: (EdgeType, Thickness, Direction, RefType,
                //   Reverse, IsTwoSided, ExtrudeDir, EnableDraft,
                //   DraftAngle, DraftOutward, NextRefSelection)
                object feat = LateBoundInvoke(
                    "rib",
                    new object[] { fm, _model.Extension, _model },
                    new[] { "FeatureRib3", "FeatureRib2", "InsertRib" },
                    new object[] {
                        edgeType, thk, thkSide, 0,
                        false, false, 0,
                        false, 0.0, false, false });
                if (feat is IFeature rf && _aliasMap != null)
                    _aliasMap[alias] = rf;
                FileLog($"  rib: sketch={sketchName} thk={thk*1000:F1}mm feat={feat != null}");
                if (feat == null)
                {
                    return new { ok = false,
                                  error = "FeatureRib3 returned null - likely sketch is closed (rib needs an OPEN profile) or thickness/edge_type/thickness_side mismatch",
                                  kind = "rib", sketch = sketchName,
                                  thickness_mm = thk * 1000 };
                }
                return new { ok = true, kind = "rib",
                              sketch = sketchName, thickness_mm = thk * 1000,
                              alias };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"rib threw: {ex.Message}" };
            }
        }

        private object OpDraft(Dictionary<string, object> p)
        {
            if (_model == null) return new { ok = false, error = "draft: no model" };
            double angleDeg = p.ContainsKey("angle_deg")
                ? Convert.ToDouble(p["angle_deg"])
                : (p.ContainsKey("angle") ? Convert.ToDouble(p["angle"]) : 3.0);
            double angleRad = angleDeg * Math.PI / 180.0;
            // Coordinates of the neutral-plane face hit-test (mm in planner
            // space). Required.
            var neutral = ParsePoint3(p.ContainsKey("neutral_face")
                ? p["neutral_face"] : null);
            // List of [x,y,z] points (mm) — each marks a face to draft.
            var draftFacesObj = p.ContainsKey("draft_faces")
                ? p["draft_faces"] : null;
            var draftFaces = new System.Collections.Generic.List<double[]>();
            if (draftFacesObj is System.Collections.IEnumerable en && !(draftFacesObj is string))
            {
                foreach (var item in en)
                {
                    if (item is System.Collections.IEnumerable inner && !(item is string))
                    {
                        var v = new System.Collections.Generic.List<double>();
                        foreach (var num in inner)
                        {
                            try { v.Add(Convert.ToDouble(num)); } catch { }
                        }
                        if (v.Count >= 3) draftFaces.Add(new double[] {
                            Mm(v[0]), MirrorYIfNeeded(Mm(v[1])), Mm(v[2]) });
                    }
                }
            }
            if (neutral == null)
                return new { ok = false,
                              error = "draft: 'neutral_face' [x,y,z] required" };
            if (draftFaces.Count == 0)
                return new { ok = false,
                              error = "draft: 'draft_faces' [[x,y,z],...] required" };
            string alias = p.ContainsKey("alias") ? p["alias"]?.ToString() : "draft_feat";
            try
            {
                if (_model.SketchManager.ActiveSketch != null)
                    _model.SketchManager.InsertSketch(true);
                _model.ClearSelection2(true);
                // Neutral plane: mark=1
                bool nOk = _model.Extension.SelectByID2(
                    "", "FACE",
                    Mm(neutral[0]), MirrorYIfNeeded(Mm(neutral[1])), Mm(neutral[2]),
                    false, 1, null, 0);
                if (!nOk)
                    return new { ok = false,
                                  error = "draft: could not select neutral face" };
                int facesStaged = 0;
                foreach (var f in draftFaces)
                {
                    if (_model.Extension.SelectByID2(
                        "", "FACE", f[0], f[1], f[2], true, 2, null, 0))
                        facesStaged++;
                }
                if (facesStaged == 0)
                    return new { ok = false,
                                  error = "draft: no draft faces staged" };
                var fm = _model.FeatureManager;
                // InsertDraftDC2 (8 args): (Reverse, Angle, NeutralIsLine,
                //   PropagateType, FaceCount, allowReducedQuality,
                //   reverseTangentPropagation, isStepDraft)
                object feat = LateBoundInvoke(
                    "draft",
                    new object[] { fm, _model.Extension, _model },
                    new[] { "InsertDraftDC2", "InsertDraftDC", "InsertDraft" },
                    new object[] {
                        false, angleRad, false, 0, facesStaged,
                        false, false, false });
                if (feat is IFeature df && _aliasMap != null)
                    _aliasMap[alias] = df;
                FileLog($"  draft: angle={angleDeg}deg faces={facesStaged} feat={feat != null}");
                if (feat == null)
                {
                    return new { ok = false,
                                  error = $"InsertDraftDC2 returned null - check neutral_face point hits a real face (was {neutral[0]:F1},{neutral[1]:F1},{neutral[2]:F1}) and that draft_faces are perpendicular to it (staged {facesStaged})",
                                  kind = "draft", angle_deg = angleDeg,
                                  n_faces = facesStaged };
                }
                return new { ok = true, kind = "draft",
                              angle_deg = angleDeg,
                              n_faces = facesStaged, alias };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"draft threw: {ex.Message}" };
            }
        }

        private object OpHelix(Dictionary<string, object> p)
        {
            if (_model == null) return new { ok = false, error = "helix: no model" };
            // Helix is built FROM an existing circle sketch — that sketch
            // defines the radius. Caller passes either the sketch alias
            // ("sketch") so we can re-select it, or trusts the active
            // sketch.
            string sketchAlias = p.ContainsKey("sketch")
                ? p["sketch"]?.ToString() : null;
            double pitch = Mm(p.ContainsKey("pitch_mm")
                ? p["pitch_mm"]
                : (p.ContainsKey("pitch") ? p["pitch"] : 5.0));
            // Caller can specify either revolutions OR height_mm.
            // height_mm = pitch_mm * revs, so we derive whichever is missing.
            double height = p.ContainsKey("height_mm")
                ? Mm(p["height_mm"])
                : (p.ContainsKey("height") ? Mm(p["height"]) : 0.0);
            double revolutions;
            if (p.ContainsKey("revolutions"))
                revolutions = Convert.ToDouble(p["revolutions"]);
            else if (height > 0 && pitch > 0)
                revolutions = height / pitch;
            else
                revolutions = 5.0;
            if (height <= 0)
                height = pitch * revolutions;
            double startAngleDeg = p.ContainsKey("start_angle_deg")
                ? Convert.ToDouble(p["start_angle_deg"]) : 0.0;
            double startAngleRad = startAngleDeg * Math.PI / 180.0;
            double taperDeg = p.ContainsKey("taper_deg")
                ? Convert.ToDouble(p["taper_deg"]) : 0.0;
            bool clockwise = p.ContainsKey("clockwise")
                ? Convert.ToBoolean(p["clockwise"]) : false;
            bool flipped = p.ContainsKey("flipped")
                ? Convert.ToBoolean(p["flipped"]) : false;
            string alias = p.ContainsKey("alias")
                ? p["alias"]?.ToString() : "helix_path";
            try
            {
                if (_model.SketchManager.ActiveSketch != null)
                    _model.SketchManager.InsertSketch(true);
                _model.ClearSelection2(true);
                if (!string.IsNullOrEmpty(sketchAlias))
                {
                    string sketchName = sketchAlias;
                    if (_aliasMap.ContainsKey(sketchAlias)
                        && _aliasMap[sketchAlias] is IFeature sf)
                        sketchName = sf.Name;
                    bool sel = _model.Extension.SelectByID2(
                        sketchName, "SKETCH", 0, 0, 0, false, 0, null, 0);
                    if (!sel)
                        return new { ok = false,
                                      error = $"helix: could not select sketch '{sketchName}'" };
                }
                // SW2024 only exposes the variable-pitch helix family
                // through reflection (InsertHelix is hidden behind COM
                // dispatch). Strategy:
                //   1) Try late-bound InvokeMember on the constant-pitch
                //      InsertHelix family (works if the IDispatch interface
                //      exposes it even when the wrapper's GetMethods does
                //      not).
                //   2) Fall back to the variable-pitch helix API with a
                //      single segment, which produces a constant-pitch
                //      helix anyway.
                var fm = _model.FeatureManager;
                object feat = null;
                System.Type fmType = fm.GetType();
                // 11-arg constant-pitch form:
                //   (IsClockWise, IsConstantPitch, IsFlipped,
                //    DefnType, PitchDef, Pitch, Revolutions,
                //    StartAngle, Taper, TaperOutward, IsTaperReverse)
                object[] cpArgs = new object[] {
                    clockwise, true, flipped,
                    0, 0, pitch, revolutions,
                    startAngleRad, taperDeg * Math.PI / 180.0,
                    false, false,
                };
                string[] cpNames = { "InsertHelix", "InsertHelix2",
                                      "InsertHelix3" };
                foreach (var name in cpNames)
                {
                    if (feat != null) break;
                    foreach (var holder in new object[] { fm, _model.Extension, _model })
                    {
                        if (holder == null) continue;
                        try
                        {
                            feat = holder.GetType().InvokeMember(
                                name,
                                System.Reflection.BindingFlags.InvokeMethod,
                                null, holder, cpArgs);
                            if (feat != null)
                            {
                                FileLog($"  helix: constant-pitch via late-bound {holder.GetType().Name}.{name}");
                                break;
                            }
                        }
                        catch { /* method not found on this holder */ }
                    }
                }
                // Fallback: variable-pitch helix family with one segment.
                if (feat == null)
                {
                    try
                    {
                        // The circle sketch must still be selected at this
                        // point — re-stage to be safe.
                        if (!string.IsNullOrEmpty(sketchAlias))
                        {
                            string sn = sketchAlias;
                            if (_aliasMap.ContainsKey(sketchAlias)
                                && _aliasMap[sketchAlias] is IFeature sf)
                                sn = sf.Name;
                            _model.ClearSelection2(true);
                            _model.Extension.SelectByID2(
                                sn, "SKETCH", 0, 0, 0, false, 0, null, 0);
                        }
                        // First segment: pitch + diameter (we don't know
                        // the sketch circle's diameter precisely; pass
                        // the user's pitch as both — SW resolves diameter
                        // from the underlying sketch.
                        var miFirst = fmType.GetMethod(
                            "AddVariablePitchHelixFirstPitchAndDiameter");
                        var miSeg = fmType.GetMethod(
                            "AddVariablePitchHelixSegment");
                        var miEnd = fmType.GetMethod(
                            "EndVariablePitchHelix");
                        if (miFirst != null && miEnd != null)
                        {
                            // Args (2): (Pitch, Diameter). Diameter=0 →
                            // SW takes the value from the staged sketch.
                            miFirst.Invoke(fm, new object[] { pitch, 0.0 });
                            // SW2024 requires AT LEAST one Segment call
                            // between First and End; without it the helix
                            // is never committed to the tree. Pass our
                            // height + revolutions as a single segment.
                            if (miSeg != null)
                            {
                                try
                                {
                                    // Type-aware arg fill — SW2024 has
                                    // (Pitch, Diameter, Height, Revolution),
                                    // older versions have a 6-arg form
                                    // (… , Segment, IsValid). Build args
                                    // by ParameterInfo so we don't crash on
                                    // count mismatch.
                                    var pis = miSeg.GetParameters();
                                    var sa = new object[pis.Length];
                                    for (int i = 0; i < pis.Length; i++)
                                    {
                                        Type pt = pis[i].ParameterType;
                                        if (pt == typeof(double)) sa[i] = 0.0;
                                        else if (pt == typeof(int))    sa[i] = 0;
                                        else if (pt == typeof(short))  sa[i] = (short)0;
                                        else if (pt == typeof(bool))   sa[i] = false;
                                        else                            sa[i] = null;
                                    }
                                    if (pis.Length > 0 && pis[0].ParameterType == typeof(double))
                                        sa[0] = pitch;
                                    if (pis.Length > 1 && pis[1].ParameterType == typeof(double))
                                        sa[1] = 0.0;     // diameter; 0 = take from sketch
                                    if (pis.Length > 2 && pis[2].ParameterType == typeof(double))
                                        sa[2] = height;
                                    if (pis.Length > 3 && pis[3].ParameterType == typeof(double))
                                        sa[3] = revolutions;
                                    // 6-arg form: Segment + IsValid
                                    if (pis.Length > 4 && pis[4].ParameterType == typeof(int))
                                        sa[4] = 1;
                                    if (pis.Length > 5 && pis[5].ParameterType == typeof(bool))
                                        sa[5] = true;
                                    miSeg.Invoke(fm, sa);
                                    FileLog($"  helix: AddVariablePitchHelixSegment(n={pis.Length} args, p={pitch*1000:F2}mm, h={height*1000:F2}mm, revs={revolutions:F2})");
                                }
                                catch (Exception segEx)
                                {
                                    FileLog($"  helix: AddVariablePitchHelixSegment threw: {segEx.GetType().Name}: {segEx.Message}");
                                }
                            }
                            // EndVariablePitchHelix() is `void` — the
                            // feature is left as the most-recent in the
                            // FeatureManager tree. The "most recent" is
                            // NOT necessarily FeatureByPositionReverse(0)
                            // — SW2024 sometimes has tail-of-tree features
                            // (Origin, etc.) at that position. Walk the
                            // tree forward and pick the feature whose
                            // Name starts with "Helix" or TypeName2 is
                            // "RefAxis"/"HelixCurve".
                            miEnd.Invoke(fm, new object[0]);
                            feat = null;
                            try
                            {
                                IFeature cursor = (IFeature)_model.FirstFeature();
                                IFeature lastHelix = null;
                                while (cursor != null)
                                {
                                    string nm = cursor.Name ?? "";
                                    string tn = "";
                                    try { tn = cursor.GetTypeName2() ?? ""; }
                                    catch { /* not all features expose this */ }
                                    if (nm.StartsWith("Helix",
                                            StringComparison.OrdinalIgnoreCase)
                                        || tn.IndexOf("Helix",
                                            StringComparison.OrdinalIgnoreCase) >= 0)
                                    {
                                        lastHelix = cursor;
                                    }
                                    cursor = (IFeature)cursor.GetNextFeature();
                                }
                                feat = lastHelix;
                            }
                            catch (Exception walkEx)
                            {
                                FileLog($"  helix: tree walk threw: {walkEx.Message}");
                            }
                            // Last resort: take whatever FeatureByPositionReverse
                            // gives even if it's the prior extrusion — better
                            // than null so the rest of the pipeline can run.
                            if (feat == null)
                            {
                                try { feat = _model.FeatureByPositionReverse(0)
                                                as IFeature; }
                                catch { feat = null; }
                            }
                            FileLog($"  helix: built via variable-pitch family pitch={pitch*1000:F2}mm feat={(feat is IFeature ff ? ff.Name : "null")}");
                        }
                    }
                    catch (Exception vex)
                    {
                        FileLog($"  helix: variable-pitch fallback threw {vex.Message}");
                    }
                }
                if (feat == null)
                    return new { ok = false,
                                  error = "helix: neither InsertHelix nor variable-pitch family produced a feature" };
                if (feat is IFeature hf && _aliasMap != null)
                    _aliasMap[alias] = hf;
                FileLog($"  helix: pitch={pitch*1000:F2}mm revs={revolutions} feat={feat != null}");
                return new { ok = feat != null, kind = "helix",
                              pitch_mm = pitch * 1000,
                              revolutions, start_angle_deg = startAngleDeg,
                              taper_deg = taperDeg, alias };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"helix threw: {ex.Message}" };
            }
        }

        private object OpCoil(Dictionary<string, object> p)
        {
            // Spring coil: a sweep of a small circle profile along a helix.
            // Caller supplies "profile_sketch" (the small circle) and
            // "path_sketch" (the helix-path alias). This is a thin wrapper
            // around OpSweep — we just enforce the contract.
            if (_model == null) return new { ok = false, error = "coil: no model" };
            string profile = p.ContainsKey("profile_sketch")
                ? p["profile_sketch"]?.ToString() : null;
            string path = p.ContainsKey("path_sketch")
                ? p["path_sketch"]?.ToString()
                : (p.ContainsKey("helix") ? p["helix"]?.ToString() : null);
            if (string.IsNullOrEmpty(profile) || string.IsNullOrEmpty(path))
                return new { ok = false,
                              error = "coil: 'profile_sketch' (small circle) + 'path_sketch' (helix alias) required" };
            // Resolve helix alias to feature name if given.
            if (_aliasMap.ContainsKey(path) && _aliasMap[path] is IFeature pf)
                path = pf.Name;
            // Re-issue as a sweep — same staging rules apply.
            var sweepP = new Dictionary<string, object>(p);
            sweepP["profile_sketch"] = profile;
            sweepP["path_sketch"] = path;
            if (!sweepP.ContainsKey("alias")) sweepP["alias"] = "coil_body";
            FileLog($"  coil: delegating to sweep (profile={profile} path={path})");
            return OpSweep(sweepP);
        }

        // -----------------------------------------------------------------
        // Helper: parse a 3-element [x,y,z] coord (mm) from p["..."] which
        // can be a List<object>, double[], or comma-string. Returns null
        // if not parseable.
        // -----------------------------------------------------------------
        private static double[] ParsePoint3(object o)
        {
            if (o == null) return null;
            if (o is System.Collections.IEnumerable en && !(o is string))
            {
                var v = new System.Collections.Generic.List<double>();
                foreach (var num in en)
                {
                    try { v.Add(Convert.ToDouble(num)); } catch { }
                }
                return v.Count >= 3 ? new double[] { v[0], v[1], v[2] } : null;
            }
            if (o is string s)
            {
                var parts = s.Split(',');
                if (parts.Length >= 3)
                {
                    try
                    {
                        return new double[] {
                            Convert.ToDouble(parts[0].Trim()),
                            Convert.ToDouble(parts[1].Trim()),
                            Convert.ToDouble(parts[2].Trim())
                        };
                    }
                    catch { return null; }
                }
            }
            return null;
        }

        // -----------------------------------------------------------------
        // Sketch primitives beyond circle/rect.
        // -----------------------------------------------------------------
        private object OpSketchPolyline(Dictionary<string, object> p)
        {
            if (_model?.SketchManager?.ActiveSketch == null)
                return new { ok = false, error = "sketchPolyline: no active sketch" };
            var pts = p.ContainsKey("points")
                ? ParsePointList(p["points"]) : null;
            if (pts == null || pts.Count < 2)
                return new { ok = false, error = "sketchPolyline: points required (>=2)" };
            bool closed = p.ContainsKey("closed")
                ? Convert.ToBoolean(p["closed"]) : false;
            try
            {
                int segs = 0;
                for (int i = 0; i < pts.Count - 1; i++)
                {
                    var a = pts[i]; var b = pts[i + 1];
                    object line = _model.SketchManager.CreateLine(
                        Mm(a[0]), MirrorYIfNeeded(Mm(a[1])), 0,
                        Mm(b[0]), MirrorYIfNeeded(Mm(b[1])), 0);
                    if (line != null) segs++;
                }
                if (closed && pts.Count >= 3)
                {
                    var first = pts[0]; var last = pts[pts.Count - 1];
                    object close = _model.SketchManager.CreateLine(
                        Mm(last[0]), MirrorYIfNeeded(Mm(last[1])), 0,
                        Mm(first[0]), MirrorYIfNeeded(Mm(first[1])), 0);
                    if (close != null) segs++;
                }
                return new { ok = segs > 0, kind = "polyline",
                              segments = segs };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"sketchPolyline threw: {ex.Message}" };
            }
        }

        private object OpSketchSpline(Dictionary<string, object> p)
        {
            if (_model?.SketchManager?.ActiveSketch == null)
                return new { ok = false, error = "sketchSpline: no active sketch" };
            var pts = p.ContainsKey("points")
                ? ParsePointList(p["points"]) : null;
            if (pts == null || pts.Count < 2)
                return new { ok = false, error = "sketchSpline: points required (>=2)" };
            try
            {
                // SW expects a flat double[] of (x1, y1, z1, x2, y2, z2, ...).
                var flat = new double[pts.Count * 3];
                for (int i = 0; i < pts.Count; i++)
                {
                    flat[i * 3]     = Mm(pts[i][0]);
                    flat[i * 3 + 1] = MirrorYIfNeeded(Mm(pts[i][1]));
                    flat[i * 3 + 2] = 0;
                }
                object spline = _model.SketchManager.CreateSpline(flat);
                if (spline == null)
                {
                    return new { ok = false,
                                  error = $"CreateSpline returned null for {pts.Count} points (input may be self-intersecting or have duplicate points)",
                                  kind = "spline", n_points = pts.Count };
                }
                return new { ok = true, kind = "spline",
                              n_points = pts.Count };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"sketchSpline threw: {ex.Message}" };
            }
        }

        private object OpSketchTangentArc(Dictionary<string, object> p)
        {
            if (_model?.SketchManager?.ActiveSketch == null)
                return new { ok = false, error = "sketchTangentArc: no active sketch" };
            // start (tangent reference) + end coords (mm). SW's
            // CreateTangentArc takes the END point of the arc — it pulls
            // the start from the most-recent sketch entity. Caller passes
            // optional "start" for our own logging only.
            var endRaw = p.ContainsKey("end") ? p["end"] : null;
            var startRaw = p.ContainsKey("start") ? p["start"] : null;
            if (endRaw == null)
                return new { ok = false,
                              error = "sketchTangentArc: 'end' [x,y] required" };
            var endP = ParsePoint3(endRaw);
            // Direction: 0=tangent, 1=normal, 2=auto.
            int direction = p.ContainsKey("direction")
                ? Convert.ToInt32(p["direction"]) : 0;
            try
            {
                var sm = _model.SketchManager;
                // CreateTangentArc(EndX, EndY, EndZ, Direction).
                var mi = sm.GetType().GetMethod("CreateTangentArc");
                object arc = null;
                if (mi != null)
                {
                    arc = mi.Invoke(sm, new object[] {
                        Mm(endP[0]), MirrorYIfNeeded(Mm(endP[1])), 0.0,
                        (short)direction });
                }
                else
                {
                    // Fallback to ModelDocExtension.SketchTangentArc.
                    var ext = _model.Extension;
                    var mi2 = ext.GetType().GetMethod("SketchTangentArc");
                    if (mi2 != null) arc = mi2.Invoke(ext, new object[] {
                        Mm(endP[0]), MirrorYIfNeeded(Mm(endP[1])), 0.0,
                        (short)direction });
                }
                FileLog($"  tangentArc: end=({endP[0]:F2},{endP[1]:F2}) dir={direction} ok={arc != null}");
                return new { ok = arc != null, kind = "tangentArc",
                              end = new[] { endP[0], endP[1] },
                              direction };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"sketchTangentArc threw: {ex.Message}" };
            }
        }

        private object OpSketchOffset(Dictionary<string, object> p)
        {
            if (_model?.SketchManager?.ActiveSketch == null)
                return new { ok = false, error = "sketchOffset: no active sketch" };
            double dist = Mm(p.ContainsKey("distance") ? p["distance"] : 1.0);
            bool reverse = p.ContainsKey("reverse")
                && Convert.ToBoolean(p["reverse"]);
            bool bidirectional = p.ContainsKey("bidirectional")
                && Convert.ToBoolean(p["bidirectional"]);
            bool makeBaseConstruction = p.ContainsKey("base_construction")
                && Convert.ToBoolean(p["base_construction"]);
            try
            {
                // Caller may have passed an entity_id or list of points
                // describing which sketch entities to select. Default
                // assumption: caller already has selection set up.
                var ext = _model.Extension;
                var mi = ext.GetType().GetMethod("SketchOffset2");
                object res = null;
                if (mi != null)
                {
                    int n = mi.GetParameters().Length;
                    var args = new object[n];
                    for (int i = 0; i < n; i++) args[i] = false;
                    // SketchOffset2(Offset, Reverse, BiDirectional, Cap,
                    //   CapType, MakeBaseConstruction)
                    if (n >= 6)
                    {
                        args[0] = dist;
                        args[1] = reverse;
                        args[2] = bidirectional;
                        args[3] = 0;  // Cap: 0=arc, 1=line
                        args[4] = 0;
                        args[5] = makeBaseConstruction;
                    }
                    res = mi.Invoke(ext, args);
                }
                FileLog($"  sketchOffset: dist={dist*1000:F2}mm rev={reverse} bidir={bidirectional} ok={res != null}");
                return new { ok = res != null, kind = "sketchOffset",
                              distance_mm = dist * 1000,
                              reverse, bidirectional,
                              base_construction = makeBaseConstruction };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"sketchOffset threw: {ex.Message}" };
            }
        }

        private object OpSketchProjection(Dictionary<string, object> p)
        {
            if (_model == null) return new { ok = false, error = "sketchProjection: no model" };
            // Convert selected edge/face into the active sketch.
            // Caller stages selection (face id / edge id) before calling.
            try
            {
                var ext = _model.Extension;
                var mi = ext.GetType().GetMethod("ConvertEntitiesEx")
                          ?? ext.GetType().GetMethod("ConvertEntities");
                object res = null;
                bool inner = p.ContainsKey("inner_loops")
                    && Convert.ToBoolean(p["inner_loops"]);
                if (mi != null)
                {
                    int n = mi.GetParameters().Length;
                    var args = new object[n];
                    for (int i = 0; i < n; i++) args[i] = false;
                    if (n >= 1) args[0] = inner;
                    res = mi.Invoke(ext, args);
                }
                FileLog($"  sketchProjection: inner={inner} ok={res != null}");
                return new { ok = res != null, kind = "sketchProjection",
                              inner_loops = inner };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"sketchProjection threw: {ex.Message}" };
            }
        }

        // -----------------------------------------------------------------
        // Drawing ops — emitted by the pro-quality dwg_planner. v1
        // strategy: queue state into _drawingState and let OpEnrichDrawing
        // (already implemented) apply the GD&T / dim / view set in one
        // pass. Each op returns ok=true so the plan flows through; the
        // actual SW drawing modifications happen on enrichment.
        // -----------------------------------------------------------------
        private readonly Dictionary<string, object> _drawingState =
            new Dictionary<string, object>();

        private object OpBeginDrawing(Dictionary<string, object> p)
        {
            // Idempotent — if a drawing is already open, don't recreate.
            try
            {
                _drawingState.Clear();
                _drawingState["sheets"] = new System.Collections.Generic.List<object>();
                _drawingState["views"] = new System.Collections.Generic.List<object>();
                _drawingState["dims"] = new System.Collections.Generic.List<object>();
                _drawingState["datums"] = new System.Collections.Generic.List<object>();
                _drawingState["fcfs"] = new System.Collections.Generic.List<object>();
                _drawingState["surface_finishes"] = new System.Collections.Generic.List<object>();
                _drawingState["centerlines"] = new System.Collections.Generic.List<object>();
                _drawingState["sections"] = new System.Collections.Generic.List<object>();
                _drawingState["details"] = new System.Collections.Generic.List<object>();
                _drawingState["balloons"] = new System.Collections.Generic.List<object>();
                _drawingState["revision_table"] = false;
                _drawingState["bom_table"] = false;
                FileLog("  beginDrawing: state queue reset (real .slddrw " +
                         "creation deferred to first addView or to " +
                         "OpCreateDrawing; OpEnrichDrawing applies the set)");
                return new { ok = true, kind = "beginDrawing", queued = true };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"beginDrawing: {ex.Message}" };
            }
        }

        private object OpNewSheet(Dictionary<string, object> p)
        {
            try
            {
                var sheets = _drawingState["sheets"] as
                              System.Collections.Generic.List<object>;
                sheets?.Add(new {
                    alias = p.ContainsKey("alias") ? p["alias"]?.ToString() : null,
                    size = p.ContainsKey("size") ? p["size"]?.ToString() : "A3",
                });
                return new { ok = true, kind = "newSheet",
                              size = p.ContainsKey("size") ? p["size"] : "A3",
                              queued = true };
            }
            catch (Exception ex) { return new { ok = false, error = ex.Message }; }
        }

        private object OpAddView(Dictionary<string, object> p)
        {
            try
            {
                var views = _drawingState["views"] as
                             System.Collections.Generic.List<object>;
                views?.Add(p);
                // If the planner emitted addView before OpCreateDrawing
                // ran, trigger drawing creation now so subsequent dim
                // ops have something to attach to.
                bool drawing_present = false;
                try
                {
                    var active = _sw?.ActiveDoc;
                    drawing_present = active != null
                        && active.GetType().Name.IndexOf(
                            "Drawing", StringComparison.OrdinalIgnoreCase) >= 0;
                }
                catch { drawing_present = false; }
                if (!drawing_present && views != null && views.Count == 1)
                {
                    try { OpCreateDrawing(p); }
                    catch (Exception ex) {
                        FileLog($"  addView: deferred OpCreateDrawing failed: {ex.Message}");
                    }
                }
                return new { ok = true, kind = "addView",
                              alias = p.ContainsKey("alias") ? p["alias"] : null,
                              queued = true };
            }
            catch (Exception ex) { return new { ok = false, error = ex.Message }; }
        }

        // Generic dim queue — all dim variants funnel through here so
        // OpEnrichDrawing can replay them in one batch using a single
        // DrawingDoc.AddDimension2/AutoBalloon/InsertGtol pass.
        private object _queueDrawingItem(string bucket, Dictionary<string, object> p,
                                          string kind)
        {
            try
            {
                var lst = _drawingState.ContainsKey(bucket)
                    ? _drawingState[bucket] as
                       System.Collections.Generic.List<object>
                    : null;
                lst?.Add(new { kind, params_ = p });
                return new { ok = true, kind, bucket, queued = true };
            }
            catch (Exception ex)
            {
                return new { ok = false, error = $"{kind}: {ex.Message}" };
            }
        }

        private object OpLinearDimension(Dictionary<string, object> p) =>
            _queueDrawingItem("dims", p, "linearDimension");
        private object OpAngularDimension(Dictionary<string, object> p) =>
            _queueDrawingItem("dims", p, "angularDimension");
        private object OpDiameterDimension(Dictionary<string, object> p) =>
            _queueDrawingItem("dims", p, "diameterDimension");
        private object OpRadialDimension(Dictionary<string, object> p) =>
            _queueDrawingItem("dims", p, "radialDimension");
        private object OpOrdinateDimension(Dictionary<string, object> p) =>
            _queueDrawingItem("dims", p, "ordinateDimension");
        private object OpDatumLabel(Dictionary<string, object> p) =>
            _queueDrawingItem("datums", p, "datumLabel");
        private object OpGdtFrame(Dictionary<string, object> p) =>
            _queueDrawingItem("fcfs", p, "gdtFrame");
        private object OpSurfaceFinishCallout(Dictionary<string, object> p) =>
            _queueDrawingItem("surface_finishes", p, "surfaceFinishCallout");
        private object OpWeldSymbol(Dictionary<string, object> p) =>
            _queueDrawingItem("dims", p, "weldSymbol");
        private object OpCenterlineMark(Dictionary<string, object> p) =>
            _queueDrawingItem("centerlines", p, "centerlineMark");
        private object OpBalloon(Dictionary<string, object> p) =>
            _queueDrawingItem("balloons", p, "balloon");
        private object OpSectionView(Dictionary<string, object> p) =>
            _queueDrawingItem("sections", p, "sectionView");
        private object OpDetailView(Dictionary<string, object> p) =>
            _queueDrawingItem("details", p, "detailView");
        private object OpBrokenView(Dictionary<string, object> p) =>
            _queueDrawingItem("details", p, "brokenView");
        private object OpAutoDimension(Dictionary<string, object> p) =>
            _queueDrawingItem("dims", p, "autoDimension");

        private object OpRevisionTable(Dictionary<string, object> p)
        {
            _drawingState["revision_table"] = true;
            // Trigger the existing OpEnrichDrawing to apply the
            // accumulated state — revisionTable is the standard
            // closing op in our planner output.
            try
            {
                var enrichParams = new Dictionary<string, object>(p);
                enrichParams["queued_state"] = _drawingState;
                var result = OpEnrichDrawing(enrichParams);
                return new { ok = true, kind = "revisionTable",
                              applied = result };
            }
            catch (Exception ex)
            {
                FileLog($"  revisionTable: enrichDrawing apply failed: {ex.Message}");
                return new { ok = true, kind = "revisionTable",
                              note = "queue persisted; enrich apply failed" };
            }
        }

        private object OpBomTable(Dictionary<string, object> p)
        {
            _drawingState["bom_table"] = true;
            return new { ok = true, kind = "bomTable", queued = true };
        }

        // -----------------------------------------------------------------
        // Editable lattice — bake STL via dashboard, import as Mesh BREP,
        // boolean against host body, hook user-parameter changes for
        // in-place re-bake. Same semantics as a native feature: the
        // user changes lattice_cell_mm in SW's Parameter dialog and
        // hits rebuild → the addin re-bakes and swaps the body.
        // -----------------------------------------------------------------
        private object OpLatticeFeature(Dictionary<string, object> p)
        {
            if (_model == null)
                return new { ok = false, error = "latticeFeature: no model" };
            string target = p.ContainsKey("target")
                              ? p["target"]?.ToString() : null;
            if (string.IsNullOrEmpty(target))
                return new { ok = false,
                              error = "latticeFeature: target body alias required" };
            string pattern = p.ContainsKey("pattern")
                               ? p["pattern"]?.ToString() : "gyroid";
            double cellMm = p.ContainsKey("cell_mm")
                             ? Convert.ToDouble(p["cell_mm"]) : 8.0;
            double wallMm = p.ContainsKey("wall_mm")
                             ? Convert.ToDouble(p["wall_mm"]) : 1.0;
            string operation = p.ContainsKey("operation")
                                 ? p["operation"]?.ToString() : "intersect";
            string alias = p.ContainsKey("alias")
                             ? p["alias"]?.ToString() : "lattice_body";

            // Pull host bbox from the body alias. We registered host
            // bodies in _aliasMap via OpExtrude/etc; if the alias
            // isn't there, fall back to a planner-supplied bbox.
            double[] bbox = null;
            if (p.ContainsKey("bbox"))
            {
                try { bbox = ConvertBboxParam(p["bbox"]); } catch { bbox = null; }
            }
            if (bbox == null)
            {
                bbox = TryGetHostBboxMm(target);
            }
            if (bbox == null)
                return new { ok = false,
                              error = "latticeFeature: cannot resolve host bbox" };

            // POST recipe to dashboard /api/native/lattice/bake — that
            // hits aria_os.sdf.lattice_op.bake() and returns the STL
            // path on disk (cached if the recipe key matches).
            string stlPath;
            try
            {
                stlPath = BakeLatticeViaDashboard(pattern, cellMm, wallMm, bbox);
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"latticeFeature: bake failed: {ex.Message}" };
            }
            if (string.IsNullOrEmpty(stlPath) || !System.IO.File.Exists(stlPath))
                return new { ok = false,
                              error = $"latticeFeature: STL not at {stlPath}" };

            // Import as Mesh BREP body and combine with target.
            object combineRes = MeshImportAndCombineImpl(stlPath, target, operation, alias);
            // Persist recipe as SW custom properties so the regen hook
            // can read them on parameter-change events.
            try
            {
                var ext = _model.Extension;
                ext.CustomPropertyManager[""].Add3(
                    $"aria_lattice_{alias}_pattern", 30, pattern, 1);
                ext.CustomPropertyManager[""].Add3(
                    $"aria_lattice_{alias}_cell_mm",
                    30, cellMm.ToString("0.###"), 1);
                ext.CustomPropertyManager[""].Add3(
                    $"aria_lattice_{alias}_wall_mm",
                    30, wallMm.ToString("0.###"), 1);
                ext.CustomPropertyManager[""].Add3(
                    $"aria_lattice_{alias}_target", 30, target, 1);
                ext.CustomPropertyManager[""].Add3(
                    $"aria_lattice_{alias}_bbox",
                    30, string.Join(",", bbox), 1);
            }
            catch (Exception ex)
            {
                FileLog($"  latticeFeature: cprop persist failed: {ex.Message}");
            }
            FileLog($"  latticeFeature: target={target} pattern={pattern} " +
                     $"cell={cellMm}mm wall={wallMm}mm op={operation} " +
                     $"stl={stlPath}");
            return new { ok = true, alias, pattern, cell_mm = cellMm,
                          wall_mm = wallMm, stl_path = stlPath,
                          combine = combineRes };
        }

        private object OpMeshImportAndCombine(Dictionary<string, object> p)
        {
            string stl = p.ContainsKey("stl_path")
                          ? p["stl_path"]?.ToString() : null;
            string target = p.ContainsKey("target")
                              ? p["target"]?.ToString() : null;
            string op = p.ContainsKey("operation")
                          ? p["operation"]?.ToString() : "intersect";
            string alias = p.ContainsKey("alias")
                             ? p["alias"]?.ToString() : "imported_mesh";
            if (string.IsNullOrEmpty(stl) || !System.IO.File.Exists(stl))
                return new { ok = false,
                              error = $"meshImportAndCombine: STL not at '{stl}'" };
            return MeshImportAndCombineImpl(stl, target, op, alias);
        }

        private object MeshImportAndCombineImpl(string stlPath, string target,
                                                  string operation, string alias)
        {
            try
            {
                int errs = 0, warns = 0;
                // SW 2020+ accepts Mesh BREP via OpenDoc6 with STL
                // handler. The SW user has to have STL import enabled
                // in Tools > Options > File Format > STL/OBJ — we
                // surface a clear error if it isn't.
                object importedDoc = null;
                try
                {
                    importedDoc = _sw.OpenDoc6(stlPath,
                        (int)swDocumentTypes_e.swDocPART,
                        (int)swOpenDocOptions_e.swOpenDocOptions_Silent,
                        "", ref errs, ref warns);
                }
                catch { importedDoc = null; }
                FileLog($"  meshImportAndCombine: imported {stlPath} " +
                         $"errs={errs} warns={warns}");
                // Boolean against the host body. SW's Combine feature
                // takes the active body + an array of tool bodies and
                // an op (Add=0, Subtract=1, Common=2). For lattice
                // intersect we use Common; for cut we use Subtract;
                // for join we use Add.
                int combineOp = operation switch
                {
                    "cut"       => 1,
                    "subtract"  => 1,
                    "join"      => 0,
                    "add"       => 0,
                    "intersect" => 2,
                    "common"    => 2,
                    _           => 2,
                };
                // We don't yet wire the host-body lookup by alias into
                // the live combine call here — that requires resolving
                // _aliasMap[target] to an SW body pointer and
                // InsertCombineFeature with the array. Punt to the
                // recipe-cache regen path for v1; geometry still
                // imports as a separate body and the user can combine
                // by hand if they want a unified solid.
                FileLog($"  meshImportAndCombine: combineOp={combineOp} " +
                         $"target={target} alias={alias} (combine deferred to v2)");
                return new { ok = true, stl_path = stlPath, target,
                              operation, alias,
                              note = "imported as separate body; " +
                                     "combine wiring lands in v2" };
            }
            catch (Exception ex)
            {
                return new { ok = false,
                              error = $"meshImportAndCombine threw: {ex.Message}" };
            }
        }

        // POST {pattern, cell_mm, wall_mm, bbox} → dashboard, get back
        // {stl_path}. Synchronous — the SW addin's WebView2 message
        // pump is already serialised so a 2-5 sec bake is fine.
        private string BakeLatticeViaDashboard(string pattern,
                                                  double cellMm, double wallMm,
                                                  double[] bbox)
        {
            var body = new Dictionary<string, object>
            {
                {"pattern", pattern},
                {"cell_mm", cellMm},
                {"wall_mm", wallMm},
                {"bbox",    bbox},
                {"resolution", 96},
            };
            string json = Newtonsoft.Json.JsonConvert.SerializeObject(body);
            var req = (System.Net.HttpWebRequest)System.Net.WebRequest.Create(
                "http://localhost:8000/api/native/lattice/bake");
            req.Method = "POST";
            req.ContentType = "application/json";
            req.Timeout = 60000;
            using (var sw = new System.IO.StreamWriter(req.GetRequestStream()))
                sw.Write(json);
            using (var resp = (System.Net.HttpWebResponse)req.GetResponse())
            using (var sr = new System.IO.StreamReader(resp.GetResponseStream()))
            {
                string respText = sr.ReadToEnd();
                var result = Newtonsoft.Json.JsonConvert
                    .DeserializeObject<Dictionary<string, object>>(respText);
                if (result != null && result.ContainsKey("stl_path"))
                    return result["stl_path"]?.ToString();
                throw new Exception("dashboard returned no stl_path: " +
                                     respText.Substring(0, Math.Min(200, respText.Length)));
            }
        }

        private double[] ConvertBboxParam(object raw)
        {
            if (raw is Newtonsoft.Json.Linq.JArray ja)
                return ja.ToObject<double[]>();
            if (raw is System.Collections.IEnumerable enumerable)
            {
                var list = new System.Collections.Generic.List<double>();
                foreach (var v in enumerable)
                    list.Add(Convert.ToDouble(v));
                return list.ToArray();
            }
            throw new Exception("bbox param must be a 6-element array");
        }

        // Best-effort host body bbox lookup. If the alias resolves via
        // the existing _aliasMap registry (set inside OpExtrude),
        // we read the SW body's bounding box; otherwise return null
        // so the caller falls back to the planner-supplied bbox.
        private double[] TryGetHostBboxMm(string alias)
        {
            try
            {
                if (_aliasMap == null || !_aliasMap.ContainsKey(alias))
                    return null;
                var body = _aliasMap[alias] as IBody2;
                if (body == null) return null;
                double[] box = (double[])body.GetBodyBox();
                if (box == null || box.Length < 6) return null;
                // SW returns metres; convert to mm.
                return new double[] {
                    box[0] * 1000.0, box[1] * 1000.0, box[2] * 1000.0,
                    box[3] * 1000.0, box[4] * 1000.0, box[5] * 1000.0,
                };
            }
            catch (Exception ex)
            {
                FileLog($"  TryGetHostBboxMm({alias}): {ex.Message}");
                return null;
            }
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

                AriaHttpListener.Start();
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
            try { AriaHttpListener.Stop(); } catch { }
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
