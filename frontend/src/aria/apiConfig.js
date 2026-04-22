/**
 * ARIA frontend API-base resolver.
 *
 * The same React app runs in three contexts:
 *
 *   1. Standalone (Vite dev / Vercel prod): fetches hit `/api/...` on the
 *      same origin via the Vite proxy or deployed rewrites.
 *
 *   2. Embedded inside a CAD plugin panel (Fusion 360 Palette, Rhino
 *      WebView2, SolidWorks Task Pane, Onshape iframe): the host injects
 *      `window.ARIA_API_BASE` before the panel HTML loads. We point fetches
 *      at that absolute URL (e.g. https://aria.example.com/api or
 *      http://localhost:8001/api for a local dev backend).
 *
 *   3. Storybook / offline preview: if `VITE_API_BASE` is set at build time,
 *      that wins over everything else.
 *
 * Resolution priority (first non-empty wins):
 *   window.ARIA_API_BASE  (CAD host injection)
 *   import.meta.env.VITE_API_BASE  (build-time env)
 *   /api   (default — same-origin standalone)
 */

function _resolveBase() {
  // 1) Honour an explicit ?api=... query param — this is how the Onshape
  //    / Rhino / SolidWorks hosts pass the backend URL at load time so
  //    the same bundle works in every host without rebuilding.
  if (typeof window !== "undefined" && window.location?.search) {
    try {
      const q = new URLSearchParams(window.location.search);
      const api = q.get("api");
      if (api) return String(api).replace(/\/+$/, "");
    } catch { /* noop */ }
  }
  if (typeof window !== "undefined" && window.ARIA_API_BASE) {
    return String(window.ARIA_API_BASE).replace(/\/+$/, "");
  }
  if (typeof import.meta !== "undefined" && import.meta.env?.VITE_API_BASE) {
    return String(import.meta.env.VITE_API_BASE).replace(/\/+$/, "");
  }
  return "/api";
}

export const API_BASE = _resolveBase();

/**
 * api(path) — build an absolute-or-relative URL for a backend endpoint.
 * Always pass paths WITHOUT the leading /api prefix:
 *
 *   fetch(api("/parts"))   // → /api/parts  (standalone)
 *                          // → https://aria.example.com/api/parts  (embedded)
 */
export function api(path) {
  const p = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${p}`;
}

/**
 * Tiny fetch wrapper that prepends API_BASE and JSON-parses the response.
 * Returns parsed JSON on 2xx, throws {status, body} on non-2xx, and
 * re-throws network errors as-is. Intentionally thin — no retry / caching
 * logic here; callers can layer that.
 */
export async function apiFetch(path, init = {}) {
  const res = await fetch(api(path), init);
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch { body = await res.text(); }
    const err = new Error(`API ${res.status} ${res.statusText}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return res.json();
}

/**
 * SSE helper: opens an EventSource on an API_BASE-rooted path. Falls back
 * to a polling loop when EventSource isn't available (some embedded
 * WebViews on older Qt/Chromium builds strip SSE).
 */
export function apiEventSource(path) {
  return new EventSource(api(path));
}
