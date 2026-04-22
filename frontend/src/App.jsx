import { useState, useEffect, useRef, useCallback, lazy, Suspense } from "react";
import { api } from "./aria/apiConfig";
import { useViewport, spacing, viewContainer } from "./responsive.js";
import { T } from "./aria/theme.js";
import { NAV, SUB_TABS } from "./aria/nav.js";
import { StatCard, Panel, Badge } from "./aria/uiPrimitives.jsx";
import { Sidebar, SubTabs, ResponsiveMain, TopBar } from "./aria/layout.jsx";
import STLViewer from "./aria/STLViewer.jsx";

const FilesTab = lazy(() => import("./tabs/FilesTab.jsx"));
const GenerateTab = lazy(() => import("./tabs/GenerateTab.jsx"));
const AgentTab = lazy(() => import("./tabs/AgentTab.jsx"));

function TabFallback() {
  return (
    <div style={{ padding: "48px 28px", color: T.text3, fontSize: "13px", textAlign: "center" }}>
      Loading…
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
          <div style={{ flex: 1, minHeight: 0 }}>
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
      const res = await fetch(api(`/parts/${selectedPart.id}/drawing`));
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
    fetch(api("/parts"))
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
      fetch(api("/sessions")).then(r => r.ok ? r.json() : { sessions: [] }).catch(() => ({ sessions: [] })),
      fetch(api("/parts")).then(r => r.ok ? r.json() : { parts: [] }).catch(() => ({ parts: [] })),
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
  const generateStreamRef = useRef(null);

  const appendPipelineLog = useCallback((line) => {
    const ts = new Date().toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    setLogLines((prev) => [...prev.slice(-200), `[${ts}] ${line}`]);
  }, []);

  const refreshParts = useCallback(() => {
    fetch(api("/parts"))
      .then((r) => (r.ok ? r.json() : { parts: [] }))
      .then((d) => {
        const arr = Array.isArray(d) ? d : (d?.parts || []);
        setParts(arr);
        if (arr.length > 0) setSelectedPart((prev) => prev || arr[0]);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetch(api("/parts"))
      .then(r => r.ok ? r.json() : { parts: [] })
      .then(data => {
        const arr = Array.isArray(data) ? data : (data?.parts || []);
        setParts(arr);
        if (arr.length > 0) setSelectedPart(arr[0]);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetch(api("/cem"))
      .then(r => r.ok ? r.json() : null)
      .then(data => data && setCemData(data))
      .catch(() => {});
  }, []);

  useEffect(() => () => {
    if (generateStreamRef.current) {
      generateStreamRef.current.close();
      generateStreamRef.current = null;
    }
  }, []);

  const streamRun = useCallback(async (post) => {
    setPipelineStatus("running");
    if (generateStreamRef.current) {
      generateStreamRef.current.close();
      generateStreamRef.current = null;
    }
    try {
      const res = await post();
      if (!res.ok) {
        let detail = "";
        try { detail = (await res.json())?.detail || ""; } catch { /* noop */ }
        throw new Error(`HTTP ${res.status}${detail ? ` — ${detail}` : ""}`);
      }
      const data = await res.json().catch(() => ({}));
      const runId = data.run_id;
      if (!runId) {
        appendPipelineLog("(no run_id from server — live log unavailable; try dashboard on :8001)");
        setPipelineStatus("idle");
        return null;
      }
      const es = new EventSource(`/api/run/${runId}/stream`);
      generateStreamRef.current = es;
      es.onmessage = (e) => {
        try {
          const payload = JSON.parse(e.data);
          if (payload.done) {
            setPipelineStatus(payload.status === "done" ? "done" : "idle");
            refreshParts();
            es.close();
            if (generateStreamRef.current === es) generateStreamRef.current = null;
            return;
          }
          const msg = payload.text ?? payload.message ?? payload.data;
          if (msg) appendPipelineLog(String(msg));
        } catch {
          appendPipelineLog(e.data);
        }
      };
      es.onerror = () => {
        es.close();
        if (generateStreamRef.current === es) generateStreamRef.current = null;
        setPipelineStatus((s) => (s === "running" ? "idle" : s));
      };
      return runId;
    } catch (e) {
      appendPipelineLog(`ERROR: ${e.message}`);
      setPipelineStatus("idle");
      return null;
    }
  }, [appendPipelineLog, refreshParts]);

  const handleGenerate = useCallback((goal, maxAttempts) => {
    appendPipelineLog(`>>> Starting: ${goal}`);
    return streamRun(() => fetch(api("/generate"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal, max_attempts: maxAttempts }),
    }));
  }, [streamRun, appendPipelineLog]);

  const setSub = (section, id) => setSubActive(prev => ({ ...prev, [section]: id }));

  const currentSub = subActive[active];
  const currentNavLabel = NAV.find(n => n.id === active)?.label || "";
  const currentSubLabel = SUB_TABS[active]?.find(t => t.id === currentSub)?.label || "";

  const renderContent = () => {
    switch (active) {
      case "generate":
        return (
          <Suspense fallback={<TabFallback />}>
            <GenerateTab
              currentSub={currentSub}
              parts={parts}
              selectedPart={selectedPart}
              setSelectedPart={setSelectedPart}
              onGenerate={handleGenerate}
              pipelineStatus={pipelineStatus}
              logLines={logLines}
              appendPipelineLog={appendPipelineLog}
              setPipelineStatus={setPipelineStatus}
              refreshParts={refreshParts}
              streamRun={streamRun}
            />
          </Suspense>
        );
      case "agent":
        return (
          <Suspense fallback={<TabFallback />}>
            <AgentTab subsection={currentSub} />
          </Suspense>
        );
      case "files":
        return (
          <Suspense fallback={<TabFallback />}>
            <FilesTab currentSub={currentSub} />
          </Suspense>
        );
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
        <div
          style={{
            flex: 1,
            minHeight: 0,
            overflowY: "auto",
            overflowX: "hidden",
            WebkitOverflowScrolling: "touch",
          }}
        >
          {renderContent()}
        </div>
      </ResponsiveMain>
    </div>
  );
}