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

        // -----------------------------------------------------------------
        // Construction
        // -----------------------------------------------------------------

        public AriaPanelHost()
        {
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
                await WebView!.EnsureCoreWebView2Async();

                var core = WebView.CoreWebView2;

                // Disable context menu to keep the panel clean.
                core.Settings.AreDefaultContextMenusEnabled = false;
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
}
