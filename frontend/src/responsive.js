/**
 * useViewport — single source of truth for responsive layout decisions.
 *
 * Returns { width, height, isMobile, isTablet, isDesktop, orientation }.
 * Re-renders the consuming component on viewport changes (debounced 100ms).
 *
 * Breakpoints (mobile-first):
 *   isMobile : ≤ 640px  (phone, single column, bottom nav)
 *   isTablet : 641-900  (compact desktop, side nav still 64px wide)
 *   isDesktop: > 900    (full layout, panels side-by-side)
 *
 * Use this hook for layout structure decisions (sidebar position, grid
 * column counts, padding values). Keep cosmetic styles inline as before.
 */
import { useEffect, useState } from "react";

const MOBILE_MAX = 640;
const TABLET_MAX = 900;

function getViewport() {
  if (typeof window === "undefined") {
    return { width: 1280, height: 800, isMobile: false, isTablet: false, isDesktop: true, orientation: "landscape" };
  }
  const w = window.innerWidth;
  const h = window.innerHeight;
  return {
    width: w,
    height: h,
    isMobile: w <= MOBILE_MAX,
    isTablet: w > MOBILE_MAX && w <= TABLET_MAX,
    isDesktop: w > TABLET_MAX,
    orientation: w > h ? "landscape" : "portrait",
  };
}

export function useViewport() {
  const [vp, setVp] = useState(getViewport);
  useEffect(() => {
    let timer = null;
    const onResize = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => setVp(getViewport()), 100);
    };
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("orientationchange", onResize);
      if (timer) clearTimeout(timer);
    };
  }, []);
  return vp;
}

/** Pick a value based on viewport. Pass {mobile, tablet, desktop} or just
 *  {mobile, desktop} (tablet falls back to desktop). */
export function pick(vp, choices) {
  if (vp.isMobile)  return choices.mobile  ?? choices.tablet ?? choices.desktop;
  if (vp.isTablet)  return choices.tablet  ?? choices.desktop;
  return choices.desktop;
}

/** Spacing scale that adapts to viewport. */
export function spacing(vp) {
  return {
    pageX:   vp.isMobile ? "12px" : vp.isTablet ? "18px" : "28px",
    pageY:   vp.isMobile ? "12px" : vp.isTablet ? "16px" : "20px",
    gap:     vp.isMobile ? "8px"  : vp.isTablet ? "12px" : "16px",
    cardPad: vp.isMobile ? "12px" : "16px",
  };
}

/** Common view container style — padding + grid + scroll behavior that
 *  adapts. Pass `cols` ("1fr 380px", "280px 1fr", "1fr 360px", etc.) — on
 *  mobile it always becomes "1fr" with vertical scrolling.
 *
 *  Usage: <div style={viewContainer(vp, "1fr 380px")}> ... </div>
 */
export function viewContainer(vp, cols = "1fr 380px") {
  const S = spacing(vp);
  const L = layout(vp);
  return {
    padding: `${S.pageY} ${S.pageX}`,
    display: "grid",
    gridTemplateColumns: vp.isMobile ? "1fr" : cols,
    gap: S.gap,
    height: vp.isMobile ? "auto" : "calc(100vh - 56px - 49px)",
    minHeight: vp.isMobile ? "calc(100vh - 56px - 49px - 64px)" : undefined,
    overflow: vp.isMobile ? "auto" : "hidden",
    WebkitOverflowScrolling: "touch",
  };
}


/** Layout dimensions that adapt to viewport. */
export function layout(vp) {
  return {
    sidebarWidth:  vp.isMobile ? "100%" : "64px",
    sidebarHeight: vp.isMobile ? "56px" : "100vh",
    sidebarPos:    vp.isMobile ? { bottom: 0, top: "auto", left: 0, right: 0 }
                                : { left: 0, top: 0 },
    sidebarFlexDir: vp.isMobile ? "row" : "column",
    sidebarPad:     vp.isMobile ? "4px 8px" : "16px 0",
    contentPadLeft: vp.isMobile ? "0" : "64px",
    contentPadBot:  vp.isMobile ? "64px" : "0",
    headerHeight:   vp.isMobile ? "48px" : "56px",
    twoColGrid:     vp.isMobile ? "1fr"  : "1fr 380px",
    threeColGrid:   vp.isMobile ? "1fr"  : vp.isTablet ? "1fr 1fr" : "1fr 1fr 1fr",
    fontBase:       vp.isMobile ? "13px" : "14px",
    fontInput:      vp.isMobile ? "16px" : "14px",  // prevents iOS zoom
    tapTarget:      vp.isMobile ? "44px" : "32px",
  };
}
