import { T } from "./theme.js";

export function Sparkline({ data, color }) {
  const max = Math.max(...data), min = Math.min(...data), range = max - min || 1;
  const pts = data.map((v, i) => `${(i / (data.length - 1)) * 100},${100 - ((v - min) / range) * 100}`).join(" ");
  const id = `sg${color.replace("#", "")}`;
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" style={{ width: "100%", height: "28px" }}>
      <defs><linearGradient id={id} x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" stopColor={color} stopOpacity="0.3" /><stop offset="1" stopColor={color} stopOpacity="0" />
      </linearGradient></defs>
      <polyline points={`0,100 ${pts} 100,100`} fill={`url(#${id})`} />
      <polyline points={pts} fill="none" stroke={color} strokeWidth="2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function StatCard({ label, value, sub, color, spark }) {
  return (
    <div style={{ position: "relative", background: `linear-gradient(180deg, ${T.bg2} 0%, ${T.bg1} 100%)`, border: `1px solid ${T.border}`, borderRadius: "12px", padding: "14px 16px", overflow: "hidden", boxShadow: "0 4px 12px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.04)" }}>
      <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: "1px", background: `linear-gradient(90deg, transparent, ${color}50, transparent)` }} />
      <div style={{ fontSize: "9px", color: T.text3, letterSpacing: "0.12em", fontWeight: 700, marginBottom: "6px" }}>{label}</div>
      <div style={{ fontSize: "24px", fontWeight: 700, color: T.text0, letterSpacing: "-0.025em", lineHeight: 1, marginBottom: "2px", fontFeatureSettings: "'tnum'" }}>{value}</div>
      <div style={{ fontSize: "10px", color: T.text3, marginBottom: "6px" }}>{sub}</div>
      {spark && <Sparkline data={spark} color={color} />}
    </div>
  );
}

export function Panel({ children, title, style = {} }) {
  return (
    <div style={{ background: `linear-gradient(180deg, ${T.bg2} 0%, ${T.bg1} 100%)`, border: `1px solid ${T.border}`, borderRadius: "14px", overflow: "hidden", boxShadow: "0 8px 24px rgba(0,0,0,0.4)", ...style }}>
      {title && (
        <div style={{ padding: "12px 18px", borderBottom: `1px solid ${T.border}`, background: "rgba(0,0,0,0.3)", fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.12em" }}>{title}</div>
      )}
      {children}
    </div>
  );
}

export function Badge({ label, color }) {
  return (
    <span style={{ fontSize: "9px", padding: "3px 8px", borderRadius: "100px", background: `${color}15`, color, border: `1px solid ${color}30`, fontWeight: 700, letterSpacing: "0.06em" }}>{label}</span>
  );
}
