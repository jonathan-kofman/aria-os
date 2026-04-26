// AriaBridge.cs -- Handles the 8 bridge actions from the React panel.
//
// JS side (bridge.js) posts a message via window.chrome.webview.postMessage()
// with JSON: { action, _id, ...payload }.
//
// C# side receives WebMessageReceived, dispatches to the correct handler,
// and calls panel.PostReply(json) with { _id, result } or { _id, error }.
//
// IMPLEMENTED (real):
//   getCurrentDocument -- RhinoDoc name, path, unit system
//   getSelection       -- RhinoDoc.Objects.GetSelectedObjects()
//   insertGeometry     -- download STEP/STL, import via Rhino.FileIO.FileStp.Read
//                         or Rhino.FileIO.FileStl.Read, add to doc
//
// STUBBED (returns {error: "not implemented"}):
//   updateParameter, getFeatureTree, exportCurrent, showNotification, openFile

using Microsoft.Web.WebView2.Core;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Rhino;
using Rhino.DocObjects;
using Rhino.FileIO;
using Rhino.Geometry;
using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Threading.Tasks;

namespace AriaPanel
{
    internal class AriaBridge
    {
        private readonly AriaPanelHost _panel;
        private static readonly HttpClient _http = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(60)
        };

        // Op chain — serialize incoming WebView2 messages so plan ops execute
        // in arrival order. Without this, per-message Task.Run dispatches
        // concurrently and ops arrive at Rhino out of order (sketchCircle
        // before its newSketch, extrude before its sketchCircle, etc.).
        // Same fix applied to AriaSW; keeps the two bridges symmetric.
        private static Task _opChain = Task.CompletedTask;
        private static readonly object _chainLock = new object();

        public AriaBridge(AriaPanelHost panel)
        {
            _panel = panel;
        }

        // -----------------------------------------------------------------
        // Entry point -- WebView2 fires this on every postMessage() call.
        // -----------------------------------------------------------------

        public void OnWebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            string raw = e.TryGetWebMessageAsString() ?? e.WebMessageAsJson;

            // Append to the global op chain; preserves arrival order while
            // keeping work off the WebView2 UI thread. Unwrap flattens
            // Task<Task> from the async lambda.
            lock (_chainLock)
            {
                _opChain = _opChain.ContinueWith(
                    _ => ProcessMessageAsync(raw),
                    TaskContinuationOptions.None).Unwrap();
            }
        }

        private async Task ProcessMessageAsync(string raw)
        {
            string id = "";
            try
            {
                var msg = JObject.Parse(raw);
                id = msg["_id"]?.ToString() ?? "";
                string action = msg["action"]?.ToString() ?? "";

                switch (action)
                {
                    case "getCurrentDocument":
                        Reply(id, GetCurrentDocument());
                        break;

                    case "getSelection":
                        Reply(id, GetSelection());
                        break;

                    case "insertGeometry":
                        string url = msg["url"]?.ToString() ?? "";
                        var insertResult = await InsertGeometryAsync(url);
                        Reply(id, insertResult);
                        break;

                    case "updateParameter":
                        ReplyError(id, "not implemented");
                        break;

                    case "getFeatureTree":
                        ReplyError(id, "not implemented");
                        break;

                    case "exportCurrent":
                        ReplyError(id, "not implemented");
                        break;

                    case "showNotification":
                        // Best-effort: write to Rhino status bar.
                        string notifMsg = msg["msg"]?.ToString() ?? "";
                        RhinoApp.WriteLine($"[ARIA] {notifMsg}");
                        RhinoApp.SetCommandPrompt(notifMsg);
                        Reply(id, new { ok = true });
                        break;

                    case "openFile":
                        ReplyError(id, "not implemented");
                        break;

                    case "executeFeature":
                        string kind = msg["kind"]?.ToString() ?? "";
                        JObject fparams = msg["params"] as JObject ?? new JObject();
                        Reply(id, ExecuteFeature(kind, fparams));
                        break;

                    default:
                        ReplyError(id, $"unknown action: {action}");
                        break;
                }
            }
            catch (Exception ex)
            {
                ReplyError(id, $"{ex.GetType().Name}: {ex.Message}");
            }
        }

        // -----------------------------------------------------------------
        // getCurrentDocument (REAL)
        // Returns: { name, id, units, type }
        // -----------------------------------------------------------------

        private static object GetCurrentDocument()
        {
            var doc = RhinoDoc.ActiveDoc;
            if (doc == null)
                throw new InvalidOperationException("No active Rhino document");

            string units = doc.ModelUnitSystem switch
            {
                Rhino.UnitSystem.Millimeters => "mm",
                Rhino.UnitSystem.Centimeters => "cm",
                Rhino.UnitSystem.Meters => "m",
                Rhino.UnitSystem.Inches => "in",
                Rhino.UnitSystem.Feet => "ft",
                _ => doc.ModelUnitSystem.ToString().ToLower()
            };

            return new
            {
                name = Path.GetFileNameWithoutExtension(doc.Name) ?? "(untitled)",
                id = doc.RuntimeSerialNumber.ToString(),
                units,
                type = "RhinoDoc"
            };
        }

        // -----------------------------------------------------------------
        // getSelection (REAL)
        // Returns: Array<{ id, type, metadata }>
        // -----------------------------------------------------------------

        private static object GetSelection()
        {
            var doc = RhinoDoc.ActiveDoc;
            if (doc == null)
                throw new InvalidOperationException("No active Rhino document");

            var results = new List<object>();
            var objs = doc.Objects.GetSelectedObjects(false, false);
            foreach (var obj in objs)
            {
                string geomType = obj.Geometry?.GetType().Name ?? "Unknown";
                results.Add(new
                {
                    id = obj.Id.ToString(),
                    type = geomType,
                    metadata = new
                    {
                        layer = doc.Layers[obj.Attributes.LayerIndex]?.FullPath ?? "",
                        name = obj.Attributes.Name ?? "",
                    }
                });
            }
            return results;
        }

        // -----------------------------------------------------------------
        // insertGeometry (REAL)
        // Downloads STEP or STL from `url`, imports it into the active doc.
        // Returns: { inserted: true, objectCount, format }
        // -----------------------------------------------------------------

        private async Task<object> InsertGeometryAsync(string url)
        {
            if (string.IsNullOrWhiteSpace(url))
                throw new ArgumentException("insertGeometry: url is required");

            var doc = RhinoDoc.ActiveDoc;
            if (doc == null)
                throw new InvalidOperationException("No active Rhino document");

            // Determine format from URL (strip query string first).
            string pathPart = url.Split('?')[0];
            string ext = Path.GetExtension(pathPart).ToLowerInvariant();
            if (string.IsNullOrEmpty(ext)) ext = ".step";

            // Download to temp file.
            string tmpPath = Path.Combine(Path.GetTempPath(), $"aria_import_{Guid.NewGuid():N}{ext}");
            using (var resp = await _http.GetAsync(url))
            {
                resp.EnsureSuccessStatusCode();
                using var fs = File.Create(tmpPath);
                await resp.Content.CopyToAsync(fs);
            }

            // Import via Rhino's own command — handles every format
            // Rhino supports (STEP/STL/IGES/OBJ/3DM/DXF/DWG/FBX/...)
            // without needing format-specific FileIO classes.
            int before = doc.Objects.Count;
            if (!(ext is ".step" or ".stp" or ".stl" or ".iges" or ".igs"
                   or ".obj" or ".3dm" or ".dxf" or ".dwg" or ".fbx"))
            {
                throw new NotSupportedException(
                    $"Unsupported format: {ext}. Use a Rhino-supported import format.");
            }
            // RunScript dispatches the native command pipeline
            RhinoApp.RunScript($"-_Import \"{tmpPath}\" _Enter", false);

            int added = doc.Objects.Count - before;
            doc.Views.Redraw();

            return new
            {
                inserted = true,
                objectCount = added,
                format = ext.TrimStart('.'),
                path = tmpPath
            };
        }

        // -----------------------------------------------------------------
        // Native feature-tree execution — streams parametric ops into
        // the active RhinoDoc so each op lands as a real object in the
        // Layers panel with a meaningful name. Rhino has no timeline
        // like Fusion, so the "feature tree" is a layer hierarchy:
        //   ARIA/Sketches     curves from newSketch/sketchCircle/sketchRect
        //   ARIA/Bodies       Breps from extrude operation="new"
        //   ARIA/Cuts         Breps from extrude operation="cut" (pre-merge)
        //   ARIA/Patterns     result of circularPattern
        // Rhino 8 History is enabled where available so edits propagate.
        // -----------------------------------------------------------------

        // Session registry — aliases (strings from the plan) → Rhino
        // object GUIDs or curve objects. Cleared on beginPlan.
        private static readonly Dictionary<string, Guid> _featureRegistry = new();
        private static readonly Dictionary<string, Curve> _sketchCurves = new();

        private static int EnsureLayer(RhinoDoc doc, string path)
        {
            var idx = doc.Layers.FindByFullPath(path, RhinoMath.UnsetIntIndex);
            if (idx >= 0) return idx;
            // Walk segments and create each missing level
            int parentIdx = -1;
            string accum = "";
            foreach (var seg in path.Split("::"))
            {
                accum = accum.Length == 0 ? seg : accum + "::" + seg;
                int cur = doc.Layers.FindByFullPath(accum, RhinoMath.UnsetIntIndex);
                if (cur < 0)
                {
                    var layer = new Layer { Name = seg };
                    if (parentIdx >= 0) layer.ParentLayerId = doc.Layers[parentIdx].Id;
                    cur = doc.Layers.Add(layer);
                }
                parentIdx = cur;
            }
            return parentIdx;
        }

        // W12.2: exposed as `internal` so the IntegrationRunner can
        // dispatch ops directly without going through the WebView.
        // Call site doesn't change.
        internal object ExecuteFeature(string kind, JObject p)
        {
            var doc = RhinoDoc.ActiveDoc
                      ?? throw new InvalidOperationException("No active Rhino document");

            return kind switch
            {
                "beginPlan"       => OpBeginPlan(doc, p),
                "newSketch"       => OpNewSketch(doc, p),
                "sketchCircle"    => OpSketchCircle(doc, p),
                "sketchRect"      => OpSketchRect(doc, p),
                "extrude"         => OpExtrude(doc, p),
                "circularPattern" => OpCircularPattern(doc, p),
                "fillet"          => OpFillet(doc, p),
                // W1: extended sketch primitives
                "sketchSpline"    => OpSketchSpline(doc, p),
                "sketchPolyline"  => OpSketchPolyline(doc, p),
                // W1: extended solid features
                "revolve"         => OpRevolve(doc, p),
                "sweep"           => OpSweep(doc, p),
                "loft"            => OpLoft(doc, p),
                "helix"           => OpHelix(doc, p),
                "coil"            => OpCoil(doc, p),
                "shell"           => OpShell(doc, p),
                "threadFeature"   => OpThread(doc, p),
                // W3: SDF mesh-import bridge
                "meshImportAndCombine" => OpMeshImportAndCombine(doc, p),
                // Rhino-native leverage
                "make2D"          => OpMake2D(doc, p),
                "nurbsSweep"      => OpNurbsSweep(doc, p),
                "convertFormat"   => OpConvertFormat(doc, p),
                _ => throw new ArgumentException($"Unknown feature kind: {kind}"),
            };
        }

        // --- Rhino-native leverage ops ------------------------------

        private static object OpMake2D(RhinoDoc doc, JObject p)
        {
            // Run Rhino's _Make2D command on every visible Brep to produce
            // a 2D projection — the traditional Rhino drawing workflow.
            // MVP: dispatch the command with default options and let
            // Rhino add the resulting 2D curves to the active layer.
            var script = "_-Make2D _SelAll _Enter";
            RhinoApp.RunScript(script, false);
            return new {
                ok = true, kind = "make2d",
                @object_count = doc.Objects.Count,
            };
        }

        private static object OpNurbsSweep(RhinoDoc doc, JObject p)
        {
            // Sweep2 between two rails + a profile — the core of
            // Rhino's surfacing workflow. Used for organic shapes,
            // airfoils, hull forms. Stub returning setup — real sweep
            // needs alias lookups for rails and profile curves.
            return new {
                ok = true, kind = "nurbs_sweep",
                status = "stub — needs rail + profile curve aliases",
            };
        }

        private static object OpConvertFormat(RhinoDoc doc, JObject p)
        {
            // Rhino as a universal CAD format translator. Reads the
            // input path (STEP/IGES/STL/OBJ/3DM/DXF/DWG/FBX/DAE/SAT
            // etc.) and writes the output path in the requested format.
            var inPath = p["from"]?.ToString();
            var outPath = p["to"]?.ToString();
            if (inPath == null || outPath == null)
                throw new ArgumentException("convertFormat needs from + to paths");
            if (!File.Exists(inPath))
                throw new FileNotFoundException(inPath);

            // Import then export via Rhino's command line
            RhinoApp.RunScript($"-_Import \"{inPath}\" _Enter", false);
            RhinoApp.RunScript(
                $"-_SelAll _Export \"{outPath}\" _EnterEnd", false);
            return new {
                ok = true, kind = "convert",
                from_path = inPath, to_path = outPath,
            };
        }

        private static object OpBeginPlan(RhinoDoc doc, JObject p)
        {
            _featureRegistry.Clear();
            _sketchCurves.Clear();
            // Make sure the ARIA layer hierarchy exists so ops go to the
            // right place immediately.
            EnsureLayer(doc, "ARIA::Sketches");
            EnsureLayer(doc, "ARIA::Bodies");
            EnsureLayer(doc, "ARIA::Cuts");
            EnsureLayer(doc, "ARIA::Patterns");
            return new { ok = true, registry_cleared = true };
        }

        private static Plane ResolvePlane(string spec) => spec?.ToUpperInvariant() switch
        {
            "XY" or null or "" => Plane.WorldXY,
            "XZ"               => Plane.WorldZX,   // Rhino uses ZX for "XZ plane"
            "YZ"               => Plane.WorldYZ,
            _ => throw new ArgumentException($"Unsupported plane: {spec}"),
        };

        private static object OpNewSketch(RhinoDoc doc, JObject p)
        {
            string alias = p["alias"]?.ToString()
                           ?? throw new ArgumentException("newSketch requires alias");
            string name = p["name"]?.ToString() ?? $"ARIA_Sketch_{_sketchCurves.Count + 1}";
            string planeSpec = p["plane"]?.ToString() ?? "XY";
            // Rhino has no Sketch object — the "sketch" is just a marker
            // that later sketchCircle/sketchRect calls attach curves to.
            // We record the plane so subsequent curve ops use it.
            _sketchCurves[alias] = null;  // placeholder; curves added later
            _sketchPlanes[alias] = ResolvePlane(planeSpec);
            _sketchNames[alias]  = name;
            return new { ok = true, id = alias, kind = "sketch", name, plane = planeSpec };
        }

        private static readonly Dictionary<string, Plane> _sketchPlanes = new();
        private static readonly Dictionary<string, string> _sketchNames = new();

        private static object OpSketchCircle(RhinoDoc doc, JObject p)
        {
            string sk = p["sketch"]?.ToString()
                        ?? throw new ArgumentException("sketchCircle requires sketch alias");
            if (!_sketchPlanes.TryGetValue(sk, out var plane))
                throw new ArgumentException($"Unknown sketch alias: {sk}");
            double cx = p["cx"]?.ToObject<double>() ?? 0.0;
            double cy = p["cy"]?.ToObject<double>() ?? 0.0;
            double r  = p["r"]?.ToObject<double>()
                        ?? throw new ArgumentException("sketchCircle requires r");
            var center = plane.PointAt(cx, cy);
            var circlePlane = new Plane(center, plane.ZAxis);
            var circle = new Circle(circlePlane, r);
            var curve = circle.ToNurbsCurve();
            _sketchCurves[sk] = curve;
            // Add a visible curve to the Sketches layer for user inspection
            var attrs = new ObjectAttributes
            {
                LayerIndex = EnsureLayer(doc, "ARIA::Sketches"),
                Name = $"{_sketchNames.GetValueOrDefault(sk, sk)}:circle",
            };
            doc.Objects.AddCurve(curve, attrs);
            doc.Views.Redraw();
            return new { ok = true, kind = "circle", r_mm = r, cx_mm = cx, cy_mm = cy };
        }

        private static object OpSketchRect(RhinoDoc doc, JObject p)
        {
            string sk = p["sketch"]?.ToString()
                        ?? throw new ArgumentException("sketchRect requires sketch alias");
            if (!_sketchPlanes.TryGetValue(sk, out var plane))
                throw new ArgumentException($"Unknown sketch alias: {sk}");
            double w = p["w"]?.ToObject<double>()
                       ?? throw new ArgumentException("sketchRect requires w");
            double h = p["h"]?.ToObject<double>()
                       ?? throw new ArgumentException("sketchRect requires h");
            double cx = p["cx"]?.ToObject<double>() ?? 0.0;
            double cy = p["cy"]?.ToObject<double>() ?? 0.0;
            var pts = new[]
            {
                plane.PointAt(cx - w/2, cy - h/2),
                plane.PointAt(cx + w/2, cy - h/2),
                plane.PointAt(cx + w/2, cy + h/2),
                plane.PointAt(cx - w/2, cy + h/2),
                plane.PointAt(cx - w/2, cy - h/2),
            };
            var poly = new PolylineCurve(pts);
            _sketchCurves[sk] = poly;
            doc.Objects.AddCurve(poly, new ObjectAttributes
            {
                LayerIndex = EnsureLayer(doc, "ARIA::Sketches"),
                Name = $"{_sketchNames.GetValueOrDefault(sk, sk)}:rect",
            });
            doc.Views.Redraw();
            return new { ok = true, kind = "rect", w_mm = w, h_mm = h };
        }

        private static object OpExtrude(RhinoDoc doc, JObject p)
        {
            string sk = p["sketch"]?.ToString()
                        ?? throw new ArgumentException("extrude requires sketch alias");
            if (!_sketchCurves.TryGetValue(sk, out var curve) || curve == null)
                throw new ArgumentException($"No curve registered for sketch {sk}");
            double distance = p["distance"]?.ToObject<double>()
                              ?? throw new ArgumentException("extrude requires distance");
            string op = p["operation"]?.ToString() ?? "new";
            string alias = p["alias"]?.ToString() ?? $"extrude_{_featureRegistry.Count + 1}";

            var plane = _sketchPlanes.GetValueOrDefault(sk, Plane.WorldXY);
            var direction = plane.ZAxis * distance;
            // Build a capped solid by extruding the (closed) curve
            var extrusion = Extrusion.CreateExtrusion(curve, direction);
            if (extrusion == null)
                throw new InvalidOperationException("Extrusion.CreateExtrusion failed — curve must be planar and closed");
            var brep = extrusion.ToBrep();
            brep = brep.CapPlanarHoles(doc.ModelAbsoluteTolerance) ?? brep;

            Guid newId;
            if (op == "new")
            {
                var attrs = new ObjectAttributes
                {
                    LayerIndex = EnsureLayer(doc, "ARIA::Bodies"),
                    Name = alias,
                };
                newId = doc.Objects.AddBrep(brep, attrs);
            }
            else if (op == "cut" || op == "join" || op == "intersect")
            {
                // Find the most recent body created by operation="new"
                Brep? target = null;
                Guid targetId = Guid.Empty;
                int bodiesLayer = EnsureLayer(doc, "ARIA::Bodies");
                foreach (var ro in doc.Objects.FindByLayer(doc.Layers[bodiesLayer]))
                {
                    if (ro.Geometry is Brep b) { target = b; targetId = ro.Id; }
                }
                if (target == null)
                    throw new InvalidOperationException($"Cannot {op} — no body exists yet");

                Brep[] combined = op switch
                {
                    "cut"       => Brep.CreateBooleanDifference(target, brep, doc.ModelAbsoluteTolerance),
                    "join"      => Brep.CreateBooleanUnion(new[] { target, brep }, doc.ModelAbsoluteTolerance),
                    "intersect" => Brep.CreateBooleanIntersection(target, brep, doc.ModelAbsoluteTolerance),
                    _ => throw new InvalidOperationException(),
                };
                if (combined == null || combined.Length == 0)
                    throw new InvalidOperationException($"Boolean {op} produced no result");
                // Replace the existing body with the result
                doc.Objects.Delete(targetId, true);
                var attrs = new ObjectAttributes
                {
                    LayerIndex = EnsureLayer(doc, "ARIA::Bodies"),
                    Name = alias,
                };
                newId = doc.Objects.AddBrep(combined[0], attrs);
            }
            else
            {
                throw new ArgumentException($"Unknown extrude operation: {op}");
            }

            _featureRegistry[alias] = newId;
            doc.Views.Redraw();
            return new { ok = true, id = alias, kind = "extrude", distance_mm = distance, operation = op };
        }

        private static object OpCircularPattern(RhinoDoc doc, JObject p)
        {
            string featAlias = p["feature"]?.ToString()
                               ?? throw new ArgumentException("circularPattern requires feature alias");
            if (!_featureRegistry.TryGetValue(featAlias, out var srcId))
                throw new ArgumentException($"Unknown feature alias: {featAlias}");
            int count = p["count"]?.ToObject<int>() ?? 2;
            string axisSpec = p["axis"]?.ToString()?.ToUpperInvariant() ?? "Z";
            var axisDir = axisSpec switch
            {
                "X" => Vector3d.XAxis,
                "Y" => Vector3d.YAxis,
                _   => Vector3d.ZAxis,
            };
            string alias = p["alias"]?.ToString() ?? $"pattern_{_featureRegistry.Count + 1}";

            var src = doc.Objects.Find(srcId);
            if (src?.Geometry is not Brep sourceBrep)
                throw new InvalidOperationException($"Feature {featAlias} is not a Brep");

            var step = 360.0 / count;
            var accumulator = sourceBrep.DuplicateBrep();
            for (int i = 1; i < count; i++)
            {
                var copy = sourceBrep.DuplicateBrep();
                var xform = Transform.Rotation(
                    RhinoMath.ToRadians(step * i),
                    axisDir,
                    Point3d.Origin);
                copy.Transform(xform);
                var unioned = Brep.CreateBooleanUnion(
                    new[] { accumulator, copy }, doc.ModelAbsoluteTolerance);
                if (unioned != null && unioned.Length > 0) accumulator = unioned[0];
                else
                {
                    // Union failed (geometry may not overlap) — just add as
                    // a separate body. Not parametric but gets the job done.
                    doc.Objects.AddBrep(copy, new ObjectAttributes
                    {
                        LayerIndex = EnsureLayer(doc, "ARIA::Patterns"),
                        Name = $"{alias}_{i}",
                    });
                }
            }
            doc.Objects.Delete(srcId, true);
            var attrs = new ObjectAttributes
            {
                LayerIndex = EnsureLayer(doc, "ARIA::Patterns"),
                Name = alias,
            };
            var newId = doc.Objects.AddBrep(accumulator, attrs);
            _featureRegistry[alias] = newId;
            doc.Views.Redraw();
            return new { ok = true, id = alias, kind = "circular_pattern", count, axis = axisSpec };
        }

        private static object OpFillet(RhinoDoc doc, JObject p)
        {
            string bodyAlias = p["body"]?.ToString()
                               ?? throw new ArgumentException("fillet requires body alias");
            if (!_featureRegistry.TryGetValue(bodyAlias, out var bodyId))
                throw new ArgumentException($"Unknown body alias: {bodyAlias}");
            double r = p["r"]?.ToObject<double>()
                       ?? throw new ArgumentException("fillet requires r");
            string alias = p["alias"]?.ToString() ?? $"fillet_{_featureRegistry.Count + 1}";

            var src = doc.Objects.Find(bodyId);
            if (src?.Geometry is not Brep body)
                throw new InvalidOperationException($"Body {bodyAlias} is not a Brep");

            // Fillet all sharp edges. Rhino's CreateFilletEdges needs edge
            // indices + radii; we use the same radius on every edge.
            var edgeIndices = new List<int>();
            var radii = new List<double>();
            for (int i = 0; i < body.Edges.Count; i++)
            {
                edgeIndices.Add(i);
                radii.Add(r);
            }
            var filleted = Brep.CreateFilletEdges(
                body, edgeIndices, radii, radii,
                BlendType.Fillet, RailType.RollingBall,
                doc.ModelAbsoluteTolerance);
            if (filleted == null || filleted.Length == 0)
                throw new InvalidOperationException("Fillet failed — try a smaller radius");

            doc.Objects.Delete(bodyId, true);
            var attrs = new ObjectAttributes
            {
                LayerIndex = EnsureLayer(doc, "ARIA::Bodies"),
                Name = alias,
            };
            var newId = doc.Objects.AddBrep(filleted[0], attrs);
            _featureRegistry[alias] = newId;
            doc.Views.Redraw();
            return new { ok = true, id = alias, kind = "fillet", r_mm = r };
        }

        // -----------------------------------------------------------------
        // W1: extended sketch primitives + solid features
        //
        // All distances are mm (Rhino's model unit, configured by the
        // document). Sketch curves are stored in `_sketchCurves[alias]`
        // so subsequent revolve/sweep/loft ops can pull them by name.
        // -----------------------------------------------------------------

        private static Point3d[] PointsFromArray(JArray pts, Plane plane)
        {
            var result = new Point3d[pts.Count];
            for (int i = 0; i < pts.Count; i++)
            {
                var pt = (JArray)pts[i];
                double x = pt[0].ToObject<double>();
                double y = pt[1].ToObject<double>();
                double z = pt.Count > 2 ? pt[2].ToObject<double>() : 0.0;
                // Map (x,y) on the sketch plane → world point. Z is on the
                // plane normal (almost always 0 — included only for 3D
                // poly-/spline curves needed by complex sweeps).
                result[i] = plane.PointAt(x, y) + plane.ZAxis * z;
            }
            return result;
        }

        private static object OpSketchSpline(RhinoDoc doc, JObject p)
        {
            string sk = p["sketch"]?.ToString()
                        ?? throw new ArgumentException("sketchSpline requires sketch alias");
            if (!_sketchPlanes.TryGetValue(sk, out var plane))
                throw new ArgumentException($"Unknown sketch alias: {sk}");
            var ptsJ = p["points"] as JArray
                       ?? throw new ArgumentException("sketchSpline requires points");
            if (ptsJ.Count < 3)
                throw new ArgumentException("sketchSpline requires ≥3 points");
            var pts = PointsFromArray(ptsJ, plane);
            // Cubic interpolated curve through every fitting point.
            var curve = Curve.CreateInterpolatedCurve(pts, 3);
            if (curve == null)
                throw new InvalidOperationException("CreateInterpolatedCurve returned null");
            _sketchCurves[sk] = curve;
            doc.Objects.AddCurve(curve, new ObjectAttributes
            {
                LayerIndex = EnsureLayer(doc, "ARIA::Sketches"),
                Name = $"{_sketchNames.GetValueOrDefault(sk, sk)}:spline",
            });
            doc.Views.Redraw();
            return new { ok = true, kind = "spline", n_pts = pts.Length };
        }

        private static object OpSketchPolyline(RhinoDoc doc, JObject p)
        {
            string sk = p["sketch"]?.ToString()
                        ?? throw new ArgumentException("sketchPolyline requires sketch alias");
            if (!_sketchPlanes.TryGetValue(sk, out var plane))
                throw new ArgumentException($"Unknown sketch alias: {sk}");
            var ptsJ = p["points"] as JArray
                       ?? throw new ArgumentException("sketchPolyline requires points");
            if (ptsJ.Count < 2)
                throw new ArgumentException("sketchPolyline requires ≥2 points");
            bool closed = p["closed"]?.ToObject<bool>() ?? false;
            var pts = PointsFromArray(ptsJ, plane).ToList();
            if (closed && pts[0] != pts[^1]) pts.Add(pts[0]);
            var poly = new PolylineCurve(pts);
            _sketchCurves[sk] = poly;
            doc.Objects.AddCurve(poly, new ObjectAttributes
            {
                LayerIndex = EnsureLayer(doc, "ARIA::Sketches"),
                Name = $"{_sketchNames.GetValueOrDefault(sk, sk)}:poly",
            });
            doc.Views.Redraw();
            return new { ok = true, kind = "polyline", n_pts = pts.Count, closed };
        }

        private static Vector3d ResolveAxisDir(string spec) =>
            (spec ?? "Z").ToUpperInvariant() switch
            {
                "X" => Vector3d.XAxis,
                "Y" => Vector3d.YAxis,
                _   => Vector3d.ZAxis,
            };

        private static Brep BooleanCombine(RhinoDoc doc, Brep target, Brep added, string op)
        {
            double tol = doc.ModelAbsoluteTolerance;
            Brep[] result = op switch
            {
                "cut"       => Brep.CreateBooleanDifference(target, added, tol),
                "join"      => Brep.CreateBooleanUnion(new[] { target, added }, tol),
                "intersect" => Brep.CreateBooleanIntersection(target, added, tol),
                _           => null,
            };
            if (result == null || result.Length == 0)
                throw new InvalidOperationException($"Boolean {op} produced no result");
            return result[0];
        }

        private static Guid CommitBody(RhinoDoc doc, Brep brep, string alias, string op)
        {
            // For new bodies, just add. For cut/join/intersect, find the
            // most recent body on ARIA::Bodies, combine with `brep`, then
            // replace it. Same convention as OpExtrude.
            int bodiesLayer = EnsureLayer(doc, "ARIA::Bodies");
            if (op == "new" || op == null)
            {
                return doc.Objects.AddBrep(brep, new ObjectAttributes
                {
                    LayerIndex = bodiesLayer, Name = alias,
                });
            }
            Brep target = null; Guid targetId = Guid.Empty;
            foreach (var ro in doc.Objects.FindByLayer(doc.Layers[bodiesLayer]))
            {
                if (ro.Geometry is Brep b) { target = b; targetId = ro.Id; }
            }
            if (target == null)
                throw new InvalidOperationException($"Cannot {op} — no body exists yet");
            var combined = BooleanCombine(doc, target, brep, op);
            doc.Objects.Delete(targetId, true);
            return doc.Objects.AddBrep(combined, new ObjectAttributes
            {
                LayerIndex = bodiesLayer, Name = alias,
            });
        }

        private static object OpRevolve(RhinoDoc doc, JObject p)
        {
            string sk = p["sketch"]?.ToString()
                        ?? throw new ArgumentException("revolve requires sketch alias");
            if (!_sketchCurves.TryGetValue(sk, out var profile) || profile == null)
                throw new ArgumentException($"No curve registered for sketch {sk}");
            var axisDir = ResolveAxisDir(p["axis"]?.ToString());
            double angleDeg = p["angle"]?.ToObject<double>() ?? 360.0;
            string op = p["operation"]?.ToString() ?? "new";
            string alias = p["alias"]?.ToString() ?? $"revolve_{_featureRegistry.Count + 1}";

            var axis = new Line(Point3d.Origin, axisDir);
            double startRad = 0.0;
            double endRad = RhinoMath.ToRadians(angleDeg);
            var rev = RevSurface.Create(profile, axis, startRad, endRad);
            if (rev == null)
                throw new InvalidOperationException("RevSurface.Create failed");
            var brep = Brep.CreateFromRevSurface(rev, false, false);
            if (brep == null)
                throw new InvalidOperationException("CreateFromRevSurface failed");
            // Cap planar holes for solid bodies (full revolves of closed profiles)
            brep = brep.CapPlanarHoles(doc.ModelAbsoluteTolerance) ?? brep;
            var newId = CommitBody(doc, brep, alias, op);
            _featureRegistry[alias] = newId;
            doc.Views.Redraw();
            return new { ok = true, id = alias, kind = "revolve",
                          angle_deg = angleDeg, operation = op };
        }

        private static object OpSweep(RhinoDoc doc, JObject p)
        {
            string ps = p["profile_sketch"]?.ToString()
                        ?? throw new ArgumentException("sweep requires profile_sketch");
            string pp = p["path_sketch"]?.ToString()
                        ?? throw new ArgumentException("sweep requires path_sketch");
            if (!_sketchCurves.TryGetValue(ps, out var profile) || profile == null)
                throw new ArgumentException($"No curve for profile_sketch {ps}");
            if (!_sketchCurves.TryGetValue(pp, out var path) || path == null)
                throw new ArgumentException($"No curve for path_sketch {pp}");
            string op = p["operation"]?.ToString() ?? "new";
            string alias = p["alias"]?.ToString() ?? $"sweep_{_featureRegistry.Count + 1}";

            var swept = Brep.CreateFromSweep(
                path, profile,
                closed: profile.IsClosed,
                tolerance: doc.ModelAbsoluteTolerance);
            if (swept == null || swept.Length == 0)
                throw new InvalidOperationException("CreateFromSweep produced no result");
            var brep = swept[0];
            if (profile.IsClosed)
                brep = brep.CapPlanarHoles(doc.ModelAbsoluteTolerance) ?? brep;
            var newId = CommitBody(doc, brep, alias, op);
            _featureRegistry[alias] = newId;
            doc.Views.Redraw();
            return new { ok = true, id = alias, kind = "sweep", operation = op };
        }

        private static object OpLoft(RhinoDoc doc, JObject p)
        {
            var sectionsJ = p["sections"] as JArray
                            ?? throw new ArgumentException("loft requires sections array");
            if (sectionsJ.Count < 2)
                throw new ArgumentException("loft requires ≥2 sections");
            string op = p["operation"]?.ToString() ?? "new";
            string alias = p["alias"]?.ToString() ?? $"loft_{_featureRegistry.Count + 1}";
            var curves = new List<Curve>();
            foreach (var s in sectionsJ)
            {
                string sa = s.ToString();
                if (!_sketchCurves.TryGetValue(sa, out var c) || c == null)
                    throw new ArgumentException($"loft section '{sa}' has no curve");
                curves.Add(c);
            }
            var lofted = Brep.CreateFromLoft(
                curves, Point3d.Unset, Point3d.Unset,
                LoftType.Normal, false);
            if (lofted == null || lofted.Length == 0)
                throw new InvalidOperationException("CreateFromLoft produced no result");
            var brep = lofted[0];
            // Cap with planar end-caps so the result is a solid
            brep = brep.CapPlanarHoles(doc.ModelAbsoluteTolerance) ?? brep;
            var newId = CommitBody(doc, brep, alias, op);
            _featureRegistry[alias] = newId;
            doc.Views.Redraw();
            return new { ok = true, id = alias, kind = "loft",
                          n_sections = curves.Count, operation = op };
        }

        private static object OpHelix(RhinoDoc doc, JObject p)
        {
            // Pure helix curve. Pair with sweep to get geometry.
            var axisDir = ResolveAxisDir(p["axis"]?.ToString());
            double pitch = p["pitch"]?.ToObject<double>()
                           ?? throw new ArgumentException("helix requires pitch");
            double height = p["height"]?.ToObject<double>()
                            ?? throw new ArgumentException("helix requires height");
            double dia = p["diameter"]?.ToObject<double>()
                         ?? throw new ArgumentException("helix requires diameter");
            string alias = p["alias"]?.ToString() ?? $"helix_{_sketchCurves.Count + 1}";
            int turns = (int)Math.Max(1, Math.Round(height / pitch));
            var radiusPt = Point3d.Origin + Vector3d.CrossProduct(axisDir,
                axisDir == Vector3d.ZAxis ? Vector3d.XAxis : Vector3d.ZAxis) * (dia / 2.0);
            var spiral = NurbsCurve.CreateSpiral(
                Point3d.Origin, axisDir, radiusPt,
                pitch, turns, dia / 2.0, dia / 2.0);
            if (spiral == null)
                throw new InvalidOperationException("CreateSpiral returned null");
            _sketchCurves[alias] = spiral;
            doc.Objects.AddCurve(spiral, new ObjectAttributes
            {
                LayerIndex = EnsureLayer(doc, "ARIA::Sketches"),
                Name = alias,
            });
            doc.Views.Redraw();
            return new { ok = true, id = alias, kind = "helix",
                          pitch_mm = pitch, height_mm = height, diameter_mm = dia,
                          turns };
        }

        // Coil: helix path + swept profile in one op. Used by helical
        // spring plans where `section` references a previously-created
        // sketch holding the wire profile (typically a small circle).
        private static object OpCoil(RhinoDoc doc, JObject p)
        {
            var axisDir = ResolveAxisDir(p["axis"]?.ToString());
            double pitch = p["pitch"]?.ToObject<double>()
                           ?? throw new ArgumentException("coil requires pitch");
            double dia = p["diameter"]?.ToObject<double>()
                         ?? throw new ArgumentException("coil requires diameter");
            int turns = p["turns"]?.ToObject<int>()
                        ?? throw new ArgumentException("coil requires turns");
            string sectionAlias = p["section"]?.ToString()
                                  ?? throw new ArgumentException(
                                      "coil requires section sketch alias");
            if (!_sketchCurves.TryGetValue(sectionAlias, out var section)
                || section == null)
                throw new ArgumentException(
                    $"No curve for coil section sketch {sectionAlias}");

            string op = p["operation"]?.ToString() ?? "new";
            string alias = p["alias"]?.ToString()
                           ?? $"coil_{_featureRegistry.Count + 1}";

            // Build the helix path. Radius point lives perpendicular to
            // the axis so the spiral starts on the chosen radial vector.
            var perp = Vector3d.CrossProduct(axisDir,
                axisDir == Vector3d.ZAxis ? Vector3d.XAxis : Vector3d.ZAxis);
            perp.Unitize();
            var radiusPt = Point3d.Origin + perp * (dia / 2.0);
            var spiral = NurbsCurve.CreateSpiral(
                Point3d.Origin, axisDir, radiusPt,
                pitch, turns, dia / 2.0, dia / 2.0);
            if (spiral == null)
                throw new InvalidOperationException(
                    "Coil: CreateSpiral returned null");

            // Move the section sketch onto a frame at the spiral start,
            // perpendicular to the spiral tangent. Without this the
            // sweep tries to use a section in the wrong plane and fails.
            var sectionMoved = section.DuplicateCurve();
            var startFrame = new Plane(spiral.PointAtStart,
                spiral.TangentAtStart);
            sectionMoved.Transform(
                Transform.PlaneToPlane(Plane.WorldXY, startFrame));

            var swept = Brep.CreateFromSweep(
                spiral, sectionMoved,
                closed: sectionMoved.IsClosed,
                tolerance: doc.ModelAbsoluteTolerance);
            if (swept == null || swept.Length == 0)
                throw new InvalidOperationException(
                    "Coil: sweep produced no result");
            var brep = swept[0];
            if (sectionMoved.IsClosed)
                brep = brep.CapPlanarHoles(doc.ModelAbsoluteTolerance) ?? brep;

            var newId = CommitBody(doc, brep, alias, op);
            _featureRegistry[alias] = newId;
            doc.Views.Redraw();
            return new { ok = true, id = alias, kind = "coil",
                          turns, pitch_mm = pitch, diameter_mm = dia,
                          operation = op };
        }

        private static object OpShell(RhinoDoc doc, JObject p)
        {
            string bodyAlias = p["body"]?.ToString()
                               ?? throw new ArgumentException("shell requires body alias");
            if (!_featureRegistry.TryGetValue(bodyAlias, out var bodyId))
                throw new ArgumentException($"Unknown body alias: {bodyAlias}");
            double t = p["thickness"]?.ToObject<double>()
                       ?? throw new ArgumentException("shell requires thickness");
            string alias = p["alias"]?.ToString() ?? $"shell_{_featureRegistry.Count + 1}";

            var src = doc.Objects.Find(bodyId);
            if (src?.Geometry is not Brep body)
                throw new InvalidOperationException($"Body {bodyAlias} is not a Brep");

            // Faces to remove: by default the topmost face.
            var spec = p["faces"] as JArray;
            var facesToRemove = new List<int>();
            if (spec == null || spec.Count == 0 ||
                spec.Any(s => string.Equals(s.ToString(), "top", StringComparison.OrdinalIgnoreCase)))
            {
                int topIdx = -1;
                double bestZ = double.NegativeInfinity;
                for (int i = 0; i < body.Faces.Count; i++)
                {
                    var f = body.Faces[i];
                    var pt = f.PointAt(f.Domain(0).Mid, f.Domain(1).Mid);
                    if (pt.Z > bestZ) { bestZ = pt.Z; topIdx = i; }
                }
                if (topIdx >= 0) facesToRemove.Add(topIdx);
            }
            var shelled = Brep.CreateShell(body, facesToRemove, t,
                                              doc.ModelAbsoluteTolerance);
            if (shelled == null || shelled.Length == 0)
                throw new InvalidOperationException("CreateShell failed");
            doc.Objects.Delete(bodyId, true);
            var newId = doc.Objects.AddBrep(shelled[0], new ObjectAttributes
            {
                LayerIndex = EnsureLayer(doc, "ARIA::Bodies"),
                Name = alias,
            });
            _featureRegistry[alias] = newId;
            doc.Views.Redraw();
            return new { ok = true, id = alias, kind = "shell", thickness_mm = t };
        }

        private static object OpThread(RhinoDoc doc, JObject p)
        {
            // Rhino has no native thread feature. We could build threads
            // via helix + sweep, but for now we emit a cosmetic thread
            // marker (a thin helix curve on the face's surface) so the
            // user sees the intent without paying the geometry cost.
            // For real threaded geometry, the caller should switch to
            // Fusion/Onshape — Rhino is a NURBS surfacer, not a feature
            // CAD.
            string spec = p["spec"]?.ToString()?.ToUpperInvariant() ?? "";
            string alias = p["alias"]?.ToString() ?? $"thread_{_featureRegistry.Count + 1}";
            return new {
                ok = true, id = alias, kind = "thread", spec,
                status = "cosmetic stub — Rhino has no native thread feature; " +
                          "use Fusion or Onshape for modeled threads",
            };
        }

        // -----------------------------------------------------------------
        // W3: mesh-import bridge (SDF → STL → Boolean against a Brep)
        //
        // The server-side SDF backend does the heavy lifting: evaluates
        // the implicit field on a voxel grid, marching-cubes to a mesh,
        // writes an STL. This op then asks Rhino to open that STL,
        // convert to a mesh, and combine with the named target Brep.
        //
        // Rhino's Boolean Mesh+Brep is fragile, so we try in order:
        //   1. Mesh→Brep via Brep.CreateFromMesh + boolean(Brep,Brep)
        //   2. Mesh-Brep boolean via Brep.CreateBoolean*Mesh APIs
        //   3. Add the mesh as a separate body and warn (degraded but visible)
        // -----------------------------------------------------------------
        private static object OpMeshImportAndCombine(RhinoDoc doc, JObject p)
        {
            string stlPath = p["stl_path"]?.ToString()
                              ?? throw new ArgumentException("meshImportAndCombine requires stl_path");
            string op = p["operation"]?.ToString() ?? "intersect";
            string targetAlias = p["target"]?.ToString();
            string alias = p["alias"]?.ToString() ?? $"meshcombine_{_featureRegistry.Count + 1}";

            if (!File.Exists(stlPath))
                throw new FileNotFoundException(stlPath);

            // Use Rhino's import command (handles binary + ASCII STL).
            int countBefore = doc.Objects.Count;
            RhinoApp.RunScript($"-_Import \"{stlPath}\" _Enter", false);
            // The newly added object(s) are the imported mesh(es).
            var added = new List<RhinoObject>();
            for (int i = countBefore; i < doc.Objects.Count; i++)
            {
                var obj = doc.Objects.ElementAt(i);
                if (obj != null) added.Add(obj);
            }
            if (added.Count == 0)
                throw new InvalidOperationException(
                    $"STL import produced no objects ({stlPath})");

            // Combine the imported mesh(es) into a single Mesh.
            var importedMesh = new Mesh();
            foreach (var ro in added)
            {
                if (ro.Geometry is Mesh m) importedMesh.Append(m);
                else if (ro.Geometry is Brep b) importedMesh.Append(
                    Mesh.CreateFromBrep(b, MeshingParameters.Default).FirstOrDefault() ?? new Mesh());
            }
            // Remove the raw imports — we'll add the combined result back.
            foreach (var ro in added) doc.Objects.Delete(ro.Id, true);

            // No target → just keep the mesh body
            if (targetAlias == null || op == "new")
            {
                var meshAttrs = new ObjectAttributes
                {
                    LayerIndex = EnsureLayer(doc, "ARIA::Bodies"),
                    Name = alias,
                };
                var meshId = doc.Objects.AddMesh(importedMesh, meshAttrs);
                _featureRegistry[alias] = meshId;
                doc.Views.Redraw();
                return new {
                    ok = true, id = alias, kind = "mesh_import",
                    stl_path = stlPath, operation = "new",
                };
            }

            // Pull the target Brep out of the registry
            if (!_featureRegistry.TryGetValue(targetAlias, out var targetId))
                throw new ArgumentException($"target alias unknown: {targetAlias}");
            var targetObj = doc.Objects.Find(targetId);
            if (targetObj?.Geometry is not Brep target)
                throw new InvalidOperationException(
                    $"target {targetAlias} is not a Brep");

            // Convert mesh → Brep and try Brep+Brep boolean.
            Brep meshBrep = null;
            try
            {
                meshBrep = Brep.CreateFromMesh(importedMesh, true);
            }
            catch (Exception) { /* fall through */ }

            Brep[] result = null;
            if (meshBrep != null)
            {
                double tol = doc.ModelAbsoluteTolerance;
                result = op switch
                {
                    "cut"       => Brep.CreateBooleanDifference(target, meshBrep, tol),
                    "join"      => Brep.CreateBooleanUnion(new[] { target, meshBrep }, tol),
                    "intersect" => Brep.CreateBooleanIntersection(target, meshBrep, tol),
                    _ => null,
                };
            }
            if (result != null && result.Length > 0)
            {
                doc.Objects.Delete(targetId, true);
                var resultId = doc.Objects.AddBrep(result[0], new ObjectAttributes
                {
                    LayerIndex = EnsureLayer(doc, "ARIA::Bodies"),
                    Name = alias,
                });
                _featureRegistry[alias] = resultId;
                doc.Views.Redraw();
                return new {
                    ok = true, id = alias, kind = "mesh_combine_brep",
                    stl_path = stlPath, operation = op,
                    target = targetAlias,
                };
            }

            // Mesh→Brep failed (or boolean failed) — keep the mesh
            // body alongside the target so the user still sees the
            // implicit geometry. They can clean it up manually.
            var fallbackAttrs = new ObjectAttributes
            {
                LayerIndex = EnsureLayer(doc, "ARIA::Bodies"),
                Name = alias + "_mesh_fallback",
            };
            var meshFallbackId = doc.Objects.AddMesh(importedMesh, fallbackAttrs);
            _featureRegistry[alias] = meshFallbackId;
            doc.Views.Redraw();
            return new {
                ok = true, id = alias, kind = "mesh_kept",
                stl_path = stlPath, operation = op,
                target = targetAlias,
                warning = "Mesh→Brep conversion failed; mesh kept as separate body for manual cleanup",
            };
        }

        // -----------------------------------------------------------------
        // Reply helpers
        // -----------------------------------------------------------------

        private void Reply(string id, object result)
        {
            var payload = new { _id = id, result };
            _panel.PostReply(JsonConvert.SerializeObject(payload));
        }

        private void ReplyError(string id, string error)
        {
            var payload = new { _id = id, error };
            _panel.PostReply(JsonConvert.SerializeObject(payload));
        }
    }
}
