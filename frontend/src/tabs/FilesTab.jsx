import { useState, useEffect, useRef } from "react";
import { useViewport, layout, spacing, viewContainer } from "../responsive.js";
import { T } from "../aria/theme.js";
import { Panel, Badge } from "../aria/uiPrimitives.jsx";
import STLViewer from "../aria/STLViewer.jsx";
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
                  height: vp.isMobile ? "auto" : "100%",
                  minHeight: 0,
                  overflow: vp.isMobile ? "visible" : "hidden" }}>
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
        <div style={{ flex: 1, minHeight: 0 }}>
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
export default function FilesTab({ currentSub }) {
  switch (currentSub) {
    case "browse": return <FilesBrowse />;
    case "upload": return <FilesUpload />;
    default: return <FilesBrowse />;
  }
}
