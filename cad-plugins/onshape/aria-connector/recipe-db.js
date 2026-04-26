/**
 * recipe-db.js — Auto-learning cache of (intent → known-good Onshape
 * REST/FeatureScript args) for the ARIA Onshape connector.
 *
 * Mirrors:
 *   cad-plugins/solidworks/AriaSW/RecipeDb.cs
 *   cad-plugins/rhino/AriaPanel/RecipeDb.cs
 *   cad-plugins/fusion360/aria_panel/recipe_db.py
 *
 * Storage: window.localStorage under key "AriaOnshape.recipes".
 * Browser-only — bridge-host.js runs in the Onshape tab outer iframe,
 * so there is no Node fs available.
 *
 * Tuning knobs persisted for Onshape extrudes:
 *   endBound          : "BLIND" / "THROUGH_ALL" / "UP_TO_NEXT" / "UP_TO_FACE"
 *   oppositeDirection : bool — flip the extrude direction
 *   bodyType          : "SOLID" / "SURFACE"
 *
 * Same auto-learning shape as the other plugins: every successful
 * REST call records the winning knob set; lookup hits the cached
 * recipe before any handcrafted fallback runs.
 */

(function (global) {
  "use strict";

  const KEY = "AriaOnshape.recipes";

  function _bootstrap() {
    return {
      "extrude_solid_new": {
        method:            "feature/extrude",
        endBound:          "BLIND",
        oppositeDirection: false,
        bodyType:          "SOLID",
        operationType:     "NEW",
      },
      "extrude_solid_join": {
        method:            "feature/extrude",
        endBound:          "BLIND",
        oppositeDirection: false,
        bodyType:          "SOLID",
        operationType:     "ADD",
      },
      "extrude_solid_cut": {
        method:            "feature/extrude",
        endBound:          "BLIND",
        oppositeDirection: false,
        bodyType:          "SOLID",
        operationType:     "REMOVE",
      },
      "extrude_solid_intersect": {
        method:            "feature/extrude",
        endBound:          "BLIND",
        oppositeDirection: false,
        bodyType:          "SOLID",
        operationType:     "INTERSECT",
      },
    };
  }

  let _store = {};
  let _ready = false;

  function _safeRead() {
    try {
      const raw = global.localStorage && global.localStorage.getItem(KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (e) {
      return {};
    }
  }

  function _safeWrite() {
    try {
      if (global.localStorage)
        global.localStorage.setItem(KEY, JSON.stringify(_store));
    } catch (e) {
      // Quota exceeded / private mode — non-fatal, addin still works.
    }
  }

  function init() {
    if (_ready) return;
    _store = _safeRead() || {};
    const boot = _bootstrap();
    for (const k of Object.keys(boot)) {
      if (!(k in _store)) _store[k] = boot[k];
    }
    _safeWrite();
    _ready = true;
    try {
      console.log(`[AriaOnshape] RecipeDb ready, ${Object.keys(_store).length} recipes`);
    } catch (e) {}
  }

  function lookup(intent) {
    if (!_ready) init();
    const v = _store[intent];
    return v ? Object.assign({}, v) : null;
  }

  function recordSuccess(intent, args) {
    if (!_ready) init();
    _store[intent] = Object.assign({}, args);
    _safeWrite();
    try {
      console.log(`[AriaOnshape] RecipeDb recorded '${intent}'`, args);
    } catch (e) {}
  }

  function count() {
    if (!_ready) init();
    return Object.keys(_store).length;
  }

  global.AriaRecipeDb = { init, lookup, recordSuccess, count };
})(typeof window !== "undefined" ? window : globalThis);
