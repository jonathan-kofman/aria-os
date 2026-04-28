/**
 * Config & Error State Provider
 *
 * Centralizes:
 * 1. API_BASE resolution (window.ARIA_API_BASE, ?api param, /api fallback)
 * 2. Error state (sticky errors visible across all panels)
 * 3. Connection status (online/offline awareness)
 *
 * All CAD panels use this context to avoid hardcoding URLs.
 * Window.ARIA_API_BASE is injected by the CAD host (SolidWorks, Rhino, Fusion, Onshape).
 */

import React, { createContext, useContext, useState, useCallback, useEffect } from "react";

const ConfigContext = createContext(null);

/**
 * Resolve API_BASE from (in priority order):
 *   1. ?api=... query param
 *   2. window.ARIA_API_BASE (CAD host injection)
 *   3. VITE_API_BASE (build-time env)
 *   4. /api (default fallback)
 */
function resolveApiBase() {
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

export function ConfigProvider({ children }) {
  const [apiBase] = useState(resolveApiBase);
  const [error, setError] = useState(null);
  const [isOnline, setIsOnline] = useState(
    typeof navigator !== "undefined" ? navigator.onLine : true
  );

  useEffect(() => {
    const handleOnline = () => setIsOnline(true);
    const handleOffline = () => setIsOnline(false);
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, []);

  const clearError = useCallback(() => setError(null), []);

  const value = {
    apiBase,
    error,
    setError,
    clearError,
    isOnline,
  };

  return (
    <ConfigContext.Provider value={value}>{children}</ConfigContext.Provider>
  );
}

/**
 * useConfig — access API_BASE, error state, connection status
 * from anywhere in the app without prop drilling.
 */
export function useConfig() {
  const ctx = useContext(ConfigContext);
  if (!ctx) {
    throw new Error(
      "useConfig must be called within <ConfigProvider>"
    );
  }
  return ctx;
}

export default ConfigProvider;
