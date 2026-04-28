// RecipeDb.cs — Auto-learning cache of (intent → known-good API call args).
//
// The promise: when ANY handcrafted op finally succeeds in OpExtrude /
// OpCircularPattern / OpSheetMetal / etc. via fallback chains, we record
// which params worked and persist them. Next request with the same intent
// hits the recipe first — no fallback chain, no LLM round-trip, no
// 6-hour debugging session per SW version quirk.
//
// Recipe = (intent_key, method_name, args_template).
// Storage = JSON file at %LOCALAPPDATA%\AriaSW\recipes.json.
//
// Auto-discovery sources, in priority:
//   1. Persisted cache (recipes.json)               — survives addin reloads
//   2. Bootstrap recipes (BootstrapRecipes())       — hand-curated
//                                                     known-good defaults
//                                                     based on public SW
//                                                     API help samples
//   3. Runtime success (RecordSuccess())            — every fallback win
//                                                     promoted to recipe
//
// Future: LLM fallback when no recipe matches, with introspection
// feedback. For now the chain just falls through to existing handcrafted
// retry logic and learns from the win.

using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace AriaSW
{
    internal static class RecipeDb
    {
        // intent key (e.g. "cut_extrude_blind", "cut_extrude_through_all",
        // "extrude_boss_new", "fillet_const_radius") → JSON object holding
        // the winning param combo. Args are name→value; the consumer maps
        // names to its method's positional args.
        private static Dictionary<string, JObject> _store = new();
        private static string _path;
        private static readonly object _lock = new object();

        public static void Init()
        {
            try
            {
                string dir = Path.Combine(
                    Environment.GetFolderPath(
                        Environment.SpecialFolder.LocalApplicationData),
                    "AriaSW");
                Directory.CreateDirectory(dir);
                _path = Path.Combine(dir, "recipes.json");

                if (File.Exists(_path))
                {
                    var raw = File.ReadAllText(_path);
                    var parsed = JsonConvert.DeserializeObject<Dictionary<string, JObject>>(raw);
                    if (parsed != null) _store = parsed;
                    AriaSwAddin.FileLog($"RecipeDb: loaded {_store.Count} recipes from {_path}");
                }
                else
                {
                    AriaSwAddin.FileLog($"RecipeDb: no cache yet — bootstrapping at {_path}");
                }

                // Layer bootstrap recipes IN ADDITION TO persisted ones.
                // Persisted wins on key collision (it represents an actual
                // successful run on this user's SW version).
                foreach (var kv in BootstrapRecipes())
                {
                    if (!_store.ContainsKey(kv.Key))
                        _store[kv.Key] = kv.Value;
                }
                Save();
                AriaSwAddin.FileLog($"RecipeDb: ready, {_store.Count} total recipes");
            }
            catch (Exception ex)
            {
                AriaSwAddin.FileLog($"RecipeDb.Init failed: {ex.Message}");
            }
        }

        public static JObject Lookup(string intent)
        {
            lock (_lock)
            {
                return _store.TryGetValue(intent, out var v) ? v : null;
            }
        }

        /// <summary>Promote a successful arg combo into the persisted
        /// recipe DB. Idempotent — overwriting an existing recipe with the
        /// same winning combo is safe; overwriting with a NEW winning combo
        /// just means the previous recipe stopped working (e.g. SW version
        /// change) and the new one took its place.</summary>
        public static void RecordSuccess(string intent, JObject args)
        {
            // Invariant guard — reject obviously-wrong recipes BEFORE they
            // poison the cache and get replayed forever. We saw a class of
            // bug where a `cut_extrude_blind` succeeded against a body with
            // through-all geometry and ended up storing `blind:false`,
            // which then misbehaved on every future blind cut. Cheap intent
            // check stops that loop without needing a manual cache wipe.
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
                    AriaSwAddin.FileLog($"RecipeDb: REJECTED '{intent}' — "
                        + "intent says blind but args have blind=false. "
                        + "Not poisoning the cache.");
                    return;
                }
                if (intentThrough && args["blind"] != null
                    && args["blind"].Type == JTokenType.Boolean
                    && args["blind"].Value<bool>())
                {
                    AriaSwAddin.FileLog($"RecipeDb: REJECTED '{intent}' — "
                        + "intent says through-all but args have blind=true. "
                        + "Not poisoning the cache.");
                    return;
                }
            }
            lock (_lock)
            {
                _store[intent] = args;
                Save();
            }
            // Use JsonConvert.SerializeObject — JToken.ToString(Formatting)
            // overload isn't present on every Newtonsoft.Json version SW
            // may have already loaded into its app domain at runtime.
            string preview;
            try { preview = JsonConvert.SerializeObject(args); }
            catch { preview = "(serialize failed)"; }
            AriaSwAddin.FileLog($"RecipeDb: recorded '{intent}' -> {preview}");
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
                AriaSwAddin.FileLog($"RecipeDb.Save failed: {ex.Message}");
            }
        }

        // ----------------------------------------------------------------
        // Bootstrap recipes — minimal "tuning knob" shape that maps 1:1
        // to TryFeatureCut's signature. Full FeatureCut4 arg expansion
        // happens inside TryFeatureCut itself; the recipe just carries
        // the values that varied between failure and success.
        //
        // Recorded keys:
        //   method        — the SW API method (informational; humans
        //                   reading recipes.json can tell what was used)
        //   blind         — true for swEndCondBlind, false for ThroughAll
        //   flip          — flip the cut/extrude direction
        //   selectBody    — pre-select target body with Mark=4
        //   useAutoSelect — let SW auto-pick affected bodies
        // ----------------------------------------------------------------
        private static Dictionary<string, JObject> BootstrapRecipes()
        {
            return new Dictionary<string, JObject>
            {
                ["cut_extrude_blind"] = JObject.FromObject(new
                {
                    method        = "FeatureCut4",
                    blind         = true,
                    flip          = false,
                    dir           = false,
                    selectBody    = false,
                    useAutoSelect = true,
                }),
                ["cut_extrude_through_all"] = JObject.FromObject(new
                {
                    method        = "FeatureCut4",
                    blind         = false,
                    flip          = false,
                    dir           = false,
                    selectBody    = false,
                    useAutoSelect = true,
                }),
                ["extrude_boss_new"] = JObject.FromObject(new
                {
                    method = "FeatureExtrusion3",
                    blind  = true,
                    flip   = false,
                    solid  = true,
                    merge  = true,
                }),
            };
        }

        public static int Count
        {
            get { lock (_lock) return _store.Count; }
        }
    }
}
