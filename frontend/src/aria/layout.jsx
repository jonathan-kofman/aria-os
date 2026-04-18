import { useState, useEffect } from "react";
import { useViewport, layout, spacing } from "../responsive.js";
import { T } from "./theme.js";
import { NAV } from "./nav.js";

export function SubTabs({ tabs, active, setActive }) {
  const vp = useViewport();
  const S = spacing(vp);
  return (
    <div style={{
      display: "flex",
      flexShrink: 0,
      gap: "4px",
      padding: vp.isMobile ? `8px ${S.pageX}` : `12px ${S.pageX}`,
      borderBottom: `1px solid ${T.border}`,
      background: "rgba(0,0,0,0.2)",
      overflowX: "auto",
      WebkitOverflowScrolling: "touch",
      flexWrap: "nowrap",
      scrollbarWidth: "none",
      msOverflowStyle: "none",
    }}>
      {tabs.map(t => (
        <button key={t.id} onClick={() => setActive(t.id)}
          style={{
            padding: vp.isMobile ? "8px 12px" : "6px 14px",
            borderRadius: "7px",
            border: `1px solid ${active === t.id ? T.ai : "transparent"}`,
            background: active === t.id ? `${T.ai}12` : "transparent",
            color: active === t.id ? T.ai : T.text3,
            fontSize: vp.isMobile ? "13px" : "12px",
            fontWeight: 600,
            cursor: "pointer",
            transition: "all 0.15s",
            flexShrink: 0,
            whiteSpace: "nowrap",
          }}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Sidebar({ active, setActive }) {
  const [hover, setHover] = useState(null);
  const vp = useViewport();
  const L = layout(vp);

  const containerStyle = {
    width: L.sidebarWidth,
    height: L.sidebarHeight,
    position: "fixed",
    ...L.sidebarPos,
    background: "rgba(15,15,24,0.85)",
    backdropFilter: "blur(20px)",
    borderRight: vp.isMobile ? "0" : `1px solid ${T.border}`,
    borderTop:   vp.isMobile ? `1px solid ${T.border}` : "0",
    display: "flex",
    flexDirection: L.sidebarFlexDir,
    alignItems: "center",
    justifyContent: vp.isMobile ? "space-around" : "flex-start",
    padding: L.sidebarPad,
    zIndex: 100,
  };

  return (
    <div style={containerStyle}>
      {!vp.isMobile && (
        <div style={{ width: "36px", height: "36px", borderRadius: "10px",
                      background: `linear-gradient(135deg, ${T.ai}, ${T.brand})`,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      fontSize: "16px", color: "#fff", fontWeight: 700,
                      marginBottom: "24px", boxShadow: `0 0 24px ${T.aiGlow}` }}>α</div>
      )}
      <div style={{ display: "flex", flexDirection: L.sidebarFlexDir,
                    gap: vp.isMobile ? "0" : "4px",
                    flex: 1,
                    width: vp.isMobile ? "100%" : "auto",
                    justifyContent: vp.isMobile ? "space-around" : "flex-start" }}>
        {NAV.map(n => (
          <div key={n.id} style={{ position: "relative" }}
               onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)}>
            <button onClick={() => setActive(n.id)}
                    style={{ width: "44px", height: "44px", borderRadius: "10px",
                             border: "none",
                             background: active === n.id
                               ? `linear-gradient(135deg, ${T.ai}25, ${T.ai}10)`
                               : "transparent",
                             color: active === n.id ? T.ai : T.text3,
                             fontSize: "16px", cursor: "pointer",
                             display: "flex", alignItems: "center", justifyContent: "center",
                             transition: "all 0.2s",
                             boxShadow: active === n.id
                               ? `inset 0 0 0 1px ${T.ai}40, 0 0 16px ${T.aiGlow}`
                               : "none" }}>{n.icon}</button>
            {hover === n.id && !vp.isMobile && (
              <div style={{ position: "absolute", left: "52px", top: "50%",
                            transform: "translateY(-50%)",
                            padding: "6px 10px", background: T.bg3,
                            border: `1px solid ${T.borderHi}`, borderRadius: "6px",
                            fontSize: "11px", color: T.text1, fontWeight: 500,
                            whiteSpace: "nowrap", pointerEvents: "none", zIndex: 200 }}>
                {n.label}
              </div>
            )}
          </div>
        ))}
      </div>
      {!vp.isMobile && (
        <div style={{ width: "36px", height: "36px", borderRadius: "9px",
                      background: `linear-gradient(135deg, ${T.ai}20, ${T.brand}20)`,
                      border: `1px solid ${T.borderHi}`,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      fontSize: "10px", color: T.ai, fontWeight: 700 }}>AI</div>
      )}
    </div>
  );
}

export function ResponsiveMain({ children }) {
  const vp = useViewport();
  const L = layout(vp);
  return (
    <div style={{
      marginLeft: L.contentPadLeft,
      paddingBottom: L.contentPadBot,
      minHeight: 0,
      height: "100vh",
      maxHeight: "100dvh",
      overflow: "hidden",
      display: "flex",
      flexDirection: "column",
    }}>
      {children}
    </div>
  );
}

export function TopBar({ section, subsection, pipelineStatus }) {
  const [time, setTime] = useState(new Date());
  const vp = useViewport();
  const L = layout(vp);
  const S = spacing(vp);
  useEffect(() => { const i = setInterval(() => setTime(new Date()), 1000); return () => clearInterval(i); }, []);
  const statusColor = pipelineStatus === "running" ? T.amber : pipelineStatus === "done" ? T.green : T.text4;
  const statusLabel = pipelineStatus === "running" ? "GENERATING" : pipelineStatus === "done" ? "COMPLETE" : "IDLE";
  return (
    <div style={{ position: "sticky", top: 0, flexShrink: 0, height: L.headerHeight,
                  padding: `0 ${S.pageX}`,
                  background: "rgba(10,10,15,0.85)",
                  backdropFilter: "blur(20px)",
                  borderBottom: `1px solid ${T.border}`,
                  display: "flex", justifyContent: "space-between", alignItems: "center", zIndex: 50 }}>
      <div style={{ display: "flex", alignItems: "center", gap: "8px",
                    fontSize: vp.isMobile ? "11px" : "13px",
                    minWidth: 0, overflow: "hidden", whiteSpace: "nowrap" }}>
        <span style={{ color: T.text3, fontWeight: 500 }}>ARIA-OS</span>
        <span style={{ color: T.text4 }}>/</span>
        <span style={{ color: T.text1, fontWeight: 500 }}>{section}</span>
        {subsection && !vp.isMobile && (
          <><span style={{ color: T.text4 }}>/</span>
            <span style={{ color: T.text0, fontWeight: 600 }}>{subsection}</span></>
        )}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: vp.isMobile ? "6px" : "12px",
                    flexShrink: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: "6px",
                      padding: vp.isMobile ? "4px 8px" : "6px 10px",
                      borderRadius: "7px",
                      background: `${statusColor}08`,
                      border: `1px solid ${statusColor}30` }}>
          <div style={{ width: "5px", height: "5px", borderRadius: "50%",
                        background: statusColor, boxShadow: `0 0 8px ${statusColor}`,
                        animation: pipelineStatus === "running" ? "pulse 1s infinite" : "none" }} />
          <span style={{ fontSize: vp.isMobile ? "9px" : "10px",
                         color: statusColor, fontWeight: 700, letterSpacing: "0.06em" }}>
            {vp.isMobile && statusLabel === "GENERATING" ? "GEN" : statusLabel}
          </span>
        </div>
        {!vp.isMobile && (
          <div style={{ fontSize: "12px", color: T.text2,
                        fontFeatureSettings: "'tnum'",
                        padding: "6px 10px", borderRadius: "7px",
                        background: "rgba(255,255,255,0.03)",
                        border: `1px solid ${T.border}` }}>
            {time.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false })}
          </div>
        )}
      </div>
    </div>
  );
}
