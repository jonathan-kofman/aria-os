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

                // Auto-learning recipe cache for native RhinoCommon ops.
                RecipeDb.Init();

                // Headless HTTP entry on localhost:7502 — orchestrator
                // can drive Rhino via curl without the panel being open.
                AriaHttpListener.Start();

                return LoadReturnCode.Success;
            }
            catch (Exception ex)
            {
                // Surface the FULL exception (incl. inner type/stack) via
                // errorMessage so Rhino's plug-in load dialog shows a
                // diagnosable error instead of "unable to load".
                // Use ErrorShowDialog so it ACTUALLY surfaces — the prior
                // ErrorNoDialog return code suppressed the dialog and
                // left the user staring at a silently-failed plug-in.
                var inner = ex.InnerException;
                errorMessage = $"ARIA plugin OnLoad failed: {ex.GetType().Name}: {ex.Message}";
                while (inner != null)
                {
                    errorMessage += $"\n  -> {inner.GetType().Name}: {inner.Message}";
                    inner = inner.InnerException;
                }
                Rhino.RhinoApp.WriteLine(errorMessage);
                Rhino.RhinoApp.WriteLine($"Stack: {ex.StackTrace}");
                return LoadReturnCode.ErrorShowDialog;
            }
        }
    }
}
