// AriaHttpListener.cs — Headless HTTP entry point for the Rhino plugin.
//
// Mirrors cad-plugins/solidworks/AriaSW/AriaHttpListener.cs so the
// orchestrator can drive Rhino the same way as SW: curl POST /op,
// curl GET /screenshot, curl GET /status. No GUI clicks required.
//
// Endpoints (all bound to http://localhost:7502/):
//   GET  /status      — { ok, doc, units, ops_dispatched, recipe_count }
//   POST /new_doc     — open a new Rhino document, returns { ok }
//   POST /op  body:{ kind, params }
//                      — execute one op via AriaBridge.ExecuteFeature
//   GET  /screenshot   — return PNG of the active viewport
//   POST /save_step    — body:{ path }, save active doc as STEP
//   POST /quit         — close active doc, returns { ok }

using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Rhino;
using Rhino.Display;
using Rhino.FileIO;
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace AriaPanel
{
    internal static class AriaHttpListener
    {
        private static HttpListener? _listener;
        private static Thread? _thread;
        private static volatile bool _running;
        private static int _opsDispatched;
        public  static readonly object DispatchLock = new object();

        private static int Port =>
            int.TryParse(System.Environment.GetEnvironmentVariable("ARIA_RHINO_PORT"),
                          out var p) ? p : 7502;

        public static void Start()
        {
            if (_running) return;
            try
            {
                _listener = new HttpListener();
                _listener.Prefixes.Add($"http://localhost:{Port}/");
                _listener.Start();
                _running = true;
                _thread = new Thread(AcceptLoop)
                {
                    IsBackground = true,
                    Name = "AriaRhino-HttpListener",
                };
                _thread.Start();
                RhinoApp.WriteLine($"AriaRhino HttpListener: http://localhost:{Port}/");
            }
            catch (Exception ex)
            {
                RhinoApp.WriteLine($"AriaRhino HttpListener.Start failed: {ex.Message}");
                _running = false;
            }
        }

        public static void Stop()
        {
            _running = false;
            try { _listener?.Stop(); } catch { }
            try { _listener?.Close(); } catch { }
            _listener = null;
        }

        private static void AcceptLoop()
        {
            while (_running)
            {
                HttpListenerContext ctx;
                try { ctx = _listener!.GetContext(); }
                catch { break; }
                ThreadPool.QueueUserWorkItem(_ => Handle(ctx));
            }
        }

        private static void Handle(HttpListenerContext ctx)
        {
            string path = ctx.Request.Url!.AbsolutePath.TrimEnd('/').ToLowerInvariant();
            string method = ctx.Request.HttpMethod.ToUpperInvariant();
            string body = "";
            try
            {
                if (ctx.Request.HasEntityBody)
                {
                    using var reader = new StreamReader(ctx.Request.InputStream,
                        ctx.Request.ContentEncoding ?? Encoding.UTF8);
                    body = reader.ReadToEnd();
                }
                RhinoApp.WriteLine($"AriaRhino HTTP {method} {path}");

                object? result;
                lock (DispatchLock)
                {
                    result = Dispatch(method, path, body, ctx);
                }
                if (result == null) return;  // already wrote response (binary)
                ReplyJson(ctx, 200, result);
            }
            catch (Exception ex)
            {
                RhinoApp.WriteLine($"AriaRhino HTTP error {method} {path}: {ex.Message}");
                ReplyJson(ctx, 500, new
                {
                    ok = false,
                    error = $"{ex.GetType().Name}: {ex.Message}",
                });
            }
        }

        private static object? Dispatch(string method, string path, string body,
                                         HttpListenerContext ctx)
        {
            if (method == "GET" && path == "/status")
            {
                var doc = RhinoDoc.ActiveDoc;
                return new
                {
                    ok = true,
                    has_active_doc = doc != null,
                    doc = doc?.Name,
                    units = doc?.ModelUnitSystem.ToString(),
                    ops_dispatched = _opsDispatched,
                    recipe_count = RecipeDb.Count,
                    port = Port,
                };
            }

            if (method == "GET" && path == "/info")
            {
                var doc = RhinoDoc.ActiveDoc
                          ?? throw new InvalidOperationException("no active doc");
                return RunOnUi<object>(() =>
                {
                    var layers = new List<object>();
                    foreach (var layer in doc.Layers)
                    {
                        if (layer.IsDeleted) continue;
                        layers.Add(new
                        {
                            name = layer.FullPath,
                            visible = layer.IsVisible,
                            object_count = doc.Objects.FindByLayer(layer)?.Length ?? 0,
                        });
                    }
                    var views = new List<object>();
                    foreach (var v in doc.Views)
                    {
                        var vp = v.ActiveViewport;
                        views.Add(new
                        {
                            name = vp.Name,
                            is_perspective = vp.IsPerspectiveProjection,
                            display_mode = vp.DisplayMode?.LocalName,
                            camera_location = $"({vp.CameraLocation.X:F1}, {vp.CameraLocation.Y:F1}, {vp.CameraLocation.Z:F1})",
                            target = $"({vp.CameraTarget.X:F1}, {vp.CameraTarget.Y:F1}, {vp.CameraTarget.Z:F1})",
                        });
                    }
                    // Compute scene bbox manually — ObjectTable has no
                    // GetBoundingBox helper in Rhino 8.
                    var bb = Rhino.Geometry.BoundingBox.Empty;
                    foreach (var ro in doc.Objects)
                    {
                        var ob = ro.Geometry?.GetBoundingBox(true);
                        if (ob.HasValue && ob.Value.IsValid)
                            bb.Union(ob.Value);
                    }
                    return new
                    {
                        ok = true,
                        object_count_total = doc.Objects.Count,
                        active_view = doc.Views.ActiveView?.ActiveViewport.Name,
                        scene_bbox = bb.IsValid
                            ? $"min({bb.Min.X:F1},{bb.Min.Y:F1},{bb.Min.Z:F1}) max({bb.Max.X:F1},{bb.Max.Y:F1},{bb.Max.Z:F1})"
                            : "(empty/invalid)",
                        layers,
                        views,
                    };
                });
            }

            // Synchronous UI-thread invoke. RhinoApp.InvokeOnUiThread is
            // fire-and-forget — without a wait we'd return JSON before
            // the work completes (and read uninitialised result vars).
            T RunOnUi<T>(Func<T> fn)
            {
                var done = new ManualResetEventSlim(false);
                T value = default!;
                Exception? caught = null;
                Rhino.RhinoApp.InvokeOnUiThread(new Action(() =>
                {
                    try { value = fn(); }
                    catch (Exception ex) { caught = ex; }
                    finally { done.Set(); }
                }));
                if (!done.Wait(TimeSpan.FromSeconds(120)))
                    throw new TimeoutException("UI thread did not respond in 120s");
                if (caught != null) throw caught;
                return value;
            }

            if (method == "POST" && path == "/new_doc")
            {
                bool ok = RunOnUi(() => RhinoApp.RunScript("_-New None _Enter", false));
                return new { ok, doc = RhinoDoc.ActiveDoc?.Name };
            }

            if (method == "POST" && path == "/op")
            {
                var msg = JObject.Parse(string.IsNullOrEmpty(body) ? "{}" : body);
                string kind = msg["kind"]?.ToString()
                              ?? throw new ArgumentException("op requires 'kind'");
                JObject p = (msg["params"] as JObject) ?? new JObject();
                _opsDispatched++;

                // Construct a bridge with null panel — ExecuteFeature only
                // uses RhinoDoc.ActiveDoc + static state, doesn't touch panel.
                var bridge = new AriaBridge(null!);
                object? result = RunOnUi(() => (object?)bridge.ExecuteFeature(kind, p));
                return new { ok = true, kind, result };
            }

            if (method == "POST" && path == "/save_step")
            {
                var doc = RhinoDoc.ActiveDoc
                          ?? throw new InvalidOperationException("no active doc");
                var msg = JObject.Parse(string.IsNullOrEmpty(body) ? "{}" : body);
                string outPath = msg["path"]?.ToString()
                                  ?? throw new ArgumentException("save_step requires 'path'");
                // doc.Export shows a STEP options dialog by default. Use
                // _-Export with leading - to suppress dialog + run silent.
                bool ok = RunOnUi(() =>
                {
                    // Select all visible objects so _-Export catches them
                    RhinoApp.RunScript("_-SelAll", false);
                    string cmd = $"_-Export \"{outPath}\" _Schema=AP214AutomotiveDesign _Enter _Enter";
                    return RhinoApp.RunScript(cmd, false);
                });
                return new { ok, path = outPath };
            }

            if (method == "GET" && path == "/screenshot")
            {
                var doc = RhinoDoc.ActiveDoc
                          ?? throw new InvalidOperationException("no active doc");

                // Use Rhino's built-in _-ViewCaptureToFile command. The
                // ViewCaptureSettings/ViewCapture API kept producing blank
                // PNGs even when the doc had geometry (display mode wasn't
                // actually flipping to shaded). The command-line form is
                // what Rhino's own "View → View Capture → To File" calls
                // and it Just Works.
                string tmpPath = Path.Combine(Path.GetTempPath(),
                    $"aria_rhino_screen_{Guid.NewGuid():N}.png");
                bool captured = RunOnUi(() =>
                {
                    foreach (var layer in doc.Layers)
                    {
                        if (layer.FullPath.StartsWith("ARIA"))
                        {
                            layer.IsVisible = true;
                            layer.IsLocked = false;
                        }
                    }
                    // Activate Perspective + shaded + zoom-to-fit.
                    RhinoApp.RunScript("_-SetActiveViewport _Perspective", false);
                    // Set Shaded explicitly via SetDisplayMode (more reliable
                    // than the toggle-style _-Shaded command which sometimes
                    // returns to wireframe). _Mode= takes the display mode
                    // name; "Shaded" exists in stock Rhino 8.
                    RhinoApp.RunScript("_-SetDisplayMode _Mode=Shaded _Enter", false);
                    RhinoApp.RunScript("_Zoom _All _Extents", false);
                    doc.Views.ActiveView?.Redraw();
                    string cmd = $"_-ViewCaptureToFile \"{tmpPath}\" "
                                  + "_Width=1024 _Height=768 _Enter";
                    return RhinoApp.RunScript(cmd, false);
                });
                // RunScript may return False even when the capture
                // succeeded (the script form's return value is unreliable
                // for ViewCaptureToFile). Trust the file existence + size.
                if (!File.Exists(tmpPath))
                    throw new InvalidOperationException(
                        $"ViewCaptureToFile failed (script ok={captured}, no file)");
                byte[] pngBytes = File.ReadAllBytes(tmpPath);
                try { File.Delete(tmpPath); } catch { }
                if (pngBytes.Length < 100)
                    throw new InvalidOperationException(
                        $"ViewCaptureToFile produced empty file ({pngBytes.Length} bytes)");

                ctx.Response.StatusCode = 200;
                ctx.Response.ContentType = "image/png";
                ctx.Response.ContentLength64 = pngBytes.Length;
                ctx.Response.OutputStream.Write(pngBytes, 0, pngBytes.Length);
                ctx.Response.OutputStream.Close();
                return null;
            }

            if (method == "POST" && path == "/quit")
            {
                bool ok = RunOnUi(() => RhinoApp.RunScript("_-New None _Enter", false));
                return new { ok };
            }

            ctx.Response.StatusCode = 404;
            return new { ok = false, error = $"unknown route {method} {path}" };
        }

        private static void ReplyJson(HttpListenerContext ctx, int status, object obj)
        {
            try
            {
                string json = JsonConvert.SerializeObject(obj);
                byte[] bytes = Encoding.UTF8.GetBytes(json);
                ctx.Response.StatusCode = status;
                ctx.Response.ContentType = "application/json";
                ctx.Response.ContentLength64 = bytes.Length;
                ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
                ctx.Response.OutputStream.Close();
            }
            catch { /* listener already closed */ }
        }
    }
}
