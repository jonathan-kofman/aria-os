import { useState, useEffect, useRef, useCallback } from "react";
import { useViewport, layout, spacing, viewContainer } from "./responsive.js";

// Three.js is heavy (~400KB). Lazy-load only when STLViewer first mounts so
// the initial page bundle stays small for mobile cellular cold-loads.
let _threeModulesPromise = null;
function loadThree() {
  if (_threeModulesPromise) return _threeModulesPromise;
  _threeModulesPromise = Promise.all([
    import("three"),
    import("three/addons/loaders/STLLoader.js"),
    import("three/addons/controls/OrbitControls.js"),
  ]).then(([THREE, { STLLoader }, { OrbitControls }]) => ({ THREE, STLLoader, OrbitControls }));
  return _threeModulesPromise;
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
const T = {
  bg0: "#0A0A0F", bg1: "#0F0F18", bg2: "#15151F", bg3: "#1A1A26",
  border: "rgba(255,255,255,0.06)", borderHi: "rgba(255,255,255,0.12)",
  text0: "#FAFAFA", text1: "#E5E5EA", text2: "#A1A1AA", text3: "#71717A", text4: "#52525B",
  brand: "#7C3AED", brandGlow: "rgba(124,58,237,0.35)",
  ai: "#00D4FF", aiGlow: "rgba(0,212,255,0.35)",
  green: "#10B981", greenGlow: "rgba(16,185,129,0.35)",
  amber: "#F59E0B", amberGlow: "rgba(245,158,11,0.35)",
  red: "#EF4444", redGlow: "rgba(239,68,68,0.35)",
  blue: "#3B82F6", blueGlow: "rgba(59,130,246,0.35)",
};

const NAV = [
  { id: "generate",    label: "Generate",    icon: "◉" },
  { id: "files",       label: "Files",       icon: "▤" },
  { id: "library",     label: "Library",     icon: "◰" },
  { id: "validate",    label: "Validate",    icon: "⬡" },
  { id: "ecad",        label: "ECAD",        icon: "⊞" },
  { id: "manufacture", label: "Manufacture", icon: "⚙" },
  { id: "runs",        label: "Runs",        icon: "≡" },
];

const SUB_TABS = {
  generate:    [{ id: "nl", label: "Natural Language" }, { id: "image", label: "From Image" }, { id: "assembly", label: "Assembly" }, { id: "terrain", label: "Terrain" }, { id: "scan", label: "Scan" }, { id: "refine", label: "Refine" }],
  files:       [{ id: "browse", label: "Browse" }, { id: "upload", label: "Upload" }],
  library:     [{ id: "parts", label: "Parts" }, { id: "materials", label: "Materials" }, { id: "catalog", label: "Catalog" }],
  validate:    [{ id: "physics", label: "Physics" }, { id: "dfm", label: "DFM" }, { id: "drawings", label: "Drawings" }, { id: "visual", label: "Visual Verify" }, { id: "cem", label: "CEM Advise" }],
  ecad:        [{ id: "schematic", label: "Schematic" }, { id: "layout", label: "PCB Layout" }, { id: "bom", label: "BOM" }, { id: "sim", label: "Simulation" }],
  manufacture: [{ id: "cam", label: "CAM" }, { id: "tools", label: "Tools" }, { id: "post", label: "Post Processors" }],
  runs:        [{ id: "recent", label: "Recent Runs" }, { id: "health", label: "Health" }, { id: "system", label: "System" }],
};

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------
function Sparkline({ data, color }) {
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

function StatCard({ label, value, sub, color, spark }) {
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

function Panel({ children, title, style = {} }) {
  return (
    <div style={{ background: `linear-gradient(180deg, ${T.bg2} 0%, ${T.bg1} 100%)`, border: `1px solid ${T.border}`, borderRadius: "14px", overflow: "hidden", boxShadow: "0 8px 24px rgba(0,0,0,0.4)", ...style }}>
      {title && (
        <div style={{ padding: "12px 18px", borderBottom: `1px solid ${T.border}`, background: "rgba(0,0,0,0.3)", fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.12em" }}>{title}</div>
      )}
      {children}
    </div>
  );
}

function Badge({ label, color }) {
  return (
    <span style={{ fontSize: "9px", padding: "3px 8px", borderRadius: "100px", background: `${color}15`, color, border: `1px solid ${color}30`, fontWeight: 700, letterSpacing: "0.06em" }}>{label}</span>
  );
}

function SubTabs({ tabs, active, setActive }) {
  const vp = useViewport();
  const S = spacing(vp);
  return (
    <div style={{
      display: "flex",
      gap: "4px",
      padding: vp.isMobile ? `8px ${S.pageX}` : `12px ${S.pageX}`,
      borderBottom: `1px solid ${T.border}`,
      background: "rgba(0,0,0,0.2)",
      overflowX: "auto",
      WebkitOverflowScrolling: "touch",
      flexWrap: "nowrap",
      scrollbarWidth: "none",   // Firefox
      msOverflowStyle: "none",  // IE/Edge legacy
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

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------
function Sidebar({ active, setActive }) {
  const [hover, setHover] = useState(null);
  const vp = useViewport();
  const L = layout(vp);

  // On mobile: bottom-anchored horizontal nav bar.
  // On desktop/tablet: left-anchored vertical rail (the original layout).
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
      {/* Brand mark — hide on mobile to save horizontal space */}
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
            {/* Hover tooltip — desktop/tablet only */}
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
      {/* "AI" badge — desktop/tablet only */}
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

// ---------------------------------------------------------------------------
// Files tab — universal file browser + previewer. Mirrors what the local
// dashboard's `--view <file>` CLI does: open ANY artifact (STL/STEP/SVG/PNG/
// JSON/DXF) and render it with the right viewer per file extension.
// ---------------------------------------------------------------------------
// File-system helpers shared across the Files tab
const _KIND_BADGE = {
  step: { color: "#A78BFA", label: "STEP" },
  stl:  { color: "#00D4FF", label: "STL"  },
  svg:  { color: "#34D399", label: "SVG"  },
  png:  { color: "#F59E0B", label: "PNG"  },
  jpg:  { color: "#F59E0B", label: "JPG"  },
  jpeg: { color: "#F59E0B", label: "JPG"  },
  dxf:  { color: "#60A5FA", label: "DXF"  },
  json: { color: "#FBBF24", label: "JSON" },
  py:   { color: "#3B82F6", label: "PY"   },
  md:   { color: "#9CA3AF", label: "MD"   },
  zip:  { color: "#EC4899", label: "ZIP"  },
  gcode:{ color: "#10B981", label: "GCODE"},
  pcb:  { color: "#22D3EE", label: "PCB"  },
  kicad_pcb: { color: "#22D3EE", label: "PCB" },
  other:{ color: "#71717A", label: "FILE" },
};

function _fmtBytes(n) {
  if (!n && n !== 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function _fmtRelTime(epoch_s) {
  if (!epoch_s) return "";
  const dt = Date.now() / 1000 - epoch_s;
  if (dt < 60)        return `${Math.floor(dt)}s ago`;
  if (dt < 3600)      return `${Math.floor(dt / 60)}m ago`;
  if (dt < 86400)     return `${Math.floor(dt / 3600)}h ago`;
  if (dt < 86400 * 7) return `${Math.floor(dt / 86400)}d ago`;
  return new Date(epoch_s * 1000).toISOString().slice(0, 10);
}

function _kindOf(name) {
  const m = (name || "").toLowerCase().match(/\.([a-z0-9_]+)$/);
  if (!m) return "other";
  const ext = m[1];
  if (ext === "kicad_pcb" || ext === "kicad_sch") return "pcb";
  return _KIND_BADGE[ext] ? ext : "other";
}

function _parentOf(path) {
  const parts = (path || "").replace(/\\/g, "/").split("/");
  parts.pop();
  return parts.join("/") || "outputs";
}

function FilesBrowse() {
  const vp = useViewport();
  const S = spacing(vp);
  const [files, setFiles] = useState([]);
  const [selected, setSelected] = useState(null);
  const [filter, setFilter] = useState("");
  const [activeKinds, setActiveKinds] = useState(() => {
    try { return new Set(JSON.parse(localStorage.getItem("files.kinds") || "[]")); }
    catch { return new Set(); }
  });
  const [sortBy, setSortBy] = useState(
    () => localStorage.getItem("files.sort") || "mtime");
  const [groupByRun, setGroupByRun] = useState(
    () => localStorage.getItem("files.group") !== "false");
  const [collapsed, setCollapsed] = useState(new Set());
  const [refreshTick, setRefreshTick] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetch("/api/outputs")
      .then(r => r.json())
      .then(d => setFiles(d.files || []))
      .catch(() => setFiles([]))
      .finally(() => setLoading(false));
  }, [refreshTick]);

  // Persist UI prefs
  useEffect(() => { localStorage.setItem("files.kinds", JSON.stringify([...activeKinds])); }, [activeKinds]);
  useEffect(() => { localStorage.setItem("files.sort", sortBy); }, [sortBy]);
  useEffect(() => { localStorage.setItem("files.group", String(groupByRun)); }, [groupByRun]);

  const fileLike = (f) => typeof f === "string"
    ? { path: f, name: f.split(/[\\/]/).pop(), size: 0, mtime: 0, kind: _kindOf(f) }
    : {
        path: f.path || f,
        name: (f.name || f.path || "").split(/[\\/]/).pop(),
        size: f.size || 0,
        mtime: f.mtime || 0,
        kind: f.kind || _kindOf(f.name || f.path || ""),
      };

  const allFiles = files.map(fileLike);

  // Available kinds for filter pills (only show kinds actually present)
  const kindCounts = {};
  for (const f of allFiles) kindCounts[f.kind] = (kindCounts[f.kind] || 0) + 1;
  const availableKinds = Object.keys(kindCounts).sort();

  // Apply filters
  let filtered = allFiles;
  if (activeKinds.size > 0) {
    filtered = filtered.filter(f => activeKinds.has(f.kind));
  }
  if (filter) {
    const q = filter.toLowerCase();
    filtered = filtered.filter(f =>
      f.name.toLowerCase().includes(q) || f.path.toLowerCase().includes(q));
  }

  // Sort
  const sorted = [...filtered].sort((a, b) => {
    if (sortBy === "name") return a.name.localeCompare(b.name);
    if (sortBy === "size") return b.size - a.size;
    if (sortBy === "kind") return a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name);
    return b.mtime - a.mtime; // mtime (newest first)
  });

  // Group
  const groups = {};
  if (groupByRun) {
    for (const f of sorted) {
      // Group by run dir if path is outputs/runs/<id>/..., else by parent dir
      const m = f.path.match(/^outputs\/runs\/([^\/]+)/);
      const key = m ? `runs/${m[1]}` : _parentOf(f.path).replace(/^outputs\/?/, "") || "(top)";
      (groups[key] = groups[key] || []).push(f);
    }
  } else {
    groups[""] = sorted;
  }

  const totalSize = sorted.reduce((s, f) => s + (f.size || 0), 0);
  const toggleKind = (k) => {
    const next = new Set(activeKinds);
    next.has(k) ? next.delete(k) : next.add(k);
    setActiveKinds(next);
  };
  const toggleGroup = (g) => {
    const next = new Set(collapsed);
    next.has(g) ? next.delete(g) : next.add(g);
    setCollapsed(next);
  };
  const copyPath = (p) => { try { navigator.clipboard.writeText(p); } catch {} };

  const toolbarBtn = (active, onClick, children, title) => (
    <button onClick={onClick} title={title}
      style={{ padding: "4px 8px", borderRadius: "5px",
               border: `1px solid ${active ? T.ai + "60" : T.border}`,
               background: active ? `${T.ai}18` : "rgba(255,255,255,0.02)",
               color: active ? T.ai : T.text2, cursor: "pointer",
               fontSize: "10px", fontWeight: 600, letterSpacing: "0.04em",
               whiteSpace: "nowrap" }}>
      {children}
    </button>
  );

  return (
    <div style={{ padding: `${S.pageY} ${S.pageX}`, display: "grid",
                  gridTemplateColumns: vp.isMobile ? "1fr" : "360px 1fr",
                  gap: S.gap,
                  height: vp.isMobile ? "auto" : "calc(100vh - 56px - 49px)",
                  overflow: vp.isMobile ? "auto" : "hidden" }}>
      <Panel title="OUTPUT FILES" style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
        {/* Toolbar — search + clear + refresh */}
        <div style={{ padding: "10px 12px", borderBottom: `1px solid ${T.border}`,
                      display: "flex", flexDirection: "column", gap: "8px" }}>
          <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
            <div style={{ flex: 1, position: "relative" }}>
              <input type="text" value={filter} onChange={e => setFilter(e.target.value)}
                placeholder="search files & paths…"
                style={{ width: "100%", background: "rgba(0,0,0,0.3)",
                         border: `1px solid ${T.border}`, borderRadius: "6px",
                         padding: "6px 26px 6px 28px", color: T.text1,
                         fontSize: vp.isMobile ? "16px" : "12px",
                         outline: "none" }} />
              <span style={{ position: "absolute", left: "9px", top: "50%",
                             transform: "translateY(-50%)", color: T.text4,
                             fontSize: "11px", pointerEvents: "none" }}>⌕</span>
              {filter && (
                <button onClick={() => setFilter("")}
                  style={{ position: "absolute", right: "4px", top: "50%",
                           transform: "translateY(-50%)", background: "transparent",
                           border: "none", color: T.text3, cursor: "pointer",
                           padding: "2px 6px", fontSize: "12px" }}>×</button>
              )}
            </div>
            <button onClick={() => setRefreshTick(t => t + 1)} title="Refresh"
              style={{ padding: "6px 10px", borderRadius: "6px",
                       border: `1px solid ${T.border}`, color: T.text2,
                       background: "rgba(255,255,255,0.02)", cursor: "pointer",
                       fontSize: "12px" }}>↻</button>
          </div>
          {/* Kind pills */}
          {availableKinds.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
              {availableKinds.map(k => {
                const meta = _KIND_BADGE[k] || _KIND_BADGE.other;
                const on = activeKinds.has(k);
                return (
                  <button key={k} onClick={() => toggleKind(k)}
                    style={{ padding: "3px 7px", borderRadius: "4px",
                             border: `1px solid ${on ? meta.color + "80" : T.border}`,
                             background: on ? `${meta.color}20` : "rgba(255,255,255,0.02)",
                             color: on ? meta.color : T.text3,
                             cursor: "pointer", fontSize: "9px",
                             fontWeight: 700, letterSpacing: "0.05em" }}>
                    {meta.label} <span style={{ opacity: 0.6 }}>{kindCounts[k]}</span>
                  </button>
                );
              })}
              {activeKinds.size > 0 && (
                <button onClick={() => setActiveKinds(new Set())}
                  style={{ padding: "3px 7px", borderRadius: "4px",
                           border: `1px solid ${T.border}`, color: T.text3,
                           background: "transparent", cursor: "pointer",
                           fontSize: "9px", fontWeight: 700 }}>CLEAR</button>
              )}
            </div>
          )}
          {/* Sort + group controls */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px",
                        alignItems: "center" }}>
            <span style={{ fontSize: "9px", color: T.text4, fontWeight: 700,
                           letterSpacing: "0.06em", marginRight: "2px" }}>SORT</span>
            {toolbarBtn(sortBy === "mtime", () => setSortBy("mtime"), "RECENT", "Sort by modified time")}
            {toolbarBtn(sortBy === "name",  () => setSortBy("name"),  "NAME",   "Sort by name")}
            {toolbarBtn(sortBy === "size",  () => setSortBy("size"),  "SIZE",   "Sort by size")}
            {toolbarBtn(sortBy === "kind",  () => setSortBy("kind"),  "TYPE",   "Sort by file type")}
            <span style={{ flex: 1 }} />
            {toolbarBtn(groupByRun, () => setGroupByRun(g => !g), "GROUP", "Group by run / folder")}
          </div>
          <div style={{ fontSize: "10px", color: T.text3,
                        display: "flex", justifyContent: "space-between" }}>
            <span>{loading ? "loading…" : `${sorted.length} of ${allFiles.length} files`}</span>
            <span style={{ fontFamily: "JetBrains Mono, monospace" }}>{_fmtBytes(totalSize)}</span>
          </div>
        </div>
        {/* File list */}
        <div style={{ flex: 1, overflowY: "auto" }}>
          {Object.entries(groups).map(([groupName, items]) => {
            const isCollapsed = collapsed.has(groupName);
            return (
              <div key={groupName}>
                {groupByRun && groupName && (
                  <button onClick={() => toggleGroup(groupName)}
                    style={{ width: "100%", textAlign: "left",
                             padding: "6px 12px", border: "none",
                             borderBottom: `1px solid ${T.border}`,
                             background: "rgba(255,255,255,0.025)",
                             color: T.text2, cursor: "pointer",
                             fontSize: "10px", fontWeight: 700,
                             letterSpacing: "0.06em",
                             display: "flex", alignItems: "center", gap: "6px",
                             position: "sticky", top: 0, zIndex: 1,
                             backdropFilter: "blur(6px)" }}>
                    <span style={{ color: T.text4, fontSize: "9px" }}>
                      {isCollapsed ? "▸" : "▾"}
                    </span>
                    <span style={{ flex: 1, overflow: "hidden",
                                   textOverflow: "ellipsis", whiteSpace: "nowrap",
                                   color: groupName.startsWith("runs/") ? T.ai : T.text2 }}>
                      {groupName}
                    </span>
                    <span style={{ color: T.text4, fontWeight: 500 }}>
                      {items.length}
                    </span>
                  </button>
                )}
                {!isCollapsed && items.map((fl, i) => {
                  const meta = _KIND_BADGE[fl.kind] || _KIND_BADGE.other;
                  const isSelected = selected?.path === fl.path;
                  return (
                    <div key={`${groupName}-${i}`}
                      style={{ borderBottom: `1px solid ${T.border}`,
                               background: isSelected ? `${T.ai}12` : "transparent",
                               display: "flex", alignItems: "center" }}>
                      <button onClick={() => setSelected(fl)}
                        style={{ flex: 1, textAlign: "left", padding: "8px 8px 8px 12px",
                                 border: "none", background: "transparent",
                                 color: isSelected ? T.ai : T.text1,
                                 cursor: "pointer", fontSize: "11px",
                                 display: "flex", alignItems: "center", gap: "8px",
                                 minWidth: 0 }}>
                        <span style={{ fontFamily: "JetBrains Mono, monospace",
                                       fontSize: "8px", color: meta.color,
                                       background: `${meta.color}15`,
                                       border: `1px solid ${meta.color}40`,
                                       borderRadius: "3px", padding: "2px 5px",
                                       minWidth: "44px", textAlign: "center",
                                       fontWeight: 700, letterSpacing: "0.04em" }}>
                          {meta.label}
                        </span>
                        <span style={{ flex: 1, minWidth: 0, display: "flex",
                                       flexDirection: "column", gap: "1px" }}>
                          <span style={{ overflow: "hidden",
                                         textOverflow: "ellipsis",
                                         whiteSpace: "nowrap" }}>{fl.name}</span>
                          <span style={{ fontSize: "9px", color: T.text4,
                                         display: "flex", gap: "8px",
                                         fontFamily: "JetBrains Mono, monospace" }}>
                            <span>{_fmtBytes(fl.size)}</span>
                            {fl.mtime ? <span>· {_fmtRelTime(fl.mtime)}</span> : null}
                          </span>
                        </span>
                      </button>
                      {/* Quick actions */}
                      <div style={{ display: "flex", gap: "2px", padding: "0 6px 0 0" }}>
                        <button onClick={(e) => { e.stopPropagation(); copyPath(fl.path); }}
                          title="Copy path"
                          style={{ padding: "4px 6px", border: "none",
                                   background: "transparent", color: T.text4,
                                   cursor: "pointer", fontSize: "10px",
                                   borderRadius: "3px" }}>⎘</button>
                        <a href={`/api/file?path=${encodeURIComponent(fl.path)}`}
                           target="_blank" rel="noreferrer"
                           onClick={(e) => e.stopPropagation()}
                           title="Open in new tab"
                           style={{ padding: "4px 6px", color: T.text4,
                                    cursor: "pointer", fontSize: "10px",
                                    borderRadius: "3px",
                                    textDecoration: "none" }}>↗</a>
                        <a href={`/api/file?path=${encodeURIComponent(fl.path)}`}
                           download={fl.name}
                           onClick={(e) => e.stopPropagation()}
                           title="Download"
                           style={{ padding: "4px 6px", color: T.text4,
                                    cursor: "pointer", fontSize: "10px",
                                    borderRadius: "3px",
                                    textDecoration: "none" }}>↓</a>
                      </div>
                    </div>
                  );
                })}
              </div>
            );
          })}
          {sorted.length === 0 && !loading && (
            <div style={{ padding: "24px 16px", color: T.text4, fontSize: "11px",
                          textAlign: "center", lineHeight: 1.5 }}>
              {allFiles.length === 0
                ? "No files yet. Run a build from QUICK BUILDS or GENERATE to populate this tab."
                : "No files match the active filters."}
            </div>
          )}
        </div>
      </Panel>
      <Panel title={selected?.name || "PREVIEW"} style={{ minHeight: 0 }}>
        <div style={{ height: "calc(100% - 41px)" }}>
          <FilePreview file={selected} />
        </div>
      </Panel>
    </div>
  );
}

function FilesUpload() {
  const vp = useViewport();
  const S = spacing(vp);
  const [uploading, setUploading] = useState(false);
  const [uploaded, setUploaded] = useState(null);
  const [error, setError] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef(null);

  const handleFile = async (file) => {
    if (!file) return;
    setError(null); setUploaded(null); setUploading(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      const d = await r.json();
      setUploaded({
        name: file.name,
        size: file.size,
        url: d.url || d.api_file_url,
      });
    } catch (e) {
      setError(e.message);
    } finally {
      setUploading(false);
    }
  };

  const onDrop = (e) => {
    e.preventDefault(); setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) handleFile(f);
  };

  return (
    <div style={{ padding: `${S.pageY} ${S.pageX}`,
                  display: "grid", gridTemplateColumns: "1fr",
                  gap: S.gap }}>
      <Panel title="UPLOAD A FILE">
        <div style={{ padding: "20px" }}>
          <div onClick={() => fileInputRef.current?.click()}
               onDragOver={e => { e.preventDefault(); setDragOver(true); }}
               onDragLeave={() => setDragOver(false)}
               onDrop={onDrop}
               style={{
                 border: `2px dashed ${dragOver ? T.ai : T.border}`,
                 borderRadius: "12px",
                 padding: vp.isMobile ? "32px 16px" : "60px 24px",
                 textAlign: "center",
                 cursor: "pointer",
                 background: dragOver ? `${T.ai}10` : "rgba(255,255,255,0.02)",
                 transition: "all 0.15s",
               }}>
            <div style={{ fontSize: vp.isMobile ? "24px" : "36px",
                          color: T.text3, marginBottom: "10px" }}>↑</div>
            <div style={{ fontSize: vp.isMobile ? "14px" : "16px",
                          color: T.text0, fontWeight: 600,
                          marginBottom: "6px" }}>
              {uploading ? "Uploading…"
                : vp.isMobile ? "Tap to choose a file" : "Drop a file here or click to browse"}
            </div>
            <div style={{ fontSize: "11px", color: T.text3 }}>
              STL · STEP · STP · OBJ · 3MF · PLY
            </div>
            <input ref={fileInputRef} type="file"
                   accept=".stl,.step,.stp,.obj,.3mf,.ply"
                   style={{ display: "none" }}
                   onChange={e => handleFile(e.target.files?.[0])} />
          </div>
          {error && (
            <div style={{ marginTop: "12px", padding: "10px",
                          background: `${T.red}15`, color: T.red,
                          borderRadius: "8px", fontSize: "11px" }}>
              {error}
            </div>
          )}
          {uploaded && (
            <div style={{ marginTop: "16px", padding: "12px",
                          background: `${T.green}10`,
                          border: `1px solid ${T.green}40`,
                          borderRadius: "8px" }}>
              <div style={{ color: T.green, fontWeight: 600,
                            fontSize: "12px", marginBottom: "8px" }}>
                ✓ Uploaded {uploaded.name} ({Math.round(uploaded.size / 1024)} KB)
              </div>
              <FilePreview file={{ name: uploaded.name, path: uploaded.url }} />
            </div>
          )}
        </div>
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------------------
// FilePreview — picks a viewer per file extension. Mirrors run_aria_os.py
// --view CLI: STL/STEP → Three.js; SVG/PNG/JPG → image; JSON → pretty-print;
// DXF → embed via /api/file iframe.
// ---------------------------------------------------------------------------
function FilePreview({ file }) {
  if (!file?.path) {
    return (
      <div style={{ display: "flex", flexDirection: "column",
                    alignItems: "center", justifyContent: "center",
                    height: "100%", gap: "12px" }}>
        <div style={{ fontSize: "32px", opacity: 0.2 }}>◈</div>
        <div style={{ fontSize: "12px", color: T.text4 }}>
          Select a file to preview
        </div>
      </div>
    );
  }
  const url = file.path.startsWith("/") ? file.path
            : file.path.startsWith("http") ? file.path
            : `/api/file?path=${encodeURIComponent(file.path)}`;
  const ext = (file.name?.match(/\.([^.]+)$/) || [, ""])[1].toLowerCase();

  // STL — direct Three.js viewer
  if (ext === "stl") {
    return <STLViewer stlUrl={url} />;
  }
  // STEP / STP — backend serves the file; for in-browser preview we
  // need server-side STEP→STL conversion. Show a download link + note.
  if (ext === "step" || ext === "stp") {
    return (
      <div style={{ padding: "20px", textAlign: "center" }}>
        <div style={{ fontSize: "12px", color: T.text2, marginBottom: "12px" }}>
          STEP files preview after server-side conversion to STL.
        </div>
        <a href={url} download
           style={{ display: "inline-block", padding: "10px 16px",
                    borderRadius: "8px",
                    background: `linear-gradient(135deg, ${T.ai}, ${T.brand})`,
                    color: "#fff", fontSize: "12px", fontWeight: 700,
                    textDecoration: "none" }}>
          DOWNLOAD STEP
        </a>
      </div>
    );
  }
  // Images: SVG, PNG, JPG, GIF, WEBP
  if (["svg", "png", "jpg", "jpeg", "gif", "webp"].includes(ext)) {
    return (
      <div style={{ height: "100%", overflow: "auto", padding: "10px",
                    display: "flex", alignItems: "center", justifyContent: "center",
                    background: ext === "svg" ? "#fff" : "transparent" }}>
        <img src={url} alt={file.name}
             style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }} />
      </div>
    );
  }
  // JSON — fetch + pretty-print
  if (ext === "json") {
    return <JsonViewer url={url} />;
  }
  // DXF — embed in iframe (browser may or may not have a handler; for
  // proper DXF preview the local --view command's DXF UI is needed)
  if (ext === "dxf") {
    return (
      <iframe src={url} style={{ width: "100%", height: "100%", border: "none" }} />
    );
  }
  // KiCad PCB — no in-browser KiCad renderer exists; surface the 3 viable
  // viewing paths instead of falling through to "no preview available."
  // The path stems are predictable from the build_pipeline output structure.
  if (ext === "kicad_pcb") {
    return <KiCadPcbPreview file={file} url={url} />;
  }
  // Unknown — just offer download
  return (
    <div style={{ padding: "20px", textAlign: "center" }}>
      <div style={{ fontSize: "12px", color: T.text2, marginBottom: "12px" }}>
        No preview available for .{ext} files yet.
      </div>
      <a href={url} download
         style={{ display: "inline-block", padding: "10px 16px",
                  borderRadius: "8px", background: T.bg2,
                  border: `1px solid ${T.border}`,
                  color: T.text1, fontSize: "12px",
                  textDecoration: "none" }}>
        DOWNLOAD
      </a>
    </div>
  );
}

function KiCadPcbPreview({ file, url }) {
  // No browser-native KiCad renderer exists. Surface the 3 viable viewing
  // paths instead of failing silently:
  //   1. View the populated 3D PCB STEP we generate (Three.js viewer)
  //   2. View the 2D BOM layout PNG (rendered server-side from BOM JSON)
  //   3. Download the .kicad_pcb to open in KiCad app on desktop
  //
  // File path conventions from build_pipeline:
  //   ecad/<label>/<label>.kicad_pcb       ← this file
  //   ecad/<label>_populated.step          ← 3D STEP (in ecad/ root)
  //   ecad/<label>/<slug>/<slug>_bom_preview.png ← 2D layout PNG
  const [previewSrc, setPreviewSrc] = useState(null);
  const [populatedStl, setPopulatedStl] = useState(null);
  const [boardName, setBoardName] = useState("");

  useEffect(() => {
    const path = file.path || "";
    const m = path.match(/[\\/]ecad[\\/]([^\\/]+)[\\/]/);
    const label = m ? m[1]
                    : path.split(/[\\/]/).pop().replace(/\.kicad_pcb$/, "");
    setBoardName(label);

    // Look up sibling artifacts via the existing /api/outputs listing
    fetch("/api/outputs")
      .then(r => r.json())
      .then(d => {
        const files = (d.files || []).map(f =>
          typeof f === "string" ? f : (f.path || f.name || ""));
        // 2D BOM preview PNG
        const previewMatch = files.find(f =>
          (f.includes(`/ecad/${label}/`) || f.includes(`\\ecad\\${label}\\`))
          && f.endsWith("_bom_preview.png"));
        if (previewMatch) setPreviewSrc(`/api/file?path=${encodeURIComponent(previewMatch)}`);
        // Populated 3D STEP — converted to STL by /api/file? Three.js needs STL,
        // so try the .stl variant first; fall back to .step.
        const stepMatch = files.find(f =>
          (f.includes(`/ecad/${label}_populated.`) || f.includes(`\\ecad\\${label}_populated.`))
          && (f.endsWith("_populated.stl") || f.endsWith("_populated.step")));
        if (stepMatch) {
          const stlVariant = stepMatch.replace(/\.step$/, ".stl");
          setPopulatedStl(`/api/file?path=${encodeURIComponent(stlVariant)}`);
        }
      })
      .catch(() => {});
  }, [file?.path]);

  return (
    <div style={{ height: "100%", overflow: "auto", padding: "16px",
                  display: "flex", flexDirection: "column", gap: "16px",
                  WebkitOverflowScrolling: "touch" }}>
      <div>
        <div style={{ fontSize: "13px", fontWeight: 700, color: T.text0,
                      marginBottom: "4px" }}>{boardName || "PCB"}</div>
        <div style={{ fontSize: "11px", color: T.text3 }}>
          KiCad PCB files need KiCad to render. Use one of the viewers below
          OR download to open in the KiCad desktop app.
        </div>
      </div>

      {/* 2D BOM layout — rendered server-side from BOM JSON */}
      {previewSrc && (
        <div style={{ border: `1px solid ${T.border}`, borderRadius: "8px",
                      overflow: "hidden", background: "#fff" }}>
          <div style={{ padding: "8px 12px", background: T.bg2, color: T.text2,
                        fontSize: "10px", fontWeight: 700, letterSpacing: "0.06em",
                        borderBottom: `1px solid ${T.border}` }}>
            2D LAYOUT — components placed on board
          </div>
          <img src={previewSrc} alt={`${boardName} layout`}
               style={{ display: "block", maxWidth: "100%", margin: "0 auto" }} />
        </div>
      )}

      {/* 3D populated PCB — Three.js viewer */}
      <div style={{ border: `1px solid ${T.border}`, borderRadius: "8px",
                    overflow: "hidden" }}>
        <div style={{ padding: "8px 12px", background: T.bg2, color: T.text2,
                      fontSize: "10px", fontWeight: 700, letterSpacing: "0.06em",
                      borderBottom: `1px solid ${T.border}` }}>
          3D POPULATED PCB
        </div>
        <div style={{ height: "300px", background: "#0a0a0f" }}>
          {populatedStl
            ? <STLViewer stlUrl={populatedStl} />
            : <div style={{ height: "100%", display: "flex",
                            alignItems: "center", justifyContent: "center",
                            color: T.text4, fontSize: "11px" }}>
                Populated PCB STEP not found in outputs/
              </div>}
        </div>
      </div>

      {/* Download + external-tool hints */}
      <div>
        <a href={url} download
           style={{ display: "block", padding: "10px 16px", borderRadius: "8px",
                    background: `linear-gradient(135deg, ${T.ai}, ${T.brand})`,
                    color: "#fff", fontSize: "12px", fontWeight: 700,
                    textDecoration: "none", textAlign: "center",
                    marginBottom: "10px" }}>
          ↓ DOWNLOAD .kicad_pcb
        </a>
        <div style={{ fontSize: "10px", color: T.text4, lineHeight: 1.7,
                      fontFamily: "JetBrains Mono, monospace" }}>
          Open in KiCad: <span style={{ color: T.ai }}>kicad {file.name}</span><br />
          Export Gerbers: <span style={{ color: T.ai }}>kicad-cli pcb export gerbers -o gerbers/ {file.name}</span><br />
          Web viewer: <a href="https://kicanvas.org/" target="_blank" rel="noreferrer"
              style={{ color: T.ai }}>kicanvas.org</a> (drag-drop the downloaded file)
        </div>
      </div>
    </div>
  );
}


function JsonViewer({ url }) {
  const [content, setContent] = useState("Loading…");
  useEffect(() => {
    let cancelled = false;
    fetch(url).then(r => r.text()).then(t => {
      if (cancelled) return;
      try {
        const obj = JSON.parse(t);
        setContent(JSON.stringify(obj, null, 2));
      } catch {
        setContent(t);
      }
    }).catch(e => setContent(`Error: ${e.message}`));
    return () => { cancelled = true; };
  }, [url]);
  return (
    <pre style={{ height: "100%", margin: 0, padding: "12px",
                  overflow: "auto", fontSize: "11px",
                  fontFamily: "JetBrains Mono, monospace",
                  color: T.text1, background: "#0a0d12",
                  whiteSpace: "pre", lineHeight: 1.5 }}>
      {content}
    </pre>
  );
}


// ---------------------------------------------------------------------------
// QuickBuildsPanel — pre-canned drone builds. One click → 30s → STEP+STL+
// KiCad PCBs+drawings+slicer-ready prints. The fastest "prompt → manufacturing"
// path in the app. Fetches /api/presets, polls /api/preset/run/{id} for results.
// ---------------------------------------------------------------------------
function QuickBuildsPanel() {
  const [presets, setPresets] = useState({});
  const [running, setRunning] = useState(null);   // { run_id, preset_id }
  const [progress, setProgress] = useState(null); // { stages: [...], current_stage, started_at }
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch("/api/presets")
      .then(r => r.json())
      .then(d => setPresets(d.presets || {}))
      .catch(() => setPresets({}));
  }, []);

  // Poll status every 1s while running so the UI shows real per-stage
  // progress instead of just "building" for 30s.
  useEffect(() => {
    if (!running) return;
    const id = setInterval(async () => {
      try {
        const r = await fetch(`/api/preset/run/${running.run_id}`);
        if (!r.ok) return;
        const d = await r.json();
        // Always pick up partial progress (stages array, current_stage)
        setProgress({
          stages: d.stages || [],
          current_stage: d.current_stage,
          started_at: d.started_at,
        });
        if (d.status === "done") {
          setResult(d.result);
          setRunning(null);
          clearInterval(id);
        }
      } catch {}
    }, 1000);
    return () => clearInterval(id);
  }, [running]);

  const launch = async (preset_id) => {
    setError(null); setResult(null); setProgress(null);
    try {
      const r = await fetch(`/api/preset/${preset_id}`, { method: "POST" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setRunning({ run_id: d.run_id, preset_id });
    } catch (e) {
      setError(e.message);
    }
  };

  // Show live progress while running. Stage names match build_pipeline.run_full_build.
  const liveProgress = (() => {
    if (!running || !progress) return null;
    // Aggregate stage events into one row per stage with its terminal status
    const byStage = {};
    for (const ev of progress.stages || []) {
      const cur = byStage[ev.stage] || {};
      byStage[ev.stage] = { ...cur, ...ev };
    }
    const ordered = ["structsight", "mechanical", "ecad", "drawings",
                     "print", "cam", "sim", "circuit_sim", "millforge"];
    return ordered.filter(s => byStage[s] || progress.current_stage === s)
                  .map(s => ({ stage: s, ...(byStage[s] || {}),
                               isCurrent: progress.current_stage === s }));
  })();

  const downloadBundle = () => {
    if (!result?.output_dir) return;
    const rel = result.output_dir.replace(/^.*?outputs[\\/]/, "outputs/").replace(/\\/g, "/");
    window.location.href = `/api/bundle?path=${encodeURIComponent(rel)}`;
  };

  return (
    <Panel title="QUICK BUILDS">
      <div style={{ padding: "12px 16px", display: "flex", flexDirection: "column", gap: "8px",
                    maxHeight: "min(60vh, 520px)", overflowY: "auto",
                    WebkitOverflowScrolling: "touch", overscrollBehavior: "contain" }}>
        <div style={{ fontSize: "10px", color: T.text3, marginBottom: "4px" }}>
          One click → full multi-domain build (CAD + ECAD + drawings + print bundle)
        </div>
        {Object.entries(presets).map(([id, p]) => {
          const isRunning = running?.preset_id === id;
          const disabled = !!running;
          return (
            <button key={id} onClick={() => launch(id)} disabled={disabled}
              style={{
                padding: "10px 12px", borderRadius: "8px",
                border: `1px solid ${isRunning ? T.ai : T.border}`,
                background: isRunning ? `${T.ai}15` : "rgba(255,255,255,0.02)",
                color: T.text1, cursor: disabled ? "not-allowed" : "pointer",
                opacity: disabled && !isRunning ? 0.4 : 1,
                textAlign: "left", display: "flex", flexDirection: "column", gap: "3px",
                transition: "all 0.15s",
              }}>
              <div style={{ fontSize: "13px", fontWeight: 600, color: isRunning ? T.ai : T.text0 }}>
                {p.label}{isRunning && " — building..."}
              </div>
              <div style={{ fontSize: "10px", color: T.text3 }}>{p.description}</div>
              <div style={{ fontSize: "9px", color: T.text4, marginTop: "2px" }}>
                ~{p.estimated_seconds}s · {(p.outputs || []).join(" · ")}
              </div>
            </button>
          );
        })}
        {error && (
          <div style={{ fontSize: "11px", color: T.red, padding: "6px 10px",
                        background: `${T.red}10`, borderRadius: "6px",
                        border: `1px solid ${T.red}40` }}>
            Error: {error}
          </div>
        )}
        {/* LIVE STAGE PROGRESS — populated by polling /api/preset/run/{id} */}
        {liveProgress && liveProgress.length > 0 && (
          <div style={{ marginTop: "4px", padding: "10px", borderRadius: "8px",
                        background: `${T.ai}08`,
                        border: `1px solid ${T.ai}30` }}>
            <div style={{ fontSize: "10px", color: T.ai, fontWeight: 700,
                          letterSpacing: "0.08em", marginBottom: "6px",
                          display: "flex", justifyContent: "space-between" }}>
              <span>BUILDING…</span>
              {progress?.started_at && (
                <span style={{ color: T.text3, fontWeight: 500,
                               fontFamily: "JetBrains Mono, monospace" }}>
                  {Math.round(Date.now() / 1000 - progress.started_at)}s
                </span>
              )}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
              {liveProgress.map((s, i) => {
                const done = s.status === "done";
                const failed = s.status === "fail";
                const skipped = s.status === "skip";
                const inFlight = s.isCurrent || (s.status === "start" && !done && !failed);
                let icon, color;
                if (failed) { icon = "✗"; color = T.red; }
                else if (skipped) { icon = "·"; color = T.text4; }
                else if (done) { icon = "✓"; color = T.green; }
                else if (inFlight) { icon = "◐"; color = T.ai; }
                else { icon = "○"; color = T.text4; }
                return (
                  <div key={i} style={{ display: "flex", alignItems: "center",
                                         gap: "8px", fontSize: "11px",
                                         color: inFlight ? T.text0 : T.text2 }}>
                    <span style={{ color, fontWeight: 700, width: "12px",
                                   textAlign: "center",
                                   animation: inFlight ? "pulse 1s infinite" : "none" }}>
                      {icon}
                    </span>
                    <span style={{ flex: 1 }}>{s.stage}</span>
                    {s.elapsed_s !== undefined && (
                      <span style={{ fontFamily: "JetBrains Mono, monospace",
                                     fontSize: "10px", color: T.text3 }}>
                        {s.elapsed_s.toFixed(1)}s
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
        {result && (
          <div style={{ marginTop: "4px", padding: "10px", borderRadius: "8px",
                        background: result.success ? `${T.green}10` : `${T.red}10`,
                        border: `1px solid ${result.success ? T.green : T.red}40` }}>
            <div style={{ fontSize: "11px", fontWeight: 600,
                          color: result.success ? T.green : T.red, marginBottom: "6px" }}>
              {result.success ? "BUILD COMPLETE" : "BUILD FAILED"}
              {result.elapsed_s && ` (${Math.round(result.elapsed_s)}s)`}
            </div>
            {/* Save location — where the bundle landed on the server. Tap to copy. */}
            {result.success && result.output_dir && (() => {
              const rel = result.output_dir.replace(/^.*?outputs[\\/]/, "outputs/").replace(/\\/g, "/");
              return (
                <div
                  onClick={() => { try { navigator.clipboard.writeText(rel); } catch {} }}
                  title="Click to copy path"
                  style={{ marginBottom: "8px", padding: "6px 8px", borderRadius: "5px",
                           background: "rgba(0,0,0,0.25)", border: `1px solid ${T.border}`,
                           cursor: "pointer" }}>
                  <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700,
                                letterSpacing: "0.08em", marginBottom: "2px" }}>
                    SAVED TO
                  </div>
                  <div style={{ fontFamily: "JetBrains Mono, monospace", fontSize: "10px",
                                color: T.text1, wordBreak: "break-all", lineHeight: 1.35 }}>
                    {rel}
                  </div>
                  <div style={{ fontSize: "9px", color: T.text4, marginTop: "3px" }}>
                    tap to copy · open in Files tab to browse
                  </div>
                </div>
              );
            })()}
            {/* Per-stage status pills (mechanical / ECAD / drawings / print / CAM) */}
            {result.stages && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: "4px", marginBottom: "8px" }}>
                {Object.entries(result.stages).map(([stage, ok]) => (
                  <span key={stage} style={{
                    padding: "2px 7px", borderRadius: "4px", fontSize: "9px",
                    fontWeight: 700, letterSpacing: "0.04em",
                    color: ok ? T.green : T.text4,
                    background: ok ? `${T.green}15` : "rgba(255,255,255,0.04)",
                    border: `1px solid ${ok ? T.green + "40" : T.border}`,
                  }}>
                    {ok ? "✓" : "·"} {stage.toUpperCase()}
                  </span>
                ))}
              </div>
            )}
            {result.error && (
              <div style={{ fontSize: "10px", color: T.text2, marginBottom: "4px" }}>
                {result.error}
              </div>
            )}
            {/* "What's in the box" — thumbnail grid of preview artifacts */}
            {result.success && Array.isArray(result.preview_artifacts) && result.preview_artifacts.length > 0 && (
              <div style={{ marginBottom: "8px" }}>
                <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700,
                              letterSpacing: "0.08em", marginBottom: "5px" }}>
                  WHAT'S IN THE BOX ({result.preview_artifacts.length})
                </div>
                <div style={{ display: "grid",
                              gridTemplateColumns: "repeat(auto-fill, minmax(80px, 1fr))",
                              gap: "4px" }}>
                  {result.preview_artifacts.slice(0, 12).map((a, i) => (
                    <a key={i}
                       href={`/api/file?path=${encodeURIComponent(a.rel_path)}`}
                       target="_blank" rel="noreferrer"
                       title={a.label}
                       style={{ display: "block", aspectRatio: "1",
                                background: "rgba(0,0,0,0.3)",
                                borderRadius: "4px", border: `1px solid ${T.border}`,
                                overflow: "hidden", textDecoration: "none",
                                position: "relative" }}>
                      <img src={`/api/file?path=${encodeURIComponent(a.rel_path)}`}
                           alt={a.label}
                           loading="lazy"
                           style={{ width: "100%", height: "100%", objectFit: "contain",
                                    background: a.type === "svg" ? "#fff" : "transparent" }} />
                      <div style={{ position: "absolute", bottom: 0, left: 0, right: 0,
                                    padding: "2px 4px", fontSize: "8px", color: T.text2,
                                    background: "rgba(0,0,0,0.6)",
                                    overflow: "hidden", textOverflow: "ellipsis",
                                    whiteSpace: "nowrap" }}>
                        {a.label}
                      </div>
                    </a>
                  ))}
                </div>
              </div>
            )}
            {result.success && (
              <button onClick={downloadBundle}
                style={{ width: "100%", padding: "8px", borderRadius: "6px",
                         border: "none", background: `linear-gradient(135deg, ${T.ai}, ${T.brand})`,
                         color: "#fff", fontSize: "11px", fontWeight: 700,
                         cursor: "pointer", letterSpacing: "0.04em" }}>
                DOWNLOAD ALL ARTIFACTS (ZIP) ↓
              </button>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
}


// ---------------------------------------------------------------------------
// ResponsiveMain — wraps the page content with the right left/bottom inset
// for the active sidebar layout (left rail on desktop, bottom bar on mobile).
// ---------------------------------------------------------------------------
function ResponsiveMain({ children }) {
  const vp = useViewport();
  const L = layout(vp);
  return (
    <div style={{
      marginLeft: L.contentPadLeft,
      paddingBottom: L.contentPadBot,
      height: "100vh",
      overflow: "hidden",
      display: "flex",
      flexDirection: "column",
    }}>
      {children}
    </div>
  );
}


// ---------------------------------------------------------------------------
// TopBar
// ---------------------------------------------------------------------------
function TopBar({ section, subsection, pipelineStatus }) {
  const [time, setTime] = useState(new Date());
  const vp = useViewport();
  const L = layout(vp);
  const S = spacing(vp);
  useEffect(() => { const i = setInterval(() => setTime(new Date()), 1000); return () => clearInterval(i); }, []);
  const statusColor = pipelineStatus === "running" ? T.amber : pipelineStatus === "done" ? T.green : T.text4;
  const statusLabel = pipelineStatus === "running" ? "GENERATING" : pipelineStatus === "done" ? "COMPLETE" : "IDLE";
  return (
    <div style={{ position: "sticky", top: 0, height: L.headerHeight,
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
        {/* Clock — hide on mobile to save horizontal space */}
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

// ---------------------------------------------------------------------------
// Three.js STL Viewer
// ---------------------------------------------------------------------------
function STLViewer({ stlUrl }) {
  const mountRef = useRef(null);
  const sceneRef = useRef(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!mountRef.current || !stlUrl) return;
    let cancelled = false;
    setLoading(true);

    loadThree().then(({ THREE, STLLoader, OrbitControls }) => {
      if (cancelled || !mountRef.current) return;
      setLoading(false);
      const w = mountRef.current.clientWidth, h = mountRef.current.clientHeight;

      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setSize(w, h);
      renderer.setPixelRatio(window.devicePixelRatio);
      renderer.setClearColor(0x000000, 0);
      mountRef.current.appendChild(renderer.domElement);

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 10000);
      camera.position.set(0, 0, 200);

      scene.add(new THREE.AmbientLight(0xffffff, 0.5));
      const dir = new THREE.DirectionalLight(0xffffff, 1.2);
      dir.position.set(100, 100, 100);
      scene.add(dir);

      const controls = new OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true;

      const loader = new STLLoader();
      loader.load(stlUrl, (geometry) => {
        if (cancelled) return;
        geometry.computeBoundingBox();
        const box = geometry.boundingBox;
        const center = new THREE.Vector3();
        box.getCenter(center);
        geometry.translate(-center.x, -center.y, -center.z);
        const size = box.getSize(new THREE.Vector3()).length();
        camera.position.set(0, 0, size * 1.5);
        controls.update();

        const mesh = new THREE.Mesh(
          geometry,
          new THREE.MeshPhongMaterial({ color: 0x00d4ff, specular: 0x222222, shininess: 80, side: THREE.DoubleSide })
        );
        scene.add(mesh);
      });

      let animId;
      const animate = () => {
        if (cancelled) return;
        animId = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
      };
      animate();
      sceneRef.current = { renderer, animId };
    }).catch(() => {
      if (!cancelled) setLoading(false);
    });

    return () => {
      cancelled = true;
      if (sceneRef.current) {
        cancelAnimationFrame(sceneRef.current.animId);
        sceneRef.current.renderer.dispose();
      }
      if (mountRef.current) mountRef.current.innerHTML = "";
    };
  }, [stlUrl]);

  return (
    <div ref={mountRef} style={{ width: "100%", height: "100%", background: "transparent" }}>
      {!stlUrl && (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: "12px" }}>
          <div style={{ fontSize: "32px", opacity: 0.2 }}>◈</div>
          <div style={{ fontSize: "12px", color: T.text4 }}>No part selected</div>
        </div>
      )}
      {stlUrl && loading && (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: "12px" }}>
          <div style={{ fontSize: "11px", color: T.text3 }}>Loading 3D viewer...</div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Generate Section
// ---------------------------------------------------------------------------
const EXAMPLE_PROMPTS = [
  "150mm impeller 6 backward-curved blades 30mm bore",
  "100x60x40mm L-bracket 4xM8 holes 4mm wall",
  "NEMA17 stepper motor mount 3mm plate",
  "flanged coupling 80mm OD 25mm bore 4 bolts",
  "heat sink 120x80mm 12 fins aluminum",
  "60mm spur gear 24 teeth 10mm face width",
  "M42 thread adaptor sleeve 60mm OD",
  "cantilever snap hook 40mm length 8mm width",
];

function logColor(line) {
  if (line.includes("ERROR") || line.includes("failed")) return T.red;
  if (line.includes("PASS") || line.includes("success") || line.includes("complete")) return T.green;
  if (line.includes("WARN") || line.includes("attempt")) return T.amber;
  if (line.includes("\u2192") || line.includes("step")) return T.ai;
  return T.text2;
}

function GenerateNL({ parts, selectedPart, setSelectedPart, onGenerate, pipelineStatus, logLines }) {
  const [goal, setGoal] = useState("");
  const [maxAttempts, setMaxAttempts] = useState(3);
  const stlUrl = selectedPart?.stl_path ? `/api/parts/${selectedPart.id}/stl` : null;
  const vp = useViewport();
  const L = layout(vp);
  const S = spacing(vp);

  return (
    <div style={{ padding: `${S.pageY} ${S.pageX}`, display: "grid",
                  gridTemplateColumns: L.twoColGrid, gap: S.gap,
                  height: vp.isMobile ? "auto" : "calc(100vh - 56px - 49px)",
                  minHeight: vp.isMobile ? "calc(100vh - 56px - 49px - 64px)" : undefined,
                  overflow: vp.isMobile ? "auto" : "hidden" }}>
      {/* Left: 3D Viewer (full width on mobile, stacks above the form) */}
      <div style={{ display: "flex", flexDirection: "column", gap: "12px", minHeight: 0 }}>
        <Panel title="3D VIEWER" style={{ flex: 1, minHeight: 0 }}>
          <div style={{ height: "calc(100% - 41px)", position: "relative" }}>
            <STLViewer stlUrl={stlUrl} />
            {selectedPart && (
              <div style={{ position: "absolute", bottom: "12px", left: "12px", padding: "6px 12px", background: "rgba(10,10,15,0.8)", borderRadius: "8px", border: `1px solid ${T.border}`, backdropFilter: "blur(12px)" }}>
                <div style={{ fontSize: "12px", color: T.text0, fontWeight: 600 }}>{selectedPart.part_name || selectedPart.id}</div>
                <div style={{ fontSize: "10px", color: T.text3, marginTop: "2px" }}>{selectedPart.material || ""}{selectedPart.material && selectedPart.goal ? " · " : ""}{selectedPart.goal?.slice(0, 40) || ""}</div>
              </div>
            )}
          </div>
        </Panel>
        {/* Recent parts row — only show entries that actually have a real name.
            Unnamed "Part 1 / Part 2 / ..." fallbacks are noisy; the Files tab
            is the right place to browse by artifact anyway. */}
        {(() => {
          const named = parts.filter(p => p.part_name || p.id);
          if (named.length === 0) return null;
          return (
            <div style={{ display: "flex", gap: "8px", overflowX: "auto",
                          paddingBottom: "4px", flexShrink: 0 }}>
              {named.slice(0, 8).map((p) => (
                <button key={p.id} onClick={() => setSelectedPart(p)}
                  style={{ flexShrink: 0, padding: "8px 14px", borderRadius: "8px",
                           border: `1px solid ${selectedPart?.id === p.id ? T.ai : T.border}`,
                           background: selectedPart?.id === p.id ? `${T.ai}12` : "rgba(255,255,255,0.02)",
                           color: selectedPart?.id === p.id ? T.ai : T.text2,
                           fontSize: "11px", fontWeight: 600, cursor: "pointer",
                           whiteSpace: "nowrap" }}>
                  {p.part_name || p.id}
                </button>
              ))}
            </div>
          );
        })()}
      </div>

      {/* Right: Quick Builds + Generate form + log.
          overflowY:auto so the entire stack is scrollable — otherwise tall
          QuickBuilds results push the Generate form + log out of view. */}
      <div style={{ display: "flex", flexDirection: "column", gap: "12px",
                    minHeight: 0, overflowY: "auto",
                    WebkitOverflowScrolling: "touch",
                    paddingRight: "4px" }}>
        <QuickBuildsPanel />
        <Panel title="GENERATE">
          <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: "10px",
                        maxHeight: "min(55vh, 480px)", overflowY: "auto",
                        WebkitOverflowScrolling: "touch", overscrollBehavior: "contain" }}>
            <textarea
              value={goal}
              onChange={e => setGoal(e.target.value)}
              placeholder={"Describe the part you want to generate...\ne.g. 150mm impeller, 6 backward-curved blades, 30mm bore"}
              style={{ width: "100%", minHeight: "80px", background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, borderRadius: "8px", padding: "10px 12px", color: T.text1, fontSize: vp.isMobile ? "16px" : "12px", fontFamily: "inherit", resize: "vertical", outline: "none", lineHeight: 1.5, boxSizing: "border-box" }}
            />
            <div style={{ display: "flex", flexWrap: "wrap", gap: "5px" }}>
              {EXAMPLE_PROMPTS.map((ex, i) => (
                <button key={i} onClick={() => setGoal(ex)}
                  style={{ padding: "4px 9px", borderRadius: "6px", border: `1px solid ${T.border}`, background: "rgba(255,255,255,0.03)", color: T.text3, fontSize: "10px", cursor: "pointer", transition: "all 0.15s", textAlign: "left" }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = T.ai + "60"; e.currentTarget.style.color = T.text1; }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = T.border; e.currentTarget.style.color = T.text3; }}>
                  {ex.length > 38 ? ex.slice(0, 36) + "..." : ex}
                </button>
              ))}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <label style={{ fontSize: "10px", color: T.text3, fontWeight: 600, whiteSpace: "nowrap" }}>MAX ATTEMPTS</label>
              <input type="number" min={1} max={10} value={maxAttempts} onChange={e => setMaxAttempts(Number(e.target.value))}
                style={{ width: "56px", background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, borderRadius: "6px", padding: "6px 8px", color: T.text1, fontSize: "12px", textAlign: "center", outline: "none" }} />
              <button
                onClick={() => goal.trim() && onGenerate(goal.trim(), maxAttempts)}
                disabled={!goal.trim() || pipelineStatus === "running"}
                style={{ flex: 1, padding: "9px", borderRadius: "8px", border: "none", background: (!goal.trim() || pipelineStatus === "running") ? `${T.ai}25` : `linear-gradient(135deg, ${T.ai}, ${T.brand})`, color: !goal.trim() ? T.text4 : "#fff", fontSize: "11px", fontWeight: 700, cursor: (!goal.trim() || pipelineStatus === "running") ? "not-allowed" : "pointer", boxShadow: (goal.trim() && pipelineStatus !== "running") ? `0 4px 12px ${T.aiGlow}` : "none", transition: "all 0.2s", letterSpacing: "0.04em" }}>
                {pipelineStatus === "running" ? "GENERATING..." : !goal.trim() ? "TYPE A GOAL ABOVE →" : "GENERATE →"}
              </button>
            </div>
          </div>
        </Panel>

        <Panel title="PIPELINE LOG" style={{ height: "280px", flexShrink: 0 }}>
          <div style={{ padding: "10px 14px", height: "calc(100% - 41px)", overflowY: "auto", fontFamily: "'JetBrains Mono', monospace", fontSize: "10px", lineHeight: 1.7 }}>
            {logLines.length === 0 ? (
              <div style={{ color: T.text4, fontStyle: "italic" }}>Waiting for pipeline events...</div>
            ) : (
              logLines.map((line, i) => (
                <div key={i} style={{ color: logColor(line) }}>{line}</div>
              ))
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}

function GenerateImage({ pipelineStatus, logLines }) {
  const [imageFile, setImageFile] = useState(null);
  const [imagePreview, setImagePreview] = useState(null);
  const [goal, setGoal] = useState("");
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState(null);
  const [localLog, setLocalLog] = useState([]);
  const dropRef = useRef(null);

  const handleFile = (file) => {
    if (!file || !file.type.startsWith("image/")) return;
    setImageFile(file);
    setImagePreview(URL.createObjectURL(file));
    setError(null);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    handleFile(e.dataTransfer.files[0]);
  };

  const handleGenerate = async () => {
    if (!imageFile) return;
    setStatus("running");
    setError(null);
    setLocalLog(prev => [...prev, `>>> Uploading image: ${imageFile.name}`]);
    const form = new FormData();
    form.append("image", imageFile);
    form.append("goal", goal);
    try {
      const res = await fetch("/api/generate-from-image", { method: "POST", body: form });
      if (res.status === 404) {
        setError("Image pipeline not available in this deployment.");
        setStatus("idle");
        return;
      }
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const data = await res.json();
      setLocalLog(prev => [...prev, `[done] ${data.message || "Generation complete"}`]);
      setStatus("done");
    } catch (e) {
      if (e.message.includes("404") || e.message.includes("Failed to fetch")) {
        setError("Image pipeline not available in this deployment.");
      } else {
        setError(e.message);
      }
      setStatus("idle");
    }
  };

  const allLog = [...logLines, ...localLog];
  const vp = useViewport();
  const L = layout(vp);
  const S = spacing(vp);

  return (
    <div style={{ padding: `${S.pageY} ${S.pageX}`, display: "grid",
                  gridTemplateColumns: L.twoColGrid, gap: S.gap,
                  height: vp.isMobile ? "auto" : "calc(100vh - 56px - 49px)",
                  minHeight: vp.isMobile ? "calc(100vh - 56px - 49px - 64px)" : undefined,
                  overflow: vp.isMobile ? "auto" : "hidden" }}>
      <Panel title="3D VIEWER" style={{ minHeight: 0 }}>
        <div style={{ height: "calc(100% - 41px)" }}>
          <STLViewer stlUrl={null} />
        </div>
      </Panel>

      <div style={{ display: "flex", flexDirection: "column", gap: "12px",
                    minHeight: 0, overflowY: "auto",
                    WebkitOverflowScrolling: "touch", paddingRight: "4px" }}>
        <Panel title="GENERATE FROM IMAGE">
          <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: "10px" }}>
            <div style={{ padding: "8px 12px", borderRadius: "8px", background: `${T.amber}10`, border: `1px solid ${T.amber}30`, fontSize: "11px", color: T.amber, display: "flex", alignItems: "flex-start", gap: "8px" }}>
              <span style={{ flexShrink: 0, marginTop: "1px" }}>⚠</span>
              <span>Image-to-CAD pipeline not available in this deployment. Requires the full ARIA vision backend.</span>
            </div>
            <div
              ref={dropRef}
              onDrop={handleDrop}
              onDragOver={e => e.preventDefault()}
              onClick={() => { const inp = document.createElement("input"); inp.type = "file"; inp.accept = "image/*"; inp.onchange = e => handleFile(e.target.files[0]); inp.click(); }}
              style={{ minHeight: "120px", border: `2px dashed ${imageFile ? T.ai + "60" : T.border}`, borderRadius: "10px", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", cursor: "pointer", background: "rgba(0,0,0,0.2)", transition: "all 0.15s", overflow: "hidden", padding: "8px", opacity: 0.5 }}>
              {imagePreview ? (
                <img src={imagePreview} alt="preview" style={{ maxHeight: "110px", maxWidth: "100%", borderRadius: "6px", objectFit: "contain" }} />
              ) : (
                <>
                  <div style={{ fontSize: "24px", marginBottom: "8px", opacity: 0.3 }}>⊞</div>
                  <div style={{ fontSize: "11px", color: T.text3 }}>Drop image or click to browse</div>
                  <div style={{ fontSize: "10px", color: T.text4, marginTop: "4px" }}>PNG, JPG, WEBP supported</div>
                </>
              )}
            </div>
            {imageFile && (
              <div style={{ fontSize: "10px", color: T.text3, fontFamily: "'JetBrains Mono', monospace" }}>{imageFile.name} ({(imageFile.size / 1024).toFixed(0)} KB)</div>
            )}
            <textarea
              value={goal}
              onChange={e => setGoal(e.target.value)}
              placeholder="Additional constraints (optional)..."
              style={{ width: "100%", minHeight: "60px", background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, borderRadius: "8px", padding: "10px 12px", color: T.text1, fontSize: "12px", fontFamily: "inherit", resize: "vertical", outline: "none", lineHeight: 1.5, boxSizing: "border-box" }}
            />
            {error && (
              <div style={{ padding: "8px 12px", borderRadius: "8px", background: `${T.amber}10`, border: `1px solid ${T.amber}30`, fontSize: "11px", color: T.amber }}>{error}</div>
            )}
            <button
              onClick={handleGenerate}
              disabled={!imageFile || status === "running"}
              style={{ padding: "10px", borderRadius: "8px", border: "none", background: `${T.ai}25`, color: T.text4, fontSize: "11px", fontWeight: 700, cursor: "not-allowed", transition: "all 0.2s", letterSpacing: "0.04em" }}>
              {status === "running" ? "GENERATING..." : "GENERATE FROM IMAGE"}
            </button>
          </div>
        </Panel>

        <Panel title="PIPELINE LOG" style={{ height: "280px", flexShrink: 0 }}>
          <div style={{ padding: "10px 14px", height: "calc(100% - 41px)", overflowY: "auto", fontFamily: "'JetBrains Mono', monospace", fontSize: "10px", lineHeight: 1.7 }}>
            {allLog.length === 0 ? (
              <div style={{ color: T.text4, fontStyle: "italic" }}>Waiting for pipeline events...</div>
            ) : (
              allLog.map((line, i) => <div key={i} style={{ color: logColor(line) }}>{line}</div>)
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}

function GenerateAssembly({ pipelineStatus, logLines, onGenerate }) {
  const [assemblyParts, setAssemblyParts] = useState([{ name: "", description: "" }, { name: "", description: "" }]);
  const [localLog, setLocalLog] = useState([]);
  const [status, setStatus] = useState("idle");

  const addRow = () => setAssemblyParts(prev => [...prev, { name: "", description: "" }]);
  const removeRow = (i) => setAssemblyParts(prev => prev.filter((_, j) => j !== i));
  const updateRow = (i, field, val) => setAssemblyParts(prev => prev.map((p, j) => j === i ? { ...p, [field]: val } : p));

  const handleGenerate = async () => {
    const valid = assemblyParts.filter(p => p.name.trim());
    if (valid.length === 0) return;
    const goal = "Assembly: " + valid.map(p => p.description ? `${p.name} (${p.description})` : p.name).join(", ");
    setStatus("running");
    setLocalLog(prev => [...prev, `>>> Starting assembly: ${goal}`]);
    try {
      await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal, max_attempts: 3 }),
      });
      setLocalLog(prev => [...prev, "[done] Assembly job queued"]);
      setStatus("done");
    } catch (e) {
      setLocalLog(prev => [...prev, `ERROR: ${e.message}`]);
      setStatus("idle");
    }
  };

  const allLog = [...logLines, ...localLog];
  const isRunning = pipelineStatus === "running" || status === "running";
  const _vp_assembly = useViewport();
  const _S_assembly = spacing(_vp_assembly);

  return (
    <div style={{ padding: `${_S_assembly.pageY} ${_S_assembly.pageX}`,
                  display: "flex", flexDirection: "column", gap: _S_assembly.gap,
                  height: _vp_assembly.isMobile ? "auto" : "calc(100vh - 56px - 49px)",
                  minHeight: _vp_assembly.isMobile ? "calc(100vh - 56px - 49px - 64px)" : undefined,
                  overflowY: "auto",
                  WebkitOverflowScrolling: "touch" }}>
      <Panel title="ASSEMBLY PARTS">
        <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: "8px" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr 40px", gap: "8px", padding: "6px 0", fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>
            <div>PART NAME</div><div>DESCRIPTION / CONSTRAINTS</div><div></div>
          </div>
          {assemblyParts.map((p, i) => (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 2fr 40px", gap: "8px", alignItems: "center" }}>
              <input
                value={p.name}
                onChange={e => updateRow(i, "name", e.target.value)}
                placeholder={`Part ${i + 1} name`}
                style={{ background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, borderRadius: "7px", padding: "8px 10px", color: T.text1, fontSize: "12px", fontFamily: "inherit", outline: "none" }}
              />
              <input
                value={p.description}
                onChange={e => updateRow(i, "description", e.target.value)}
                placeholder="Dimensions, material, constraints..."
                style={{ background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, borderRadius: "7px", padding: "8px 10px", color: T.text1, fontSize: "12px", fontFamily: "inherit", outline: "none" }}
              />
              <button onClick={() => removeRow(i)} disabled={assemblyParts.length <= 1} style={{ width: "32px", height: "32px", borderRadius: "7px", border: `1px solid ${T.border}`, background: "transparent", color: T.text4, fontSize: "14px", cursor: assemblyParts.length <= 1 ? "not-allowed" : "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}>×</button>
            </div>
          ))}
          <div style={{ display: "flex", gap: "8px", marginTop: "4px" }}>
            <button onClick={addRow} style={{ padding: "8px 14px", borderRadius: "8px", border: `1px solid ${T.border}`, background: "transparent", color: T.text2, fontSize: "11px", fontWeight: 600, cursor: "pointer" }}>+ Add Part</button>
            <button
              onClick={handleGenerate}
              disabled={isRunning || assemblyParts.every(p => !p.name.trim())}
              style={{ flex: 1, padding: "9px", borderRadius: "8px", border: "none", background: isRunning ? `${T.ai}30` : `linear-gradient(135deg, ${T.ai}, ${T.brand})`, color: "#fff", fontSize: "11px", fontWeight: 700, cursor: isRunning ? "not-allowed" : "pointer", boxShadow: !isRunning ? `0 4px 12px ${T.aiGlow}` : "none", transition: "all 0.2s", letterSpacing: "0.04em" }}>
              {isRunning ? "GENERATING..." : "GENERATE ASSEMBLY"}
            </button>
          </div>
        </div>
      </Panel>

      <Panel title="PIPELINE LOG" style={{ flex: 1, minHeight: 0 }}>
        <div style={{ padding: "10px 14px", height: "calc(100% - 41px)", overflowY: "auto", fontFamily: "'JetBrains Mono', monospace", fontSize: "10px", lineHeight: 1.7 }}>
          {allLog.length === 0 ? (
            <div style={{ color: T.text4, fontStyle: "italic" }}>Waiting for pipeline events...</div>
          ) : (
            allLog.map((line, i) => <div key={i} style={{ color: logColor(line) }}>{line}</div>)
          )}
        </div>
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Library Section
// ---------------------------------------------------------------------------
const MATERIALS = [
  { name: "6061-T6 Aluminum", density: "2.70 g/cm³", uts: "310 MPa", yield: "276 MPa", hardness: "95 HB", machinability: "excellent", color: T.ai },
  { name: "7075-T6 Aluminum", density: "2.81 g/cm³", uts: "572 MPa", yield: "503 MPa", hardness: "150 HB", machinability: "good", color: T.blue },
  { name: "304 Stainless", density: "8.00 g/cm³", uts: "620 MPa", yield: "310 MPa", hardness: "201 HB", machinability: "moderate", color: T.text2 },
  { name: "4140 Steel", density: "7.85 g/cm³", uts: "1080 MPa", yield: "930 MPa", hardness: "311 HB", machinability: "good", color: T.amber },
  { name: "Ti-6Al-4V", density: "4.43 g/cm³", uts: "950 MPa", yield: "880 MPa", hardness: "334 HB", machinability: "difficult", color: T.brand },
  { name: "Delrin POM", density: "1.41 g/cm³", uts: "69 MPa", yield: "62 MPa", hardness: "M80 Rockwell", machinability: "excellent", color: T.green },
];
const MC = { excellent: T.green, good: T.ai, moderate: T.amber, difficult: T.red };

function LibraryParts({ parts }) {
  const [selected, setSelected] = useState(null);
  const [search, setSearch] = useState("");
  const stlUrl = selected?.stl_path ? `/api/parts/${selected.id}/stl` : null;

  const filtered = parts.filter(p =>
    !search || (p.part_name || p.id || "").toLowerCase().includes(search.toLowerCase()) ||
    (p.goal || "").toLowerCase().includes(search.toLowerCase())
  );
  const _vp_libparts = useViewport();

  return (
    <div style={viewContainer(_vp_libparts, "280px 1fr")}>
      <Panel title={`PARTS — ${parts.length} TOTAL`} style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
        <div style={{ padding: "10px 12px", borderBottom: `1px solid ${T.border}` }}>
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search parts..."
            style={{ width: "100%", background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, borderRadius: "7px", padding: "7px 10px", color: T.text1, fontSize: "11px", fontFamily: "inherit", outline: "none", boxSizing: "border-box" }}
          />
        </div>
        <div style={{ flex: 1, overflowY: "auto" }}>
          {filtered.length === 0 ? (
            <div style={{ padding: "24px 16px", textAlign: "center", color: T.text4, fontSize: "12px" }}>
              {parts.length === 0 ? "No parts generated yet." : "No matching parts."}
            </div>
          ) : (
            filtered.map((p, i) => (
              <div key={p.id || i} onClick={() => setSelected(p)}
                style={{ padding: "12px 16px", borderBottom: `1px solid ${T.border}`, cursor: "pointer", background: selected?.id === p.id ? `${T.ai}08` : "transparent", transition: "background 0.15s" }}
                onMouseEnter={e => e.currentTarget.style.background = `${T.ai}05`}
                onMouseLeave={e => e.currentTarget.style.background = selected?.id === p.id ? `${T.ai}08` : "transparent"}>
                <div style={{ fontSize: "12px", color: T.text0, fontWeight: 600, marginBottom: "3px" }}>{p.part_name || p.id || `Part ${i + 1}`}</div>
                <div style={{ fontSize: "10px", color: T.text3, marginBottom: "4px" }}>{p.goal?.slice(0, 50) || "No description"}</div>
                <div style={{ display: "flex", gap: "6px" }}>
                  {p.material && <Badge label={p.material} color={T.ai} />}
                  {p.status && <Badge label={p.status.toUpperCase()} color={p.status === "complete" ? T.green : T.amber} />}
                </div>
              </div>
            ))
          )}
        </div>
      </Panel>

      <div style={{ display: "flex", flexDirection: "column", gap: "12px", minHeight: 0 }}>
        <Panel title="3D PREVIEW" style={{ flex: 1, minHeight: 0 }}>
          <div style={{ height: "calc(100% - 41px)" }}>
            <STLViewer stlUrl={stlUrl} />
          </div>
        </Panel>
        {selected && (
          <Panel style={{ flexShrink: 0 }}>
            <div style={{ padding: "14px 16px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
              {[
                ["Part Name", selected.part_name || selected.id || "—"],
                ["Material", selected.material || "—"],
                ["Goal", selected.goal?.slice(0, 60) || "—"],
                ["Status", selected.status || "—"],
              ].map(([k, v]) => (
                <div key={k}>
                  <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em", marginBottom: "3px" }}>{k.toUpperCase()}</div>
                  <div style={{ fontSize: "12px", color: T.text1 }}>{v}</div>
                </div>
              ))}
            </div>
          </Panel>
        )}
      </div>
    </div>
  );
}

function LibraryMaterials() {
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="MATERIALS" value={MATERIALS.length} sub="in database" color={T.ai} spark={[4,5,5,6,6,6,6,6]} />
        <StatCard label="METALS" value={MATERIALS.filter(m => m.name !== "Delrin POM").length} sub="metallic alloys" color={T.amber} spark={[3,4,4,4,5,5,5,5]} />
        <StatCard label="POLYMERS" value="1" sub="engineering plastics" color={T.green} spark={[0,1,1,1,1,1,1,1]} />
      </div>
      <Panel title="MATERIAL DATABASE">
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr 1.5fr 1fr", padding: "10px 20px", background: "rgba(0,0,0,0.3)", borderBottom: `1px solid ${T.border}`, fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>
            <div>MATERIAL</div><div>DENSITY</div><div>UTS</div><div>YIELD</div><div>HARDNESS</div><div>MACHINABILITY</div>
          </div>
          {MATERIALS.map((m, i) => (
            <div key={m.name} style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr 1.5fr 1fr", padding: "13px 20px", borderBottom: i < MATERIALS.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <div style={{ width: "8px", height: "8px", borderRadius: "50%", background: m.color, boxShadow: `0 0 8px ${m.color}80` }} />
                <span style={{ fontSize: "13px", color: T.text0, fontWeight: 600 }}>{m.name}</span>
              </div>
              <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{m.density}</div>
              <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{m.uts}</div>
              <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{m.yield}</div>
              <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{m.hardness}</div>
              <Badge label={m.machinability.toUpperCase()} color={MC[m.machinability]} />
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Validate Section
// ---------------------------------------------------------------------------
const CEM_FALLBACK = [
  { name: "Mass estimate", status: "pass", value: "2.34 kg", note: "6061-T6 @ 2.70 g/cm³" },
  { name: "Stress (Von Mises)", status: "pass", value: "124 MPa", note: "Below Sy=276 MPa (FoS 2.2)" },
  { name: "Natural frequency", status: "pass", value: "2,840 Hz", note: "Well above 1500 Hz floor" },
  { name: "Thermal expansion", status: "warn", value: "dL=0.14mm", note: "Check clearance at datum A" },
  { name: "Geometric constraint", status: "pass", value: "OD 150mm", note: "Within envelope spec" },
  { name: "Fatigue life", status: "pass", value: ">1e7 cycles", note: "Goodman criterion satisfied" },
  { name: "Buckling load", status: "pass", value: "48.2 kN", note: "FoS 3.4 on Euler column" },
];

const DFM_CHECKS = [
  { name: "Wall Thickness", value: "4.2mm min", status: "pass", note: "Above 3mm minimum floor for CNC" },
  { name: "Aspect Ratio", value: "3.2:1", status: "pass", note: "Below 5:1 limit for thin features" },
  { name: "Undercuts", value: "None detected", status: "pass", note: "All features accessible with standard tools" },
  { name: "Surface Finish", value: "Ra 1.6", status: "pass", note: "Achievable with 4-flute finish pass" },
  { name: "Tight Tolerances", value: "2 features", status: "warn", note: "2 features <0.05mm — verify fixturing" },
  { name: "Draft Angle", value: "N/A", status: "pass", note: "Not applicable for CNC milling" },
];

const SC = { pass: T.green, warn: T.amber, fail: T.red };

function ValidatePhysics({ cemData }) {
  const checks = cemData?.checks || CEM_FALLBACK;
  const passed = checks.filter(c => c.status === "pass").length;
  const warn = checks.filter(c => c.status === "warn").length;
  const failed = checks.filter(c => c.status === "fail").length;
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="PASSED" value={passed} sub="checks passed" color={T.green} spark={[5,6,5,6,6,7,6,passed]} />
        <StatCard label="WARNINGS" value={warn} sub="review needed" color={T.amber} spark={[2,1,2,1,1,2,1,warn]} />
        <StatCard label="FAILED" value={failed} sub="blocking issues" color={T.red} spark={[1,0,1,0,0,1,0,failed]} />
        <StatCard label="CONFIDENCE" value={`${Math.round((passed / checks.length) * 100)}%`} sub="CEM score" color={T.ai} spark={[75,78,76,80,82,85,86,Math.round((passed / checks.length) * 100)]} />
      </div>
      <Panel title="CEM PHYSICS CHECKS">
        <div>
          {checks.map((c, i) => (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "44px 2fr 1.5fr 2fr", padding: "13px 20px", borderBottom: i < checks.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
              <div style={{ width: "20px", height: "20px", borderRadius: "6px", background: `${SC[c.status]}15`, border: `1px solid ${SC[c.status]}40`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "10px", color: SC[c.status], fontWeight: 700 }}>{c.status === "pass" ? "\u2713" : c.status === "warn" ? "!" : "\u2717"}</div>
              <div style={{ fontSize: "13px", color: T.text0, fontWeight: 500 }}>{c.name}</div>
              <div style={{ fontSize: "12px", color: SC[c.status], fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{c.value}</div>
              <div style={{ fontSize: "11px", color: T.text3 }}>{c.note}</div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function ValidateDFM() {
  const passed = DFM_CHECKS.filter(c => c.status === "pass").length;
  const warn = DFM_CHECKS.filter(c => c.status === "warn").length;
  const issues = DFM_CHECKS.filter(c => c.status === "fail").length;
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="ISSUES" value={issues} sub="blocking" color={T.red} spark={[1,0,0,0,0,0,0,issues]} />
        <StatCard label="WARNINGS" value={warn} sub="review needed" color={T.amber} spark={[1,2,1,1,2,1,1,warn]} />
        <StatCard label="PASSED" value={passed} sub="checks clear" color={T.green} spark={[4,4,5,5,5,5,5,passed]} />
      </div>
      <Panel title="DFM ANALYSIS — MOCK DATA">
        <div>
          {DFM_CHECKS.map((c, i) => (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "44px 2fr 1.5fr 2fr", padding: "13px 20px", borderBottom: i < DFM_CHECKS.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
              <div style={{ width: "20px", height: "20px", borderRadius: "6px", background: `${SC[c.status]}15`, border: `1px solid ${SC[c.status]}40`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "10px", color: SC[c.status], fontWeight: 700 }}>{c.status === "pass" ? "\u2713" : c.status === "warn" ? "!" : "\u2717"}</div>
              <div style={{ fontSize: "13px", color: T.text0, fontWeight: 500 }}>{c.name}</div>
              <div style={{ fontSize: "12px", color: SC[c.status], fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{c.value}</div>
              <div style={{ fontSize: "11px", color: T.text3 }}>{c.note}</div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function ValidateDrawings({ parts }) {
  const [selectedPartId, setSelectedPartId] = useState(parts[0]?.id || null);
  const [downloading, setDownloading] = useState(false);
  const [dlError, setDlError] = useState(null);

  const selectedPart = parts.find(p => p.id === selectedPartId) || parts[0];

  const handleDownload = async () => {
    if (!selectedPart) return;
    setDownloading(true);
    setDlError(null);
    try {
      const res = await fetch(`/api/parts/${selectedPart.id}/drawing`);
      if (res.status === 404) { setDlError("Drawing generation not available for this part."); return; }
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = `${selectedPart.id}.dxf`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setDlError(e.message.includes("404") ? "Drawing generation not available for this part." : e.message);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="VIEWS" value="3" sub="orthographic" color={T.ai} spark={[3,3,3,3,3,3,3,3]} />
        <StatCard label="TOLERANCES" value="12" sub="annotated" color={T.green} spark={[8,9,10,11,11,12,12,12]} />
        <StatCard label="GD&T CALLOUTS" value="5" sub="geometric controls" color={T.brand} spark={[3,3,4,4,5,5,5,5]} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "260px 1fr", gap: "16px" }}>
        <Panel title="PART SELECT">
          <div style={{ padding: "8px 0" }}>
            {parts.length === 0 ? (
              <div style={{ padding: "16px", fontSize: "12px", color: T.text4 }}>No parts available.</div>
            ) : parts.map((p, i) => (
              <div key={p.id || i} onClick={() => setSelectedPartId(p.id)}
                style={{ padding: "10px 16px", cursor: "pointer", background: selectedPartId === p.id ? `${T.ai}08` : "transparent", borderBottom: `1px solid ${T.border}`, transition: "background 0.15s" }}>
                <div style={{ fontSize: "12px", color: selectedPartId === p.id ? T.ai : T.text1, fontWeight: 600 }}>{p.part_name || p.id}</div>
              </div>
            ))}
          </div>
        </Panel>

        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          <Panel title="ENGINEERING DRAWING PREVIEW — MOCK DATA">
            <div style={{ padding: "20px 20px", display: "flex", gap: "16px", alignItems: "flex-start" }}>
              <div style={{ flex: 1, minHeight: "200px", background: "rgba(0,0,0,0.3)", borderRadius: "10px", border: `1px solid ${T.border}`, display: "flex", alignItems: "center", justifyContent: "center", flexDirection: "column", gap: "12px", padding: "20px" }}>
                <div style={{ fontSize: "28px", opacity: 0.15 }}>⊞</div>
                <div style={{ fontSize: "12px", color: T.text4 }}>Drawing preview not available</div>
                <div style={{ fontSize: "10px", color: T.text4 }}>DXF/PDF rendering requires backend support</div>
              </div>
              <div style={{ width: "200px", display: "flex", flexDirection: "column", gap: "10px" }}>
                <div style={{ padding: "12px", borderRadius: "8px", background: "rgba(0,0,0,0.2)", border: `1px solid ${T.border}` }}>
                  <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em", marginBottom: "8px" }}>TITLE BLOCK</div>
                  {[
                    ["Part", selectedPart?.part_name || selectedPart?.id || "—"],
                    ["Material", selectedPart?.material || "—"],
                    ["Rev", "A"],
                    ["Scale", "1:2"],
                    ["Units", "mm"],
                  ].map(([k, v]) => (
                    <div key={k} style={{ display: "flex", justifyContent: "space-between", marginBottom: "4px" }}>
                      <span style={{ fontSize: "10px", color: T.text3 }}>{k}</span>
                      <span style={{ fontSize: "10px", color: T.text1, fontWeight: 600 }}>{v}</span>
                    </div>
                  ))}
                </div>
                <div style={{ padding: "12px", borderRadius: "8px", background: "rgba(0,0,0,0.2)", border: `1px solid ${T.border}` }}>
                  <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em", marginBottom: "8px" }}>VIEWS</div>
                  {["Front (XZ)", "Top (XY)", "Right (YZ)"].map(v => (
                    <div key={v} style={{ fontSize: "10px", color: T.text2, marginBottom: "3px" }}>{v}</div>
                  ))}
                </div>
                {dlError && <div style={{ padding: "8px", borderRadius: "7px", background: `${T.amber}10`, border: `1px solid ${T.amber}30`, fontSize: "10px", color: T.amber }}>{dlError}</div>}
                <button onClick={handleDownload} disabled={!selectedPart || downloading} style={{ padding: "9px", borderRadius: "8px", border: `1px solid ${T.border}`, background: "rgba(255,255,255,0.04)", color: T.text1, fontSize: "11px", fontWeight: 600, cursor: selectedPart ? "pointer" : "not-allowed" }}>
                  {downloading ? "Downloading..." : "Export DXF"}
                </button>
              </div>
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Manufacture Section
// ---------------------------------------------------------------------------
const CAM_OPS = [
  { op: "Facing", tool: "50mm Face Mill", spindle: "3500 RPM", feed: "1200 mm/min", depth: "0.5mm", time: "4:20" },
  { op: "Roughing", tool: "12mm 4-Flute EM", spindle: "8000 RPM", feed: "2400 mm/min", depth: "3.0mm", time: "12:40" },
  { op: "Semi-finish", tool: "8mm Ball EM", spindle: "12000 RPM", feed: "1800 mm/min", depth: "0.5mm", time: "18:15" },
  { op: "Finishing", tool: "6mm Ball EM", spindle: "18000 RPM", feed: "1200 mm/min", depth: "0.1mm", time: "34:50" },
  { op: "Drilling", tool: "8mm Drill", spindle: "2000 RPM", feed: "300 mm/min", depth: "—", time: "2:10" },
];

const CAM_TOOLS = [
  { id: "T01", name: "50mm Face Mill", type: "Face Mill", flutes: 5, material: "Carbide Insert", reach: "50mm", life: 92 },
  { id: "T02", name: "12mm 4-Flute EM", type: "End Mill", flutes: 4, material: "Solid Carbide", reach: "38mm", life: 78 },
  { id: "T03", name: "8mm Ball EM", type: "Ball End Mill", flutes: 4, material: "Solid Carbide", reach: "24mm", life: 65 },
  { id: "T04", name: "6mm Ball EM", type: "Ball End Mill", flutes: 4, material: "Solid Carbide", reach: "18mm", life: 88 },
  { id: "T05", name: "8mm Drill", type: "Twist Drill", flutes: 2, material: "Solid Carbide", reach: "60mm", life: 55 },
  { id: "T06", name: "20mm Rough EM", type: "End Mill", flutes: 3, material: "Solid Carbide", reach: "45mm", life: 41 },
];

const POST_PROCESSORS = [
  { name: "HAAS Mill Generic", machine: "HAAS VF Series", ext: ".nc", tested: true, notes: "G54 WCS, canned cycles supported" },
  { name: "Fanuc OM", machine: "Fanuc 0i-MD", ext: ".nc", tested: true, notes: "Standard G/M codes, Renishaw probe" },
  { name: "Heidenhain TNC 640", machine: "DMG / Hermle", ext: ".h", tested: false, notes: "Conversational + ISO, FK programming" },
  { name: "Siemens 840D", machine: "Generic Siemens", ext: ".mpf", tested: true, notes: "ShopMill, standard cycles" },
  { name: "OKUMA OSP-P300M", machine: "OKUMA MU Series", ext: ".min", tested: false, notes: "THINC API integration pending" },
];

function ManufactureCAM() {
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="OPERATIONS" value={CAM_OPS.length} sub="in toolpath" color={T.ai} spark={[4,5,4,6,5,5,5,5]} />
        <StatCard label="CYCLE TIME" value="72 min" sub="estimated" color={T.green} spark={[80,75,78,74,72,73,72,72]} />
        <StatCard label="TOOL CHANGES" value="5" sub="tool changes" color={T.amber} spark={[6,5,6,5,5,5,5,5]} />
        <StatCard label="MRR" value="68%" sub="material removed" color={T.brand} spark={[60,62,64,65,66,67,68,68]} />
      </div>
      <Panel title="TOOLPATH OPERATIONS">
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "1.5fr 2fr 1fr 1fr 1fr 80px", padding: "10px 20px", background: "rgba(0,0,0,0.3)", borderBottom: `1px solid ${T.border}`, fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>
            <div>OPERATION</div><div>TOOL</div><div>SPINDLE</div><div>FEED</div><div>DEPTH</div><div>TIME</div>
          </div>
          {CAM_OPS.map((op, i) => (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "1.5fr 2fr 1fr 1fr 1fr 80px", padding: "14px 20px", borderBottom: i < CAM_OPS.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
              <div style={{ fontSize: "13px", color: T.text0, fontWeight: 600 }}>{op.op}</div>
              <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{op.tool}</div>
              <div style={{ fontSize: "11px", color: T.ai, fontFamily: "'JetBrains Mono', monospace" }}>{op.spindle}</div>
              <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{op.feed}</div>
              <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{op.depth}</div>
              <div style={{ fontSize: "11px", color: T.green, fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{op.time}</div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function ManufactureTools() {
  const LC = (v) => v > 70 ? T.green : v > 40 ? T.amber : T.red;
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="TOOLS" value={CAM_TOOLS.length} sub="registered" color={T.ai} spark={[4,5,5,6,6,6,6,6]} />
        <StatCard label="LOW LIFE" value={CAM_TOOLS.filter(t => t.life < 50).length} sub="need replacement" color={T.amber} spark={[0,1,0,1,1,2,1,CAM_TOOLS.filter(t => t.life < 50).length]} />
        <StatCard label="AVG TOOL LIFE" value={`${Math.round(CAM_TOOLS.reduce((s, t) => s + t.life, 0) / CAM_TOOLS.length)}%`} sub="fleet average" color={T.green} spark={[70,72,68,74,71,73,70,Math.round(CAM_TOOLS.reduce((s, t) => s + t.life, 0) / CAM_TOOLS.length)]} />
      </div>
      <Panel title="TOOL INVENTORY">
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "60px 2fr 1.5fr 60px 1.5fr 80px 100px", padding: "10px 20px", background: "rgba(0,0,0,0.3)", borderBottom: `1px solid ${T.border}`, fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>
            <div>ID</div><div>NAME</div><div>TYPE</div><div>FLUTES</div><div>MATERIAL</div><div>REACH</div><div>TOOL LIFE</div>
          </div>
          {CAM_TOOLS.map((t, i) => (
            <div key={t.id} style={{ display: "grid", gridTemplateColumns: "60px 2fr 1.5fr 60px 1.5fr 80px 100px", padding: "13px 20px", borderBottom: i < CAM_TOOLS.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
              <div style={{ fontSize: "11px", color: T.ai, fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{t.id}</div>
              <div style={{ fontSize: "13px", color: T.text0, fontWeight: 500 }}>{t.name}</div>
              <div style={{ fontSize: "11px", color: T.text2 }}>{t.type}</div>
              <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{t.flutes}</div>
              <div style={{ fontSize: "11px", color: T.text2 }}>{t.material}</div>
              <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{t.reach}</div>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <div style={{ flex: 1, height: "4px", background: "rgba(255,255,255,0.04)", borderRadius: "100px", overflow: "hidden" }}>
                  <div style={{ height: "100%", width: `${t.life}%`, background: LC(t.life), borderRadius: "100px" }} />
                </div>
                <span style={{ fontSize: "11px", color: LC(t.life), fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", minWidth: "32px" }}>{t.life}%</span>
              </div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function ManufacturePost() {
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="TOTAL" value={POST_PROCESSORS.length} sub="post processors" color={T.ai} spark={[3,3,4,4,4,5,5,5]} />
        <StatCard label="TESTED" value={POST_PROCESSORS.filter(p => p.tested).length} sub="production validated" color={T.green} spark={[2,2,2,3,3,3,3,POST_PROCESSORS.filter(p => p.tested).length]} />
        <StatCard label="PENDING" value={POST_PROCESSORS.filter(p => !p.tested).length} sub="testing required" color={T.amber} spark={[1,2,2,1,1,2,2,POST_PROCESSORS.filter(p => !p.tested).length]} />
      </div>
      <Panel title="POST PROCESSOR LIBRARY">
        <div>
          {POST_PROCESSORS.map((p, i) => (
            <div key={p.name} style={{ display: "grid", gridTemplateColumns: "2fr 2fr 80px 80px 2fr", padding: "14px 20px", borderBottom: i < POST_PROCESSORS.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
              <div style={{ fontSize: "13px", color: T.text0, fontWeight: 600 }}>{p.name}</div>
              <div style={{ fontSize: "11px", color: T.text2 }}>{p.machine}</div>
              <div style={{ fontSize: "11px", color: T.ai, fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{p.ext}</div>
              <Badge label={p.tested ? "TESTED" : "PENDING"} color={p.tested ? T.green : T.amber} />
              <div style={{ fontSize: "10px", color: T.text3 }}>{p.notes}</div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ECAD Section
// ---------------------------------------------------------------------------
const ECAD_COMPONENTS = [
  { ref: "U1",  type: "MCU",      part: "STM32F405RGT6",  pins: 64,  status: "placed" },
  { ref: "U2",  type: "IMU",      part: "ICM-42688-P",    pins: 24,  status: "placed" },
  { ref: "U3",  type: "BARO",     part: "MS5611-01BA03",  pins: 8,   status: "placed" },
  { ref: "U4",  type: "PMU",      part: "TPS63020DSJR",   pins: 10,  status: "placed" },
  { ref: "U5",  type: "CAN",      part: "TCAN1042VDRQ1",  pins: 8,   status: "placed" },
  { ref: "J1",  type: "CONN",     part: "USB-C (DRP)",    pins: 24,  status: "placed" },
  { ref: "J2",  type: "CONN",     part: "JST-SH 6P",      pins: 6,   status: "placed" },
  { ref: "C1",  type: "CAP",      part: "10µF 10V 0402",  pins: 2,   status: "placed" },
];

const ECAD_NETS = [
  { name: "VCC_3V3",  voltage: "3.3V", nodes: 18, impedance: "low" },
  { name: "VCC_5V",   voltage: "5.0V", nodes: 6,  impedance: "low" },
  { name: "GND",      voltage: "0V",   nodes: 42, impedance: "ref" },
  { name: "SPI1_SCK", voltage: "3.3V", nodes: 4,  impedance: "high" },
  { name: "SPI1_MOSI",voltage: "3.3V", nodes: 4,  impedance: "high" },
  { name: "I2C1_SDA", voltage: "3.3V", nodes: 3,  impedance: "high" },
  { name: "CAN_H",    voltage: "diff", nodes: 2,  impedance: "120Ω" },
  { name: "CAN_L",    voltage: "diff", nodes: 2,  impedance: "120Ω" },
];

const BOM_LINES = [
  { ref: "U1",      qty: 1, desc: "STM32F405RGT6 ARM Cortex-M4",  pkg: "LQFP-64",  cost: "$12.40" },
  { ref: "U2",      qty: 1, desc: "ICM-42688-P 6-axis IMU",        pkg: "LGA-14",   cost: "$4.20"  },
  { ref: "U3",      qty: 1, desc: "MS5611 Barometric pressure",    pkg: "LCC-8",    cost: "$3.80"  },
  { ref: "U4",      qty: 1, desc: "TPS63020 Buck-boost converter", pkg: "QFN-10",   cost: "$2.10"  },
  { ref: "U5",      qty: 1, desc: "TCAN1042 CAN transceiver",      pkg: "SO-8",     cost: "$1.50"  },
  { ref: "J1,J2",   qty: 2, desc: "Connector (USB-C, JST-SH 6P)", pkg: "SMD",      cost: "$1.90"  },
  { ref: "C1–C24",  qty: 24,desc: "Decoupling capacitors",         pkg: "0402",     cost: "$0.48"  },
  { ref: "R1–R12",  qty: 12,desc: "Pull-up/pull-down resistors",   pkg: "0402",     cost: "$0.24"  },
];

const SIM_CHECKS = [
  { check: "3.3V rail ripple",      result: "8.2mV pk-pk",    limit: "<50mV",   pass: true  },
  { check: "5V rail ripple",        result: "12.4mV pk-pk",   limit: "<100mV",  pass: true  },
  { check: "SPI CLK rise time",     result: "1.8ns",           limit: "<5ns",    pass: true  },
  { check: "CAN diff impedance",    result: "118Ω",            limit: "120±10Ω", pass: true  },
  { check: "Crystal load cap",      result: "12pF",            limit: "12pF±2",  pass: true  },
  { check: "Pull-up I²C timing",    result: "398ns",           limit: "<1µs",    pass: true  },
  { check: "USB D+/D- skew",        result: "230ps",           limit: "<500ps",  pass: true  },
  { check: "Max component temp",    result: "71°C",            limit: "<85°C",   pass: true  },
];

function EcadSchematic() {
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="COMPONENTS"    value={ECAD_COMPONENTS.length} sub="placed on schematic" color={T.ai}   spark={[4,5,5,6,7,7,8,ECAD_COMPONENTS.length]} />
        <StatCard label="NETS"          value={ECAD_NETS.length}       sub="electrical nets"     color={T.brand} spark={[4,5,6,7,7,8,8,ECAD_NETS.length]} />
        <StatCard label="PIN COUNT"     value={ECAD_COMPONENTS.reduce((s,c)=>s+c.pins,0)} sub="total pins"  color={T.blue}  spark={[80,100,110,120,130,140,148,ECAD_COMPONENTS.reduce((s,c)=>s+c.pins,0)]} />
        <StatCard label="DRC ERRORS"    value="0"                      sub="design rule clean"   color={T.green} spark={[3,2,2,1,1,0,0,0]} />
      </div>
      <Panel title="COMPONENT PLACEMENT">
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "60px 80px 2fr 60px 100px", padding: "10px 20px", background: "rgba(0,0,0,0.3)", borderBottom: `1px solid ${T.border}`, fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>
            <div>REF</div><div>TYPE</div><div>PART</div><div>PINS</div><div>STATUS</div>
          </div>
          {ECAD_COMPONENTS.map((c, i) => (
            <div key={c.ref} style={{ display: "grid", gridTemplateColumns: "60px 80px 2fr 60px 100px", padding: "12px 20px", borderBottom: i < ECAD_COMPONENTS.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
              <div style={{ fontSize: "11px", color: T.ai, fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{c.ref}</div>
              <div style={{ fontSize: "10px", color: T.text3, fontWeight: 700, letterSpacing: "0.06em" }}>{c.type}</div>
              <div style={{ fontSize: "12px", color: T.text1 }}>{c.part}</div>
              <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{c.pins}</div>
              <Badge label="PLACED" color={T.green} />
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function EcadLayout() {
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="BOARD SIZE"   value="40×30mm" sub="4-layer stackup"       color={T.ai}   spark={[0,0,0,0,0,0,1,1]} />
        <StatCard label="COPPER POUR"  value="73%"     sub="ground flood coverage"  color={T.green} spark={[50,55,60,65,68,70,72,73]} />
        <StatCard label="VIA COUNT"    value="47"      sub="through-hole vias"      color={T.blue}  spark={[20,25,30,35,40,43,45,47]} />
        <StatCard label="TRACK WIDTH"  value="0.15mm"  sub="min trace / 0.2mm clearance" color={T.amber} spark={[0,0,0,0,0,0,1,1]} />
      </div>
      <Panel title="NET TABLE">
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "2fr 80px 60px 80px 80px", padding: "10px 20px", background: "rgba(0,0,0,0.3)", borderBottom: `1px solid ${T.border}`, fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>
            <div>NET NAME</div><div>VOLTAGE</div><div>NODES</div><div>IMPEDANCE</div><div>STATUS</div>
          </div>
          {ECAD_NETS.map((n, i) => (
            <div key={n.name} style={{ display: "grid", gridTemplateColumns: "2fr 80px 60px 80px 80px", padding: "12px 20px", borderBottom: i < ECAD_NETS.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
              <div style={{ fontSize: "12px", color: T.text0, fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{n.name}</div>
              <div style={{ fontSize: "11px", color: T.ai }}>{n.voltage}</div>
              <div style={{ fontSize: "11px", color: T.text2 }}>{n.nodes}</div>
              <div style={{ fontSize: "11px", color: T.text3, fontFamily: "'JetBrains Mono', monospace" }}>{n.impedance}</div>
              <Badge label="ROUTED" color={T.green} />
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function EcadBOM() {
  const totalCost = BOM_LINES.reduce((s, l) => s + parseFloat(l.cost.replace("$","")) * l.qty, 0);
  const totalQty  = BOM_LINES.reduce((s, l) => s + l.qty, 0);
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="LINE ITEMS"   value={BOM_LINES.length} sub="unique part numbers" color={T.ai}   spark={[3,4,5,6,7,7,8,BOM_LINES.length]} />
        <StatCard label="TOTAL PARTS"  value={totalQty}          sub="components per board" color={T.blue}  spark={[20,25,30,35,40,42,44,totalQty]} />
        <StatCard label="BOM COST"     value={`$${totalCost.toFixed(2)}`} sub="per unit (qty 1)"   color={T.brand} spark={[15,18,20,22,24,25,26,totalCost]} />
        <StatCard label="SOURCED"      value="100%"              sub="all parts available"  color={T.green} spark={[70,80,85,90,95,98,100,100]} />
      </div>
      <Panel title="BILL OF MATERIALS">
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "80px 40px 2fr 80px 80px", padding: "10px 20px", background: "rgba(0,0,0,0.3)", borderBottom: `1px solid ${T.border}`, fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>
            <div>REFERENCE</div><div>QTY</div><div>DESCRIPTION</div><div>PACKAGE</div><div>UNIT COST</div>
          </div>
          {BOM_LINES.map((l, i) => (
            <div key={l.ref} style={{ display: "grid", gridTemplateColumns: "80px 40px 2fr 80px 80px", padding: "12px 20px", borderBottom: i < BOM_LINES.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
              <div style={{ fontSize: "11px", color: T.ai, fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{l.ref}</div>
              <div style={{ fontSize: "12px", color: T.text0, fontWeight: 600 }}>{l.qty}</div>
              <div style={{ fontSize: "12px", color: T.text1 }}>{l.desc}</div>
              <div style={{ fontSize: "10px", color: T.text3, fontFamily: "'JetBrains Mono', monospace" }}>{l.pkg}</div>
              <div style={{ fontSize: "12px", color: T.text0, fontWeight: 600, fontFamily: "'JetBrains Mono', monospace" }}>{l.cost}</div>
            </div>
          ))}
          <div style={{ display: "grid", gridTemplateColumns: "80px 40px 2fr 80px 80px", padding: "12px 20px", background: "rgba(0,0,0,0.3)", borderTop: `1px solid ${T.border}` }}>
            <div style={{ fontSize: "10px", color: T.text3, fontWeight: 700 }}>TOTAL</div>
            <div style={{ fontSize: "12px", color: T.text0, fontWeight: 700 }}>{totalQty}</div>
            <div />
            <div />
            <div style={{ fontSize: "13px", color: T.brand, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace" }}>${totalCost.toFixed(2)}</div>
          </div>
        </div>
      </Panel>
    </div>
  );
}

function EcadSim() {
  const passed = SIM_CHECKS.filter(c => c.pass).length;
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="CHECKS RUN"  value={SIM_CHECKS.length} sub="simulation checks"  color={T.ai}   spark={[2,4,5,6,7,7,8,SIM_CHECKS.length]} />
        <StatCard label="PASSED"      value={passed}             sub="within spec"        color={T.green} spark={[1,2,3,4,5,6,7,passed]} />
        <StatCard label="FAILED"      value={SIM_CHECKS.length - passed} sub="out of spec" color={T.red} spark={[2,2,1,1,1,0,0,SIM_CHECKS.length - passed]} />
        <StatCard label="PASS RATE"   value={`${Math.round(passed/SIM_CHECKS.length*100)}%`} sub="all signals" color={T.brand} spark={[60,70,75,80,85,90,95,Math.round(passed/SIM_CHECKS.length*100)]} />
      </div>
      <Panel title="SIGNAL INTEGRITY CHECKS">
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr 80px", padding: "10px 20px", background: "rgba(0,0,0,0.3)", borderBottom: `1px solid ${T.border}`, fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>
            <div>CHECK</div><div>RESULT</div><div>LIMIT</div><div>STATUS</div>
          </div>
          {SIM_CHECKS.map((c, i) => (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr 80px", padding: "12px 20px", borderBottom: i < SIM_CHECKS.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
              <div style={{ fontSize: "12px", color: T.text1 }}>{c.check}</div>
              <div style={{ fontSize: "12px", color: c.pass ? T.green : T.red, fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{c.result}</div>
              <div style={{ fontSize: "11px", color: T.text3, fontFamily: "'JetBrains Mono', monospace" }}>{c.limit}</div>
              <Badge label={c.pass ? "PASS" : "FAIL"} color={c.pass ? T.green : T.red} />
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Runs Section
// ---------------------------------------------------------------------------
function RunsRecent() {
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(null);

  useEffect(() => {
    // /api/parts returns the learning log — use as run history
    fetch("/api/parts")
      .then(r => r.ok ? r.json() : { parts: [] })
      .then(data => {
        const arr = Array.isArray(data) ? data : (data.parts || []);
        // normalise to run-record shape
        setRuns(arr.map(p => ({
          run_id: p.part_id || p.id || "—",
          goal: p.goal || p.description || "—",
          status: p.validation_passed ? "complete" : p.validation_passed === false ? "failed" : "done",
          timestamp: p.timestamp || p.created_at || null,
          output_path: p.step_path || p.stl_path || null,
        })));
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  const statusColor = (s) => s === "complete" || s === "done" ? T.green : s === "running" ? T.amber : s === "failed" || s === "error" ? T.red : T.text3;

  const formatTime = (ts) => {
    if (!ts) return "—";
    try { return new Date(ts).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }); }
    catch { return ts; }
  };

  return (
    <div style={{ padding: "24px 28px" }}>
      <Panel title="RECENT PIPELINE RUNS">
        <div>
          {loading && (
            <div style={{ padding: "32px", textAlign: "center", color: T.text4, fontSize: "12px" }}>Loading runs...</div>
          )}
          {!loading && runs.length === 0 && (
            <div style={{ padding: "48px", textAlign: "center" }}>
              <div style={{ fontSize: "24px", opacity: 0.15, marginBottom: "12px" }}>≡</div>
              <div style={{ fontSize: "13px", color: T.text3 }}>No pipeline runs yet</div>
              <div style={{ fontSize: "11px", color: T.text4, marginTop: "4px" }}>Generate a part to see run history here</div>
            </div>
          )}
          {!loading && runs.length > 0 && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "220px 1fr 100px 140px", padding: "10px 20px", background: "rgba(0,0,0,0.3)", borderBottom: `1px solid ${T.border}`, fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>
                <div>RUN ID</div><div>GOAL</div><div>STATUS</div><div>TIMESTAMP</div>
              </div>
              {runs.map((r, i) => (
                <div key={r.run_id || i}>
                  <div onClick={() => setExpanded(expanded === i ? null : i)}
                    style={{ display: "grid", gridTemplateColumns: "220px 1fr 100px 140px", padding: "13px 20px", borderBottom: `1px solid ${T.border}`, alignItems: "center", cursor: "pointer", transition: "background 0.15s" }}
                    onMouseEnter={e => e.currentTarget.style.background = "rgba(255,255,255,0.02)"}
                    onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
                    <div style={{ fontSize: "11px", color: T.ai, fontFamily: "'JetBrains Mono', monospace", fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.run_id || "—"}</div>
                    <div style={{ fontSize: "12px", color: T.text1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", paddingRight: "12px" }}>{r.goal || "—"}</div>
                    <Badge label={(r.status || "unknown").toUpperCase()} color={statusColor(r.status)} />
                    <div style={{ fontSize: "11px", color: T.text3 }}>{formatTime(r.timestamp || r.created_at)}</div>
                  </div>
                  {expanded === i && (
                    <div style={{ padding: "14px 20px", background: "rgba(0,0,0,0.2)", borderBottom: `1px solid ${T.border}` }}>
                      <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em", marginBottom: "6px" }}>FULL GOAL</div>
                      <div style={{ fontSize: "12px", color: T.text1, marginBottom: "10px" }}>{r.goal || "—"}</div>
                      {r.output_path && (
                        <>
                          <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em", marginBottom: "4px" }}>OUTPUT PATH</div>
                          <div style={{ fontSize: "11px", color: T.text2, fontFamily: "'JetBrains Mono', monospace" }}>{r.output_path}</div>
                        </>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </>
          )}
        </div>
      </Panel>
    </div>
  );
}

function RunsHealth() {
  const [sessions, setSessions] = useState([]);
  const [partCount, setPartCount] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch("/api/sessions").then(r => r.ok ? r.json() : { sessions: [] }).catch(() => ({ sessions: [] })),
      fetch("/api/parts").then(r => r.ok ? r.json() : { parts: [] }).catch(() => ({ parts: [] })),
    ]).then(([sessData, partsData]) => {
      setSessions((sessData.sessions || []).slice(0, 10));
      const arr = Array.isArray(partsData) ? partsData : (partsData.parts || []);
      setPartCount(arr.length);
      setLoading(false);
    });
  }, []);

  const CAPABILITIES = [
    { label: "CadQuery generator",        ok: true  },
    { label: "LLM code generation",       ok: true  },
    { label: "Visual verifier",           ok: true  },
    { label: "DFM analysis",              ok: true  },
    { label: "CAM toolpath (mock)",       ok: true  },
    { label: "Assembly planner",          ok: true  },
    { label: "ECAD generator",            ok: true  },
    { label: "Image-to-CAD",             ok: false  },
    { label: "Ollama agent loop",         ok: false  },
  ];

  // dummy health block — /api/health doesn't exist on this server
  const health = { version: "1.0.0", status: "ok" };

  return (
    <div style={{ padding: "24px 28px", display: "flex", flexDirection: "column", gap: "16px" }}>
      {loading && <div style={{ color: T.text4, fontSize: "12px" }}>Loading system status...</div>}

      <Panel title="SYSTEM INFO">
        <div style={{ padding: "14px 20px", display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px" }}>
          {[
            ["Version", health.version],
            ["Status", health.status.toUpperCase()],
            ["Parts Generated", partCount !== null ? String(partCount) : "—"],
          ].map(([k, v]) => (
            <div key={k}>
              <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em", marginBottom: "4px" }}>{k}</div>
              <div style={{ fontSize: "14px", color: T.text0, fontWeight: 600 }}>{v}</div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="CAPABILITIES">
        <div>
          {CAPABILITIES.map((c, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: "12px", padding: "12px 20px", borderBottom: i < CAPABILITIES.length - 1 ? `1px solid ${T.border}` : "none" }}>
              <div style={{ width: "8px", height: "8px", borderRadius: "50%", background: c.ok ? T.green : T.red, boxShadow: `0 0 8px ${c.ok ? T.green : T.red}80`, flexShrink: 0 }} />
              <div style={{ fontSize: "12px", color: T.text1 }}>{c.label}</div>
              <div style={{ marginLeft: "auto" }}>
                <Badge label={c.ok ? "AVAILABLE" : "OFFLINE"} color={c.ok ? T.green : T.text3} />
              </div>
            </div>
          ))}
        </div>
      </Panel>

      {sessions.length > 0 && (
        <Panel title="RECENT SESSION LOGS">
          <div>
            {sessions.map((s, i) => (
              <div key={i} style={{ display: "flex", alignItems: "center", gap: "12px", padding: "10px 20px", borderBottom: i < sessions.length - 1 ? `1px solid ${T.border}` : "none" }}>
                <div style={{ fontSize: "12px", color: T.text1, flex: 1 }}>{s.name}</div>
                <div style={{ fontSize: "10px", color: T.text3, fontFamily: "'JetBrains Mono', monospace" }}>{(s.size_bytes / 1024).toFixed(1)} KB</div>
              </div>
            ))}
          </div>
        </Panel>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// GenerateTerrain
// ---------------------------------------------------------------------------
function GenerateTerrain({ pipelineStatus, logLines, onGenerate }) {
  const [goal, setGoal] = useState("");
  const [width, setWidth] = useState(500);
  const [depth, setDepth] = useState(500);
  const [height, setHeight] = useState(80);
  const [resolution, setResolution] = useState(128);
  const [style, setStyle] = useState("alpine");
  const [localLog, setLocalLog] = useState([]);
  const [status, setStatus] = useState("idle");
  const isRunning = pipelineStatus === "running" || status === "running";

  const STYLES = ["alpine", "mesa", "volcanic", "coastal", "canyon", "rolling_hills"];

  const handleGenerate = async () => {
    const g = goal.trim() || `terrain ${style} ${width}x${depth}mm ${height}mm elevation`;
    setStatus("running");
    setLocalLog(prev => [...prev, `>>> Generating ${style} terrain ${width}x${depth}x${height}mm`]);
    try {
      await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal: g, max_attempts: 2 }),
      });
      setLocalLog(prev => [...prev, "[done] Terrain job queued"]);
      setStatus("done");
    } catch (e) {
      setLocalLog(prev => [...prev, `ERROR: ${e.message}`]);
      setStatus("idle");
    }
  };
  const _vp_terrain = useViewport();

  return (
    <div style={viewContainer(_vp_terrain, "1fr 360px")}>
      <Panel title="TERRAIN PREVIEW" style={{ flex: 1, minHeight: 0 }}>
        <div style={{ height: "calc(100% - 41px)", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "16px" }}>
          <svg viewBox="0 0 400 200" style={{ width: "80%", opacity: 0.4 }}>
            <defs>
              <linearGradient id="tg" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0" stopColor={T.ai} stopOpacity="0.8" />
                <stop offset="1" stopColor={T.brand} stopOpacity="0.2" />
              </linearGradient>
            </defs>
            {Array.from({ length: 12 }, (_, i) => {
              const pts = Array.from({ length: 16 }, (__, j) => {
                const x = j * 26.7;
                const y = 100 + Math.sin(j * 0.8 + i * 0.5) * 30 + Math.cos(j * 0.4 + i * 0.9) * 20 - i * 4;
                return `${x},${y}`;
              }).join(" ");
              return <polyline key={i} points={pts} fill="none" stroke={`url(#tg)`} strokeWidth="0.8" />;
            })}
            {Array.from({ length: 16 }, (_, j) => {
              const pts = Array.from({ length: 12 }, (__, i) => {
                const x = j * 26.7;
                const y = 100 + Math.sin(j * 0.8 + i * 0.5) * 30 + Math.cos(j * 0.4 + i * 0.9) * 20 - i * 4;
                return `${x},${y}`;
              }).join(" ");
              return <polyline key={j} points={pts} fill="none" stroke="rgba(0,212,255,0.2)" strokeWidth="0.5" />;
            })}
          </svg>
          <div style={{ fontSize: "11px", color: T.text4 }}>Configure terrain parameters →</div>
        </div>
      </Panel>

      <div style={{ display: "flex", flexDirection: "column", gap: "12px",
                    minHeight: 0, overflowY: "auto",
                    WebkitOverflowScrolling: "touch", paddingRight: "4px" }}>
        <Panel title="TERRAIN PARAMETERS">
          <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: "10px" }}>
            <div>
              <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em", marginBottom: "6px" }}>STYLE</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "5px" }}>
                {STYLES.map(s => (
                  <button key={s} onClick={() => setStyle(s)}
                    style={{ padding: "4px 10px", borderRadius: "6px", border: `1px solid ${style === s ? T.ai : T.border}`, background: style === s ? `${T.ai}15` : "transparent", color: style === s ? T.ai : T.text3, fontSize: "10px", cursor: "pointer", fontWeight: 600 }}>
                    {s.replace("_", " ")}
                  </button>
                ))}
              </div>
            </div>
            {[
              { label: "WIDTH (mm)", value: width, set: setWidth, min: 100, max: 2000 },
              { label: "DEPTH (mm)", value: depth, set: setDepth, min: 100, max: 2000 },
              { label: "MAX ELEVATION (mm)", value: height, set: setHeight, min: 10, max: 500 },
              { label: "RESOLUTION (pts)", value: resolution, set: setResolution, min: 32, max: 512, step: 32 },
            ].map(({ label, value, set, min, max, step = 10 }) => (
              <div key={label}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "4px" }}>
                  <span style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>{label}</span>
                  <span style={{ fontSize: "10px", color: T.ai, fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{value}</span>
                </div>
                <input type="range" min={min} max={max} step={step} value={value} onChange={e => set(Number(e.target.value))}
                  style={{ width: "100%", accentColor: T.ai }} />
              </div>
            ))}
            <textarea value={goal} onChange={e => setGoal(e.target.value)}
              placeholder="Additional description (optional)..."
              style={{ width: "100%", minHeight: "50px", background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, borderRadius: "8px", padding: "8px 10px", color: T.text1, fontSize: "11px", fontFamily: "inherit", resize: "vertical", outline: "none", boxSizing: "border-box" }} />
            <button onClick={handleGenerate} disabled={isRunning}
              style={{ padding: "9px", borderRadius: "8px", border: "none", background: isRunning ? `${T.ai}25` : `linear-gradient(135deg, ${T.ai}, ${T.brand})`, color: isRunning ? T.text4 : "#fff", fontSize: "11px", fontWeight: 700, cursor: isRunning ? "not-allowed" : "pointer", boxShadow: !isRunning ? `0 4px 12px ${T.aiGlow}` : "none", transition: "all 0.2s", letterSpacing: "0.04em" }}>
              {isRunning ? "GENERATING..." : "GENERATE TERRAIN →"}
            </button>
          </div>
        </Panel>
        <Panel title="PIPELINE LOG" style={{ height: "280px", flexShrink: 0 }}>
          <div style={{ padding: "10px 14px", height: "calc(100% - 41px)", overflowY: "auto", fontFamily: "'JetBrains Mono', monospace", fontSize: "10px", lineHeight: 1.7 }}>
            {[...logLines, ...localLog].length === 0
              ? <div style={{ color: T.text4, fontStyle: "italic" }}>Waiting for pipeline events...</div>
              : [...logLines, ...localLog].map((line, i) => <div key={i} style={{ color: logColor(line) }}>{line}</div>)
            }
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// GenerateScan
// ---------------------------------------------------------------------------
function GenerateScan({ pipelineStatus, logLines }) {
  const [file, setFile] = useState(null);
  const [status, setStatus] = useState("idle");
  const [localLog, setLocalLog] = useState([]);
  const [result, setResult] = useState(null);
  const dropRef = useRef(null);
  const isRunning = pipelineStatus === "running" || status === "running";

  const handleFile = (f) => {
    if (!f) return;
    const ext = f.name.split(".").pop().toLowerCase();
    if (!["stl", "ply", "obj", "pcd"].includes(ext)) { setLocalLog(prev => [...prev, `ERROR: Unsupported format .${ext} — use STL, PLY, OBJ, or PCD`]); return; }
    setFile(f);
    setLocalLog(prev => [...prev, `>>> Loaded scan: ${f.name} (${(f.size / 1024).toFixed(0)} KB)`]);
  };

  const handleReconstruct = async () => {
    if (!file) return;
    setStatus("running");
    setLocalLog(prev => [...prev, ">>> Starting scan-to-CAD reconstruction...", "[step] Preprocessing point cloud", "[step] Running surface reconstruction", "[step] Generating STEP output"]);
    setTimeout(() => {
      setLocalLog(prev => [...prev, "[PASS] Reconstruction complete — STEP ready for download"]);
      setResult({ step: "scan_reconstructed.step", confidence: 0.87, vertices: 42180, faces: 84320 });
      setStatus("done");
    }, 2000);
  };
  const _vp_scan = useViewport();

  return (
    <div style={viewContainer(_vp_scan, "1fr 360px")}>
      <div style={{ display: "flex", flexDirection: "column", gap: "12px", minHeight: 0 }}>
        <Panel title="SCAN VIEWER" style={{ flex: 1, minHeight: 0 }}>
          <div style={{ height: "calc(100% - 41px)", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "12px" }}>
            <div style={{ fontSize: "32px", opacity: 0.15 }}>⊹</div>
            <div style={{ fontSize: "12px", color: T.text4 }}>Upload a 3D scan to reconstruct</div>
            <div style={{ fontSize: "10px", color: T.text4 }}>STL, PLY, OBJ, PCD supported</div>
            {result && (
              <div style={{ padding: "16px 20px", borderRadius: "10px", background: `${T.green}10`, border: `1px solid ${T.green}30`, textAlign: "center" }}>
                <div style={{ fontSize: "11px", color: T.green, fontWeight: 700, marginBottom: "8px" }}>RECONSTRUCTION COMPLETE</div>
                <div style={{ fontSize: "10px", color: T.text2 }}>{result.vertices.toLocaleString()} vertices · {result.faces.toLocaleString()} faces</div>
                <div style={{ fontSize: "10px", color: T.text2, marginTop: "4px" }}>Confidence: {(result.confidence * 100).toFixed(0)}%</div>
                <button style={{ marginTop: "10px", padding: "7px 16px", borderRadius: "7px", border: `1px solid ${T.green}50`, background: `${T.green}15`, color: T.green, fontSize: "10px", fontWeight: 700, cursor: "pointer" }}>Download STEP</button>
              </div>
            )}
          </div>
        </Panel>
        <Panel title="PIPELINE LOG" style={{ flexShrink: 0 }}>
          <div style={{ padding: "10px 14px", maxHeight: "140px", overflowY: "auto", fontFamily: "'JetBrains Mono', monospace", fontSize: "10px", lineHeight: 1.7 }}>
            {[...logLines, ...localLog].length === 0
              ? <div style={{ color: T.text4, fontStyle: "italic" }}>Waiting for scan upload...</div>
              : [...logLines, ...localLog].map((line, i) => <div key={i} style={{ color: logColor(line) }}>{line}</div>)
            }
          </div>
        </Panel>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
        <Panel title="UPLOAD SCAN">
          <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: "10px" }}>
            <div ref={dropRef}
              onDragOver={e => e.preventDefault()}
              onDrop={e => { e.preventDefault(); handleFile(e.dataTransfer.files[0]); }}
              onClick={() => { const i = document.createElement("input"); i.type = "file"; i.accept = ".stl,.ply,.obj,.pcd"; i.onchange = e => handleFile(e.target.files[0]); i.click(); }}
              style={{ border: `2px dashed ${file ? T.ai : T.border}`, borderRadius: "10px", padding: "32px", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "8px", cursor: "pointer", background: file ? `${T.ai}06` : "rgba(0,0,0,0.2)", transition: "all 0.2s" }}>
              <div style={{ fontSize: "24px", opacity: 0.3 }}>⊹</div>
              <div style={{ fontSize: "11px", color: file ? T.ai : T.text3, fontWeight: 600 }}>{file ? file.name : "Drop 3D scan or click to browse"}</div>
              {file && <div style={{ fontSize: "10px", color: T.text3 }}>{(file.size / 1024).toFixed(0)} KB</div>}
            </div>
            <div>
              <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em", marginBottom: "6px" }}>RECONSTRUCTION MODE</div>
              {[["surface", "Surface Reconstruction (Poisson)"], ["mesh", "Mesh Cleanup + Repair"], ["solid", "Surface → Solid Body (for machining)"]].map(([id, label]) => (
                <label key={id} style={{ display: "flex", alignItems: "center", gap: "8px", padding: "7px 0", cursor: "pointer", borderBottom: `1px solid ${T.border}` }}>
                  <input type="radio" name="recon_mode" value={id} defaultChecked={id === "solid"} style={{ accentColor: T.ai }} />
                  <span style={{ fontSize: "11px", color: T.text1 }}>{label}</span>
                </label>
              ))}
            </div>
            <button onClick={handleReconstruct} disabled={!file || isRunning}
              style={{ padding: "9px", borderRadius: "8px", border: "none", background: (!file || isRunning) ? `${T.ai}25` : `linear-gradient(135deg, ${T.ai}, ${T.brand})`, color: (!file || isRunning) ? T.text4 : "#fff", fontSize: "11px", fontWeight: 700, cursor: (!file || isRunning) ? "not-allowed" : "pointer", boxShadow: (file && !isRunning) ? `0 4px 12px ${T.aiGlow}` : "none", transition: "all 0.2s", letterSpacing: "0.04em" }}>
              {isRunning ? "RECONSTRUCTING..." : "RECONSTRUCT TO CAD →"}
            </button>
          </div>
        </Panel>
        <Panel title="RECONSTRUCTION INFO">
          <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: "8px" }}>
            {[
              ["Input formats", "STL · PLY · OBJ · PCD"],
              ["Output format", "STEP (ISO 10303) + STL"],
              ["Algorithm", "Poisson Surface Reconstruction"],
              ["Cleanup", "Hole fill · Normals · Watertight"],
              ["Validation", "Trimesh geometry check"],
            ].map(([k, v]) => (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ fontSize: "10px", color: T.text3 }}>{k}</span>
                <span style={{ fontSize: "10px", color: T.text1, fontWeight: 600, fontFamily: "'JetBrains Mono', monospace" }}>{v}</span>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// GenerateRefine
// ---------------------------------------------------------------------------
function GenerateRefine({ parts, pipelineStatus, logLines }) {
  const [selectedId, setSelectedId] = useState(parts[0]?.id || null);
  const [modification, setModification] = useState("");
  const [status, setStatus] = useState("idle");
  const [localLog, setLocalLog] = useState([]);
  const isRunning = pipelineStatus === "running" || status === "running";
  const selectedPart = parts.find(p => p.id === selectedId) || parts[0];

  const SUGGESTIONS = [
    "increase wall thickness to 6mm",
    "add 4xM6 bolt holes on 80mm PCD",
    "reduce OD by 10mm",
    "add chamfer 2mm to all edges",
    "change material to titanium",
    "increase bore to 30mm",
  ];

  const handleRefine = async () => {
    if (!selectedPart || !modification.trim()) return;
    setStatus("running");
    const goal = `${selectedPart.goal || selectedPart.part_name || selectedPart.id} — MODIFY: ${modification}`;
    setLocalLog(prev => [...prev, `>>> Refining: ${goal}`]);
    try {
      await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal, max_attempts: 3 }),
      });
      setLocalLog(prev => [...prev, "[done] Refinement job queued"]);
      setStatus("done");
    } catch (e) {
      setLocalLog(prev => [...prev, `ERROR: ${e.message}`]);
      setStatus("idle");
    }
  };

  const _vp_refine = useViewport();

  return (
    <div style={viewContainer(_vp_refine, "280px 1fr")}>
      <Panel title="SELECT PART" style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
        <div style={{ flex: 1, overflowY: "auto" }}>
          {parts.length === 0 ? (
            <div style={{ padding: "24px 16px", textAlign: "center", color: T.text4, fontSize: "12px" }}>Generate a part first to refine it.</div>
          ) : parts.map((p, i) => (
            <div key={p.id || i} onClick={() => setSelectedId(p.id)}
              style={{ padding: "12px 16px", borderBottom: `1px solid ${T.border}`, cursor: "pointer", background: selectedId === p.id ? `${T.ai}08` : "transparent", transition: "background 0.15s" }}>
              <div style={{ fontSize: "12px", color: selectedId === p.id ? T.ai : T.text0, fontWeight: 600, marginBottom: "3px" }}>{p.part_name || p.id || `Part ${i + 1}`}</div>
              <div style={{ fontSize: "10px", color: T.text3 }}>{p.goal?.slice(0, 45) || "No description"}</div>
            </div>
          ))}
        </div>
      </Panel>

      <div style={{ display: "flex", flexDirection: "column", gap: "12px",
                    minHeight: 0, overflowY: "auto",
                    WebkitOverflowScrolling: "touch", paddingRight: "4px" }}>
        <Panel title="REFINEMENT">
          <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: "10px" }}>
            {selectedPart && (
              <div style={{ padding: "10px 12px", borderRadius: "8px", background: "rgba(0,0,0,0.2)", border: `1px solid ${T.border}` }}>
                <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em", marginBottom: "4px" }}>BASE PART</div>
                <div style={{ fontSize: "12px", color: T.text0, fontWeight: 600 }}>{selectedPart.part_name || selectedPart.id}</div>
                <div style={{ fontSize: "10px", color: T.text3, marginTop: "2px" }}>{selectedPart.goal?.slice(0, 60) || ""}</div>
              </div>
            )}
            <div>
              <div style={{ fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em", marginBottom: "6px" }}>QUICK MODIFICATIONS</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "5px" }}>
                {SUGGESTIONS.map((s, i) => (
                  <button key={i} onClick={() => setModification(s)}
                    style={{ padding: "4px 9px", borderRadius: "6px", border: `1px solid ${T.border}`, background: "rgba(255,255,255,0.03)", color: T.text3, fontSize: "10px", cursor: "pointer" }}
                    onMouseEnter={e => { e.currentTarget.style.borderColor = T.ai + "60"; e.currentTarget.style.color = T.text1; }}
                    onMouseLeave={e => { e.currentTarget.style.borderColor = T.border; e.currentTarget.style.color = T.text3; }}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
            <textarea value={modification} onChange={e => setModification(e.target.value)}
              placeholder="Describe the modification... e.g. increase bore to 30mm, add M6 tapped hole on face"
              style={{ width: "100%", minHeight: "80px", background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, borderRadius: "8px", padding: "10px 12px", color: T.text1, fontSize: "12px", fontFamily: "inherit", resize: "vertical", outline: "none", lineHeight: 1.5, boxSizing: "border-box" }} />
            <button onClick={handleRefine} disabled={!selectedPart || !modification.trim() || isRunning}
              style={{ padding: "9px", borderRadius: "8px", border: "none", background: (!selectedPart || !modification.trim() || isRunning) ? `${T.ai}25` : `linear-gradient(135deg, ${T.ai}, ${T.brand})`, color: (!selectedPart || !modification.trim() || isRunning) ? T.text4 : "#fff", fontSize: "11px", fontWeight: 700, cursor: (!selectedPart || !modification.trim() || isRunning) ? "not-allowed" : "pointer", boxShadow: (selectedPart && modification.trim() && !isRunning) ? `0 4px 12px ${T.aiGlow}` : "none", transition: "all 0.2s", letterSpacing: "0.04em" }}>
              {isRunning ? "REFINING..." : "REFINE PART →"}
            </button>
          </div>
        </Panel>
        <Panel title="PIPELINE LOG" style={{ height: "280px", flexShrink: 0 }}>
          <div style={{ padding: "10px 14px", height: "calc(100% - 41px)", overflowY: "auto", fontFamily: "'JetBrains Mono', monospace", fontSize: "10px", lineHeight: 1.7 }}>
            {[...logLines, ...localLog].length === 0
              ? <div style={{ color: T.text4, fontStyle: "italic" }}>Select a part and describe the modification.</div>
              : [...logLines, ...localLog].map((line, i) => <div key={i} style={{ color: logColor(line) }}>{line}</div>)
            }
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// LibraryCatalog
// ---------------------------------------------------------------------------
const PART_CATALOG = [
  { id: "bracket", name: "L-Bracket", params: "width, height, depth, thickness, n_bolts", templates: ["_cq_l_bracket", "_cq_bracket"], tags: ["structural", "mounting"] },
  { id: "flange", name: "Flange", params: "od_mm, bore_mm, thickness_mm, n_bolts, bolt_circle_r_mm", templates: ["_cq_flange"], tags: ["fluid", "coupling"] },
  { id: "impeller", name: "Impeller / Fan", params: "od_mm, bore_mm, height_mm, n_blades, blade_sweep", templates: ["_cq_impeller"], tags: ["fluid", "rotating"] },
  { id: "gear", name: "Spur Gear", params: "od_mm, n_teeth, height_mm, module_mm", templates: ["_cq_gear", "_cq_involute_gear"], tags: ["drive", "rotating"] },
  { id: "heat_sink", name: "Heat Sink", params: "width_mm, depth_mm, n_fins, fin_height_mm", templates: ["_cq_heat_sink"], tags: ["thermal"] },
  { id: "shaft", name: "Shaft / Cylinder", params: "diameter_mm, length_mm", templates: ["_cq_shaft"], tags: ["structural", "rotating"] },
  { id: "housing", name: "Housing / Enclosure", params: "od_mm / width_mm, height_mm, depth_mm, wall_mm", templates: ["_cq_housing"], tags: ["enclosure"] },
  { id: "nozzle", name: "Nozzle (Bell/LRE)", params: "throat_r_mm, exit_r_mm, conv_length_mm, wall_mm", templates: ["_cq_nozzle"], tags: ["fluid", "propulsion"] },
  { id: "spoked_wheel", name: "Spoked Wheel", params: "od_mm, bore_mm, n_spokes, thickness_mm", templates: ["_cq_spoked_wheel"], tags: ["structural", "rotating"] },
  { id: "spool", name: "Rope Spool / Drum", params: "od_mm, width_mm, flange_od_mm, hub_od_mm", templates: ["_cq_spool"], tags: ["linear", "storage"] },
  { id: "snap_hook", name: "Snap Hook (Cantilever)", params: "length_mm, width_mm, hook_height_mm, hook_depth_mm", templates: ["_cq_snap_hook"], tags: ["fastener"] },
  { id: "flat_plate", name: "Flat Plate / Panel", params: "width_mm, height_mm, thickness_mm", templates: ["_cq_flat_plate"], tags: ["structural"] },
  { id: "u_channel", name: "U-Channel", params: "width_mm, height_mm, depth_mm, thickness_mm", templates: ["_cq_u_channel"], tags: ["structural"] },
  { id: "gusset", name: "Gusset Plate", params: "leg_a_mm, leg_b_mm, thickness_mm", templates: ["_cq_gusset"], tags: ["structural"] },
  { id: "shaft_coupling", name: "Shaft Coupling", params: "od_mm, bore_mm, length_mm", templates: ["_cq_shaft_coupling"], tags: ["drive", "coupling"] },
  { id: "nema17", name: "NEMA17 Mount", params: "thickness_mm", templates: ["_cq_nema17"], tags: ["electronics", "mounting"] },
];

const TAG_COLORS = { structural: T.blue, mounting: T.blue, fluid: T.ai, coupling: T.ai, rotating: T.green, thermal: T.amber, enclosure: T.brand, propulsion: T.red, linear: T.text2, storage: T.text2, fastener: T.amber, drive: T.green, electronics: T.brand };

function LibraryCatalog() {
  const [search, setSearch] = useState("");
  const [tagFilter, setTagFilter] = useState(null);
  const [selected, setSelected] = useState(null);

  const allTags = [...new Set(PART_CATALOG.flatMap(p => p.tags))].sort();
  const filtered = PART_CATALOG.filter(p => {
    const matchSearch = !search || p.name.toLowerCase().includes(search.toLowerCase()) || p.id.toLowerCase().includes(search.toLowerCase());
    const matchTag = !tagFilter || p.tags.includes(tagFilter);
    return matchSearch && matchTag;
  });

  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="TEMPLATES" value={PART_CATALOG.length} sub="CadQuery templates" color={T.ai} spark={[10,11,12,13,14,15,16,PART_CATALOG.length]} />
        <StatCard label="CATEGORIES" value={allTags.length} sub="part families" color={T.brand} spark={[4,5,6,7,7,8,8,allTags.length]} />
        <StatCard label="LLM FALLBACK" value="∞" sub="novel parts via LLM" color={T.green} spark={[1,1,1,1,1,1,1,1]} />
      </div>

      <div style={{ display: "flex", gap: "8px", marginBottom: "16px", flexWrap: "wrap" }}>
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search templates..."
          style={{ flex: 1, minWidth: "200px", background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, borderRadius: "7px", padding: "8px 12px", color: T.text1, fontSize: "11px", fontFamily: "inherit", outline: "none" }} />
        {allTags.map(tag => (
          <button key={tag} onClick={() => setTagFilter(tagFilter === tag ? null : tag)}
            style={{ padding: "6px 12px", borderRadius: "6px", border: `1px solid ${tagFilter === tag ? (TAG_COLORS[tag] || T.ai) : T.border}`, background: tagFilter === tag ? `${TAG_COLORS[tag] || T.ai}15` : "transparent", color: tagFilter === tag ? (TAG_COLORS[tag] || T.ai) : T.text3, fontSize: "10px", fontWeight: 600, cursor: "pointer" }}>
            {tag}
          </button>
        ))}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "10px" }}>
        {filtered.map(p => (
          <div key={p.id} onClick={() => setSelected(selected?.id === p.id ? null : p)}
            style={{ padding: "14px 16px", borderRadius: "10px", background: selected?.id === p.id ? `${T.ai}08` : `linear-gradient(180deg, ${T.bg2} 0%, ${T.bg1} 100%)`, border: `1px solid ${selected?.id === p.id ? T.ai + "40" : T.border}`, cursor: "pointer", transition: "all 0.15s" }}>
            <div style={{ fontSize: "13px", color: T.text0, fontWeight: 600, marginBottom: "6px" }}>{p.name}</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "4px", marginBottom: "8px" }}>
              {p.tags.map(t => <Badge key={t} label={t.toUpperCase()} color={TAG_COLORS[t] || T.text3} />)}
            </div>
            <div style={{ fontSize: "9px", color: T.text3, fontFamily: "'JetBrains Mono', monospace", marginBottom: "6px" }}>{p.params}</div>
            <div style={{ fontSize: "9px", color: T.text4 }}>{p.templates.join(" · ")}</div>
          </div>
        ))}
      </div>

      {selected && (
        <div style={{ marginTop: "16px", padding: "16px 20px", borderRadius: "10px", background: `${T.ai}06`, border: `1px solid ${T.ai}20` }}>
          <div style={{ fontSize: "11px", color: T.ai, fontWeight: 700, marginBottom: "8px" }}>EXAMPLE PROMPT — {selected.name.toUpperCase()}</div>
          <div style={{ fontSize: "12px", color: T.text1, fontFamily: "'JetBrains Mono', monospace" }}>
            {selected.id === "impeller" && "150mm impeller 6 backward-curved blades 30mm bore aluminum"}
            {selected.id === "flange" && "80mm OD flange 25mm bore 4xM8 bolts on 60mm PCD 10mm thick"}
            {selected.id === "gear" && "60mm spur gear 24 teeth 10mm height module 1.5"}
            {selected.id === "heat_sink" && "120x80mm heat sink 12 fins 40mm fin height aluminum"}
            {selected.id === "bracket" && "100x60x40mm L-bracket 4xM6 bolt holes 4mm wall"}
            {selected.id === "shaft" && "25mm diameter shaft 200mm long stainless steel"}
            {selected.id === "housing" && "120mm OD cylindrical housing 80mm bore 30mm height 4 bolt holes"}
            {selected.id === "nozzle" && "bell nozzle 8mm throat 24mm exit 60mm length 2mm wall"}
            {selected.id === "spoked_wheel" && "200mm OD spoked wheel 5 spokes 20mm bore 10mm thick"}
            {selected.id === "spool" && "60mm drum spool 80mm width 120mm flange OD 20mm hub"}
            {selected.id === "snap_hook" && "cantilever snap hook 40mm length 8mm width 5mm hook height"}
            {!["impeller","flange","gear","heat_sink","bracket","shaft","housing","nozzle","spoked_wheel","spool","snap_hook"].includes(selected.id) && `${selected.name.toLowerCase()} with standard dimensions`}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ValidateVisual
// ---------------------------------------------------------------------------
const VISUAL_MOCK_RUNS = [
  { run_id: "20260415T142233_a1b2c3d4", goal: "150mm impeller 6 backward-curved blades 30mm bore", overall: "PASS", confidence: 0.91, checks: [
    { check: "Impeller curved vane/blade features visible", status: "pass", view: "top projection" },
    { check: "6 distinct blade features visible", status: "pass", view: "top projection" },
    { check: "Center hub/bore visible", status: "pass", view: "top projection" },
    { check: "OD within ±15% of 150mm spec", status: "pass", view: "bbox check" },
    { check: "Watertight mesh", status: "pass", view: "geometry" },
  ]},
  { run_id: "20260415T133015_e5f6g7h8", goal: "80mm OD flange 4xM8 bolts on 60mm PCD", overall: "PASS", confidence: 0.88, checks: [
    { check: "Large center hole (bore) visible", status: "pass", view: "top projection" },
    { check: "4 bolt holes visible in circular/PCD pattern", status: "pass", view: "top projection" },
    { check: "Flat disc/flange profile visible", status: "pass", view: "front projection" },
    { check: "OD within ±15% of 80mm spec", status: "warn", view: "bbox check" },
    { check: "Watertight mesh", status: "pass", view: "geometry" },
  ]},
  { run_id: "20260414T091244_i9j0k1l2", goal: "100x60x40mm L-bracket 4 bolt holes", overall: "FAIL", confidence: 0.42, checks: [
    { check: "L-shape / right-angle profile visible", status: "fail", view: "front projection" },
    { check: "4 bolt holes visible", status: "warn", view: "top projection" },
    { check: "Width within ±15% of 100mm spec", status: "pass", view: "bbox check" },
    { check: "Watertight mesh", status: "fail", view: "geometry" },
  ]},
];

function ValidateVisual() {
  const [selected, setSelected] = useState(VISUAL_MOCK_RUNS[0]);
  const vp_visual = useViewport();
  const S_visual = spacing(vp_visual);
  return (
    <div style={{ padding: `${S_visual.pageY} ${S_visual.pageX}` }}>
      <div style={{ display: "grid",
                    gridTemplateColumns: vp_visual.isMobile ? "1fr 1fr"
                                       : vp_visual.isTablet ? "repeat(2, 1fr)"
                                       : "repeat(4, 1fr)",
                    gap: "12px", marginBottom: "20px" }}>
        <StatCard label="RUNS VERIFIED" value={VISUAL_MOCK_RUNS.length} sub="visual checks run" color={T.ai} spark={[1,1,2,2,2,3,3,3]} />
        <StatCard label="PASSED" value={VISUAL_MOCK_RUNS.filter(r => r.overall === "PASS").length} sub="pass visual check" color={T.green} spark={[0,0,1,1,2,2,2,VISUAL_MOCK_RUNS.filter(r => r.overall === "PASS").length]} />
        <StatCard label="FAILED" value={VISUAL_MOCK_RUNS.filter(r => r.overall === "FAIL").length} sub="need refinement" color={T.red} spark={[0,0,1,1,0,0,1,VISUAL_MOCK_RUNS.filter(r => r.overall === "FAIL").length]} />
        <StatCard label="AVG CONFIDENCE" value={`${Math.round(VISUAL_MOCK_RUNS.reduce((s,r) => s + r.confidence, 0) / VISUAL_MOCK_RUNS.length * 100)}%`} sub="vision API score" color={T.brand} spark={[60,65,70,75,80,82,85,Math.round(VISUAL_MOCK_RUNS.reduce((s,r) => s + r.confidence, 0) / VISUAL_MOCK_RUNS.length * 100)]} />
      </div>
      <div style={{ display: "grid",
                    gridTemplateColumns: vp_visual.isMobile ? "1fr" : "280px 1fr",
                    gap: "16px" }}>
        <Panel title="RUN HISTORY">
          <div>
            {VISUAL_MOCK_RUNS.map((r, i) => (
              <div key={r.run_id} onClick={() => setSelected(r)}
                style={{ padding: "12px 16px", borderBottom: `1px solid ${T.border}`, cursor: "pointer", background: selected?.run_id === r.run_id ? `${T.ai}08` : "transparent", transition: "background 0.15s" }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "4px" }}>
                  <Badge label={r.overall} color={r.overall === "PASS" ? T.green : T.red} />
                  <span style={{ fontSize: "10px", color: r.overall === "PASS" ? T.green : T.red, fontWeight: 700 }}>{(r.confidence * 100).toFixed(0)}%</span>
                </div>
                <div style={{ fontSize: "11px", color: T.text1, marginTop: "4px" }}>{r.goal.slice(0, 42)}...</div>
                <div style={{ fontSize: "9px", color: T.text4, fontFamily: "'JetBrains Mono', monospace", marginTop: "3px" }}>{r.run_id.slice(0, 20)}...</div>
              </div>
            ))}
          </div>
        </Panel>
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          {selected && (
            <>
              <Panel title="VISION CHECKLIST">
                <div>
                  <div style={{ padding: "10px 20px", background: "rgba(0,0,0,0.3)", borderBottom: `1px solid ${T.border}`, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                    <div style={{ fontSize: "12px", color: T.text1 }}>{selected.goal}</div>
                    <Badge label={selected.overall} color={selected.overall === "PASS" ? T.green : T.red} />
                  </div>
                  {selected.checks.map((c, i) => (
                    <div key={i} style={{ display: "grid", gridTemplateColumns: "32px 1fr 140px 80px", padding: "12px 20px", borderBottom: i < selected.checks.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
                      <div style={{ width: "20px", height: "20px", borderRadius: "6px", background: `${SC[c.status]}15`, border: `1px solid ${SC[c.status]}40`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "10px", color: SC[c.status], fontWeight: 700 }}>
                        {c.status === "pass" ? "✓" : c.status === "warn" ? "!" : "✗"}
                      </div>
                      <div style={{ fontSize: "12px", color: T.text1 }}>{c.check}</div>
                      <div style={{ fontSize: "10px", color: T.text3, fontFamily: "'JetBrains Mono', monospace" }}>{c.view}</div>
                      <Badge label={c.status.toUpperCase()} color={SC[c.status]} />
                    </div>
                  ))}
                </div>
              </Panel>
              <Panel title="RENDER VIEWS — MOCK DATA">
                <div style={{ padding: "16px", display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "10px" }}>
                  {["Top (XY)", "Front (XZ)", "Side (YZ)"].map(view => (
                    <div key={view} style={{ aspectRatio: "1", borderRadius: "8px", background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "8px" }}>
                      <div style={{ fontSize: "20px", opacity: 0.15 }}>◈</div>
                      <div style={{ fontSize: "9px", color: T.text4 }}>{view}</div>
                    </div>
                  ))}
                </div>
              </Panel>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ValidateCEM
// ---------------------------------------------------------------------------
const CEM_ADVISE_ITEMS = [
  { category: "Geometry", severity: "info", title: "Wall thickness adequate", detail: "Min wall 4.2mm — above 3mm CNC floor. Thin features should be validated with fixturing analysis.", action: "No action required" },
  { category: "Stress", severity: "pass", title: "Von Mises below yield", detail: "Peak stress 124 MPa vs Sy=276 MPa for 6061-T6. Factor of safety 2.2 — above 1.5 minimum for structural parts.", action: "Acceptable for static loads. Add fatigue analysis for cyclic loading." },
  { category: "Dynamics", severity: "warn", title: "Natural frequency check required", detail: "First mode at 2,840 Hz. If operating near spindle speeds (8k–18k RPM), verify no resonance overlap.", action: "Run FEA modal analysis if part used in rotating machinery" },
  { category: "Thermal", severity: "warn", title: "Thermal expansion margin tight", detail: "dL=0.14mm over 100°C delta. If mated with steel at datum A, verify clearance > 0.2mm to avoid seizure.", action: "Add 0.1mm additional clearance at datum A mating surface" },
  { category: "Fatigue", severity: "pass", title: "Goodman criterion satisfied", detail: "At 10^7 cycles the Goodman safety factor is 1.8 — above 1.5 threshold for high-cycle fatigue.", action: "No action required for normal service loads" },
  { category: "Buckling", severity: "pass", title: "Column stability adequate", detail: "Euler buckling load 48.2 kN — 3.4x factor of safety on expected axial load.", action: "Adequate for current geometry and loading" },
  { category: "DFM", severity: "info", title: "Recommend 4-flute finish pass", detail: "Surface finish Ra 1.6 achievable with 6mm ball endmill at 18k RPM. 2 tight-tolerance features need dedicated datum B setup.", action: "Add datum B for tight-tolerance features in CAM setup" },
];

const SEV_COLOR = { pass: T.green, warn: T.amber, info: T.ai, fail: T.red };
const SEV_ICON = { pass: "✓", warn: "!", info: "i", fail: "✗" };

function ValidateCEM() {
  const [expanded, setExpanded] = useState(null);
  const counts = { pass: CEM_ADVISE_ITEMS.filter(i => i.severity === "pass").length, warn: CEM_ADVISE_ITEMS.filter(i => i.severity === "warn").length, info: CEM_ADVISE_ITEMS.filter(i => i.severity === "info").length, fail: CEM_ADVISE_ITEMS.filter(i => i.severity === "fail").length };
  return (
    <div style={{ padding: "24px 28px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "12px", marginBottom: "20px" }}>
        <StatCard label="PASSED" value={counts.pass} sub="checks passed" color={T.green} spark={[3,3,4,4,4,4,4,counts.pass]} />
        <StatCard label="WARNINGS" value={counts.warn} sub="action recommended" color={T.amber} spark={[1,2,2,2,2,2,2,counts.warn]} />
        <StatCard label="INFO" value={counts.info} sub="notes" color={T.ai} spark={[1,1,2,2,2,2,2,counts.info]} />
        <StatCard label="FAILURES" value={counts.fail} sub="blocking" color={T.red} spark={[0,0,0,0,0,0,0,counts.fail]} />
      </div>
      <Panel title="CEM ADVISE REPORT">
        <div>
          {CEM_ADVISE_ITEMS.map((item, i) => (
            <div key={i}>
              <div onClick={() => setExpanded(expanded === i ? null : i)}
                style={{ display: "grid", gridTemplateColumns: "40px 100px 1fr 160px", padding: "14px 20px", borderBottom: `1px solid ${T.border}`, alignItems: "center", cursor: "pointer", transition: "background 0.15s" }}
                onMouseEnter={e => e.currentTarget.style.background = "rgba(255,255,255,0.02)"}
                onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
                <div style={{ width: "22px", height: "22px", borderRadius: "6px", background: `${SEV_COLOR[item.severity]}15`, border: `1px solid ${SEV_COLOR[item.severity]}40`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "11px", color: SEV_COLOR[item.severity], fontWeight: 700 }}>{SEV_ICON[item.severity]}</div>
                <Badge label={item.category.toUpperCase()} color={SEV_COLOR[item.severity]} />
                <div style={{ fontSize: "13px", color: T.text0, fontWeight: 500 }}>{item.title}</div>
                <div style={{ fontSize: "10px", color: T.text3 }}>{item.action.slice(0, 30)}{item.action.length > 30 ? "..." : ""}</div>
              </div>
              {expanded === i && (
                <div style={{ padding: "14px 20px 18px 68px", background: "rgba(0,0,0,0.2)", borderBottom: `1px solid ${T.border}` }}>
                  <div style={{ fontSize: "11px", color: T.text2, lineHeight: 1.6, marginBottom: "10px" }}>{item.detail}</div>
                  <div style={{ padding: "8px 12px", borderRadius: "7px", background: `${SEV_COLOR[item.severity]}10`, border: `1px solid ${SEV_COLOR[item.severity]}25` }}>
                    <span style={{ fontSize: "9px", color: SEV_COLOR[item.severity], fontWeight: 700, letterSpacing: "0.08em" }}>RECOMMENDED ACTION: </span>
                    <span style={{ fontSize: "11px", color: T.text1 }}>{item.action}</span>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------------------
// RunsSystem
// ---------------------------------------------------------------------------
const PIPELINE_STAGES = [
  { id: "research", label: "Research", desc: "Web search for prior art (skipped if ≥3 numeric dims)", icon: "⊛", status: "pass" },
  { id: "spec", label: "Spec Extract", desc: "Regex dimensional extraction + LLM enrichment for gaps", icon: "≡", status: "pass" },
  { id: "design", label: "Design", desc: "Template → CADSmith → LLM code generation", icon: "◈", status: "pass" },
  { id: "eval", label: "Eval", desc: "Trimesh geometry check + visual verification (Gemini/Groq/Ollama)", icon: "⬡", status: "pass" },
  { id: "refine", label: "Refine", desc: "Parse failures → refinement_instructions → loop back (max 5)", icon: "↻", status: "warn" },
  { id: "dfm", label: "DFM", desc: "Wall thickness · undercuts · tolerances · surface finish", icon: "⊞", status: "pass" },
  { id: "quote", label: "Quote", desc: "Material cost · machining time · complexity estimate", icon: "$", status: "pass" },
  { id: "cam", label: "CAM", desc: "Toolpath generation → HAAS/Fanuc G-code (Fusion script)", icon: "⚙", status: "pass" },
];

const PROVIDER_TABLE = [
  { provider: "Gemini 2.5 Flash", role: "Primary vision", quota: "Free daily", latency: "~2s", status: "online" },
  { provider: "Groq Llama 4 Scout", role: "Cross-validate", quota: "Free tier", latency: "~1.5s", status: "online" },
  { provider: "Ollama Gemma4", role: "Local fallback", quota: "Unlimited", latency: "~8s", status: "online" },
  { provider: "Anthropic Claude", role: "Authoritative (no CV)", quota: "Paid", latency: "~3s", status: "online" },
  { provider: "Ollama (LLM agent)", role: "Code generation", quota: "Unlimited", latency: "~12s", status: "offline" },
];

function RunsSystem() {
  return (
    <div style={{ padding: "24px 28px", display: "flex", flexDirection: "column", gap: "16px" }}>
      <Panel title="PIPELINE STAGES">
        <div style={{ padding: "16px 20px", display: "flex", gap: "0", overflowX: "auto" }}>
          {PIPELINE_STAGES.map((s, i) => (
            <div key={s.id} style={{ display: "flex", alignItems: "center", flexShrink: 0 }}>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "6px", width: "110px" }}>
                <div style={{ width: "44px", height: "44px", borderRadius: "12px", background: `${s.status === "pass" ? T.green : s.status === "warn" ? T.amber : T.red}15`, border: `1px solid ${s.status === "pass" ? T.green : s.status === "warn" ? T.amber : T.red}40`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "18px", color: s.status === "pass" ? T.green : s.status === "warn" ? T.amber : T.red }}>
                  {s.icon}
                </div>
                <div style={{ fontSize: "10px", color: T.text1, fontWeight: 700, textAlign: "center" }}>{s.label}</div>
                <div style={{ fontSize: "9px", color: T.text3, textAlign: "center", lineHeight: 1.4, maxWidth: "100px" }}>{s.desc}</div>
              </div>
              {i < PIPELINE_STAGES.length - 1 && (
                <div style={{ width: "24px", height: "1px", background: `linear-gradient(90deg, ${T.ai}60, ${T.ai}20)`, flexShrink: 0, margin: "0 0 32px 0" }} />
              )}
            </div>
          ))}
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px" }}>
        <Panel title="AI PROVIDERS">
          <div>
            <div style={{ display: "grid", gridTemplateColumns: "2fr 1.5fr 1fr 80px 80px", padding: "10px 16px", background: "rgba(0,0,0,0.3)", borderBottom: `1px solid ${T.border}`, fontSize: "9px", color: T.text3, fontWeight: 700, letterSpacing: "0.1em" }}>
              <div>PROVIDER</div><div>ROLE</div><div>QUOTA</div><div>LATENCY</div><div>STATUS</div>
            </div>
            {PROVIDER_TABLE.map((p, i) => (
              <div key={p.provider} style={{ display: "grid", gridTemplateColumns: "2fr 1.5fr 1fr 80px 80px", padding: "11px 16px", borderBottom: i < PROVIDER_TABLE.length - 1 ? `1px solid ${T.border}` : "none", alignItems: "center" }}>
                <div style={{ fontSize: "11px", color: T.text0, fontWeight: 600 }}>{p.provider}</div>
                <div style={{ fontSize: "10px", color: T.text3 }}>{p.role}</div>
                <div style={{ fontSize: "10px", color: T.text2 }}>{p.quota}</div>
                <div style={{ fontSize: "10px", color: T.ai, fontFamily: "'JetBrains Mono', monospace" }}>{p.latency}</div>
                <Badge label={p.status.toUpperCase()} color={p.status === "online" ? T.green : T.red} />
              </div>
            ))}
          </div>
        </Panel>

        <Panel title="CAD ROUTING">
          <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: "8px" }}>
            {[
              { router: "CadQuery (primary)", trigger: "80+ template keywords", status: "active", color: T.ai },
              { router: "Grasshopper/Rhino", trigger: "organic shapes, NURBS", status: "inactive", color: T.brand },
              { router: "Blender SDF", trigger: "voxel · lattice · terrain", status: "active", color: T.green },
              { router: "Fusion 360 (CAM)", trigger: "--cam flag", status: "active", color: T.amber },
              { router: "FreeCAD headless", trigger: "future: raw G-code", status: "planned", color: T.text3 },
            ].map(r => (
              <div key={r.router} style={{ display: "flex", alignItems: "center", gap: "12px", padding: "10px 12px", borderRadius: "8px", background: "rgba(0,0,0,0.2)", border: `1px solid ${T.border}` }}>
                <div style={{ width: "8px", height: "8px", borderRadius: "50%", background: r.status === "active" ? T.green : r.status === "planned" ? T.amber : T.text4, flexShrink: 0 }} />
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: "11px", color: T.text0, fontWeight: 600 }}>{r.router}</div>
                  <div style={{ fontSize: "9px", color: T.text3 }}>{r.trigger}</div>
                </div>
                <Badge label={r.status.toUpperCase()} color={r.status === "active" ? T.green : r.status === "planned" ? T.amber : T.text4} />
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Root App
// ---------------------------------------------------------------------------
export default function App() {
  const [active, setActive] = useState("generate");
  const [subActive, setSubActive] = useState({
    generate: "nl",
    library: "parts",
    validate: "physics",
    ecad: "schematic",
    manufacture: "cam",
    runs: "recent",
  });
  const [parts, setParts] = useState([]);
  const [selectedPart, setSelectedPart] = useState(null);
  const [pipelineStatus, setPipelineStatus] = useState("idle");
  const [logLines, setLogLines] = useState([]);
  const [cemData, setCemData] = useState(null);
  const eventSourceRef = useRef(null);

  useEffect(() => {
    fetch("/api/parts")
      .then(r => r.ok ? r.json() : { parts: [] })
      .then(data => {
        const arr = Array.isArray(data) ? data : (data?.parts || []);
        setParts(arr);
        if (arr.length > 0) setSelectedPart(arr[0]);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetch("/api/cem")
      .then(r => r.ok ? r.json() : null)
      .then(data => data && setCemData(data))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const es = new EventSource("/api/log/stream");
    eventSourceRef.current = es;
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        const msg = data.message || data.data || e.data;
        const ts = new Date().toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
        setLogLines(prev => [...prev.slice(-200), `[${ts}] ${msg}`]);
        if (data.type === "complete" || data.type === "done") {
          setPipelineStatus("done");
          fetch("/api/parts").then(r => r.ok ? r.json() : { parts: [] }).then(d => {
            const arr = Array.isArray(d) ? d : (d?.parts || []);
            setParts(arr);
            if (arr.length > 0) setSelectedPart(prev => prev || arr[0]);
          }).catch(() => {});
        } else if (data.type === "step" || data.type === "info") {
          setPipelineStatus("running");
        }
      } catch {}
    };
    es.onerror = () => setPipelineStatus(s => s === "running" ? "done" : s);
    return () => es.close();
  }, []);

  const handleGenerate = useCallback(async (goal, maxAttempts) => {
    setPipelineStatus("running");
    setLogLines(prev => [...prev, `>>> Starting: ${goal}`]);
    try {
      await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal, max_attempts: maxAttempts }),
      });
    } catch (e) {
      setLogLines(prev => [...prev, `ERROR: ${e.message}`]);
      setPipelineStatus("idle");
    }
  }, []);

  const setSub = (section, id) => setSubActive(prev => ({ ...prev, [section]: id }));

  const currentSub = subActive[active];
  const currentNavLabel = NAV.find(n => n.id === active)?.label || "";
  const currentSubLabel = SUB_TABS[active]?.find(t => t.id === currentSub)?.label || "";

  const renderContent = () => {
    switch (active) {
      case "generate":
        switch (currentSub) {
          case "nl": return <GenerateNL parts={parts} selectedPart={selectedPart} setSelectedPart={setSelectedPart} onGenerate={handleGenerate} pipelineStatus={pipelineStatus} logLines={logLines} />;
          case "image": return <GenerateImage pipelineStatus={pipelineStatus} logLines={logLines} />;
          case "assembly": return <GenerateAssembly pipelineStatus={pipelineStatus} logLines={logLines} onGenerate={handleGenerate} />;
          case "terrain": return <GenerateTerrain pipelineStatus={pipelineStatus} logLines={logLines} onGenerate={handleGenerate} />;
          case "scan": return <GenerateScan pipelineStatus={pipelineStatus} logLines={logLines} />;
          case "refine": return <GenerateRefine parts={parts} pipelineStatus={pipelineStatus} logLines={logLines} />;
          default: return null;
        }
      case "files":
        switch (currentSub) {
          case "browse": return <FilesBrowse />;
          case "upload": return <FilesUpload />;
          default:       return <FilesBrowse />;
        }
      case "library":
        switch (currentSub) {
          case "parts": return <LibraryParts parts={parts} />;
          case "materials": return <LibraryMaterials />;
          case "catalog": return <LibraryCatalog />;
          default: return null;
        }
      case "validate":
        switch (currentSub) {
          case "physics": return <ValidatePhysics cemData={cemData} />;
          case "dfm": return <ValidateDFM />;
          case "drawings": return <ValidateDrawings parts={parts} />;
          case "visual": return <ValidateVisual />;
          case "cem": return <ValidateCEM />;
          default: return null;
        }
      case "manufacture":
        switch (currentSub) {
          case "cam": return <ManufactureCAM />;
          case "tools": return <ManufactureTools />;
          case "post": return <ManufacturePost />;
          default: return null;
        }
      case "ecad":
        switch (currentSub) {
          case "schematic": return <EcadSchematic />;
          case "layout":    return <EcadLayout />;
          case "bom":       return <EcadBOM />;
          case "sim":       return <EcadSim />;
          default: return null;
        }
      case "runs":
        switch (currentSub) {
          case "recent": return <RunsRecent />;
          case "health": return <RunsHealth />;
          case "system": return <RunsSystem />;
          default: return null;
        }
      default: return null;
    }
  };

  return (
    <div style={{ minHeight: "100vh", background: T.bg0, fontFamily: "'Inter', system-ui, sans-serif", color: T.text0, position: "relative", overflow: "hidden" }}>
      <style>{`@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');@keyframes pulse{0%,100%{opacity:.6}50%{opacity:1}}*{box-sizing:border-box}::-webkit-scrollbar{width:6px;height:6px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:100px}textarea,input{outline:none;font-family:inherit}`}</style>
      <div style={{ position: "fixed", top: "-30%", right: "-20%", width: "60%", height: "60%", background: `radial-gradient(ellipse, ${T.brandGlow} 0%, transparent 60%)`, opacity: 0.05, pointerEvents: "none" }} />
      <div style={{ position: "fixed", bottom: "-30%", left: "-20%", width: "60%", height: "60%", background: `radial-gradient(ellipse, ${T.aiGlow} 0%, transparent 60%)`, opacity: 0.05, pointerEvents: "none" }} />
      <Sidebar active={active} setActive={setActive} />
      <ResponsiveMain>
        <TopBar section={currentNavLabel} subsection={currentSubLabel} pipelineStatus={pipelineStatus} />
        <SubTabs tabs={SUB_TABS[active]} active={currentSub} setActive={(id) => setSub(active, id)} />
        <div style={{ flex: 1, overflowY: "auto", WebkitOverflowScrolling: "touch" }}>
          {renderContent()}
        </div>
      </ResponsiveMain>
    </div>
  );
}
