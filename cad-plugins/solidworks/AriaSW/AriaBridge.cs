// AriaBridge.cs — Handles bridge actions from the React panel.
//
// Mirrors cad-plugins/rhino/AriaPanel/AriaBridge.cs. The JS side
// (frontend/src/aria/bridge.js) treats SolidWorks identically to Rhino:
// posts JSON via window.chrome.webview.postMessage with { action, _id, ... }
// and awaits a reply via PostWebMessageAsJson with { _id, result }
// or { _id, error }.
//
// IMPLEMENTED (real SW API):
//   getCurrentDocument — IModelDoc2 name, units
//   getSelection       — SelectionMgr.GetSelectedObject6
//   insertGeometry     — download STEP/STL → swApp.OpenDoc6
//   showNotification   — swApp.SendMsgToUser2 + status bar
//   executeFeature     — delegates to AriaSwAddin.ExecuteFeature (ops live there)
//
// STUBBED (returns {error:"not implemented"}):
//   updateParameter, getFeatureTree, exportCurrent, openFile

using Microsoft.Web.WebView2.Core;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Threading.Tasks;

namespace AriaSW
{
    internal class AriaBridge
    {
        private readonly AriaPanelHost _panel;
        private static readonly HttpClient _http = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(60),
        };

        // Op chain: each incoming message appends a continuation to the tail.
        // Without this, per-message Task.Run dispatches concurrently and ops
        // arrive at SW in non-deterministic order — sketchCircle can run
        // before its newSketch, extrude can run before its sketchCircle, etc.
        // The chain preserves arrival order while still keeping work off the
        // WebView2 UI thread.
        private static Task _opChain = Task.CompletedTask;
        private static readonly object _chainLock = new object();

        public AriaBridge(AriaPanelHost panel)
        {
            _panel = panel;
        }

        // -----------------------------------------------------------------
        // Entry point — WebView2 fires this on every postMessage() call.
        // Runs on the WebView2 thread; SW API calls below must marshal
        // to the SW main thread when needed (most read-only API works
        // off-thread).
        // -----------------------------------------------------------------

        public void OnWebMessageReceived(object sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            string raw;
            try { raw = e.TryGetWebMessageAsString() ?? e.WebMessageAsJson; }
            catch { raw = e.WebMessageAsJson; }
            AriaSwAddin.FileLog($"WV2 msg in: {(raw?.Length > 200 ? raw.Substring(0, 200) + "..." : raw)}");

            // Append to the global op chain so messages process in arrival
            // order. Unwrap flattens Task<Task> returned by the async lambda.
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
                AriaSwAddin.FileLog($"  dispatch: action={action} id={id}");

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
                        string notifMsg = msg["msg"]?.ToString() ?? "";
                        ShowNotification(notifMsg);
                        Reply(id, new { ok = true });
                        break;

                    case "openFile":
                        ReplyError(id, "not implemented");
                        break;

                    case "executeFeature":
                        string kind = msg["kind"]?.ToString() ?? "";
                        JObject fparams = msg["params"] as JObject ?? new JObject();
                        // Delegate to addin's existing dispatcher so op
                        // implementations live in one place.
                        var dict = ToDict(fparams);
                        var result = AriaSwAddin.Current?.ExecuteFeature(kind, dict)
                                     ?? new { ok = false, error = "addin not loaded" };
                        Reply(id, result);
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
        // getCurrentDocument
        // -----------------------------------------------------------------

        private static object GetCurrentDocument()
        {
            var sw = AriaSwAddin.Current?.SwApp
                ?? throw new InvalidOperationException("SolidWorks not connected");
            var model = sw.IActiveDoc2 as IModelDoc2;
            if (model == null)
                throw new InvalidOperationException("No active SolidWorks document");

            // ModelDoc2.GetUnits returns [linearUnit, angularUnit, ...].
            // For our purposes we map the linear unit to a short string.
            string units = "mm";
            try
            {
                int linearUnit = model.LengthUnit;
                units = linearUnit switch
                {
                    (int)swLengthUnit_e.swMM        => "mm",
                    (int)swLengthUnit_e.swCM        => "cm",
                    (int)swLengthUnit_e.swMETER     => "m",
                    (int)swLengthUnit_e.swINCHES    => "in",
                    (int)swLengthUnit_e.swFEET      => "ft",
                    (int)swLengthUnit_e.swFEETINCHES=> "ft",
                    _ => "mm",
                };
            }
            catch { /* fall back to mm */ }

            string typeStr = model.GetType() switch
            {
                (int)swDocumentTypes_e.swDocPART     => "Part",
                (int)swDocumentTypes_e.swDocASSEMBLY => "Assembly",
                (int)swDocumentTypes_e.swDocDRAWING  => "Drawing",
                _ => "Unknown",
            };

            return new
            {
                name = Path.GetFileNameWithoutExtension(model.GetTitle()) ?? "(untitled)",
                id   = model.GetPathName() ?? "",
                units,
                type = typeStr,
            };
        }

        // -----------------------------------------------------------------
        // getSelection
        // -----------------------------------------------------------------

        private static object GetSelection()
        {
            var sw = AriaSwAddin.Current?.SwApp
                ?? throw new InvalidOperationException("SolidWorks not connected");
            var model = sw.IActiveDoc2 as IModelDoc2;
            if (model == null)
                throw new InvalidOperationException("No active SolidWorks document");
            var selMgr = model.SelectionManager as ISelectionMgr;
            if (selMgr == null) return new List<object>();

            int count = selMgr.GetSelectedObjectCount2(-1);
            var results = new List<object>(count);
            for (int i = 1; i <= count; i++)
            {
                var obj = selMgr.GetSelectedObject6(i, -1);
                int sel_type = selMgr.GetSelectedObjectType3(i, -1);
                string typeName = ((swSelectType_e)sel_type).ToString();
                results.Add(new
                {
                    id = i.ToString(),
                    type = typeName,
                    metadata = new
                    {
                        mark = selMgr.GetSelectedObjectMark(i),
                    },
                });
            }
            return results;
        }

        // -----------------------------------------------------------------
        // insertGeometry — download STEP/STL → OpenDoc6
        // -----------------------------------------------------------------

        private async Task<object> InsertGeometryAsync(string url)
        {
            if (string.IsNullOrWhiteSpace(url))
                throw new ArgumentException("insertGeometry: url is required");
            var sw = AriaSwAddin.Current?.SwApp
                ?? throw new InvalidOperationException("SolidWorks not connected");

            // Determine format from URL (strip query string first).
            string pathPart = url.Split('?')[0];
            string ext = Path.GetExtension(pathPart).ToLowerInvariant();
            if (string.IsNullOrEmpty(ext)) ext = ".step";

            // Download to temp file.
            string tmpPath = Path.Combine(Path.GetTempPath(),
                $"aria_import_{Guid.NewGuid():N}{ext}");
            using (var resp = await _http.GetAsync(url))
            {
                resp.EnsureSuccessStatusCode();
                using (var fs = File.Create(tmpPath))
                    await resp.Content.CopyToAsync(fs);
            }

            // OpenDoc6 with the right type flag. SW imports STEP/IGES as
            // Part documents by default.
            int docType = ext switch
            {
                ".step" or ".stp" or ".iges" or ".igs" => (int)swDocumentTypes_e.swDocPART,
                ".sldprt" => (int)swDocumentTypes_e.swDocPART,
                ".sldasm" => (int)swDocumentTypes_e.swDocASSEMBLY,
                ".slddrw" => (int)swDocumentTypes_e.swDocDRAWING,
                ".stl"    => (int)swDocumentTypes_e.swDocPART,
                _ => (int)swDocumentTypes_e.swDocPART,
            };

            int errors = 0, warnings = 0;
            object opened = sw.OpenDoc6(tmpPath, docType,
                (int)swOpenDocOptions_e.swOpenDocOptions_Silent,
                "", ref errors, ref warnings);

            return new
            {
                inserted = opened != null,
                format = ext.TrimStart('.'),
                path = tmpPath,
                errors,
                warnings,
            };
        }

        // -----------------------------------------------------------------
        // showNotification — status bar + (optional) message box
        // -----------------------------------------------------------------

        private static void ShowNotification(string msg)
        {
            var sw = AriaSwAddin.Current?.SwApp;
            if (sw == null) return;
            try
            {
                // Status-bar style: cheap, non-modal. Avoid SendMsgToUser2
                // (modal popup) on every notification — it's spammy.
                var frame = sw.Frame() as IFrame;
                frame?.SetStatusBarText(msg);
            }
            catch { /* best-effort */ }
            AriaSwAddin.Log(msg);
        }

        // -----------------------------------------------------------------
        // Helpers
        // -----------------------------------------------------------------

        private static Dictionary<string, object> ToDict(JObject obj)
        {
            var d = new Dictionary<string, object>();
            foreach (var prop in obj.Properties())
                d[prop.Name] = prop.Value.ToObject<object>();
            return d;
        }

        private void Reply(string id, object result)
        {
            var json = JsonConvert.SerializeObject(new { _id = id, result });
            AriaSwAddin.FileLog($"  reply id={id}: {(json.Length > 160 ? json.Substring(0, 160) + "..." : json)}");
            _panel.PostReply(json);
        }

        private void ReplyError(string id, string error)
        {
            var json = JsonConvert.SerializeObject(new { _id = id, error });
            AriaSwAddin.FileLog($"  reply-err id={id}: {error}");
            _panel.PostReply(json);
        }
    }
}
