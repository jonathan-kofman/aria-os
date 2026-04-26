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
            lock (_lock)
            {
                _store[intent] = args;
                Save();
            }
            Rhino.RhinoApp.WriteLine($"AriaRhino RecipeDb: recorded '{intent}' -> {args.ToString(Formatting.None)}");
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
