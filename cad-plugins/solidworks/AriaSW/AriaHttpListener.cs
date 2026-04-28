// AriaHttpListener.cs — Headless HTTP entry point for the SW addin.
//
// Why this exists:
//   The WebView2 panel mediates op dispatch from the React frontend.
//   That requires the user to manually click the ARIA Generate task pane
//   icon AND click "Load" to spin up WebView2. Until both happen, no ops
//   reach SW — autonomy is impossible.
//
//   This listener gives the orchestrator a direct way to drive SW from
//   any process (curl, Python script, dashboard pipeline) without any
//   GUI clicks. Same dispatch path as WebView2 — both call
//   AriaSwAddin.Current.ExecuteFeature, so behaviour stays identical.
//
// Endpoints (all bound to http://localhost:7501/):
//   GET  /status          — { ok, doc, units, ops_dispatched }
//   POST /new_part        — open a blank Part document, returns { ok }
//   POST /op  body:{kind, params}
//                          — execute one op via ExecuteFeature
//   POST /save_step       — body:{path}, save active doc as STEP
//   GET  /screenshot      — return PNG of the active view (IModelView.SaveBMP
//                          + System.Drawing PNG re-encode)
//   POST /quit            — graceful: closes active doc, returns { ok }
//
// Thread safety:
//   HttpListener runs on its own thread. SW COM calls follow the same
//   pattern as the WebView2 bridge — direct call, with a static lock
//   serialising both input paths so they don't race. SW addin COM is
//   STA but most modify-side calls work fine off-thread; the lock just
//   stops the HTTP and WebView2 paths interleaving.

using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace AriaSW
{
    internal static class AriaHttpListener
    {
        private static HttpListener _listener;
        private static Thread _thread;
        private static volatile bool _running;
        private static int _opsDispatched;
        public  static readonly object DispatchLock = new object();

        // Default port; override via env ARIA_SW_PORT before SW starts.
        // (System.Environment, not SolidWorks.Interop.sldworks.Environment.)
        private static int Port =>
            int.TryParse(System.Environment.GetEnvironmentVariable("ARIA_SW_PORT"),
                          out var p) ? p : 7501;

        public static void Start()
        {
            if (_running) return;
            try
            {
                _listener = new HttpListener();
                // Bind both hostnames. Windows HTTP.sys filters by Host header
                // at the kernel layer BEFORE our handler runs, so a single
                // "localhost" prefix rejects 127.0.0.1 requests with HTTP 400
                // Bad Hostname. Adding both is required so any client (script,
                // browser, dashboard) reaches us regardless of how they spell
                // the loopback address.
                _listener.Prefixes.Add($"http://localhost:{Port}/");
                _listener.Prefixes.Add($"http://127.0.0.1:{Port}/");
                _listener.Start();
                _running = true;
                _thread = new Thread(AcceptLoop)
                {
                    IsBackground = true,
                    Name = "AriaSW-HttpListener",
                };
                _thread.Start();
                AriaSwAddin.FileLog($"HttpListener: listening on http://localhost:{Port}/ and http://127.0.0.1:{Port}/");
            }
            catch (Exception ex)
            {
                AriaSwAddin.FileLog($"HttpListener.Start failed: {ex.GetType().Name}: {ex.Message}");
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
                try { ctx = _listener.GetContext(); }
                catch { break; }
                ThreadPool.QueueUserWorkItem(_ => Handle(ctx));
            }
        }

        private static void Handle(HttpListenerContext ctx)
        {
            string path = ctx.Request.Url.AbsolutePath.TrimEnd('/').ToLowerInvariant();
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
                AriaSwAddin.FileLog($"HTTP {method} {path} body={(body.Length > 200 ? body.Substring(0, 200) + "..." : body)}");

                object result;
                lock (DispatchLock)
                {
                    result = Dispatch(method, path, body, ctx);
                }
                if (result == null) return;  // already wrote response (e.g. binary)
                ReplyJson(ctx, 200, result);
            }
            catch (Exception ex)
            {
                AriaSwAddin.FileLog($"HTTP error {method} {path}: {ex.GetType().Name}: {ex.Message}");
                ReplyJson(ctx, 500, new
                {
                    ok = false,
                    error = $"{ex.GetType().Name}: {ex.Message}",
                });
            }
        }

        private static object Dispatch(string method, string path, string body,
                                        HttpListenerContext ctx)
        {
            var addin = AriaSwAddin.Current;

            if (method == "GET" && path == "/status")
            {
                var sw = addin?.SwApp;
                var doc = sw?.IActiveDoc2 as IModelDoc2;
                return new
                {
                    ok = addin != null,
                    sw_connected = sw != null,
                    doc = doc?.GetTitle(),
                    has_active_doc = doc != null,
                    ops_dispatched = _opsDispatched,
                    recipe_count = RecipeDb.Count,
                    port = Port,
                };
            }

            if (method == "POST" && path == "/new_part")
            {
                var sw = addin?.SwApp ?? throw new InvalidOperationException(
                    "SolidWorks not connected");
                // NewDocument with empty template path uses the user default.
                string tmpl = sw.GetUserPreferenceStringValue(
                    (int)swUserPreferenceStringValue_e.swDefaultTemplatePart);
                IModelDoc2 doc = sw.NewDocument(tmpl, 0, 0.0, 0.0) as IModelDoc2;
                if (doc == null)
                    throw new InvalidOperationException("NewDocument returned null");
                AriaSwAddin.FileLog($"HTTP /new_part: opened '{doc.GetTitle()}'");
                return new { ok = true, title = doc.GetTitle() };
            }

            if (method == "POST" && path == "/op")
            {
                if (addin == null)
                    throw new InvalidOperationException("addin not loaded");
                var msg = JObject.Parse(string.IsNullOrEmpty(body) ? "{}" : body);
                string kind = msg["kind"]?.ToString()
                              ?? throw new ArgumentException("op requires 'kind'");
                JObject p = (msg["params"] as JObject) ?? new JObject();
                Dictionary<string, object> dict = ToDict(p);
                _opsDispatched++;
                var result = addin.ExecuteFeature(kind, dict);
                return new { ok = true, kind, result };
            }

            if (method == "POST" && path == "/save_step")
            {
                var sw = addin?.SwApp ?? throw new InvalidOperationException(
                    "SolidWorks not connected");
                var doc = sw.IActiveDoc2 as IModelDoc2
                          ?? throw new InvalidOperationException("no active doc");
                var msg = JObject.Parse(string.IsNullOrEmpty(body) ? "{}" : body);
                string outPath = msg["path"]?.ToString()
                                  ?? throw new ArgumentException("save_step requires 'path'");
                int err = 0;
                int warn = 0;
                bool ok = doc.Extension.SaveAs(outPath,
                    (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                    (int)swSaveAsOptions_e.swSaveAsOptions_Silent,
                    null, ref err, ref warn);
                AriaSwAddin.FileLog($"HTTP /save_step '{outPath}' ok={ok} err={err} warn={warn}");
                return new { ok, err, warn, path = outPath };
            }

            if (method == "GET" && path == "/screenshot")
            {
                var sw = addin?.SwApp ?? throw new InvalidOperationException(
                    "SolidWorks not connected");
                var doc = sw.IActiveDoc2 as IModelDoc2;
                if (doc == null)
                    throw new InvalidOperationException("no active doc");
                // Force isometric view so the screenshot is meaningful even
                // when the user hasn't manually rotated the camera.
                doc.ShowNamedView2("*Isometric", -1);
                doc.ViewZoomtofit2();
                string bmpPath = Path.Combine(Path.GetTempPath(),
                    $"aria_sw_screen_{Guid.NewGuid():N}.bmp");
                bool savedView = doc.SaveBMP(bmpPath, 1024, 768);
                AriaSwAddin.FileLog($"HTTP /screenshot: SaveBMP={savedView} -> {bmpPath}");
                if (!savedView || !File.Exists(bmpPath))
                    throw new InvalidOperationException("SaveBMP failed");
                // Re-encode BMP -> PNG for smaller wire bytes.
                byte[] pngBytes;
                using (var bmp = (Bitmap)Image.FromFile(bmpPath))
                using (var ms  = new MemoryStream())
                {
                    bmp.Save(ms, System.Drawing.Imaging.ImageFormat.Png);
                    pngBytes = ms.ToArray();
                }
                try { File.Delete(bmpPath); } catch { }
                ctx.Response.StatusCode = 200;
                ctx.Response.ContentType = "image/png";
                ctx.Response.ContentLength64 = pngBytes.Length;
                ctx.Response.OutputStream.Write(pngBytes, 0, pngBytes.Length);
                ctx.Response.OutputStream.Close();
                return null;  // signals "we wrote our own response"
            }

            if (method == "POST" && path == "/quit")
            {
                var sw = addin?.SwApp;
                var doc = sw?.IActiveDoc2 as IModelDoc2;
                if (doc != null)
                {
                    string title = doc.GetTitle();
                    sw.CloseDoc(title);
                    AriaSwAddin.FileLog($"HTTP /quit: closed '{title}'");
                    return new { ok = true, closed = title };
                }
                return new { ok = true, closed = (string)null };
            }

            ctx.Response.StatusCode = 404;
            return new { ok = false, error = $"unknown route {method} {path}" };
        }

        private static Dictionary<string, object> ToDict(JObject o)
        {
            var d = new Dictionary<string, object>();
            foreach (var prop in o.Properties())
            {
                if (prop.Value is JObject sub) d[prop.Name] = ToDict(sub);
                else if (prop.Value is JArray arr) d[prop.Name] = arr;
                else d[prop.Name] = ((JValue)prop.Value).Value;
            }
            return d;
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
