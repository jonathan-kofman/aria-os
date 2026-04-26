// AriaPanel.cs — WinForms UserControl hosting a WebView2 that loads the
// ARIA React frontend, sized for a SolidWorks Task Pane.
//
// Mirrors cad-plugins/rhino/AriaPanel/AriaPanel.cs but uses the WinForms
// flavour of WebView2 (Microsoft.Web.WebView2.WinForms) because SolidWorks
// Task Panes accept a COM-visible UserControl, and the WPF interop layer
// inside an unmanaged Task Pane host is finicky. WinForms with a plain
// UserControl is the path of least resistance.
//
// JS side (frontend/src/aria/bridge.js) detects host as "solidworks"
// when window.ARIA_HOST_HINT === "solidworks". The detection then
// routes through the same _rhinoCall path (chrome.webview.postMessage /
// PostWebMessageAsJson reply) that Rhino uses, so no JS-side changes
// are needed beyond what already exists.
//
// URL override: set ARIA_PANEL_URL env var (useful for production).

using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace AriaSW
{
    /// <summary>
    /// WinForms UserControl that SolidWorks hosts inside a Task Pane.
    /// Must be COM-visible so swApp.CreateTaskpaneView3 + AddControl can
    /// instantiate it via its ProgID.
    /// </summary>
    [ComVisible(true)]
    [ClassInterface(ClassInterfaceType.AutoDual)]
    [Guid("608d290d-545d-48cd-98cb-302d33275e0b")]
    [ProgId("AriaSW.AriaPanelHost")]
    public class AriaPanelHost : UserControl
    {
        // -----------------------------------------------------------------
        // Config
        // -----------------------------------------------------------------

        private static readonly string _url =
            Environment.GetEnvironmentVariable("ARIA_PANEL_URL")
            ?? "http://localhost:3000/?host=solidworks";

        // -----------------------------------------------------------------
        // Internal state
        // -----------------------------------------------------------------

        internal WebView2 WebView { get; private set; }
        private AriaBridge _bridge;
        private bool _webViewReady;

        // Most-recently-constructed instance — used by AriaReload to find
        // the live WebView2 without going through SW's Task Pane registry.
        internal static AriaPanelHost Current { get; private set; }

        // -----------------------------------------------------------------
        // Construction
        // -----------------------------------------------------------------

        public AriaPanelHost()
        {
            Current = this;
            AriaSwAddin.FileLog("AriaPanelHost ctor enter");
            try
            {
                InitView();
                AriaSwAddin.FileLog("AriaPanelHost ctor InitView OK");
            }
            catch (Exception ex)
            {
                AriaSwAddin.FileLog(
                    $"AriaPanelHost ctor failed: {ex.GetType().Name}: {ex.Message}");
                // Replace failed WebView2 host with a plain label so the
                // Task Pane still appears (otherwise SW gets an empty
                // control and the Task Pane silently never opens).
                Controls.Clear();
                Controls.Add(new Label
                {
                    Dock = DockStyle.Fill,
                    Text = $"ARIA panel failed to initialize:\n{ex.Message}\n\n"
                           + $"Check %LOCALAPPDATA%\\AriaSW\\addin.log",
                    TextAlign = System.Drawing.ContentAlignment.MiddleCenter,
                });
            }
        }

        private Button _loadButton;
        private Label  _statusLabel;

        private void InitView()
        {
            // DEFERRED INIT: creating + initializing WebView2 during SW
            // bootstrap kills the process natively (no .NET exception,
            // no WER event). We instead show a placeholder with a button;
            // WebView2 is only constructed when the user clicks Load,
            // long after SW has finished starting up.
            AriaSwAddin.FileLog("InitView: deferred mode — placeholder + Load button");

            var layout = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 1,
                RowCount = 3,
                BackColor = System.Drawing.Color.FromArgb(0x1A, 0x1D, 0x24),
            };
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 50));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 50));

            _statusLabel = new Label
            {
                Text = "ARIA panel\n\nClick Load to start the WebView2 panel.",
                TextAlign = System.Drawing.ContentAlignment.BottomCenter,
                Dock = DockStyle.Fill,
                ForeColor = System.Drawing.Color.FromArgb(0xE0, 0xE2, 0xE6),
                Font = new System.Drawing.Font("Segoe UI", 10F),
                AutoSize = false,
            };
            _loadButton = new Button
            {
                Text = "Load ARIA panel",
                AutoSize = true,
                Anchor = AnchorStyles.None,
                BackColor = System.Drawing.Color.FromArgb(0x5D, 0x8C, 0xDC),
                ForeColor = System.Drawing.Color.White,
                FlatStyle = FlatStyle.Flat,
                Padding = new Padding(12, 6, 12, 6),
            };
            _loadButton.FlatAppearance.BorderSize = 0;
            _loadButton.Click += OnLoadClicked;

            layout.Controls.Add(_statusLabel, 0, 0);
            var buttonHolder = new Panel { Dock = DockStyle.Fill, Height = 40 };
            buttonHolder.Controls.Add(_loadButton);
            _loadButton.Location = new System.Drawing.Point(
                (buttonHolder.Width - _loadButton.PreferredSize.Width) / 2, 4);
            layout.Controls.Add(buttonHolder, 0, 1);
            layout.Controls.Add(new Label { Dock = DockStyle.Fill }, 0, 2);

            Controls.Add(layout);
        }

        private void OnLoadClicked(object sender, EventArgs e)
        {
            AriaSwAddin.FileLog("Load button clicked");
            _loadButton.Enabled = false;
            _statusLabel.Text = "Initializing WebView2...";
            try
            {
                Controls.Clear();
                WebView = new WebView2 { Dock = DockStyle.Fill };
                Controls.Add(WebView);
                _ = InitWebViewAsync();
            }
            catch (Exception ex)
            {
                AriaSwAddin.FileLog(
                    $"Load failed: {ex.GetType().Name}: {ex.Message}");
                Controls.Clear();
                Controls.Add(new Label
                {
                    Dock = DockStyle.Fill,
                    Text = $"WebView2 failed to load:\n{ex.Message}",
                    TextAlign = System.Drawing.ContentAlignment.MiddleCenter,
                });
            }
        }

        private async System.Threading.Tasks.Task InitWebViewAsync()
        {
            AriaSwAddin.FileLog("InitWebViewAsync start");
            try
            {
                // WebView2's default user-data folder lives next to the
                // host executable. For SolidWorks that's in Program Files
                // which is read-only without admin → E_ACCESSDENIED.
                // Point it at a writable per-user dir instead.
                var userDataFolder = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "AriaSW", "WebView2");
                Directory.CreateDirectory(userDataFolder);

                var env = await CoreWebView2Environment.CreateAsync(
                    browserExecutableFolder: null,
                    userDataFolder: userDataFolder,
                    options: null);
                await WebView.EnsureCoreWebView2Async(env);

                var core = WebView.CoreWebView2;

#if DEBUG
                core.Settings.AreDefaultContextMenusEnabled = true;
                core.Settings.AreDevToolsEnabled = true;
#else
                core.Settings.AreDefaultContextMenusEnabled = false;
                core.Settings.AreDevToolsEnabled = false;
#endif
                core.Settings.IsStatusBarEnabled = false;

                // Inject ARIA_HOST_HINT so bridge.js detectHost() returns
                // "solidworks" (without this it would return "rhino" since
                // both expose chrome.webview).
                await core.AddScriptToExecuteOnDocumentCreatedAsync(
                    "window.ARIA_HOST_HINT = 'solidworks';");

                // Wire incoming messages → bridge dispatch.
                _bridge = new AriaBridge(this);
                core.WebMessageReceived += _bridge.OnWebMessageReceived;

                // Hook every event we can so the log captures whatever
                // happens up to the moment SW dies.
                core.ProcessFailed += (s, ev) =>
                    AriaSwAddin.FileLog(
                        $"WV2 ProcessFailed: kind={ev.ProcessFailedKind} reason={ev.Reason}");
                core.NavigationStarting += (s, ev) =>
                    AriaSwAddin.FileLog($"WV2 NavigationStarting: {ev.Uri}");
                core.NavigationCompleted += (s, ev) =>
                    AriaSwAddin.FileLog(
                        $"WV2 NavigationCompleted: success={ev.IsSuccess} status={ev.WebErrorStatus}");
                core.DOMContentLoaded += (s, ev) =>
                    AriaSwAddin.FileLog($"WV2 DOMContentLoaded id={ev.NavigationId}");

                // Probe the URL host:port first. If the dev server isn't
                // up, navigating triggers WebView2's connection-refused
                // error page, which rendered inside a SolidWorks Task
                // Pane has crashed SW natively (no .NET exception, no
                // WER report) every time. Load a static data: URI as
                // the placeholder until the server is reachable.
                string targetUrl = _url;
                if (!IsHostReachable(_url))
                {
                    AriaSwAddin.FileLog($"dev server unreachable; loading placeholder ({_url})");
                    targetUrl = WaitingDataUri(_url);
                }
                else
                {
                    AriaSwAddin.FileLog($"dev server reachable; navigating to {_url}");
                }
                core.Navigate(targetUrl);
                _webViewReady = true;
                AriaSwAddin.FileLog("WebView2 Navigate() called");
            }
            catch (Exception ex)
            {
                AriaSwAddin.FileLog(
                    $"WebView2 init failed: {ex.GetType().Name}: {ex.Message}");
                AriaSwAddin.FileLog($"  stack: {ex.StackTrace}");
            }
        }

        // -----------------------------------------------------------------
        // Reply helper — called by AriaBridge to send data back to JS.
        // Must be called on the UI thread; we marshal via Invoke.
        // -----------------------------------------------------------------

        private static bool IsHostReachable(string url)
        {
            try
            {
                var uri = new Uri(url);
                int port = uri.Port > 0 ? uri.Port : (uri.Scheme == "https" ? 443 : 80);
                using (var sock = new TcpClient())
                {
                    var connect = sock.BeginConnect(uri.Host, port, null, null);
                    bool ok = connect.AsyncWaitHandle.WaitOne(TimeSpan.FromSeconds(1));
                    if (!ok) return false;
                    sock.EndConnect(connect);
                    return sock.Connected;
                }
            }
            catch { return false; }
        }

        private static string WaitingDataUri(string url)
        {
            // HTML-escape the URL so it can't break out of the attribute.
            string safe = (url ?? "")
                .Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;");
            // JS-string-escape for the embedded script literal.
            string jsTarget = (url ?? "").Replace("\\", "\\\\").Replace("'", "\\'");
            string html =
                "<!doctype html><html><head><meta charset='utf-8'>" +
                "<style>" +
                "body{margin:0;font:14px system-ui,Segoe UI,Arial;" +
                "background:#1a1d24;color:#e0e2e6;" +
                "display:flex;align-items:center;justify-content:center;" +
                "height:100vh;text-align:center}" +
                ".box{max-width:380px;padding:24px}" +
                ".title{font-size:18px;font-weight:600;color:#5d8cdc;margin-bottom:12px}" +
                ".hint{opacity:.7;margin-top:16px;font-size:12px;line-height:1.5}" +
                "#status{margin-top:8px;font-size:12px;opacity:.6}" +
                "code{background:#2a2e38;padding:2px 6px;border-radius:3px}" +
                "</style></head><body><div class='box'>" +
                "<div class='title'>ARIA panel</div>" +
                "<div>Waiting for the React dev server.</div>" +
                "<div class='hint'>" +
                "Start it with <code>npm run dev</code> in the " +
                "<code>frontend/</code> directory.<br>" +
                "Target: " + safe + "</div>" +
                "<div id='status'>polling...</div>" +
                "</div>" +
                "<script>" +
                "const target='" + jsTarget + "';" +
                "const status=document.getElementById('status');" +
                "let tries=0;" +
                "async function check(){" +
                "  tries++;" +
                "  status.textContent='polling... (try '+tries+')';" +
                "  try{" +
                "    await fetch(target,{method:'GET',mode:'no-cors',cache:'no-store'});" +
                // no-cors fetch resolves opaque on success and throws on connection-
                // refused, so reaching here means the server is up.
                "    status.textContent='dev server up — loading...';" +
                "    location.replace(target);" +
                "  }catch(e){" +
                "    setTimeout(check,3000);" +
                "  }" +
                "}" +
                "setTimeout(check,500);" +
                "</script>" +
                "</body></html>";
            return "data:text/html;charset=utf-8," + Uri.EscapeDataString(html);
        }

        internal void PostReply(string json)
        {
            if (!_webViewReady) return;
            // CoreWebView2 throws "can only be accessed from the UI thread"
            // even on null-check. Marshal to UI thread BEFORE touching it.
            if (InvokeRequired)
            {
                try { BeginInvoke((Action)(() => PostReply(json))); }
                catch (Exception ex)
                {
                    AriaSwAddin.FileLog(
                        $"PostReply BeginInvoke failed: {ex.GetType().Name}: {ex.Message}");
                }
                return;
            }
            try
            {
                var core = WebView?.CoreWebView2;
                if (core == null)
                {
                    AriaSwAddin.FileLog("PostReply DROPPED — CoreWebView2 null on UI thread");
                    return;
                }
                core.PostWebMessageAsJson(json);
            }
            catch (Exception ex)
            {
                AriaSwAddin.FileLog($"PostReply failed: {ex.GetType().Name}: {ex.Message}");
            }
        }

        /// <summary>
        /// Reload the WebView2 content (re-fetches the dev-server URL).
        /// Useful when iterating on bridge.js or React without rebuilding
        /// the SW add-in.
        /// </summary>
        internal void ReloadWebView()
        {
            if (!_webViewReady) return;
            if (InvokeRequired)
            {
                try { BeginInvoke((Action)ReloadWebView); } catch { }
                return;
            }
            try { WebView?.CoreWebView2?.Reload(); }
            catch (Exception ex) { AriaSwAddin.Log($"Reload failed: {ex.Message}"); }
        }
    }
}
