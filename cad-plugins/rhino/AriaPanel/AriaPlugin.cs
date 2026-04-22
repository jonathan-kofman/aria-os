// AriaPlugin.cs -- RhinoCommon PlugIn entry point for ARIA-OS panel.
//
// Rhino calls OnLoad() at startup. We register the dockable panel class and
// add a toolbar button command. The panel itself (AriaPanelHost) hosts a
// WebView2 control that loads the ARIA React frontend.

using Rhino;
using Rhino.PlugIns;
using Rhino.UI;
using System;

namespace AriaPanel
{
    public class AriaPlugin : PlugIn
    {
        // -----------------------------------------------------------------
        // Singleton (Rhino supplies the instance via reflection)
        // -----------------------------------------------------------------

        public static AriaPlugin? Instance { get; private set; }

        public AriaPlugin()
        {
            Instance = this;
        }

        // -----------------------------------------------------------------
        // PlugIn metadata — set via AssemblyInfo attributes instead of
        // overriding properties, since RhinoCommon's PlugIn.Name / Id
        // aren't virtual (they read from assembly metadata).
        // -----------------------------------------------------------------

        // -----------------------------------------------------------------
        // Lifecycle
        // -----------------------------------------------------------------

        protected override LoadReturnCode OnLoad(ref string errorMessage)
        {
            try
            {
                // Register the dockable panel so Rhino knows how to show/hide it.
                Panels.RegisterPanel(this, typeof(AriaPanelHost), "ARIA Generate",
                    System.Drawing.SystemIcons.Application);

                // Wire up toolbar command handlers.
                AriaCommands.Register();

                return LoadReturnCode.Success;
            }
            catch (Exception ex)
            {
                errorMessage = $"ARIA plugin failed to load: {ex.Message}";
                return LoadReturnCode.ErrorNoDialog;
            }
        }
    }
}
