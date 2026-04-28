// RecipeDb.cs — Auto-learning cache of (intent → known-good RhinoCommon
// args) for the ARIA Rhino plugin. Mirrors cad-plugins/solidworks/AriaSW/
// RecipeDb.cs so all CAD plugins share the same auto-discovery shape.
//
// Recipe = (intent_key, JObject of args).
// Storage = JSON at %LOCALAPPDATA%\AriaRhino\recipes.json.
//
// Knobs we cache for Rhino:
//   - capPlanarHoles    (Brep solidification after Extrusion.CreateExtrusion)
//   - reverseDirection  (flip the extrusion vector)
//   - toleranceMultiple (multiplier on doc.ModelAbsoluteTolerance — boolean
//                        ops sometimes need a looser tol to converge)
//
// RhinoCommon's API surface is more stable than SW's COM, so the cache
// here is mostly a record of "what worked for this user's tolerance /
// unit / template config" rather than a recovery from null returns. But
// the same auto-learning shape applies — when a fallback strategy wins,
// it's persisted so the next call hits the recipe directly.

using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;

namespace AriaPanel
{
    internal static class RecipeDb
    {
        private static Dictionary<string, JObject> _store = new();
        private static string? _path;
        private static readonly object _lock = new object();

        public static void Init()
        {
            try
            {
                string dir = Path.Combine(
                    Environment.GetFolderPath(
                        Environment.SpecialFolder.LocalApplicationData),
                    "AriaRhino");
                Directory.CreateDirectory(dir);
                _path = Path.Combine(dir, "recipes.json");

                if (File.Exists(_path))
                {
                    var raw = File.ReadAllText(_path);
                    var parsed = JsonConvert.DeserializeObject<Dictionary<string, JObject>>(raw);
                    if (parsed != null) _store = parsed;
                    Rhino.RhinoApp.WriteLine($"AriaRhino RecipeDb: loaded {_store.Count} recipes from {_path}");
                }
                else
                {
                    Rhino.RhinoApp.WriteLine($"AriaRhino RecipeDb: no cache yet — bootstrapping at {_path}");
                }

                foreach (var kv in BootstrapRecipes())
                {
                    if (!_store.ContainsKey(kv.Key))
                        _store[kv.Key] = kv.Value;
                }
                Save();
                Rhino.RhinoApp.WriteLine($"AriaRhino RecipeDb: ready, {_store.Count} total recipes");
            }
            catch (Exception ex)
            {
                Rhino.RhinoApp.WriteLine($"AriaRhino RecipeDb.Init failed: {ex.Message}");
            }
        }

        public static JObject? Lookup(string intent)
        {
            lock (_lock)
            {
                return _store.TryGetValue(intent, out var v) ? v : null;
            }
        }

        public static void RecordSuccess(string intent, JObject args)
        {
            // Same intent-vs-args invariant as the SW addin RecipeDb —
            // refuse to persist a recipe that contradicts its own intent
            // (e.g. blind=false stored under cut_extrude_blind). Without
            // this guard a single odd-shape success poisons the cache and
            // every subsequent matching intent replays the bad combo.
            if (intent != null && args != null)
            {
                bool intentBlind = intent.IndexOf("blind",
                    StringComparison.OrdinalIgnoreCase) >= 0;
                bool intentThrough = intent.IndexOf("through",
                    StringComparison.OrdinalIgnoreCase) >= 0;
                if (intentBlind && args["blind"] != null
                    && args["blind"].Type == JTokenType.Boolean
                    && !args["blind"].Value<bool>())
                {
                    Rhino.RhinoApp.WriteLine($"AriaRhino RecipeDb: REJECTED "
                        + $"'{intent}' — intent says blind but args have "
                        + "blind=false. Not poisoning the cache.");
                    return;
                }
                if (intentThrough && args["blind"] != null
                    && args["blind"].Type == JTokenType.Boolean
                    && args["blind"].Value<bool>())
                {
                    Rhino.RhinoApp.WriteLine($"AriaRhino RecipeDb: REJECTED "
                        + $"'{intent}' — intent says through-all but args "
                        + "have blind=true. Not poisoning the cache.");
                    return;
                }
            }
            lock (_lock)
            {
                _store[intent] = args;
                Save();
            }
            // JToken.ToString(Formatting) overload isn't on every Newtonsoft
            // version that Rhino may have already loaded. Use JsonConvert.
            string preview;
            try { preview = JsonConvert.SerializeObject(args); }
            catch { preview = "(serialize failed)"; }
            Rhino.RhinoApp.WriteLine($"AriaRhino RecipeDb: recorded '{intent}' -> {preview}");
        }

        private static void Save()
        {
            if (_path == null) return;
            try
            {
                File.WriteAllText(_path,
                    JsonConvert.SerializeObject(_store, Formatting.Indented));
            }
            catch (Exception ex)
            {
                Rhino.RhinoApp.WriteLine($"AriaRhino RecipeDb.Save failed: {ex.Message}");
            }
        }

        private static Dictionary<string, JObject> BootstrapRecipes()
        {
            return new Dictionary<string, JObject>
            {
                ["extrude_solid_new"] = JObject.FromObject(new
                {
                    method             = "Extrusion.CreateExtrusion",
                    capPlanarHoles     = true,
                    reverseDirection   = false,
                }),
                ["extrude_solid_cut"] = JObject.FromObject(new
                {
                    method             = "Brep.CreateBooleanDifference",
                    capPlanarHoles     = true,
                    reverseDirection   = false,
                    toleranceMultiple  = 1.0,
                }),
                ["extrude_solid_join"] = JObject.FromObject(new
                {
                    method             = "Brep.CreateBooleanUnion",
                    capPlanarHoles     = true,
                    reverseDirection   = false,
                    toleranceMultiple  = 1.0,
                }),
                ["extrude_solid_intersect"] = JObject.FromObject(new
                {
                    method             = "Brep.CreateBooleanIntersection",
                    capPlanarHoles     = true,
                    reverseDirection   = false,
                    toleranceMultiple  = 1.0,
                }),
            };
        }

        public static int Count
        {
            get { lock (_lock) return _store.Count; }
        }
    }
}
