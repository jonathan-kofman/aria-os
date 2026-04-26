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
                // SW-native leverage
                "toolboxHardware" => OpToolboxHardware(p),
                "weldmentProfile" => OpWeldmentProfile(p),
                "dimXpertAuto"    => OpDimXpertAuto(p),
                "exportEdrawings" => OpExportEdrawings(p),
                _ => throw new ArgumentException($"Unknown kind: {kind}"),
            };
        }

        // Op stubs — to be implemented when a SW install is available.
        private object OpBeginPlan()             { _registry.Clear(); return new { ok = true }; }
        private object OpNewSketch(Dictionary<string, object> p)       => new { ok = false, todo = "SW sketch" };
        private object OpSketchCircle(Dictionary<string, object> p)    => new { ok = false, todo = "SW circle" };
        private object OpSketchRect(Dictionary<string, object> p)      => new { ok = false, todo = "SW rect" };
        private object OpExtrude(Dictionary<string, object> p)         => new { ok = false, todo = "SW extrude" };
        private object OpCircularPattern(Dictionary<string, object> p) => new { ok = false, todo = "SW circ pattern" };
        private object OpFillet(Dictionary<string, object> p)          => new { ok = false, todo = "SW fillet" };
        private object OpAddParameter(Dictionary<string, object> p)    => new { ok = false, todo = "SW param via Equation Manager" };

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
