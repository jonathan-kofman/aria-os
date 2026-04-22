// AriaSwAddin.cs — SolidWorks add-in skeleton for ARIA.
//
// Mirrors the executeFeature contract used by Fusion / Rhino / Onshape.
// Unlike Fusion (in-process WebView2) or Rhino (in-process WebView2
// via dockable panel), SolidWorks integrations typically route commands
// over a local HTTP loopback because SW's PMPageHost is cumbersome
// for rich UI. We host a tiny HTTP server that receives JSON ops and
// dispatches them against the ISldWorks API.
//
// STATUS: scaffold only — not yet built or tested. The handler names
// match the other CAD backends exactly so the ARIA backend can stream
// the same op plans to any of them.

using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidWorks.Interop.swpublished;

namespace AriaSW
{
    [Guid("A71A0000-0000-0000-0000-00000000ARIA")]
    [ComVisible(true)]
    [SwAddin(Description = "ARIA: AI CAD pipeline",
             Title = "ARIA",
             LoadAtStartup = true)]
    public class AriaSwAddin : ISwAddin
    {
        private ISldWorks _sw;
        private int _cookie;

        // --- Shared op dispatcher ---------------------------------------
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

        // Op stubs — to be implemented when a SW install is available
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

        // --- ISwAddin ---------------------------------------------------
        public bool ConnectToSW(object ThisSW, int Cookie)
        {
            _sw = (ISldWorks)ThisSW;
            _cookie = Cookie;
            _sw.SetAddinCallbackInfo(0, this, Cookie);
            // TODO: start local HTTP listener on 127.0.0.1:17701 for bridge ops
            return true;
        }

        public bool DisconnectFromSW()
        {
            // TODO: stop local HTTP listener
            return true;
        }
    }
}
