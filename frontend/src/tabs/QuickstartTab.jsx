/**
 * QuickstartTab.jsx — single-textarea launchpad for ARIA-OS.
 *
 * Route: /quickstart  (handled in main.jsx — not a sub-tab of App)
 *
 * Goal: match Blueprint.am-style input simplicity. The user lands on a
 * blank page, types one sentence describing what they want to build,
 * hits Enter, and watches the existing ARIA pipeline run end-to-end.
 *
 * Reuses (does NOT modify) the same backend SSE + artifact patterns
 * that ChatPanel.jsx uses — just wrapped in a much simpler UI.
 *
 *   1. POST /api/v1/quickstart/generate  (thin wrapper around /api/generate)
 *   2. Subscribe to /api/log/stream for live pipeline progress
 *   3. Render five result panels: MCAD / Schematic / BOM / DFM / Quote
 *   4. Each panel degrades gracefully — if ECAD never produces output,
 *      the Schematic panel says "ECAD pipeline did not run" instead of
 *      crashing the page. MCAD is independent.
 *
 * No emojis in code; ASCII-only icons. Inline styles to avoid pulling in
 * the heavier dashboard layout system.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, API_BASE } from "../aria/apiConfig";
import bridge from "../aria/bridge";
import STLViewer from "../aria/STLViewer.jsx";

// ---------------------------------------------------------------------------
// Theme — borrows the dashboard palette (T) but kept inline so this file
// has no dependency on App.jsx initialization order.
// ---------------------------------------------------------------------------
const C = {
  bg0:    "#0A0A0F",
  bg1:    "#13131A",
  bg2:    "#1A1A22",
  border: "rgba(255,255,255,0.08)",
  borderHi: "rgba(255,255,255,0.18)",
  text0:  "#F5F5F7",
  text1:  "#D8DAE3",
  text2:  "#9CA3B0",
  text3:  "#6B7180",
  brand:  "#FF7A1A",
  brandSoft: "rgba(255,122,26,0.12)",
  ai:     "#00D4FF",
  green:  "#5BD17A",
  amber:  "#FFB84A",
  red:    "#FF6B6B",
};

const FONT = "'Inter', system-ui, -apple-system, sans-serif";
const FONT_MONO = "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace";

// ---------------------------------------------------------------------------
// SSE event classification (lifted minimal subset from ChatPanel.jsx).
// We only need a small read of the event stream — when something looks
// like it produced an artifact, surface it. The pipeline emits events
// like "STEP exported", "STL exported", "Pipeline complete", etc.
// ---------------------------------------------------------------------------
function _phaseOf(msg) {
  const low = (msg || "").toLowerCase();
  if (low.startsWith("received goal")) return "intake";
  if (low.startsWith("quickstart"))    return "intake";
  if (low.startsWith("mode:"))         return "intake";
  if (low.startsWith("template matched"))      return "template";
  if (low.startsWith("template code emitted")) return "template";
  if (low.startsWith("script written"))        return "script";
  if (low.startsWith("executing cadquery"))    return "exec";
  if (low.startsWith("geometry valid"))        return "geom";
  if (low.startsWith("step exported"))         return "export";
  if (low.startsWith("stl exported"))          return "export";
  if (low.startsWith("planning"))              return "plan";
  if (low.startsWith("tool:"))                 return "route";
  if (low.startsWith("dfm"))                   return "dfm";
  if (low.startsWith("quote"))                 return "quote";
  if (low.startsWith("ecad") || low.includes("kicad")) return "ecad";
  if (low.startsWith("pipeline complete"))     return "complete";
  if (low.startsWith("pipeline error"))        return "error";
  return "step";
}

// ---------------------------------------------------------------------------
// Section card — graceful-degradation wrapper. If the section's artifact
// hasn't arrived yet, render a placeholder that explains why.
// ---------------------------------------------------------------------------
function ResultSection({ title, status, children, hint }) {
  const isError = status === "error";
  const isPending = status === "pending";
  const accent = isError ? C.amber : (isPending ? C.text3 : C.ai);
  return (
    <div style={{
      background: C.bg1,
      border: `1px solid ${C.border}`,
      borderRadius: 14,
      overflow: "hidden",
      display: "flex",
      flexDirection: "column",
    }}>
      <div style={{
        padding: "12px 16px",
        borderBottom: `1px solid ${C.border}`,
        display: "flex",
        alignItems: "center",
        gap: 10,
        background: "rgba(255,255,255,0.02)",
      }}>
        <div style={{
          width: 6, height: 6, borderRadius: "50%",
          background: accent,
          boxShadow: status === "ready" ? `0 0 8px ${accent}` : "none",
        }} />
        <div style={{
          fontSize: 11, fontWeight: 700, letterSpacing: "0.12em",
          color: C.text1,
        }}>{title}</div>
        <div style={{ flex: 1 }} />
        <div style={{
          fontSize: 10, color: accent, textTransform: "uppercase",
          letterSpacing: "0.08em", fontWeight: 600,
        }}>
          {status === "ready"   && "ready"}
          {status === "pending" && "generating"}
          {status === "error"   && "skipped"}
          {status === "idle"    && "queued"}
        </div>
      </div>
      <div style={{ padding: 16, minHeight: 60 }}>
        {isPending && (
          <div style={{ color: C.text3, fontSize: 13 }}>
            Generating… (pipeline still running)
          </div>
        )}
        {isError && (
          <div style={{ color: C.text2, fontSize: 13 }}>
            {hint || "This stage was not produced. Other stages may still complete."}
          </div>
        )}
        {status === "idle" && (
          <div style={{ color: C.text3, fontSize: 13 }}>
            Waiting for pipeline to start.
          </div>
        )}
        {status === "ready" && children}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Voice + image affordances. Reuses bridge.recordAudio when hosted (Fusion
// / Rhino), falls back to file inputs when running standalone in the
// browser (which is the YC-demo case).
// ---------------------------------------------------------------------------
function MicChip({ onTranscript, disabled }) {
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);

  const handleClick = async () => {
    if (busy || disabled) return;
    if (bridge.isHosted) {
      setBusy(true);
      try {
        const reply = await bridge.recordAudio(30);
        const transcript = reply?.transcript || reply?.text;
        if (transcript) onTranscript(transcript);
      } catch (e) {
        // Fall through to file input as a graceful fallback
        fileRef.current?.click();
      } finally {
        setBusy(false);
      }
    } else {
      // Standalone web: upload an audio file to the same STT endpoint
      fileRef.current?.click();
    }
  };

  const handleFile = async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("audio", f);
      const r = await fetch(api("/stt/transcribe"), { method: "POST", body: fd });
      if (r.ok) {
        const j = await r.json();
        if (j?.transcript) onTranscript(j.transcript);
      }
    } catch { /* non-fatal — silent */ }
    finally { setBusy(false); e.target.value = ""; }
  };

  return (
    <>
      <button onClick={handleClick} disabled={busy || disabled}
        title="Voice input"
        style={{
          padding: "7px 12px", borderRadius: 999,
          background: busy ? C.brandSoft : "transparent",
          border: `1px solid ${C.border}`,
          color: busy ? C.brand : C.text2,
          fontSize: 12, cursor: disabled ? "not-allowed" : "pointer",
          fontFamily: FONT,
          display: "inline-flex", alignItems: "center", gap: 6,
        }}>
        <span aria-hidden="true">{busy ? "..." : "[mic]"}</span>
        <span>{busy ? "Listening" : "Voice"}</span>
      </button>
      <input ref={fileRef} type="file" accept="audio/*"
              onChange={handleFile} style={{ display: "none" }} />
    </>
  );
}

function ImageChip({ onSubmit, disabled }) {
  const fileRef = useRef(null);
  const [busy, setBusy] = useState(false);

  const handleFile = async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("image", f);
      fd.append("prompt", "Recreate this part as accurately as possible");
      fd.append("mode", "native");
      fd.append("quality_tier", "balanced");
      const r = await fetch(api("/image_to_cad"), {
        method: "POST", body: fd,
      });
      if (r.ok) {
        const j = await r.json();
        // Push the (now-tagged-as-quickstart) goal into the same results
        // flow. We let the parent component subscribe to the same
        // /api/log/stream that all pipelines emit on.
        onSubmit({ kind: "image", filename: f.name, body: j });
      }
    } catch { /* non-fatal */ }
    finally { setBusy(false); e.target.value = ""; }
  };

  return (
    <>
      <button onClick={() => fileRef.current?.click()} disabled={busy || disabled}
        title="Upload reference image"
        style={{
          padding: "7px 12px", borderRadius: 999,
          background: "transparent",
          border: `1px solid ${C.border}`,
          color: busy ? C.brand : C.text2,
          fontSize: 12, cursor: disabled ? "not-allowed" : "pointer",
          fontFamily: FONT,
          display: "inline-flex", alignItems: "center", gap: 6,
        }}>
        <span aria-hidden="true">{busy ? "..." : "[img]"}</span>
        <span>{busy ? "Uploading" : "Image"}</span>
      </button>
      <input ref={fileRef} type="file" accept="image/*"
              onChange={handleFile} style={{ display: "none" }} />
    </>
  );
}

// ---------------------------------------------------------------------------
// ClarifyView — shown when /api/clarify identifies missing critical
// fields. Renders one row per question with options (radio chips) or a
// free-text fallback. User can answer none/some/all and hit Generate;
// answers get appended to the goal as a "## Clarifications" block.
// ---------------------------------------------------------------------------
function ClarifyView({ data, answers, setAnswers, onGenerate, onSkip, onBack }) {
  const update = (field, value) => {
    setAnswers(prev => ({ ...prev, [field]: value }));
  };
  const allAnswered = data.clarifications.every(
    c => (answers[c.field] || "").toString().trim() !== "");

  return (
    <div style={{
      minHeight: "100vh", background: C.bg0, color: C.text0,
      fontFamily: FONT, padding: "48px 24px",
      display: "flex", flexDirection: "column", alignItems: "center",
    }}>
      <style>{`@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');*{box-sizing:border-box}textarea,input,select{outline:none;font-family:inherit}`}</style>
      <div style={{ maxWidth: 760, width: "100%" }}>
        <div style={{
          fontSize: 11, letterSpacing: "0.18em", color: C.brand,
          fontFamily: FONT_MONO, marginBottom: 12, textTransform: "uppercase",
        }}>
          ARIA-OS - clarify
        </div>
        <h1 style={{
          fontSize: "clamp(22px, 3vw, 30px)", fontWeight: 700,
          margin: "0 0 6px", letterSpacing: "-0.02em",
        }}>
          A few specifics first.
        </h1>
        <div style={{
          color: C.text2, fontSize: 14, marginBottom: 8,
        }}>
          {data.summary || data.goal}
        </div>
        {data.part_family && (
          <div style={{
            display: "inline-block", padding: "3px 10px",
            background: C.brandSoft, color: C.brand,
            borderRadius: 100, fontSize: 11, fontFamily: FONT_MONO,
            marginBottom: 24,
          }}>
            detected: {data.part_family}
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          {data.clarifications.map((c, i) => (
            <div key={c.field || i} style={{
              padding: "16px 18px",
              background: C.bg1,
              border: `1px solid ${C.border}`,
              borderRadius: 12,
            }}>
              <div style={{
                fontSize: 14, fontWeight: 600, marginBottom: 4,
                color: C.text0,
              }}>
                {c.question}
              </div>
              {c.rationale && (
                <div style={{
                  fontSize: 12, color: C.text3, marginBottom: 12,
                  fontStyle: "italic",
                }}>
                  {c.rationale}
                </div>
              )}
              {(c.options && c.options.length > 0) ? (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {c.options.map(opt => {
                    const ov = typeof opt === "object"
                      ? (opt.value ?? opt.label ?? "")
                      : opt;
                    const ol = typeof opt === "object"
                      ? (opt.label ?? opt.value ?? "")
                      : opt;
                    const selected = String(answers[c.field] || "") === String(ov);
                    return (
                      <button key={String(ov)}
                        onClick={() => update(c.field, ov)}
                        style={{
                          padding: "7px 14px",
                          borderRadius: 100,
                          border: `1px solid ${selected ? C.brand : C.border}`,
                          background: selected ? C.brandSoft : C.bg2,
                          color: selected ? C.brand : C.text1,
                          fontSize: 12, cursor: "pointer",
                          fontFamily: FONT,
                        }}>
                        {ol}
                      </button>
                    );
                  })}
                </div>
              ) : (
                <input type="text"
                  value={answers[c.field] || ""}
                  onChange={e => update(c.field, e.target.value)}
                  placeholder={c.default ? `default: ${c.default}` : "your answer"}
                  style={{
                    width: "100%", padding: "8px 12px",
                    background: C.bg2, color: C.text0,
                    border: `1px solid ${C.border}`,
                    borderRadius: 8, fontSize: 13,
                  }} />
              )}
            </div>
          ))}
        </div>

        <div style={{
          marginTop: 28, display: "flex", gap: 10, alignItems: "center",
          justifyContent: "flex-end",
        }}>
          <button onClick={onBack}
            style={{
              padding: "9px 16px", borderRadius: 8,
              border: `1px solid ${C.border}`, background: "transparent",
              color: C.text2, fontSize: 12, cursor: "pointer", fontFamily: FONT,
            }}>
            Back
          </button>
          <button onClick={onSkip}
            title="Generate using engineering defaults instead of answering"
            style={{
              padding: "9px 16px", borderRadius: 8,
              border: `1px solid ${C.border}`, background: C.bg2,
              color: C.text1, fontSize: 12, cursor: "pointer", fontFamily: FONT,
            }}>
            Skip - use defaults
          </button>
          <button onClick={onGenerate}
            style={{
              padding: "10px 22px", borderRadius: 10, border: "none",
              background: `linear-gradient(135deg, ${C.brand}, #FF9D4A)`,
              color: "#0A0A0F", fontSize: 13, fontWeight: 700,
              letterSpacing: "0.04em", cursor: "pointer", fontFamily: FONT,
            }}>
            {allAnswered ? "Generate ->" : "Generate with these ->"}
          </button>
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export default function QuickstartTab() {
  // Phase: "intro" (textarea)
  //     -> "clarify" (LLM identified missing critical fields, render Q form)
  //     -> "running" (results view + SSE stream)
  const [phase, setPhase] = useState("intro");
  const [goal, setGoal] = useState("");
  const [running, setRunning] = useState(false);
  // Clarification state -- populated by /api/clarify when prompt is
  // ambiguous. `clarifyData` holds the questions; `clarifyAnswers`
  // accumulates the user's answers as they fill the form.
  const [clarifyLoading, setClarifyLoading] = useState(false);
  const [clarifyData, setClarifyData] = useState(null);
  const [clarifyAnswers, setClarifyAnswers] = useState({});
  const [events, setEvents] = useState([]);
  const [pipelineStatus, setPipelineStatus] = useState("idle");
  const streamRef = useRef(null);

  // Per-section state, all updated from the same SSE stream so the
  // failure of one (e.g. ECAD) never blocks the others.
  const [mcadArtifact, setMcadArtifact] = useState(null);
  const [ecadArtifact, setEcadArtifact] = useState(null);
  const [bomRows, setBomRows]           = useState(null);
  const [dfmIssues, setDfmIssues]       = useState(null);
  const [quoteData, setQuoteData]       = useState(null);

  // Independent error markers — each section can flip to "error" without
  // affecting the others. This is the graceful-degradation guarantee.
  const [sectionStatus, setSectionStatus] = useState({
    mcad: "idle", ecad: "idle", bom: "idle", dfm: "idle", quote: "idle",
  });

  const setSection = useCallback((key, status) => {
    setSectionStatus(prev => prev[key] === status ? prev : { ...prev, [key]: status });
  }, []);

  // Cleanup on unmount
  useEffect(() => () => {
    if (streamRef.current) {
      try { streamRef.current.close(); } catch { /* noop */ }
      streamRef.current = null;
    }
  }, []);

  // ------------------------------------------------------------------
  // runPipelineWithGoal -- internal helper that does the actual work.
  // POSTs the goal to /api/v1/quickstart/generate and opens the shared
  // /api/log/stream SSE channel. Caller is responsible for goal text
  // (already augmented with clarifications when applicable).
  // ------------------------------------------------------------------
  const runPipelineWithGoal = useCallback(async (goalText) => {
    const trimmed = (goalText || "").trim();
    if (!trimmed || running) return;
    setRunning(true);
    setPhase("running");
    setEvents([]);
    setPipelineStatus("running");
    // Reset sections to "pending" so the UI shows generating spinners
    setSectionStatus({
      mcad: "pending", ecad: "pending", bom: "pending",
      dfm: "pending", quote: "pending",
    });
    setMcadArtifact(null);
    setEcadArtifact(null);
    setBomRows(null);
    setDfmIssues(null);
    setQuoteData(null);

    // Capture starting seq so we IGNORE replay events from prior runs
    // (the SSE handshake replays the last 30 history events).
    let startSeq = 0;
    try {
      const r = await fetch(api("/log/recent?n=1"));
      if (r.ok) {
        const d = await r.json();
        const last = (d.events || [])[0];
        startSeq = (last && last.seq) || 0;
      }
    } catch { /* non-fatal — 0 means accept all */ }
    const seenSeqs = new Set();

    try {
      const res = await fetch(api("/v1/quickstart/generate"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          goal: trimmed,
          mode: "text",
          quality_tier: "balanced",
          max_attempts: 3,
        }),
      });
      if (!res.ok) {
        let detail = "";
        try { detail = (await res.json())?.detail || ""; }
        catch { /* noop */ }
        throw new Error(`HTTP ${res.status}${detail ? " " + detail : ""}`);
      }

      // Open SSE stream — same endpoint dashboard + ChatPanel use
      const es = new EventSource(api("/log/stream"));
      streamRef.current = es;

      es.onmessage = (e) => {
        try {
          const payload = JSON.parse(e.data);
          const seq = payload.seq;
          if (typeof seq === "number") {
            if (seq <= startSeq) return;
            if (seenSeqs.has(seq)) return;
            seenSeqs.add(seq);
          }
          const msg = payload.message || payload.text || payload.data || "";
          const data = payload.data || {};
          const phase = _phaseOf(typeof msg === "string" ? msg : "");

          setEvents(prev => [...prev.slice(-200), { ...payload, _phase: phase }]);

          // ---- MCAD: STEP / STL exports + bbox ----
          if (typeof msg === "string") {
            const low = msg.toLowerCase();
            if (low.startsWith("step exported") && data.path) {
              setMcadArtifact(prev => ({
                ...(prev || {}),
                step_path: data.path,
                step_url: api("/artifacts/download?path="
                                + encodeURIComponent(data.path)),
                part_id: data.part_id || prev?.part_id,
              }));
              setSection("mcad", "ready");
            }
            if (low.startsWith("stl exported") && data.path) {
              setMcadArtifact(prev => ({
                ...(prev || {}),
                stl_path: data.path,
                stl_url: api("/artifacts/download?path="
                                + encodeURIComponent(data.path)),
                part_id: data.part_id || prev?.part_id,
              }));
              setSection("mcad", "ready");
            }
            if (low.startsWith("geometry valid") && data.bbox) {
              setMcadArtifact(prev => ({ ...(prev || {}), bbox: data.bbox }));
            }
            // ---- ECAD ----
            if (low.startsWith("ecad generation complete") ||
                low.includes("kicad_pcb") || low.startsWith("schematic exported")) {
              setEcadArtifact(prev => ({
                ...(prev || {}),
                pcb_path: data.pcb_path || data.path,
                bom_path: data.bom_path,
              }));
              setSection("ecad", "ready");
            }
            // ---- BOM ----
            if (low.startsWith("bom") && (data.rows || data.items)) {
              setBomRows(data.rows || data.items);
              setSection("bom", "ready");
            }
            // ---- DFM ----
            if (low.startsWith("dfm") && (data.issues || data.checks)) {
              setDfmIssues(data.issues || data.checks);
              setSection("dfm", "ready");
            }
            // ---- Quote ----
            if (low.startsWith("quote") && (data.unit_price || data.total)) {
              setQuoteData(data);
              setSection("quote", "ready");
            }
            // ---- Pipeline complete ----
            if (low.startsWith("pipeline complete")) {
              // For any section still pending at the end, mark as
              // "error" (skipped) — graceful degradation guarantee:
              // missing ECAD output never breaks the MCAD section.
              setSectionStatus(prev => {
                const next = { ...prev };
                for (const k of Object.keys(next)) {
                  if (next[k] === "pending") next[k] = "error";
                }
                return next;
              });
              setPipelineStatus("done");
              setRunning(false);
              try { es.close(); } catch { /* noop */ }
              if (streamRef.current === es) streamRef.current = null;
            }
            if (low.startsWith("pipeline error")) {
              setSectionStatus(prev => {
                const next = { ...prev };
                for (const k of Object.keys(next)) {
                  if (next[k] === "pending") next[k] = "error";
                }
                return next;
              });
              setPipelineStatus("error");
              setRunning(false);
              try { es.close(); } catch { /* noop */ }
              if (streamRef.current === es) streamRef.current = null;
            }
          }

          // Terminal "complete" event variant (per-run streams)
          if (payload.done) {
            setPipelineStatus(payload.status === "done" ? "done" : "error");
            setRunning(false);
            try { es.close(); } catch { /* noop */ }
            if (streamRef.current === es) streamRef.current = null;
          }
        } catch {
          /* swallow: malformed event lines (heartbeats etc.) are fine */
        }
      };

      es.onerror = () => {
        try { es.close(); } catch { /* noop */ }
        if (streamRef.current === es) streamRef.current = null;
        // Don't flip everything to error on a single transport blip;
        // the pipeline may still be writing artifacts to disk. Keep
        // whatever we already have and stop spinning.
        setRunning(false);
        setPipelineStatus(prev => prev === "running" ? "idle" : prev);
      };
    } catch (err) {
      setPipelineStatus("error");
      setRunning(false);
      setEvents(prev => [...prev, {
        type: "error", message: `Submit failed: ${err.message}`, _phase: "error",
      }]);
      // Mark all sections as skipped — but don't crash the page
      setSectionStatus({
        mcad: "error", ecad: "error", bom: "error",
        dfm: "error", quote: "error",
      });
    }
  }, [running, setSection]);

  // ------------------------------------------------------------------
  // submit -- the user-facing entry. Asks /api/clarify whether the
  // prompt is specific enough, and if not, transitions to the clarify
  // form. Otherwise (or on clarify error) calls runPipelineWithGoal.
  //
  // Pattern mirrors ChatPanel.jsx's submit + submitWithClarifications
  // intentionally so the two surfaces stay in sync.
  // ------------------------------------------------------------------
  const submit = useCallback(async () => {
    const trimmed = goal.trim();
    if (!trimmed || running) return;
    setClarifyLoading(true);
    try {
      const r = await fetch(api("/clarify"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal: trimmed, quality_tier: "fast" }),
      });
      if (r.ok) {
        const j = await r.json();
        if (!j.enough_info && (j.clarifications || []).length > 0) {
          setClarifyData({
            goal: trimmed,
            part_family: j.part_family || "",
            summary: j.summary || "",
            clarifications: j.clarifications,
          });
          // Pre-fill answers with each question's `default` so the user
          // can hit Generate immediately if defaults look right.
          setClarifyAnswers(Object.fromEntries(
            j.clarifications.map(c => [c.field, c.default || ""])
          ));
          setPhase("clarify");
          setClarifyLoading(false);
          return;
        }
      }
    } catch { /* non-fatal -- proceed to pipeline without clarify */ }
    setClarifyLoading(false);
    await runPipelineWithGoal(trimmed);
  }, [goal, running, runPipelineWithGoal]);

  // ------------------------------------------------------------------
  // submitClarifications -- called by the clarify form's Generate
  // button. Augments the original goal with the user's answers and
  // dispatches into the pipeline. If user hit Skip (empty answers),
  // falls back to the original goal.
  // ------------------------------------------------------------------
  const submitClarifications = useCallback(async (answers) => {
    if (!clarifyData) return;
    const entries = Object.entries(answers || {})
      .filter(([, v]) => v != null && String(v).trim() !== "");
    const augmented = entries.length > 0
      ? clarifyData.goal + "\n\n## Clarifications\n"
        + entries.map(([k, v]) => `- ${k}: ${v}`).join("\n")
      : clarifyData.goal;
    await runPipelineWithGoal(augmented);
  }, [clarifyData, runPipelineWithGoal]);

  const skipClarifications = useCallback(async () => {
    if (!clarifyData) return;
    await runPipelineWithGoal(clarifyData.goal);
  }, [clarifyData, runPipelineWithGoal]);

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault(); submit();
    }
  };

  const reset = () => {
    if (streamRef.current) {
      try { streamRef.current.close(); } catch { /* noop */ }
      streamRef.current = null;
    }
    setPhase("intro");
    setRunning(false);
    setClarifyData(null);
    setClarifyAnswers({});
    setClarifyLoading(false);
    setEvents([]);
    setPipelineStatus("idle");
    setMcadArtifact(null);
    setEcadArtifact(null);
    setBomRows(null);
    setDfmIssues(null);
    setQuoteData(null);
    setSectionStatus({
      mcad: "idle", ecad: "idle", bom: "idle", dfm: "idle", quote: "idle",
    });
  };

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------
  if (phase === "clarify" && clarifyData) {
    return (
      <ClarifyView
        data={clarifyData}
        answers={clarifyAnswers}
        setAnswers={setClarifyAnswers}
        onGenerate={() => submitClarifications(clarifyAnswers)}
        onSkip={skipClarifications}
        onBack={() => { setPhase("intro"); setClarifyData(null); }}
      />
    );
  }

  if (phase === "intro") {
    return (
      <div style={{
        minHeight: "100vh", background: C.bg0, color: C.text0,
        fontFamily: FONT, display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center",
        padding: "48px 24px", position: "relative", overflow: "hidden",
      }}>
        <style>{`@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');*{box-sizing:border-box}textarea,input{outline:none;font-family:inherit}`}</style>
        <div style={{
          position: "absolute", top: "-30%", right: "-20%",
          width: "60%", height: "60%",
          background: `radial-gradient(ellipse, ${C.brand}22 0%, transparent 60%)`,
          opacity: 0.5, pointerEvents: "none",
        }} />
        <div style={{
          position: "absolute", bottom: "-30%", left: "-20%",
          width: "60%", height: "60%",
          background: `radial-gradient(ellipse, ${C.ai}22 0%, transparent 60%)`,
          opacity: 0.4, pointerEvents: "none",
        }} />

        <div style={{
          width: "100%", maxWidth: 760, position: "relative", zIndex: 1,
        }}>
          <div style={{
            fontSize: 11, letterSpacing: "0.22em", color: C.brand,
            fontWeight: 700, marginBottom: 14, textAlign: "center",
          }}>ARIA-OS QUICKSTART</div>
          <h1 style={{
            fontSize: "clamp(28px, 5vw, 44px)", fontWeight: 700,
            margin: "0 0 12px 0", textAlign: "center",
            letterSpacing: "-0.02em", lineHeight: 1.15,
          }}>
            Describe what you want to build.
          </h1>
          <p style={{
            fontSize: 16, color: C.text2, margin: "0 0 32px 0",
            textAlign: "center", lineHeight: 1.5,
          }}>
            One sentence. We will produce CAD, schematic, BOM, DFM,
            and a quote.
          </p>

          <div style={{
            background: C.bg1, border: `1px solid ${C.border}`,
            borderRadius: 16,
            boxShadow: "0 16px 48px rgba(0,0,0,0.45)",
            overflow: "hidden",
          }}>
            <textarea
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder={'STM32 motor controller with USB-C and encoder feedback\n\n100mm flange, 4 M6 bolts on 80mm PCD\n\nCantilever beam, 200mm long, 6061 aluminum'}
              autoFocus
              style={{
                width: "100%",
                minHeight: 168,
                padding: "20px 22px",
                background: "transparent",
                color: C.text0,
                border: "none",
                resize: "vertical",
                fontSize: 17,
                lineHeight: 1.55,
                fontFamily: FONT,
              }}
            />
            <div style={{
              padding: "12px 16px",
              borderTop: `1px solid ${C.border}`,
              display: "flex", alignItems: "center", gap: 10,
              flexWrap: "wrap",
            }}>
              <MicChip
                onTranscript={(t) => setGoal(g => g ? g + " " + t : t)}
                disabled={running} />
              <ImageChip
                onSubmit={() => {
                  // Image flow already kicked off /api/image_to_cad; just
                  // transition to the running view so events stream in.
                  setPhase("running");
                  setRunning(true);
                  setPipelineStatus("running");
                }}
                disabled={running} />
              <div style={{ flex: 1 }} />
              <div style={{
                fontSize: 11, color: C.text3,
                fontFamily: FONT_MONO, marginRight: 6,
              }}>
                Cmd/Ctrl+Enter to submit
              </div>
              <button
                onClick={submit}
                disabled={!goal.trim() || running || clarifyLoading}
                style={{
                  padding: "10px 22px",
                  borderRadius: 10,
                  border: "none",
                  background: goal.trim()
                    ? `linear-gradient(135deg, ${C.brand}, #FF9D4A)`
                    : "rgba(255,255,255,0.06)",
                  color: goal.trim() ? "#0A0A0F" : C.text3,
                  fontWeight: 700,
                  letterSpacing: "0.04em",
                  cursor: (goal.trim() && !clarifyLoading) ? "pointer" : "not-allowed",
                  fontSize: 13,
                  fontFamily: FONT,
                }}>
                {clarifyLoading
                  ? "Analyzing..."
                  : (running ? "Submitting..." : "Generate ->")}
              </button>
            </div>
          </div>

          <div style={{
            marginTop: 28, fontSize: 11, color: C.text3,
            textAlign: "center", letterSpacing: "0.06em",
          }}>
            ARIA pipeline · CadQuery · KiCad · Trimesh · DFM · CAM
          </div>
        </div>
      </div>
    );
  }

  // ----- RUNNING / RESULTS view -----
  return (
    <div style={{
      minHeight: "100vh", background: C.bg0, color: C.text0,
      fontFamily: FONT,
    }}>
      <style>{`@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');*{box-sizing:border-box}::-webkit-scrollbar{width:6px;height:6px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:100px}`}</style>
      <div style={{
        padding: "16px 24px",
        borderBottom: `1px solid ${C.border}`,
        display: "flex", alignItems: "center", gap: 12,
      }}>
        <div style={{
          fontSize: 11, letterSpacing: "0.18em", color: C.brand,
          fontWeight: 700,
        }}>ARIA-OS QUICKSTART</div>
        <div style={{ flex: 1 }} />
        <div style={{
          fontSize: 12, color: C.text2,
          fontFamily: FONT_MONO,
          maxWidth: 480, whiteSpace: "nowrap",
          overflow: "hidden", textOverflow: "ellipsis",
        }}>
          {goal}
        </div>
        <div style={{ flex: 1 }} />
        <div style={{
          fontSize: 11, padding: "4px 10px", borderRadius: 999,
          background: pipelineStatus === "done"
            ? "rgba(91,209,122,0.12)"
            : pipelineStatus === "error"
              ? "rgba(255,107,107,0.12)"
              : "rgba(0,212,255,0.12)",
          color: pipelineStatus === "done" ? C.green
                  : pipelineStatus === "error" ? C.red : C.ai,
          fontWeight: 700, letterSpacing: "0.08em",
          textTransform: "uppercase",
        }}>{pipelineStatus}</div>
        <button onClick={reset} style={{
          padding: "6px 14px", borderRadius: 8,
          background: "transparent",
          border: `1px solid ${C.border}`,
          color: C.text2,
          fontSize: 12, cursor: "pointer", fontFamily: FONT,
        }}>New goal</button>
      </div>

      <div style={{
        display: "grid",
        gridTemplateColumns: "320px 1fr",
        gap: 20, padding: 20, alignItems: "stretch",
      }}>
        {/* Pipeline progress sidebar */}
        <div style={{
          background: C.bg1, border: `1px solid ${C.border}`,
          borderRadius: 14, padding: 14,
          maxHeight: "calc(100vh - 160px)", overflow: "hidden",
          display: "flex", flexDirection: "column",
        }}>
          <div style={{
            fontSize: 10, letterSpacing: "0.14em",
            color: C.text3, fontWeight: 700, marginBottom: 10,
          }}>PIPELINE</div>
          <div style={{
            flex: 1, overflowY: "auto", paddingRight: 4,
            fontFamily: FONT_MONO, fontSize: 11,
          }}>
            {events.length === 0 && (
              <div style={{ color: C.text3 }}>Starting pipeline…</div>
            )}
            {events.map((ev, i) => {
              const msg = ev.message || ev.text || ev.data || "";
              const phase = ev._phase || _phaseOf(typeof msg === "string" ? msg : "");
              const tone = phase === "error" ? C.red
                          : phase === "complete" ? C.green
                          : phase === "export"   ? C.ai
                          : C.text2;
              return (
                <div key={i} style={{
                  display: "flex", gap: 8, padding: "3px 0",
                  borderBottom: i === events.length - 1
                    ? "none" : `1px dashed ${C.border}`,
                }}>
                  <span style={{
                    color: tone, fontWeight: 600, minWidth: 56,
                    textTransform: "uppercase", fontSize: 9,
                    letterSpacing: "0.08em",
                  }}>{phase}</span>
                  <span style={{
                    color: C.text1, flex: 1, wordBreak: "break-word",
                  }}>
                    {typeof msg === "string" ? msg : JSON.stringify(msg)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Section grid */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gridAutoRows: "minmax(220px, auto)",
          gap: 16,
        }}>
          <ResultSection title="MCAD" status={sectionStatus.mcad}
            hint="No mechanical artifact was produced — try a goal with explicit dimensions.">
            {mcadArtifact && (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                <div style={{ height: 220, background: C.bg2,
                  borderRadius: 10, overflow: "hidden",
                  border: `1px solid ${C.border}` }}>
                  {mcadArtifact.stl_url
                    ? <STLViewer stlUrl={mcadArtifact.stl_url} />
                    : <div style={{ padding: 20, color: C.text3, fontSize: 12 }}>
                        STL not exported yet. STEP available.
                      </div>}
                </div>
                <div style={{ fontSize: 12, color: C.text2,
                  fontFamily: FONT_MONO, lineHeight: 1.5 }}>
                  {mcadArtifact.part_id && <div>part: {mcadArtifact.part_id}</div>}
                  {mcadArtifact.bbox && <div>bbox: {JSON.stringify(mcadArtifact.bbox)}</div>}
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  {mcadArtifact.step_url && (
                    <a href={mcadArtifact.step_url} target="_blank" rel="noreferrer"
                      style={chipBtn}>Download STEP</a>
                  )}
                  {mcadArtifact.stl_url && (
                    <a href={mcadArtifact.stl_url} target="_blank" rel="noreferrer"
                      style={chipBtn}>Download STL</a>
                  )}
                </div>
              </div>
            )}
          </ResultSection>

          <ResultSection title="SCHEMATIC" status={sectionStatus.ecad}
            hint="ECAD pipeline did not run for this goal. MCAD is unaffected.">
            {ecadArtifact && (
              <div style={{ fontSize: 13, color: C.text1, lineHeight: 1.6 }}>
                <div style={{ marginBottom: 8 }}>
                  PCB: <code style={{ color: C.ai }}>
                    {(ecadArtifact.pcb_path || "").split(/[\\/]/).pop() || "(path)"}
                  </code>
                </div>
                {ecadArtifact.pcb_path && (
                  <a href={api("/artifacts/download?path="
                              + encodeURIComponent(ecadArtifact.pcb_path))}
                    target="_blank" rel="noreferrer" style={chipBtn}>
                    Download .kicad_pcb
                  </a>
                )}
              </div>
            )}
          </ResultSection>

          <ResultSection title="BOM" status={sectionStatus.bom}
            hint="No bill of materials produced. Common when the part has no electrical content.">
            {bomRows && Array.isArray(bomRows) && (
              <div style={{ fontSize: 12, fontFamily: FONT_MONO,
                maxHeight: 200, overflow: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead>
                    <tr style={{ borderBottom: `1px solid ${C.border}` }}>
                      <th style={tdHead}>Ref</th>
                      <th style={tdHead}>Part</th>
                      <th style={tdHead}>Qty</th>
                    </tr>
                  </thead>
                  <tbody>
                    {bomRows.slice(0, 50).map((r, i) => (
                      <tr key={i} style={{ borderBottom: `1px dashed ${C.border}` }}>
                        <td style={td}>{r.ref || r.designator || "-"}</td>
                        <td style={td}>{r.part || r.value || r.name || "-"}</td>
                        <td style={td}>{r.qty || r.quantity || 1}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </ResultSection>

          <ResultSection title="DFM" status={sectionStatus.dfm}
            hint="DFM checks did not produce output for this part.">
            {dfmIssues && Array.isArray(dfmIssues) && (
              <div style={{ display: "flex", flexDirection: "column", gap: 6,
                fontSize: 12 }}>
                {dfmIssues.length === 0 && (
                  <div style={{ color: C.green }}>No DFM issues detected.</div>
                )}
                {dfmIssues.slice(0, 12).map((iss, i) => (
                  <div key={i} style={{
                    padding: "6px 10px", borderRadius: 8,
                    background: "rgba(255,184,74,0.08)",
                    border: `1px solid rgba(255,184,74,0.25)`,
                    color: C.text1,
                  }}>
                    {typeof iss === "string"
                      ? iss
                      : (iss.message || iss.note || iss.name || JSON.stringify(iss))}
                  </div>
                ))}
              </div>
            )}
          </ResultSection>

          <ResultSection title="QUOTE" status={sectionStatus.quote}
            hint="Cost estimate not available for this part.">
            {quoteData && (
              <div style={{ display: "flex", flexDirection: "column", gap: 8,
                fontSize: 13 }}>
                {quoteData.unit_price != null && (
                  <div style={{ display: "flex", gap: 8 }}>
                    <span style={{ color: C.text3 }}>Unit price:</span>
                    <span style={{ color: C.text0, fontWeight: 600,
                      fontFamily: FONT_MONO }}>
                      ${Number(quoteData.unit_price).toFixed(2)}
                    </span>
                  </div>
                )}
                {quoteData.total != null && (
                  <div style={{ display: "flex", gap: 8 }}>
                    <span style={{ color: C.text3 }}>Total:</span>
                    <span style={{ color: C.text0, fontWeight: 600,
                      fontFamily: FONT_MONO }}>
                      ${Number(quoteData.total).toFixed(2)}
                    </span>
                  </div>
                )}
                {quoteData.cycle_time_min != null && (
                  <div style={{ display: "flex", gap: 8 }}>
                    <span style={{ color: C.text3 }}>Cycle time:</span>
                    <span style={{ color: C.text0, fontWeight: 600,
                      fontFamily: FONT_MONO }}>
                      {quoteData.cycle_time_min} min
                    </span>
                  </div>
                )}
              </div>
            )}
          </ResultSection>
        </div>
      </div>
    </div>
  );
}

const chipBtn = {
  padding: "6px 12px", borderRadius: 999,
  background: "transparent",
  border: `1px solid ${C.border}`,
  color: C.text1, fontSize: 12, cursor: "pointer",
  textDecoration: "none", display: "inline-block",
  fontFamily: FONT,
};
const tdHead = {
  padding: "6px 8px", textAlign: "left", color: C.text3,
  fontSize: 10, letterSpacing: "0.08em", textTransform: "uppercase",
  fontWeight: 700,
};
const td = {
  padding: "6px 8px", color: C.text1, fontSize: 11,
};
