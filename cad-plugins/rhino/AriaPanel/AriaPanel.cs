// AriaPanel.cs -- WPF UserControl hosting a WebView2 that loads the ARIA
// React frontend.  Registered as a Rhino dockable panel.
//
// The WebView2 control uses window.chrome.webview.postMessage() which
// bridge.js detects as host == "rhino". Replies come back via
// CoreWebView2.PostWebMessageAsJson() which the JS receives as a
// "message" event on the window.
//
// URL override: set ARIA_PANEL_URL env var (useful for production).

using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using Rhino;
using Rhino.UI;
using System;
using System.Windows;
using System.Windows.Controls;

namespace AriaPanel
{
    /// <summary>
    /// WPF UserControl that Rhino docks as a panel.
    /// </summary>
    [System.Runtime.InteropServices.Guid("609b1ead-2a09-4136-9754-766e6f993fa3")]
    public class AriaPanelHost : UserControl
    {
        // -----------------------------------------------------------------
        // Config
        // -----------------------------------------------------------------

        private static readonly string _url =
            Environment.GetEnvironmentVariable("ARIA_PANEL_URL")
            ?? "http://localhost:5173/?host=rhino";

        // -----------------------------------------------------------------
        // Internal state
        // -----------------------------------------------------------------

        internal WebView2? WebView { get; private set; }
        private AriaBridge? _bridge;
        private bool _webViewReady;

        // Most-recently-constructed panel instance — used by AriaReload
        // command to find the live WebView2 without going through Rhino's
        // panel registry (which requires a doc-serial parameter).
        internal static AriaPanelHost? Current { get; private set; }

        // -----------------------------------------------------------------
        // Construction
        // -----------------------------------------------------------------

        public AriaPanelHost()
        {
            Current = this;
            InitView();
        }

        private void InitView()
        {
            var grid = new Grid();

            WebView = new WebView2
            {
                HorizontalAlignment = HorizontalAlignment.Stretch,
                VerticalAlignment = VerticalAlignment.Stretch,
            };

            grid.Children.Add(WebView);
            Content = grid;

            // Initialize WebView2 asynchronously.
            _ = InitWebViewAsync();
        }

        private async System.Threading.Tasks.Task InitWebViewAsync()
        {
            try
            {
                // WebView2's default user-data folder lives next to the host
                // executable. For Rhino that's C:\Program Files\Rhino 8\...
                // which is read-only without admin → E_ACCESSDENIED. Point
                // it at a writable per-user dir instead.
                var userDataFolder = System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "AriaPanel", "WebView2");
                System.IO.Directory.CreateDirectory(userDataFolder);
                var env = await CoreWebView2Environment.CreateAsync(
                    browserExecutableFolder: null,
                    userDataFolder: userDataFolder,
                    options: null);
                await WebView!.EnsureCoreWebView2Async(env);

                var core = WebView.CoreWebView2;

                // Disable context menu in Release; keep it on for Debug
                // so devs get right-click → Reload / Inspect element.
#if DEBUG
                core.Settings.AreDefaultContextMenusEnabled = true;
                core.Settings.AreDevToolsEnabled = true;
#else
                core.Settings.AreDefaultContextMenusEnabled = false;
                core.Settings.AreDevToolsEnabled = false;
#endif
                core.Settings.IsStatusBarEnabled = false;

                // Inject ARIA_HOST_HINT so bridge.js confirms "rhino".
                await core.AddScriptToExecuteOnDocumentCreatedAsync(
                    "window.ARIA_HOST_HINT = 'rhino';");

                // Wire incoming messages → bridge dispatch.
                _bridge = new AriaBridge(this);
                core.WebMessageReceived += _bridge.OnWebMessageReceived;

                // Navigate to the panel URL.
                core.Navigate(_url);
                _webViewReady = true;
            }
            catch (Exception ex)
            {
                RhinoApp.WriteLine($"[ARIA] WebView2 init failed: {ex.Message}");
            }
        }

        // -----------------------------------------------------------------
        // Reply helper — called by AriaBridge to send data back to JS.
        // -----------------------------------------------------------------

        /// <summary>
        /// Post a JSON message back to the WebView2 content.
        /// Must be called on the UI thread or via dispatcher.
        /// </summary>
        /// <summary>
        /// Reload the WebView2 content (re-fetches the dev-server URL).
        /// Called by AriaReload command — useful when bridge.js or React
        /// changed but the C# plug-in didn't, so a full Rhino restart is
        /// overkill.
        /// </summary>
        internal void ReloadWebView()
        {
            if (!_webViewReady || WebView?.CoreWebView2 == null) return;
            try
            {
                Dispatcher.Invoke(() => WebView.CoreWebView2.Reload());
            }
            catch (Exception ex)
            {
                RhinoApp.WriteLine($"[ARIA] Reload failed: {ex.Message}");
            }
        }

        internal void PostReply(string json)
        {
            if (!_webViewReady || WebView?.CoreWebView2 == null) return;
            try
            {
                Dispatcher.Invoke(() =>
                {
                    WebView.CoreWebView2.PostWebMessageAsJson(json);
                });
            }
            catch (Exception ex)
            {
                RhinoApp.WriteLine($"[ARIA] PostReply failed: {ex.Message}");
            }
        }
    }

    // -----------------------------------------------------------------
    // Toolbar command — "AriaGenerate" opens / focuses the panel.
    // -----------------------------------------------------------------

    public class AriaCommands
    {
        public static void Register()
        {
            // Nothing to do here in Rhino 8 — the command is defined by
            // AriaGenerateCommand below. Rhino discovers it via the PlugIn.
        }
    }

    [System.Runtime.InteropServices.Guid("9b7a8636-e1fb-4a09-9cde-d8de5533ab44")]
    public class AriaGenerateCommand : Rhino.Commands.Command
    {
        public override string EnglishName => "AriaGenerate";

        protected override Rhino.Commands.Result RunCommand(
            RhinoDoc doc, Rhino.Commands.RunMode mode)
        {
            Panels.OpenPanel(typeof(AriaPanelHost).GUID);
            return Rhino.Commands.Result.Success;
        }
    }

    // -----------------------------------------------------------------
    // AriaReload — reloads the WebView2 content without rebuilding the
    // .rhp. Useful when iterating on the React frontend or bridge.js
    // and the C# plug-in itself didn't change.
    // -----------------------------------------------------------------

    [System.Runtime.InteropServices.Guid("4e8c1a52-9b3d-4f7e-a6c2-8d1e5b0f3a91")]
    public class AriaReloadCommand : Rhino.Commands.Command
    {
        public override string EnglishName => "AriaReload";

        protected override Rhino.Commands.Result RunCommand(
            RhinoDoc doc, Rhino.Commands.RunMode mode)
        {
            var host = AriaPanelHost.Current;
            if (host == null)
            {
                RhinoApp.WriteLine(
                    "[ARIA] No panel open — run AriaGenerate first.");
                return Rhino.Commands.Result.Failure;
            }
            host.ReloadWebView();
            RhinoApp.WriteLine("[ARIA] Panel reloaded.");
            return Rhino.Commands.Result.Success;
        }
    }
}
