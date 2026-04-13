import { useState, useEffect, useRef, useCallback } from "react";
import * as THREE from "three";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

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
  { id: "library",     label: "Library",     icon: "◰" },
  { id: "validate",    label: "Validate",    icon: "⬡" },
  { id: "ecad",        label: "ECAD",        icon: "⊞" },
  { id: "manufacture", label: "Manufacture", icon: "⚙" },
  { id: "runs",        label: "Runs",        icon: "≡" },
];

const SUB_TABS = {
  generate:    [{ id: "nl", label: "Natural Language" }, { id: "image", label: "From Image" }, { id: "assembly", label: "Assembly" }],
  library:     [{ id: "parts", label: "Parts" }, { id: "materials", label: "Materials" }],
  validate:    [{ id: "physics", label: "Physics" }, { id: "dfm", label: "DFM" }, { id: "drawings", label: "Drawings" }],
  ecad:        [{ id: "schematic", label: "Schematic" }, { id: "layout", label: "PCB Layout" }, { id: "bom", label: "BOM" }, { id: "sim", label: "Simulation" }],
  manufacture: [{ id: "cam", label: "CAM" }, { id: "tools", label: "Tools" }, { id: "post", label: "Post Processors" }],
  runs:        [{ id: "recent", label: "Recent Runs" }, { id: "health", label: "Health" }],
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
  return (
    <div style={{ display: "flex", gap: "4px", padding: "12px 28px", borderBottom: `1px solid ${T.border}`, background: "rgba(0,0,0,0.2)" }}>
      {tabs.map(t => (
        <button key={t.id} onClick={() => setActive(t.id)}
          style={{ padding: "6px 14px", borderRadius: "7px", border: `1px solid ${active === t.id ? T.ai : "transparent"}`, background: active === t.id ? `${T.ai}12` : "transparent", color: active === t.id ? T.ai : T.text3, fontSize: "12px", fontWeight: 600, cursor: "pointer", transition: "all 0.15s" }}>
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
  return (
    <div style={{ width: "64px", height: "100vh", position: "fixed", left: 0, top: 0, background: "rgba(15,15,24,0.8)", backdropFilter: "blur(20px)", borderRight: `1px solid ${T.border}`, display: "flex", flexDirection: "column", alignItems: "center", padding: "16px 0", zIndex: 100 }}>
      <div style={{ width: "36px", height: "36px", borderRadius: "10px", background: `linear-gradient(135deg, ${T.ai}, ${T.brand})`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "16px", color: "#fff", fontWeight: 700, marginBottom: "24px", boxShadow: `0 0 24px ${T.aiGlow}` }}>α</div>
      <div style={{ display: "flex", flexDirection: "column", gap: "4px", flex: 1 }}>
        {NAV.map(n => (
          <div key={n.id} style={{ position: "relative" }} onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)}>
            <button onClick={() => setActive(n.id)} style={{ width: "44px", height: "44px", borderRadius: "10px", border: "none", background: active === n.id ? `linear-gradient(135deg, ${T.ai}25, ${T.ai}10)` : "transparent", color: active === n.id ? T.ai : T.text3, fontSize: "16px", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", transition: "all 0.2s", boxShadow: active === n.id ? `inset 0 0 0 1px ${T.ai}40, 0 0 16px ${T.aiGlow}` : "none" }}>{n.icon}</button>
            {hover === n.id && <div style={{ position: "absolute", left: "52px", top: "50%", transform: "translateY(-50%)", padding: "6px 10px", background: T.bg3, border: `1px solid ${T.borderHi}`, borderRadius: "6px", fontSize: "11px", color: T.text1, fontWeight: 500, whiteSpace: "nowrap", pointerEvents: "none", zIndex: 200 }}>{n.label}</div>}
          </div>
        ))}
      </div>
      <div style={{ width: "36px", height: "36px", borderRadius: "9px", background: `linear-gradient(135deg, ${T.ai}20, ${T.brand}20)`, border: `1px solid ${T.borderHi}`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "10px", color: T.ai, fontWeight: 700 }}>AI</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// TopBar
// ---------------------------------------------------------------------------
function TopBar({ section, subsection, pipelineStatus }) {
  const [time, setTime] = useState(new Date());
  useEffect(() => { const i = setInterval(() => setTime(new Date()), 1000); return () => clearInterval(i); }, []);
  const statusColor = pipelineStatus === "running" ? T.amber : pipelineStatus === "done" ? T.green : T.text4;
  const statusLabel = pipelineStatus === "running" ? "GENERATING" : pipelineStatus === "done" ? "COMPLETE" : "IDLE";
  return (
    <div style={{ position: "sticky", top: 0, height: "56px", padding: "0 28px", background: "rgba(10,10,15,0.8)", backdropFilter: "blur(20px)", borderBottom: `1px solid ${T.border}`, display: "flex", justifyContent: "space-between", alignItems: "center", zIndex: 50 }}>
      <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "13px" }}>
        <span style={{ color: T.text3, fontWeight: 500 }}>ARIA-OS</span>
        <span style={{ color: T.text4 }}>/</span>
        <span style={{ color: T.text1, fontWeight: 500 }}>{section}</span>
        {subsection && <><span style={{ color: T.text4 }}>/</span><span style={{ color: T.text0, fontWeight: 600 }}>{subsection}</span></>}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "6px", padding: "6px 10px", borderRadius: "7px", background: `${statusColor}08`, border: `1px solid ${statusColor}30` }}>
          <div style={{ width: "5px", height: "5px", borderRadius: "50%", background: statusColor, boxShadow: `0 0 8px ${statusColor}`, animation: pipelineStatus === "running" ? "pulse 1s infinite" : "none" }} />
          <span style={{ fontSize: "10px", color: statusColor, fontWeight: 700, letterSpacing: "0.06em" }}>{statusLabel}</span>
        </div>
        <div style={{ fontSize: "12px", color: T.text2, fontFeatureSettings: "'tnum'", padding: "6px 10px", borderRadius: "7px", background: "rgba(255,255,255,0.03)", border: `1px solid ${T.border}` }}>{time.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false })}</div>
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

  useEffect(() => {
    if (!mountRef.current || !stlUrl) return;
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
    scene.add(new THREE.DirectionalLight(0x88ccff, 0.4).position.set(-100, -50, -100) && dir);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;

    const loader = new STLLoader();
    loader.load(stlUrl, (geometry) => {
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
      animId = requestAnimationFrame(animate);
      controls.update();
      renderer.render(scene, camera);
    };
    animate();
    sceneRef.current = { renderer, animId };

    return () => {
      cancelAnimationFrame(animId);
      renderer.dispose();
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

  return (
    <div style={{ padding: "20px 28px", display: "grid", gridTemplateColumns: "1fr 380px", gap: "16px", height: "calc(100vh - 56px - 49px)", overflow: "hidden" }}>
      {/* Left: 3D Viewer */}
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
        {parts.length > 0 && (
          <div style={{ display: "flex", gap: "8px", overflowX: "auto", paddingBottom: "4px", flexShrink: 0 }}>
            {parts.slice(0, 8).map((p, i) => (
              <button key={p.id || i} onClick={() => setSelectedPart(p)} style={{ flexShrink: 0, padding: "8px 14px", borderRadius: "8px", border: `1px solid ${selectedPart?.id === p.id ? T.ai : T.border}`, background: selectedPart?.id === p.id ? `${T.ai}12` : "rgba(255,255,255,0.02)", color: selectedPart?.id === p.id ? T.ai : T.text2, fontSize: "11px", fontWeight: 600, cursor: "pointer", whiteSpace: "nowrap" }}>
                {p.part_name || p.id || `Part ${i + 1}`}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Right: Generate form + log */}
      <div style={{ display: "flex", flexDirection: "column", gap: "12px", minHeight: 0 }}>
        <Panel title="GENERATE">
          <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: "10px" }}>
            <textarea
              value={goal}
              onChange={e => setGoal(e.target.value)}
              placeholder={"Describe the part you want to generate...\ne.g. 150mm impeller, 6 backward-curved blades, 30mm bore"}
              style={{ width: "100%", minHeight: "80px", background: "rgba(0,0,0,0.3)", border: `1px solid ${T.border}`, borderRadius: "8px", padding: "10px 12px", color: T.text1, fontSize: "12px", fontFamily: "inherit", resize: "vertical", outline: "none", lineHeight: 1.5, boxSizing: "border-box" }}
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

        <Panel title="PIPELINE LOG" style={{ flex: 1, minHeight: 0 }}>
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

  return (
    <div style={{ padding: "20px 28px", display: "grid", gridTemplateColumns: "1fr 380px", gap: "16px", height: "calc(100vh - 56px - 49px)", overflow: "hidden" }}>
      <Panel title="3D VIEWER" style={{ minHeight: 0 }}>
        <div style={{ height: "calc(100% - 41px)" }}>
          <STLViewer stlUrl={null} />
        </div>
      </Panel>

      <div style={{ display: "flex", flexDirection: "column", gap: "12px", minHeight: 0 }}>
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

  return (
    <div style={{ padding: "20px 28px", display: "flex", flexDirection: "column", gap: "16px", height: "calc(100vh - 56px - 49px)", overflow: "hidden" }}>
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

  return (
    <div style={{ padding: "20px 28px", display: "grid", gridTemplateColumns: "280px 1fr", gap: "16px", height: "calc(100vh - 56px - 49px)", overflow: "hidden" }}>
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
      .then(r => r.ok ? r.json() : [])
      .then(data => {
        const arr = Array.isArray(data) ? data : [];
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
          fetch("/api/parts").then(r => r.ok ? r.json() : []).then(d => {
            const arr = Array.isArray(d) ? d : [];
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
          default: return null;
        }
      case "library":
        switch (currentSub) {
          case "parts": return <LibraryParts parts={parts} />;
          case "materials": return <LibraryMaterials />;
          default: return null;
        }
      case "validate":
        switch (currentSub) {
          case "physics": return <ValidatePhysics cemData={cemData} />;
          case "dfm": return <ValidateDFM />;
          case "drawings": return <ValidateDrawings parts={parts} />;
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
      <div style={{ marginLeft: "64px", height: "100vh", overflow: "hidden", display: "flex", flexDirection: "column" }}>
        <TopBar section={currentNavLabel} subsection={currentSubLabel} pipelineStatus={pipelineStatus} />
        <SubTabs tabs={SUB_TABS[active]} active={currentSub} setActive={(id) => setSub(active, id)} />
        <div style={{ flex: 1, overflowY: "auto" }}>
          {renderContent()}
        </div>
      </div>
    </div>
  );
}
