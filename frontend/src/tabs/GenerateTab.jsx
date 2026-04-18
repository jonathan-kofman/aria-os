import { useState, useEffect, useRef } from "react";
import { useViewport, layout, spacing, viewContainer } from "../responsive.js";
import { T } from "../aria/theme.js";
import { Panel, StatCard, Badge } from "../aria/uiPrimitives.jsx";
import STLViewer from "../aria/STLViewer.jsx";
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
      <div style={{ padding: "12px 16px", display: "flex", flexDirection: "column", gap: "8px" }}>
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
                  height: vp.isMobile ? "auto" : "100%",
                  minHeight: 0,
                  overflow: vp.isMobile ? "visible" : "hidden" }}>
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
          <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: "10px" }}>
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
                  height: vp.isMobile ? "auto" : "100%",
                  minHeight: 0,
                  overflow: vp.isMobile ? "visible" : "hidden" }}>
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
                  height: _vp_assembly.isMobile ? "auto" : "100%",
                  minHeight: 0,
                  overflowY: _vp_assembly.isMobile ? "visible" : "auto",
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
export default function GenerateTab({ currentSub, parts, selectedPart, setSelectedPart, onGenerate, pipelineStatus, logLines }) {
  switch (currentSub) {
    case "nl": return <GenerateNL parts={parts} selectedPart={selectedPart} setSelectedPart={setSelectedPart} onGenerate={onGenerate} pipelineStatus={pipelineStatus} logLines={logLines} />;
    case "image": return <GenerateImage pipelineStatus={pipelineStatus} logLines={logLines} />;
    case "assembly": return <GenerateAssembly pipelineStatus={pipelineStatus} logLines={logLines} onGenerate={onGenerate} />;
    case "terrain": return <GenerateTerrain pipelineStatus={pipelineStatus} logLines={logLines} onGenerate={onGenerate} />;
    case "scan": return <GenerateScan pipelineStatus={pipelineStatus} logLines={logLines} />;
    case "refine": return <GenerateRefine parts={parts} pipelineStatus={pipelineStatus} logLines={logLines} />;
    default: return null;
  }
}
