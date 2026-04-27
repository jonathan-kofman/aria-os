/**
 * ChatPanel — Claude-style chat UI for the ARIA CAD plugin.
 *
 * Loads by default when the panel is hosted inside a CAD plugin
 * (Fusion 360 Palette, Rhino WebView2, Onshape iframe). Detection is via
 * `?host=fusion|rhino|onshape` query param OR the `bridge.isHosted` flag.
 *
 * Shape (left-to-right):
 *   - Transcript: alternating user / assistant messages, vertical scroll
 *   - Pipeline events stream into the current assistant message as a
 *     collapsible "Thinking…" accordion (like Claude's extended thinking)
 *   - Generated artifacts (STEP / STL / DXF) render as chip cards with an
 *     "Insert into <host>" button that calls bridge.insertGeometry()
 *   - Input pinned to the bottom; Cmd/Ctrl+Enter submits
 *
 * Intentionally minimal chrome: no tabs, no sidebar, no header. The host
 * CAD application IS the chrome — the panel is just a chat surface.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, API_BASE } from "../aria/apiConfig";
import bridge from "../aria/bridge";
import { DinoRunner } from "../components/DinoRunner";

/* ------------------------------------------------------------------------- */
/* Styles — Claude.ai's actual palette. AdamCAD clones this look; so do we.  */
/* Tokens verified against assistant-ui Claude-clone example (2026) and      */
/* Claude brand spec on Mobbin. Light theme primary, serif throughout.       */
/* ------------------------------------------------------------------------- */

const THEME = {
  bg:        "#F5F5F0",                    // warm cream canvas
  bgRaised:  "#FFFFFF",                    // pure white composer
  bgCode:    "#EFEDE6",                    // muted cream for thinking blocks
  border:    "rgba(0,0,0,0.08)",           // #00000015 — barely visible
  borderHi:  "rgba(0,0,0,0.14)",
  text:      "#1A1A18",                    // near-black, slight warm bias
  muted:     "#6B6864",
  mutedLo:   "#8F8B85",
  accent:    "#AE5630",                    // Claude orange / copper
  accentBg:  "rgba(174,86,48,0.08)",
  user:      "#DDD9CE",                    // taupe user pill (NOT saturated)
  userText:  "#1A1A18",
  success:   "#4A8A5B",
  error:     "#B44A3D",
  shadowSm:  "0 0.25rem 1.25rem rgba(0,0,0,0.035)",  // composer lift
  shadowXs:  "0 1px 3px rgba(0,0,0,0.04)",
};

// Claude uses serif throughout. `ui-serif` → the OS's default serif
// (Cambria/Times), which gives the immediately-recognizable Anthropic look
// without shipping a custom font file.
const FONT_SERIF = '"Tiempos Text", Copernicus, ui-serif, Georgia, Cambria, "Times New Roman", Times, serif';
const FONT_MONO  = '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace';

const ROOT_STYLE = {
  height: "100vh",
  width: "100%",
  display: "flex",
  flexDirection: "column",
  background: THEME.bg,
  color: THEME.text,
  fontFamily: FONT_SERIF,                       // serif everywhere — the signature
  fontSize: 15,                                 // slightly larger for serif readability
  lineHeight: 1.6,                              // generous — matches claude.ai
  overflow: "hidden",
  "-webkit-font-smoothing": "antialiased",
};

/* ------------------------------------------------------------------------- */
/* IconBtn — a 32×32 tertiary toolbar button with hover fade                 */
/* ------------------------------------------------------------------------- */

/* MicButton — hold-to-talk style. Clicks to start/stop recording. Sends
   the captured blob to /api/stt/transcribe and feeds the result back
   via onTranscript. Red-dot indicator while recording. */
function MicButton({ onTranscript }) {
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const mediaRec = useRef(null);
  const chunks = useRef([]);

  const [errorMsg, setErrorMsg] = useState("");

  const recognitionRef = useRef(null);

  const start = async () => {
    if (recording || busy) return;
    setErrorMsg("");

    // Primary path when hosted in Fusion/Rhino: async recording via
    // the add-in. The add-in returns immediately; panel polls for
    // completion OR user clicks mic again to stop early.
    if (bridge.isHosted) {
      setRecording(true);
      try {
        const startReply = await bridge.recordAudio(30);
        if (!startReply?.ok) {
          setRecording(false);
          const m = startReply?.error || "Failed to start recording";
          bridge.showNotification(`Voice: ${m}`, "error");
          return;
        }
        const sessionId = startReply.session_id;
        recognitionRef.current = { sessionId, stopped: false };

        // Poll until done (user-stop or max-duration timeout). Panel
        // updates its "recording" indicator based on elapsed time; the
        // user can click the mic again → stop() cancels early.
        let attempts = 0;
        const maxAttempts = 200;   // 200 × 250ms = 50s ceiling
        while (attempts < maxAttempts) {
          await new Promise(r => setTimeout(r, 250));
          attempts += 1;
          const poll = await bridge.pollRecording(sessionId);
          if (!poll?.ok && poll?.status !== "recording") {
            const m = poll?.error || "Recording failed";
            bridge.showNotification(`Voice: ${m}`, "error");
            setRecording(false);
            return;
          }
          if (poll?.status === "done" && poll.audio_b64) {
            setRecording(false);
            setBusy(true);
            // Base64 → Blob → Groq Whisper
            const raw = atob(poll.audio_b64);
            const bytes = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
            const blob = new Blob([bytes],
              { type: poll.mime || "audio/wav" });
            const fd = new FormData();
            fd.append("audio", blob, "voice.wav");
            const r = await fetch(api("/stt/transcribe"),
                                   { method: "POST", body: fd });
            const j = await r.json();
            if (j.text) {
              onTranscript?.(j.text);
            } else {
              const m = j.error || "Whisper returned no text";
              bridge.showNotification(`STT: ${m}`, "error");
            }
            setBusy(false);
            return;
          }
          // Still recording — loop
        }
        // Fell out of the poll loop
        setRecording(false);
        bridge.showNotification(
          "Voice: recording timed out", "error");
      } catch (err) {
        const m = `Voice pipeline failed: ${err?.message || err}`;
        setErrorMsg(m);
        bridge.showNotification(m, "error");
        setRecording(false);
      }
      return;
    }

    // Standalone browser fallback: try Web Speech API first, then
    // getUserMedia → Groq Whisper. Neither works in Fusion's WebView2
    // but both work in Chrome/Edge outside Fusion.
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SR) {
      try {
        const rec = new SR();
        rec.lang = "en-US";
        rec.continuous = false;
        rec.interimResults = false;
        rec.maxAlternatives = 1;
        rec.onresult = (e) => {
          const text = e.results?.[0]?.[0]?.transcript || "";
          if (text) onTranscript?.(text);
          setRecording(false);
        };
        rec.onerror = (e) => {
          const msg = `Speech: ${e.error || "unknown error"}`;
          setErrorMsg(msg);
          bridge.showNotification(msg, "error");
          setRecording(false);
        };
        rec.onend = () => setRecording(false);
        rec.start();
        recognitionRef.current = rec;
        setRecording(true);
        return;
      } catch (err) {
        // SR construction failed — fall through to getUserMedia path
      }
    }

    // Fallback: getUserMedia → MediaRecorder → Groq Whisper. Works in
    // regular browsers but usually not in Fusion Palette.
    if (typeof navigator === "undefined" || !navigator.mediaDevices) {
      const m = "Voice input isn't available in this WebView. " +
                "Attach a .wav/.m4a file via the paperclip instead.";
      setErrorMsg(m);
      bridge.showNotification(m, "error");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const candidateMimes = [
        "audio/webm;codecs=opus", "audio/webm",
        "audio/ogg;codecs=opus", "audio/mp4", "",
      ];
      let mr = null;
      for (const mime of candidateMimes) {
        if (!mime || MediaRecorder.isTypeSupported?.(mime)) {
          try {
            mr = mime ? new MediaRecorder(stream, { mimeType: mime })
                      : new MediaRecorder(stream);
            break;
          } catch { /* try next */ }
        }
      }
      if (!mr) throw new Error("No supported MediaRecorder mime type");

      chunks.current = [];
      mr.ondataavailable = (e) => {
        if (e.data.size > 0) chunks.current.push(e.data);
      };
      mr.onstop = async () => {
        setBusy(true);
        try {
          const mime = mr.mimeType || "audio/webm";
          const blob = new Blob(chunks.current, { type: mime });
          const ext = mime.includes("mp4") ? "m4a"
                    : mime.includes("ogg") ? "ogg" : "webm";
          const fd = new FormData();
          fd.append("audio", blob, `voice.${ext}`);
          const r = await fetch(api("/stt/transcribe"),
                                 { method: "POST", body: fd });
          const j = await r.json();
          if (j.text) onTranscript?.(j.text);
          else bridge.showNotification(
            `STT: ${j.error || "no text"}`, "error");
        } catch (err) {
          bridge.showNotification(
            `STT upload failed: ${err?.message || err}`, "error");
        } finally {
          setBusy(false);
          stream.getTracks().forEach(t => t.stop());
        }
      };
      mr.start();
      mediaRec.current = mr;
      setRecording(true);
    } catch (err) {
      const name = err?.name || "";
      let m = `Mic failed: ${err?.message || err}`;
      if (name === "NotAllowedError") {
        m = "Mic permission denied. Attach a recorded audio file via the paperclip.";
      } else if (name === "NotFoundError") {
        m = "No microphone found. Check Windows sound settings.";
      }
      setErrorMsg(m);
      bridge.showNotification(m, "error");
    }
  };

  const stop = () => {
    // Stop the async bridge recording (hosted path). The Python thread
    // calls waveInStop() on the current buffer, flushes whatever it
    // has, and the next poll returns the partial WAV. We do NOT clear
    // `recording` state here — the poll loop will flip it when the
    // audio lands and transcription completes.
    const rec = recognitionRef.current;
    if (rec && typeof rec === "object" && rec.sessionId != null) {
      try { bridge.stopRecording(); } catch {}
      rec.stopped = true;
      return;
    }
    // SpeechRecognition path (standalone browser with Web Speech API)
    if (rec && typeof rec.stop === "function") {
      try { rec.stop(); } catch {}
      recognitionRef.current = null;
    }
    // MediaRecorder fallback
    const mr = mediaRec.current;
    if (mr && mr.state !== "inactive") mr.stop();
    setRecording(false);
  };

  // While recording, widen the button into a clearly-labeled "Stop"
  // pill with a pulsing red dot so there's no ambiguity about what
  // clicking it does. Busy (transcribing) shows a spinner.
  if (recording) {
    return (
      <button type="button" onClick={stop}
        title="Click to stop recording"
        style={{
          display: "inline-flex", alignItems: "center", gap: 8,
          padding: "5px 12px 5px 10px",
          background: "rgba(220, 38, 38, 0.12)",
          border: "1px solid rgba(220, 38, 38, 0.45)",
          borderRadius: 100,
          color: "#dc2626",
          fontFamily: FONT_SERIF, fontSize: 12, fontWeight: 600,
          cursor: "pointer",
          transition: "all 0.12s",
        }}>
        <span style={{
          display: "inline-block", width: 8, height: 8,
          borderRadius: "50%",
          background: "#dc2626",
          animation: "ariaPulse 1s ease-in-out infinite",
          boxShadow: "0 0 0 3px rgba(220, 38, 38, 0.2)",
        }} />
        <span>Stop recording</span>
      </button>
    );
  }
  return (
    <IconBtn title="Voice input — click, speak, click again to stop"
      onClick={start} disabled={busy}>
      {busy ? (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2"
             style={{ animation: "ariaSpin 0.9s linear infinite" }}>
          <circle cx="12" cy="12" r="9" strokeDasharray="42" strokeLinecap="round"/>
        </svg>
      ) : (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="1.8"
             strokeLinecap="round" strokeLinejoin="round">
          <rect x="9" y="2" width="6" height="12" rx="3"/>
          <path d="M5 10v2a7 7 0 0 0 14 0v-2"/>
          <line x1="12" y1="19" x2="12" y2="23"/>
        </svg>
      )}
    </IconBtn>
  );
}


function IconBtn({ children, onClick, title, disabled, active }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      style={{
        width: 32, height: 32, borderRadius: 8,
        background: active ? THEME.accentBg : "transparent",
        color: active ? THEME.accent : THEME.muted,
        border: `1px solid ${active ? THEME.accent : "transparent"}`,
        padding: 0,
        display: "flex", alignItems: "center", justifyContent: "center",
        cursor: disabled ? "not-allowed" : "pointer",
        transition: "background 0.12s, color 0.12s, border-color 0.12s",
      }}
      onMouseEnter={e => {
        if (disabled || active) return;
        e.currentTarget.style.background = THEME.bgCode;
        e.currentTarget.style.color = THEME.text;
      }}
      onMouseLeave={e => {
        if (active) return;
        e.currentTarget.style.background = "transparent";
        e.currentTarget.style.color = THEME.muted;
      }}
    >{children}</button>
  );
}

/* ------------------------------------------------------------------------- */
/* ModelSelector — real dropdown mirroring AdamCAD's ModelSelector.tsx.      */
/* Our "models" are the skill-profile tiers (fast/balanced/premium) since    */
/* that's the LLM-routing axis the SkillProfile already controls.            */
/* ------------------------------------------------------------------------- */

const MODELS = [
  { id: "fast",     label: "ARIA Fast",     hint: "Gemini + Haiku · cheapest, seconds to first token" },
  { id: "balanced", label: "ARIA Balanced", hint: "Gemini → Sonnet · default for most parts" },
  { id: "premium",  label: "ARIA Premium",  hint: "Sonnet-first · best code for complex assemblies" },
];

/* Modes: default is Auto — backend inspects the prompt keywords and
   routes to the right pipeline (mechanical part, PCB, drawing, assembly).
   Power users can force a specific path through the dropdown. */
const MODES = [
  { id: "auto",      label: "Auto",   hint: "Detect from prompt — recommended" },
  { id: "native",    label: "Part",   hint: "Mechanical part — streams features into Fusion's tree" },
  { id: "kicad",     label: "PCB",    hint: "KiCad PCB layout" },
  { id: "asm",       label: "Assembly", hint: "Multi-component assembly + joints" },
  { id: "dwg",       label: "Drawing",  hint: "2D drawing sheet with views" },
  { id: "mechanical", label: "Legacy CadQuery", hint: "Old CadQuery → STEP import path" },
];

/* Compact mode selector — same dropdown pattern as ModelSelector, just
   narrower visuals so the composer row doesn't look crowded. Default
   "Auto" means the backend decides based on the prompt. */
function ModeSelector({ value, onChange }) {
  const [open, setOpen] = useState(false);
  const [triggerRect, setTriggerRect] = useState(null);
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const selected = MODES.find(m => m.id === value) || MODES[0];
  useEffect(() => {
    if (!open) return;
    if (triggerRef.current)
      setTriggerRect(triggerRef.current.getBoundingClientRect());
    const onScroll = () => {
      if (triggerRef.current)
        setTriggerRect(triggerRef.current.getBoundingClientRect());
    };
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const h = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);
  return (
    <div ref={rootRef} style={{ position: "relative" }}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(o => !o)}
        title={selected.hint}
        style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          padding: "5px 10px",
          background: open ? THEME.bgCode : "transparent",
          border: `1px solid ${open ? THEME.borderHi : "transparent"}`,
          borderRadius: 100,
          color: THEME.muted,
          fontFamily: FONT_SERIF,
          fontSize: 12, fontStyle: "italic",
          cursor: "pointer",
          transition: "all 0.12s",
        }}
        onMouseEnter={e => { if (!open) e.currentTarget.style.background = THEME.bgCode; }}
        onMouseLeave={e => { if (!open) e.currentTarget.style.background = "transparent"; }}
      >
        <span style={{ color: THEME.text, fontStyle: "normal" }}>{selected.label}</span>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2.4"
             strokeLinecap="round" strokeLinejoin="round"
             style={{ transform: open ? "rotate(180deg)" : "none",
                       transition: "transform 0.15s" }}>
          <polyline points="6 9 12 15 18 9"/>
        </svg>
      </button>
      {open && triggerRect && (
        <div className="aria-scroll" style={{
          position: "fixed",
          bottom: `calc(100vh - ${triggerRect.top - 8}px)`,
          left: `${triggerRect.left}px`,
          minWidth: 240, maxWidth: 320, width: "max-content",
          maxHeight: `min(360px, calc(${triggerRect.top}px - 16px))`,
          overflowY: "auto",
          background: THEME.bgRaised,
          border: `1px solid ${THEME.borderHi}`,
          borderRadius: 12,
          boxShadow: THEME.shadowSm,
          padding: 4, zIndex: 1000,
        }}>
          {MODES.map(m => (
            <button key={m.id}
              onClick={() => { onChange?.(m.id); setOpen(false); }}
              style={{
                width: "100%",
                display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 2,
                padding: "10px 12px",
                background: m.id === value ? THEME.accentBg : "transparent",
                border: "none", borderRadius: 8,
                textAlign: "left", cursor: "pointer",
                fontFamily: FONT_SERIF, color: THEME.text,
              }}
              onMouseEnter={e => { if (m.id !== value)
                e.currentTarget.style.background = THEME.bgCode; }}
              onMouseLeave={e => { if (m.id !== value)
                e.currentTarget.style.background = "transparent"; }}
            >
              <div style={{ fontSize: 14, fontWeight: 500,
                             color: m.id === value ? THEME.accent : THEME.text,
                             display: "flex", alignItems: "center", gap: 6 }}>
                {m.label}
                {m.id === value && (
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="2.8"
                       strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                )}
              </div>
              <div style={{ fontSize: 12, color: THEME.muted,
                             fontStyle: "italic", lineHeight: 1.4,
                             whiteSpace: "normal", wordBreak: "break-word" }}>{m.hint}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}


function ModelSelector({ value, onChange }) {
  const [open, setOpen] = useState(false);
  const [triggerRect, setTriggerRect] = useState(null);
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const selected = MODELS.find(m => m.id === value) || MODELS[1];

  // When opening, capture the trigger's screen rect so we can position
  // the popover with `position: fixed` and escape every parent's
  // overflow clip (the composer has overflow:visible now but the outer
  // panel root still clips).
  useEffect(() => {
    if (!open) return;
    if (triggerRef.current) {
      setTriggerRect(triggerRef.current.getBoundingClientRect());
    }
    const onScroll = () => {
      if (triggerRef.current) {
        setTriggerRect(triggerRef.current.getBoundingClientRect());
      }
    };
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open]);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const h = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);

  return (
    <div ref={rootRef} style={{ position: "relative" }}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(o => !o)}
        style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          padding: "6px 10px 6px 12px",
          background: open ? THEME.bgCode : "transparent",
          border: `1px solid ${open ? THEME.borderHi : THEME.border}`,
          borderRadius: 100,
          color: THEME.muted,
          fontFamily: FONT_SERIF,
          fontSize: 12,
          cursor: "pointer",
          transition: "background 0.12s, border-color 0.12s",
        }}
        onMouseEnter={e => { if (!open) e.currentTarget.style.background = THEME.bgCode; }}
        onMouseLeave={e => { if (!open) e.currentTarget.style.background = "transparent"; }}
      >
        <span style={{ color: THEME.text }}>{selected.label}</span>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2.4"
             strokeLinecap="round" strokeLinejoin="round"
             style={{ transform: open ? "rotate(180deg)" : "none",
                       transition: "transform 0.15s" }}>
          <polyline points="6 9 12 15 18 9"/>
        </svg>
      </button>
      {open && triggerRect && (
        <div className="aria-scroll" style={{
          // position: fixed escapes EVERY ancestor's overflow clip — the
          // panel root, the composer card, even Fusion's WebView2 iframe
          // boundary. Coordinates computed from the trigger's bounding
          // rect so the popover sticks to the "ARIA Balanced ▾" button.
          position: "fixed",
          // Open UPWARD: bottom edge 8px above the trigger's top edge
          bottom: `calc(100vh - ${triggerRect.top - 8}px)`,
          // Right edge aligned with the trigger's right edge
          right: `calc(100vw - ${triggerRect.right}px)`,
          minWidth: Math.max(220, triggerRect.width),
          maxWidth: "min(320px, calc(100vw - 16px))",
          width: "max-content",
          maxHeight: `min(340px, calc(${triggerRect.top}px - 16px))`,
          overflowY: "auto",
          background: THEME.bgRaised,
          border: `1px solid ${THEME.borderHi}`,
          borderRadius: 12,
          boxShadow: THEME.shadowSm,
          padding: 4,
          zIndex: 1000,
        }}>
          {MODELS.map(m => (
            <button key={m.id}
              onClick={() => { onChange?.(m.id); setOpen(false); }}
              style={{
                width: "100%",
                display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 2,
                padding: "10px 12px",
                background: m.id === value ? THEME.accentBg : "transparent",
                border: "none", borderRadius: 8,
                textAlign: "left", cursor: "pointer",
                fontFamily: FONT_SERIF,
                color: THEME.text,
              }}
              onMouseEnter={e => { if (m.id !== value)
                e.currentTarget.style.background = THEME.bgCode; }}
              onMouseLeave={e => { if (m.id !== value)
                e.currentTarget.style.background = "transparent"; }}
            >
              <div style={{
                fontSize: 14, fontWeight: 500,
                color: m.id === value ? THEME.accent : THEME.text,
                display: "flex", alignItems: "center", gap: 6,
              }}>
                {m.label}
                {m.id === value && (
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="2.8"
                       strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                )}
              </div>
              <div style={{
                fontSize: 12, color: THEME.muted,
                fontStyle: "italic",
                lineHeight: 1.4,
                whiteSpace: "normal",
                wordBreak: "break-word",
              }}>{m.hint}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------------- */
/* Artifact card — a generated file that can be inserted into the host       */
/* ------------------------------------------------------------------------- */

function _formatBbox(bbox) {
  // bbox may be: null/undefined, [w,h,d], {x,y,z}, {width,height,depth}, or a string.
  // Always produce a safe "80.0 × 60.0 × 40.0" or null.
  if (!bbox) return null;
  let dims;
  if (Array.isArray(bbox)) {
    dims = bbox;
  } else if (typeof bbox === "object") {
    dims = [bbox.x ?? bbox.width ?? bbox.length,
            bbox.y ?? bbox.height ?? bbox.width,
            bbox.z ?? bbox.depth ?? bbox.height];
  } else {
    return String(bbox);
  }
  const clean = dims.filter(n => typeof n === "number" && isFinite(n));
  if (clean.length < 2) return null;
  return clean.map(n => n.toFixed(1)).join(" × ") + " mm";
}

function ArtifactCard({ artifact, onInsert, onOpen, inserting, onAction }) {
  const name = artifact.filename || artifact.path?.split(/[\\/]/).pop() || "part";
  const kind = (artifact.kind || artifact.format ||
                 (name.endsWith(".step") || name.endsWith(".stp") ? "STEP"
                  : name.endsWith(".stl") ? "STL"
                  : name.endsWith(".dxf") ? "DXF"
                  : name.split(".").pop() || "FILE")).toUpperCase();
  const bboxLine = _formatBbox(artifact.bbox);
  const [busyAction, setBusyAction] = useState(null);

  // Post-creation engineering actions. Each one fires the existing
  // backend pipeline (no new UI surface — inline secondary buttons).
  // When triggered, the backend emits events into the same SSE stream
  // so the feature tree shows progress.
  const actions = [
    { id: "drawing", label: "📐 Drawing",
      hint: "Multi-view engineering drawing (FreeCAD TechDraw)" },
    { id: "dfm",     label: "🧪 DFM",
      hint: "Design-for-manufacturing review — wall thickness, undercuts, tolerances" },
    { id: "quote",   label: "💵 Quote",
      hint: "Machining cost + cycle time estimate" },
    { id: "cam",     label: "⚙ CAM",
      hint: "Fusion-compatible CAM script with toolpaths + G-code" },
    { id: "fea",     label: "🔬 FEA",
      hint: "CalculiX static structural analysis (von Mises stress)" },
    { id: "gerbers", label: "📤 Gerbers",
      hint: "Fab-ready Gerber + Excellon drill files (PCB)" },
    { id: "bom",     label: "📋 BOM",
      hint: "Bill of materials (PCB)" },
    { id: "drc",     label: "🔍 DRC",
      hint: "KiCad design rule check (PCB)" },
  ];
  const mechActions = ["drawing", "dfm", "quote", "cam", "fea"];
  const pcbActions  = ["gerbers", "drc", "bom", "quote", "dfm"];
  const visibleActions =
    (kind === "STEP" || kind === "STL")
      ? actions.filter(a => mechActions.includes(a.id))
      : (kind === "KICAD_PCB" || kind === "PCB" ||
          (artifact.filename || "").endsWith(".kicad_pcb"))
        ? actions.filter(a => pcbActions.includes(a.id))
        : [];

  return (
    <div style={{
      display: "flex", flexDirection: "column", gap: 8,
      padding: "10px 14px",
      background: THEME.bgRaised,
      border: `1px solid ${THEME.border}`,
      borderRadius: 12,
      marginTop: 8, maxWidth: "100%",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div style={{
          width: 38, height: 38, borderRadius: 10,
          background: THEME.accentBg, color: THEME.accent,
          display: "flex", alignItems: "center", justifyContent: "center",
          fontWeight: 700, fontSize: 10, letterSpacing: 0.5, flexShrink: 0,
        }}>{kind}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 500, color: THEME.text,
                         whiteSpace: "nowrap", overflow: "hidden",
                         textOverflow: "ellipsis" }}>{name}</div>
          {bboxLine && (
            <div style={{ fontSize: 12, color: THEME.muted }}>{bboxLine}</div>
          )}
        </div>
        {bridge.isHosted && (kind === "STEP" || kind === "STL") && (
          <button onClick={() => onInsert(artifact)} disabled={inserting}
            style={{
              padding: "6px 14px",
              background: inserting ? THEME.bgCode : THEME.accent,
              color: inserting ? THEME.muted : "#FFFFFF",
              border: "none", borderRadius: 8,
              fontSize: 13, fontWeight: 500,
              cursor: inserting ? "wait" : "pointer", fontFamily: "inherit",
            }}>{inserting ? "inserting…" : `Insert into ${bridge.kind}`}</button>
        )}
        {onOpen && (
          <button onClick={() => onOpen(artifact)} style={{
            padding: "6px 12px", background: "transparent",
            color: THEME.muted, border: `1px solid ${THEME.border}`,
            borderRadius: 8, fontSize: 13,
            cursor: "pointer", fontFamily: "inherit",
          }}>Open</button>
        )}
      </div>
      {visibleActions.length > 0 && onAction && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap",
                       paddingTop: 4, borderTop: `1px dashed ${THEME.border}` }}>
          {visibleActions.map(a => (
            <button key={a.id}
              title={a.hint}
              disabled={busyAction === a.id}
              onClick={async () => {
                setBusyAction(a.id);
                try { await onAction(a.id, artifact); }
                finally { setBusyAction(null); }
              }}
              style={{
                padding: "4px 10px", background: "transparent",
                color: busyAction === a.id ? THEME.accent : THEME.muted,
                border: `1px solid ${THEME.border}`,
                borderRadius: 100, fontSize: 11,
                cursor: busyAction === a.id ? "wait" : "pointer",
                fontFamily: FONT_SERIF, fontStyle: "italic",
                transition: "color 0.12s, background 0.12s",
              }}
              onMouseEnter={e => {
                if (busyAction !== a.id)
                  e.currentTarget.style.background = THEME.bgCode;
              }}
              onMouseLeave={e => {
                if (busyAction !== a.id)
                  e.currentTarget.style.background = "transparent";
              }}
            >{busyAction === a.id ? "…" : a.label}</button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------------- */
/* Collapsible "thinking" block — streams pipeline events inline            */
/* ------------------------------------------------------------------------- */

/* ------------------------------------------------------------------------- */
/* StepTree — live-updating list of pipeline stages                          */
/*                                                                           */
/* Replaces the old collapsed "Thought for X steps" summary. This is the     */
/* feature tree: each pipeline event renders as a row with status icon,     */
/* phase label, and detail. Auto-scrolls as new events arrive. A disclosure */
/* at the bottom exposes the raw event log for debugging.                    */
/* ------------------------------------------------------------------------- */

// Classify a raw event into a structural step row.
function _classifyEvent(ev, isCurrent) {
  // ev: { type, message, data, seq, timestamp } or a raw string (legacy)
  if (typeof ev === "string") {
    return { label: ev, phase: "log", status: "done", detail: null };
  }
  const type = ev.type || "log";
  const msg = ev.message || ev.text || "";
  const m = msg.trim();

  // Checkpoint events — "PLAN: PASS" / "ROUTE: FAIL -- ..."
  if (type === "checkpoint") {
    const parts = m.split(":");
    const phase = (parts[0] || "CHECKPOINT").trim();
    const rest = parts.slice(1).join(":").trim();
    const status = rest.startsWith("PASS") ? "done"
                 : rest.startsWith("FAIL") ? "error"
                 : rest.startsWith("WARN") ? "warning"
                 : "done";
    return { phase, label: phase.toLowerCase() + " " + (status === "done" ? "passed" : rest.toLowerCase()),
              detail: rest.replace(/^(PASS|FAIL|WARN)/, "").replace(/^[-\s]+/, ""),
              status };
  }

  // Infer phase from the MESSAGE content (not the event type, which is
  // almost always "step"). Message-based classification lets us tag each
  // row with a meaningful phase like GEN / EXPORT / EXEC rather than a
  // generic "STEP".
  const phaseOf = (() => {
    const low = m.toLowerCase();
    // Intake
    if (low.startsWith("received goal")) return "Intake";
    if (low.startsWith("mode:"))         return "Intake";
    if (low.startsWith("pipeline started")) return "Start";
    // Generation sub-steps
    if (low.startsWith("template matched"))      return "Template";
    if (low.startsWith("template code emitted")) return "Template";
    if (low.startsWith("script written"))        return "Script";
    if (low.startsWith("executing cadquery"))    return "Exec";
    if (low.startsWith("geometry valid"))        return "Geom";
    if (low.startsWith("step exported"))         return "Export";
    if (low.startsWith("stl exported"))          return "Export";
    if (low.startsWith("planning"))              return "Plan";
    if (low.startsWith("tool:"))                 return "Route";
    if (low.startsWith("iteration"))             return "Iter";
    if (low.startsWith("cadsmith") || low.includes("cadsmith")) return "CadSmith";
    if (low.startsWith("no template"))           return "Route";
    // Agent stage emits (refinement_loop.py + designer_agent.py)
    if (low.startsWith("researchagent"))         return "Research";
    if (low.startsWith("specagent"))             return "Spec";
    if (low.startsWith("designeragent"))         return "Design";
    if (low.startsWith("evalagent"))             return "Eval";
    if (low.startsWith("refineragent"))          return "Refine";
    // Visual-verifier emits (visual_verifier.py)
    if (low.startsWith("rendering")
        || low.startsWith("rendered")
        || low.startsWith("geometry precheck"))  return "Verify";
    if (low.startsWith("calling vision")
        || low.startsWith("vision result"))      return "Vision";
    // Type-based fallbacks
    if (type === "validation")      return "Validate";
    if (type === "llm_output")      return "LLM";
    if (type === "tool_call")       return "Tool";
    if (type === "complete")        return "Done";
    if (type === "error")           return "Error";
    if (type === "warning")         return "Warn";
    if (type === "cadsmith")        return "CadSmith";
    if (type === "cem")             return "CEM";
    if (type === "visual")          return "Verify";
    if (type === "agent")           return "Agent";
    // cad_op events carry a `phase` in data (Sketch/Extrude/Cut/Hole/…)
    if (type === "cad_op")          return ev.data?.phase || "Feature";
    // Native feature-tree ops — each one lands as a real Fusion entry
    if (type === "native_op") {
      const k = (ev.data?.kind || "").toLowerCase();
      if (k.startsWith("sketch"))        return "Sketch";
      if (k === "extrude")               return "Extrude";
      if (k === "circularpattern")       return "Pattern";
      if (k === "fillet")                return "Fillet";
      if (k === "beginplan")             return "Setup";
      return "Feature";
    }
    if (type === "native_result")   return "Result";
    if (type === "step")            return "Step";
    return type.charAt(0).toUpperCase() + type.slice(1);
  })();

  const status = type === "error" ? "error"
               : type === "warning" ? "warning"
               : type === "complete" ? "done"
               : isCurrent ? "running" : "done";

  return {
    phase: phaseOf,
    label: m || type,
    detail: ev.data && Object.keys(ev.data).length
              ? Object.entries(ev.data)
                  .filter(([k]) => !["seq", "timestamp"].includes(k))
                  .map(([k, v]) => {
                    const s = typeof v === "string" ? v : JSON.stringify(v);
                    return s.length > 80 ? `${k}: ${s.slice(0, 80)}…` : `${k}: ${s}`;
                  }).slice(0, 3).join(" · ")
              : null,
    status,
    timestamp: ev.timestamp,
  };
}

// Status icon for a step row
function _StatusIcon({ status }) {
  const pulse = "ariaPulse 1.2s ease-in-out infinite";
  if (status === "running") {
    return (
      <span style={{
        display: "inline-block", width: 10, height: 10, borderRadius: "50%",
        border: `2px solid ${THEME.accent}`, borderTopColor: "transparent",
        animation: "ariaSpin 0.9s linear infinite", flexShrink: 0,
      }} />
    );
  }
  if (status === "done") {
    return (
      <span style={{
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        width: 14, height: 14, borderRadius: "50%",
        background: THEME.success, color: "#FFF", flexShrink: 0,
      }}>
        <svg width="9" height="9" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="4"
             strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
      </span>
    );
  }
  if (status === "error") {
    return (
      <span style={{
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        width: 14, height: 14, borderRadius: "50%",
        background: THEME.error, color: "#FFF", flexShrink: 0,
      }}>
        <svg width="8" height="8" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="4"
             strokeLinecap="round" strokeLinejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"/>
          <line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </span>
    );
  }
  if (status === "warning") {
    return (
      <span style={{
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        width: 14, height: 14, borderRadius: "50%",
        background: "#E8A93C", color: "#FFF", flexShrink: 0, fontSize: 10, fontWeight: 700,
      }}>!</span>
    );
  }
  // pending
  return (
    <span style={{
      display: "inline-block", width: 10, height: 10, borderRadius: "50%",
      border: `2px solid ${THEME.border}`, flexShrink: 0,
    }} />
  );
}

function StepTree({ events, done, error }) {
  // Tick once a second while running so the "waited Xs" label updates.
  // When `done`/`error` flips, the useEffect stops the interval.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (done || error) return;
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, [done, error]);

  // Mark the LAST non-terminal event as "running" while the pipeline
  // is still active. Once `done` flips true, everything is final.
  const steps = (events || []).map((ev, i, arr) => {
    const isLast = i === arr.length - 1;
    const isCurrent = !done && !error && isLast;
    return _classifyEvent(ev, isCurrent);
  });

  // Heartbeat row — shown when the pipeline is running but no event has
  // arrived in the last ~5 seconds. Tells the user the stream is alive
  // even while the backend is stuck on a slow LLM call.
  const lastEventTs = (() => {
    const last = (events || [])[(events || []).length - 1];
    if (!last || typeof last !== "object") return null;
    // Parse "HH:MM:SS" or fall back to Date.now()
    if (last._clientTs) return last._clientTs;
    return null;
  })();
  // Track client-side arrival time of the last event (injected via
  // a mutation in the parent's append path). If we don't have one,
  // fall back to a tick-based "since component mounted" proxy.
  void tick; // referenced so React re-renders this component each second
  // A soft error mid-pipeline (e.g. "CQ validation failed after 3 attempts"
  // followed by successful fallback) should NOT brand the whole run as
  // failed. The run is only "hit issues" if:
  //   - the panel explicitly received a terminal error, OR
  //   - the LAST event was an error / warning (pipeline died with it)
  const lastStep = steps[steps.length - 1];
  const terminalFailure = !!error ||
    (done && lastStep && (lastStep.status === "error"));
  const hasSoftError = steps.some(s => s.status === "error");
  const [rawOpen, setRawOpen] = useState(false);
  const tailRef = useRef(null);

  useEffect(() => {
    // Auto-scroll to the newest row when the list grows
    if (tailRef.current) tailRef.current.scrollIntoView({ block: "nearest" });
  }, [events?.length]);

  return (
    <div style={{
      marginTop: 10,
      border: `1px solid ${THEME.border}`,
      borderRadius: 12,
      background: THEME.bgCode,
      overflow: "hidden",
    }}>
      {/* Header bar — always visible status */}
      <div style={{
        display: "flex", alignItems: "center", gap: 8,
        padding: "10px 14px",
        borderBottom: `1px solid ${THEME.border}`,
      }}>
        <_StatusIcon status={
          terminalFailure ? "error"
          : done ? "done"
          : "running"
        } />
        <span style={{
          flex: 1, color: THEME.text,
          fontFamily: FONT_SERIF, fontSize: 13, fontStyle: "italic",
        }}>
          {terminalFailure
            ? "Pipeline stopped — see the red rows for where"
            : done
              ? (hasSoftError
                  ? `Completed in ${events?.length || 0} steps (with recovered warnings)`
                  : `Completed in ${events?.length || 0} steps`)
              : events?.length
                ? `Working… ${events[events.length - 1]?.message?.slice(0, 60) || ""}`
                : "Working…"}
        </span>
      </div>

      {/* Step rows — always visible */}
      <div className="aria-scroll aria-scroll-raised" style={{
        maxHeight: 340, overflowY: "auto",
        padding: "4px 0",
      }}>
        {steps.map((s, i) => (
          <div key={i} style={{
            display: "flex", alignItems: "flex-start", gap: 10,
            padding: "6px 14px",
            fontSize: 13,
            color: s.status === "error" ? THEME.error
                  : s.status === "warning" ? "#8A6A1B"
                  : THEME.text,
          }}>
            <span style={{ marginTop: 4 }}><_StatusIcon status={s.status} /></span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{
                display: "flex", gap: 8, alignItems: "baseline",
              }}>
                <span style={{
                  fontSize: 10, fontWeight: 600, letterSpacing: "0.08em",
                  textTransform: "uppercase",
                  color: THEME.mutedLo,
                  fontFamily: FONT_MONO,
                  flexShrink: 0, minWidth: 56,
                }}>{s.phase}</span>
                <span style={{
                  fontFamily: FONT_SERIF,
                  wordBreak: "break-word",
                }}>{s.label}</span>
                {s.timestamp && (
                  <span style={{
                    marginLeft: "auto", fontSize: 10,
                    color: THEME.mutedLo, fontFamily: FONT_MONO,
                    flexShrink: 0,
                  }}>{s.timestamp}</span>
                )}
              </div>
              {s.detail && (
                <div style={{
                  fontSize: 11, color: THEME.muted,
                  fontFamily: FONT_MONO, marginTop: 2,
                  wordBreak: "break-word",
                }}>{s.detail}</div>
              )}
            </div>
          </div>
        ))}
        {/* Endless-runner loading animation — shown the whole time the
            pipeline is running. After 5s of silence we also append a
            "Still working after <phase> · waited Xs" caption so users
            know the slow LLM stretches (30-90s) aren't a freeze. */}
        {!done && !error && (events || []).length > 0 && (() => {
          const last = events[events.length - 1];
          const lastTs = (last && last._clientTs) || 0;
          const waited = lastTs ? Math.floor((Date.now() - lastTs) / 1000) : 0;
          let caption = null;
          if (waited >= 5) {
            const phase = (() => {
              const c = _classifyEvent(last, false);
              return c.phase || "Step";
            })();
            caption = `Still working after ${phase} · waited ${waited}s${
              waited > 30 ? " — slow LLM call, hold tight" : "…"
            }`;
          }
          return (
            <div style={{
              borderTop: `1px dashed ${THEME.border}`,
              marginTop: 2,
            }}>
              <DinoRunner
                message={caption}
                theme={{
                  bg:     "transparent",
                  fg:     THEME.text,
                  accent: THEME.accent,
                  muted:  THEME.mutedLo,
                }}
              />
            </div>
          );
        })()}
        <div ref={tailRef} />
      </div>

      {/* Raw log disclosure — keep for debugging */}
      <div style={{ borderTop: `1px solid ${THEME.border}` }}>
        <button
          onClick={() => setRawOpen(o => !o)}
          style={{
            width: "100%",
            display: "flex", alignItems: "center", gap: 6,
            padding: "6px 14px",
            background: "transparent",
            border: "none",
            cursor: "pointer", textAlign: "left",
            fontFamily: FONT_SERIF, fontSize: 11,
            color: THEME.mutedLo, fontStyle: "italic",
          }}
        >
          <span style={{
            transform: rawOpen ? "rotate(90deg)" : "none",
            transition: "transform 0.15s", display: "inline-block",
          }}>▸</span>
          Raw event log ({steps.length})
        </button>
        {rawOpen && (
          <pre className="aria-scroll aria-scroll-raised" style={{
            margin: 0, padding: "8px 14px 12px",
            fontFamily: FONT_MONO, fontSize: 11,
            color: THEME.muted,
            whiteSpace: "pre-wrap", wordBreak: "break-word",
            maxHeight: 200, overflowY: "auto",
            background: "transparent",
          }}>
            {(events || []).map((ev, i) =>
              typeof ev === "string" ? ev
                : `[${ev.seq ?? i}] ${ev.timestamp || ""} ${ev.type} :: ${ev.message || ""}` +
                  (ev.data && Object.keys(ev.data).length
                    ? "\n    data: " + JSON.stringify(ev.data).slice(0, 200)
                    : "")
            ).join("\n") || "(no events)"}
          </pre>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------- */
/* Message bubble                                                            */
/* ------------------------------------------------------------------------- */

/* Clarification card — renders the LLM's list of missing critical
   fields as an inline form. Each field gets a dropdown (if options)
   or a text input. Pre-filled with the LLM's smart default so the
   user can hit Generate immediately or tweak first. */
function ClarifyCard({ msg, onSubmit }) {
  const [answers, setAnswers] = useState(() => {
    const init = {};
    for (const c of msg.clarifications || []) {
      init[c.field] = c.default ?? "";
    }
    return init;
  });
  const [submitted, setSubmitted] = useState(false);
  if (submitted) {
    return (
      <div style={{
        margin: "14px 0", padding: "10px 14px",
        fontSize: 13, color: THEME.muted, fontStyle: "italic",
      }}>Generating with your clarifications…</div>
    );
  }
  return (
    <div style={{
      margin: "14px 0",
      padding: "14px 16px",
      background: THEME.bgRaised,
      border: `1px solid ${THEME.borderHi}`,
      borderRadius: 14,
      boxShadow: THEME.shadowSm,
    }}>
      <div style={{
        fontSize: 11, letterSpacing: "0.14em", fontWeight: 700,
        color: THEME.accent, marginBottom: 4,
      }}>NEEDS A FEW ENGINEERING SPECS</div>
      <div style={{
        fontFamily: FONT_SERIF, fontStyle: "italic",
        fontSize: 15, color: THEME.text, marginBottom: 14,
      }}>{msg.summary || "A few production details before we design."}</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {(msg.clarifications || []).map(c => (
          <div key={c.field}>
            <div style={{
              fontSize: 13, fontWeight: 500, color: THEME.text,
              marginBottom: 4,
            }}>{c.question}</div>
            {c.options && c.options.length > 0 ? (
              <select
                value={answers[c.field] ?? ""}
                onChange={e => setAnswers(a => ({ ...a, [c.field]: e.target.value }))}
                style={{
                  width: "100%", padding: "7px 10px",
                  fontSize: 13, fontFamily: "inherit",
                  background: THEME.bgCode,
                  border: `1px solid ${THEME.border}`,
                  borderRadius: 8,
                  color: THEME.text, cursor: "pointer",
                }}
              >
                {c.options.map(opt => (
                  <option key={opt} value={opt}>{opt}</option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={answers[c.field] ?? ""}
                onChange={e => setAnswers(a => ({ ...a, [c.field]: e.target.value }))}
                style={{
                  width: "100%", padding: "7px 10px",
                  fontSize: 13, fontFamily: "inherit",
                  background: THEME.bgCode,
                  border: `1px solid ${THEME.border}`,
                  borderRadius: 8,
                  color: THEME.text,
                }}
              />
            )}
            {c.rationale && (
              <div style={{ fontSize: 11, color: THEME.muted,
                             fontStyle: "italic", marginTop: 3 }}>
                {c.rationale}
              </div>
            )}
          </div>
        ))}
      </div>
      <div style={{ marginTop: 16, display: "flex", gap: 10,
                     justifyContent: "flex-end" }}>
        <button
          onClick={() => {
            setSubmitted(true);
            // Skip: use the ORIGINAL goal without any clarifications
            onSubmit?.(msg.goal, {});
          }}
          title="Generate without the clarifications (engineering defaults)"
          style={{
            padding: "8px 16px",
            background: "transparent",
            color: THEME.muted,
            border: `1px solid ${THEME.border}`,
            borderRadius: 8,
            fontSize: 13, fontWeight: 500, cursor: "pointer",
            fontFamily: "inherit",
          }}
        >Skip · use defaults</button>
        <button
          onClick={() => {
            setSubmitted(true);
            onSubmit?.(msg.goal, answers);
          }}
          style={{
            padding: "8px 18px",
            background: THEME.accent, color: "#FFF",
            border: "none", borderRadius: 8,
            fontSize: 13, fontWeight: 600, cursor: "pointer",
            fontFamily: "inherit",
          }}
        >Generate with clarifications</button>
      </div>
    </div>
  );
}


function Message({ msg, onInsert, onOpen, onAction, insertingId,
                    onClarifySubmit }) {
  // Clarification form — inline card with the LLM-detected missing
  // fields. User picks / edits → Generate fires with enriched spec.
  if (msg.role === "clarify") {
    return <ClarifyCard msg={msg} onSubmit={onClarifySubmit} />;
  }
  const isUser = msg.role === "user";
  if (isUser) {
    // Claude-style user message: soft taupe pill, right-aligned, rounded
    return (
      <div style={{
        display: "flex", justifyContent: "flex-end",
        margin: "28px 0 0",
      }}>
        <div style={{
          maxWidth: "82%",
          padding: "12px 18px",
          background: THEME.user,
          color: THEME.userText,
          borderRadius: 20,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          fontSize: "inherit",
          lineHeight: 1.55,
        }}>{msg.content}</div>
      </div>
    );
  }
  // Claude-style assistant message: NO bubble, NO avatar, just serif text
  // on the canvas. Only the thinking block and artifacts get their own
  // card treatment.
  return (
    <div style={{ margin: "20px 0 0", padding: "0 2px" }}>
      {msg.content && (
        <div style={{
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          color: THEME.text,
        }}>
          {msg.content}
        </div>
      )}
      {msg.thinking && msg.thinking.length > 0 && (
        <StepTree events={msg.thinking}
          done={msg.status === "done"}
          error={msg.status === "error"} />
      )}
      {msg.artifacts && msg.artifacts.map((art, i) => (
        <ArtifactCard key={i} artifact={art}
          onInsert={onInsert} onOpen={onOpen} onAction={onAction}
          inserting={insertingId === (art.path || art.id)} />
      ))}
      {msg.error && (
        <div style={{
          marginTop: 8, padding: "8px 12px",
          background: "#FDF2EF",
          border: `1px solid ${THEME.error}33`,
          borderRadius: 8,
          color: THEME.error, fontSize: 13,
        }}>{msg.error}</div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------------- */
/* Main panel                                                                 */
/* ------------------------------------------------------------------------- */

export default function ChatPanel() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [insertingId, setInsertingId] = useState(null);
  const [composerFocused, setComposerFocused] = useState(false);
  const [model, setModel] = useState(() =>
    localStorage.getItem("aria.model") || "balanced");
  const [mode, setMode] = useState(() =>
    localStorage.getItem("aria.mode") || "auto");
  // "auto" lets the backend pick the right pipeline from the prompt.
  // Users can force a specific mode via the compact dropdown next to
  // the model picker.
  const [enhancing, setEnhancing] = useState(false);
  const transcriptRef = useRef(null);
  const streamRef = useRef(null);
  const fileInputRef = useRef(null);
  const [attachments, setAttachments] = useState([]);

  // Persist model + mode so the panel remembers across sessions
  useEffect(() => { localStorage.setItem("aria.model", model); }, [model]);
  useEffect(() => { localStorage.setItem("aria.mode", mode); }, [mode]);

  // Auto-scroll on new content
  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [messages]);

  const appendAssistantThinking = useCallback((evOrLine) => {
    // Stamp the event with client-arrival time so StepTree can render a
    // "waited Xs" heartbeat row during long silent stretches (slow LLM).
    const stamped = typeof evOrLine === "object"
      ? { ...evOrLine, _clientTs: Date.now() }
      : { _raw: String(evOrLine), _clientTs: Date.now(), type: "log",
           message: String(evOrLine) };
    setMessages(msgs => {
      if (!msgs.length) return msgs;
      const last = msgs[msgs.length - 1];
      if (last.role !== "assistant") return msgs;
      return [
        ...msgs.slice(0, -1),
        { ...last, thinking: [...(last.thinking || []), stamped] },
      ];
    });
  }, []);

  const updateAssistant = useCallback((updater) => {
    setMessages(msgs => {
      if (!msgs.length) return msgs;
      const last = msgs[msgs.length - 1];
      if (last.role !== "assistant") return msgs;
      return [...msgs.slice(0, -1), updater(last)];
    });
  }, []);

  const submit = useCallback(async () => {
    const goal = input.trim();
    if (!goal || running) return;
    setInput("");
    // Show the user bubble immediately
    setMessages(m => [...m, { role: "user", content: goal }]);

    // Pre-planner: LLM identifies missing production-critical fields.
    // If it finds any, we render an inline form and return — the form's
    // Generate button calls _generateCore with the enriched goal.
    try {
      const r = await fetch(api("/clarify"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          goal,
          quality_tier: localStorage.getItem("aria.model") || "fast",
        }),
      });
      if (r.ok) {
        const j = await r.json();
        if (!j.enough_info && (j.clarifications || []).length > 0) {
          setMessages(m => [...m, {
            role: "clarify",
            goal,
            part_family: j.part_family || "",
            summary: j.summary || "",
            clarifications: j.clarifications,
          }]);
          return;   // wait for user to click Generate on the form
        }
      }
    } catch { /* non-fatal */ }

    // No clarification needed — run the pipeline directly
    await _generateCore(goal);
  }, [input, running]);

  // Called by the clarify form's Generate button with enriched goal.
  // If user hit "Skip", answers is {} — don't append an empty
  // Clarifications header; just send the original goal untouched.
  const submitWithClarifications = useCallback(async (originalGoal, answers) => {
    const entries = Object.entries(answers || {})
      .filter(([, v]) => v != null && v !== "");
    const augmented = entries.length > 0
      ? originalGoal + "\n\n## Clarifications\n"
        + entries.map(([k, v]) => `- ${k}: ${v}`).join("\n")
      : originalGoal;
    // Only show a re-send user bubble when there were actual clarifications;
    // otherwise the user already sees the original prompt above the form.
    if (entries.length > 0) {
      setMessages(m => [
        ...m, { role: "user", content: augmented, isClarified: true },
      ]);
    }
    await _generateCore(augmented);
  }, []);

  const _generateCore = useCallback(async (goal) => {
    setRunning(true);
    setMessages(m => [...m, {
      role: "assistant", content: "", thinking: [], artifacts: [],
      status: "running",
    }]);

    // Close any prior stream
    if (streamRef.current) {
      try { streamRef.current.close(); } catch { /* noop */ }
      streamRef.current = null;
    }

    // Capture the current highest event seq so we can IGNORE any events
    // replayed from prior runs (the SSE handshake sends the last 30 as
    // history). Anything seq <= startSeq belongs to an older run.
    let startSeq = 0;
    try {
      const r = await fetch(api("/log/recent?n=1"));
      if (r.ok) {
        const d = await r.json();
        const last = (d.events || [])[0];
        startSeq = (last && last.seq) || 0;
      }
    } catch { /* non-fatal — 0 means we accept all events */ }
    const seenSeqs = new Set();

    // Gather live host context: selection, feature tree, current user
    // parameters. Lets ARIA (a) target user-selected entities, (b) see
    // what's already in the design so continuation prompts work,
    // (c) stay in sync with parameter edits the user made in Fusion.
    let hostContext = null;
    if (bridge.isHosted) {
      try {
        const [sel, tree, params] = await Promise.all([
          bridge.getSelection().catch(() => []),
          bridge.getFeatureTree().catch(() => ({})),
          bridge.getUserParameters().catch(() => ({ parameters: [] })),
        ]);
        hostContext = {
          selection: sel || [],
          feature_tree: tree || {},
          user_parameters: (params && params.parameters) || [],
          host: bridge.kind,
        };
      } catch { /* non-fatal — backend handles null */ }
    }

    try {
      // Route by attachment type:
      //   - image files → POST /api/image_to_cad (multipart + prompt)
      //   - STL/PLY/OBJ → POST /api/scan_to_cad (multipart + prompt)
      //   - nothing / plain text → normal /api/generate
      // First attachment wins when multiple types are present; extra
      // files are sent along as metadata so the pipeline can reference
      // them (e.g. "reference image + scan").
      const imageAttach = attachments.find(a => a.route === "image_to_cad");
      const scanAttach  = attachments.find(a => a.route === "scan_to_cad");
      let res;
      if (imageAttach) {
        const fd = new FormData();
        fd.append("image", imageAttach.file);
        fd.append("prompt", goal);
        fd.append("mode", mode === "auto" ? "native" : mode);
        fd.append("quality_tier", model);
        res = await fetch(api("/image_to_cad"), { method: "POST", body: fd });
      } else if (scanAttach) {
        const fd = new FormData();
        fd.append("scan", scanAttach.file);
        fd.append("prompt", goal);
        fd.append("mode", mode === "auto" ? "native" : mode);
        fd.append("quality_tier", model);
        res = await fetch(api("/scan_to_cad"), { method: "POST", body: fd });
      } else {
        res = await fetch(api("/generate"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // model + mode flow through as hints. Backend reads:
          //   quality_tier → passed into call_llm as quality=fast|balanced|premium
          //   mode         → selects the domain router (auto/native/kicad/asm/dwg)
          //   host_context → CURRENT state from Fusion: selection, feature
          //                   tree, user parameters. Lets the planner
          //                   detect delta prompts, target selected
          //                   entities, and respect user-edited dims.
          body: JSON.stringify({
            goal, max_attempts: 3,
            quality_tier: model,
            mode,
            attachments: attachments.map(a => ({ name: a.name, size: a.size, route: a.route })),
            host_context: hostContext,
          }),
        });
      }
      // Clear attachments after a successful send so they don't replay
      // on the next prompt.
      setAttachments([]);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json().catch(() => ({}));

      // Backend emits events on the shared /api/log/stream (all pipelines,
      // not per-run-scoped). If a future backend provides {run_id}, we'll
      // honor it via /api/run/{id}/stream — the dual-path matches both
      // dashboard/aria_server.py (existing) and aria_os/api_server.py
      // (per-run experimental).
      const runId = body.run_id;
      const streamUrl = runId
        ? api(`/run/${runId}/stream`)
        : api("/log/stream");
      const es = new EventSource(streamUrl);
      streamRef.current = es;

      // Track settle-timer: when no events arrive for N seconds AFTER we've
      // seen at least one, assume the pipeline is truly stalled (not just
      // waiting on an LLM) and close the stream.
      //
      // 2026-04-21 bumped 90s → 300s. The dashboard emits `complete` /
      // `error` events on both success AND failure paths, so settle is
      // pure insurance for malformed runs. A slow Anthropic call on the
      // balanced tier can easily go 60-120s between emits on cold-start,
      // and we'd rather keep the stream alive through silence than cut
      // someone off mid-generation.
      let settleTimer = null;
      let sawAnyEvent = false;
      const SETTLE_MS = 300_000;
      const resetSettle = () => {
        if (settleTimer) clearTimeout(settleTimer);
        settleTimer = setTimeout(() => {
          updateAssistant(m => m.status === "running"
            ? { ...m, status: "done",
                 content: m.content || "Done — no events for 5 minutes; check the generated artifacts below." }
            : m);
          es.close();
          if (streamRef.current === es) streamRef.current = null;
          setRunning(false);
        }, SETTLE_MS);
      };

      es.onmessage = (e) => {
        try {
          const payload = JSON.parse(e.data);
          // Run-scope guard: skip anything replayed from a prior pipeline
          // run AND anything we've already delivered to the tree.
          const seq = payload.seq;
          if (typeof seq === "number") {
            if (seq <= startSeq) return;
            if (seenSeqs.has(seq)) return;
            seenSeqs.add(seq);
          }
          sawAnyEvent = true;
          resetSettle();
          // Per-run stream format: `{done: true, status: ..., artifacts: [...]}`
          if (payload.done) {
            updateAssistant(m => ({
              ...m,
              status: payload.status === "done" ? "done" : "error",
              content: payload.status === "done"
                ? (m.content || "Done. Generated artifacts below.")
                : (m.content || "Pipeline stopped."),
              artifacts: payload.artifacts || m.artifacts || [],
              error: payload.status && payload.status !== "done"
                ? "Pipeline did not finish cleanly" : undefined,
            }));
            if (settleTimer) clearTimeout(settleTimer);
            es.close();
            if (streamRef.current === es) streamRef.current = null;
            setRunning(false);
            return;
          }
          // dashboard/aria_server.py emits `{type, message, data, seq}` events
          const kind = payload.type;
          const msg = payload.message ?? payload.text ?? payload.data;
          // Explicit terminal events — close immediately, don't wait for settle
          if (kind === "complete") {
            // Pipeline emits multiple "complete" events (e.g. "CadQuery
            // generation complete" then "Pipeline complete for X"). Only
            // the LAST one should add an artifact card. We treat a complete
            // whose message starts with "Pipeline complete" as terminal,
            // and ignore earlier sub-stage completes for artifact purposes.
            const isTerminal = typeof msg === "string" &&
                                /^pipeline complete/i.test(msg);
            updateAssistant(m => ({
              ...m,
              // Only flip to "done" on the terminal complete. Sub-stage
              // completes need to leave status === "running" so the
              // DinoRunner stays mounted for the rest of the pipeline.
              status: isTerminal ? "done" : (m.status || "running"),
              content: m.content ||
                (typeof msg === "string" ? msg : "Pipeline complete."),
              artifacts: payload.data?.artifacts || payload.artifacts
                          || m.artifacts || [],
            }));
            // Sub-stage complete — just append the event, don't close yet.
            if (!isTerminal) {
              appendAssistantThinking(payload);
              return;
            }
            // Native-mode post-stream verification: after all ops land
            // in Fusion/Rhino, export the actual host geometry back and
            // ask the backend to run EvalAgent on it. Keep SSE OPEN so
            // the verify_visual events flow into the same feature tree.
            // The eval endpoint emits its own final complete (with an
            // "EvalAgent:" prefix that isTerminal also matches) which
            // will close the stream.
            const isNativeRun = payload.data?.mode === "native" && bridge.isHosted;
            if (isNativeRun && !es._ariaEvalKicked) {
              es._ariaEvalKicked = true;
              (async () => {
                try {
                  appendAssistantThinking({
                    type: "visual",
                    message: "Exporting host geometry for EvalAgent…",
                    timestamp: new Date().toTimeString().slice(0, 8),
                  });
                  const exp = await bridge.exportCurrent("stl");
                  // Prefer the absolute path (Fusion adds-in return it
                  // directly now); fall back to url parsing.
                  let pathForPost = exp?.path || null;
                  let urlForPost  = null;
                  if (!pathForPost && exp?.url) {
                    if (exp.url.startsWith("file://")) {
                      // Windows file URL: file:///C:/path/to/file → C:/path/to/file
                      pathForPost = decodeURIComponent(
                        exp.url.replace(/^file:\/+/, ""));
                    } else {
                      urlForPost = exp.url;
                    }
                  }
                  if (!pathForPost && !urlForPost) {
                    appendAssistantThinking({
                      type: "warning",
                      message: "Host returned no STL path — skipping eval",
                    });
                    if (settleTimer) clearTimeout(settleTimer);
                    es.close();
                    if (streamRef.current === es) streamRef.current = null;
                    setRunning(false);
                    return;
                  }
                  appendAssistantThinking({
                    type: "visual",
                    message: `Exported ${exp?.bytes || '?'} bytes → ${(pathForPost||urlForPost||'').split(/[\\/]/).pop()}`,
                  });
                  // Loop: eval → (if REFINING) wait for new ops to
                  // stream → re-export → eval again. Stop at PASS, FAIL
                  // with no retries left, or iteration cap.
                  const runEvalLoop = async (iteration) => {
                    const resp = await fetch(api("/native_eval"), {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({
                        goal: payload.data?.goal || "",
                        stl_url: urlForPost,
                        stl_path: pathForPost,
                        iteration,
                        max_iterations: 3,
                        quality_tier: localStorage.getItem("aria.model") || "balanced",
                      }),
                    });
                    if (!resp.ok) {
                      const errTxt = await resp.text().catch(() => "");
                      appendAssistantThinking({
                        type: "error",
                        message: `EvalAgent HTTP ${resp.status}: ${errTxt.slice(0, 300)}`,
                      });
                      if (settleTimer) clearTimeout(settleTimer);
                      es.close();
                      if (streamRef.current === es) streamRef.current = null;
                      setRunning(false);
                      return;
                    }
                    const j = await resp.json().catch(() => ({}));
                    if (j.verdict === "REFINING") {
                      // Backend just streamed a patch plan. Wait for the
                      // panel to dispatch every op, then re-export +
                      // re-eval. 2 seconds is a rough upper bound for the
                      // bridge round-trips on a typical N=10 plan.
                      appendAssistantThinking({
                        type: "agent",
                        message: `Refinement pass ${iteration} streamed — waiting for Fusion, then re-eval…`,
                      });
                      setTimeout(async () => {
                        try {
                          const exp2 = await bridge.exportCurrent("stl");
                          const u2 = exp2?.url || null;
                          if (u2) {
                            urlForPost = u2.startsWith("file://") ? null : u2;
                            pathForPost = u2.startsWith("file://")
                              ? decodeURIComponent(u2.replace(/^file:\/+/, "/"))
                              : null;
                            await runEvalLoop(iteration + 1);
                          }
                        } catch (e) {
                          appendAssistantThinking({
                            type: "warning",
                            message: `Re-eval skipped: ${e?.message || e}`,
                          });
                        }
                      }, 2500);
                    }
                    // PASS / FAIL-no-retry land as normal complete events
                    // over SSE, closing the stream cleanly.
                  };
                  await runEvalLoop(1);
                } catch (err) {
                  appendAssistantThinking({
                    type: "warning",
                    message: `Native eval skipped: ${err?.message || err}`,
                  });
                  if (settleTimer) clearTimeout(settleTimer);
                  es.close();
                  if (streamRef.current === es) streamRef.current = null;
                  setRunning(false);
                }
              })();
              // DON'T close SSE here — wait for eval's terminal complete
              return;
            }

            if (settleTimer) clearTimeout(settleTimer);
            es.close();
            if (streamRef.current === es) streamRef.current = null;
            setRunning(false);

            // Build the artifact directly from the events we've observed in
            // THIS run. Dedupe by path so repeated completes don't add
            // multiple cards for the same file.
            updateAssistant(m => {
              const thinking = m.thinking || [];
              let stepPath = null, stlPath = null, bbox = null, partId = null;
              for (let i = thinking.length - 1; i >= 0; i--) {
                const ev = thinking[i];
                if (!ev || typeof ev !== "object") continue;
                const lmsg = (ev.message || "").toLowerCase();
                const d = ev.data || {};
                if (!stepPath && lmsg.startsWith("step exported") && d.path) {
                  stepPath = d.path; partId = partId || d.part_id;
                }
                if (!stlPath && lmsg.startsWith("stl exported") && d.path) {
                  stlPath = d.path; partId = partId || d.part_id;
                }
                if (!bbox && lmsg.startsWith("geometry valid") && d.bbox) {
                  bbox = d.bbox; partId = partId || d.part_id;
                }
                if (!partId && lmsg.startsWith("template matched") && d.part_id) {
                  partId = d.part_id;
                }
                if (stepPath && stlPath && bbox && partId) break;
              }
              // Also look for terminal-complete's part_id in data
              partId = partId || payload.data?.part_id
                     || payload.data?.session?.part_id;

              // Dedupe existing artifacts by path
              const existing = (m.artifacts || []);
              const hasPath = p => p && existing.some(a => a.path === p);

              // Stream didn't carry paths — fall back to /api/parts (agent-loop
              // path in DesignerAgent doesn't emit through cadquery_generator)
              if (!stepPath && !stlPath) {
                const runStarted = Date.now() - 5 * 60 * 1000;
                fetch(api("/parts")).then(r => r.ok ? r.json() : { parts: [] })
                  .then(d => {
                    const parts = (d.parts || []).slice().sort((a, b) =>
                      (Date.parse(b.timestamp || "") || 0) -
                      (Date.parse(a.timestamp || "") || 0));
                    const fresh = parts.filter(p => {
                      const t = Date.parse(p.timestamp || "");
                      return isFinite(t) && t >= runStarted;
                    });
                    const picked = (fresh.length ? fresh : parts).slice(0, 1);
                    if (!picked.length) return;
                    updateAssistant(mm => {
                      const have = mm.artifacts || [];
                      const newArts = picked
                        .filter(p => !have.some(a =>
                          a.path === (p.step_path || p.path)))
                        .map(p => {
                          const pth = p.step_path || p.path;
                          return {
                            filename: p.part_id || p.filename,
                            path: pth,
                            url: pth ? api("/artifacts/download?path="
                                            + encodeURIComponent(pth)) : null,
                            bbox: p.bbox,
                            kind: "STEP",
                          };
                        });
                      return { ...mm, artifacts: [...have, ...newArts] };
                    });
                  }).catch(() => {});
                return m;
              }
              const primaryPath = stepPath || stlPath;
              if (hasPath(primaryPath)) return m;   // dedupe
              const art = {
                filename: partId ||
                  (primaryPath || "").split(/[\\/]/).pop(),
                path: primaryPath,
                url: api("/artifacts/download?path=" +
                          encodeURIComponent(primaryPath)),
                stl_path: stlPath,
                bbox,
                kind: "STEP",
              };
              return { ...m, artifacts: [...existing, art] };
            });
            return;
          }
          // We want the FULL event object in the step tree, not just text,
          // so StepTree can classify by type / data / timestamp.
          // Append the structured event first; handle terminal states after.
          if (payload && (payload.type || payload.message)) {
            appendAssistantThinking(payload);
          } else if (msg) {
            appendAssistantThinking(
              typeof msg === "string" ? msg : JSON.stringify(msg));
          }

          // Native mode: every `native_op` event is a single feature-tree
          // operation. Dispatch it through the bridge so Fusion (or Rhino)
          // executes it as a real native feature. Each op's outcome is
          // appended as a new event so the tree shows success/failure
          // inline beneath the op that triggered it.
          // Exception: ECAD ops (`domain: "ecad"`) execute server-side
          // against a growing .kicad_pcb — no bridge dispatch needed.
          if (kind === "native_op" && payload.data?.domain === "ecad") {
            // nothing to do here; the server already executes + emits
            // native_result events with the outcome of each op.
          } else if (kind === "native_op" && bridge.isHosted) {
            const d = payload.data || {};
            const opKind = d.kind;
            const opParams = d.params || {};
            bridge.executeFeature(opKind, opParams).then((reply) => {
              appendAssistantThinking({
                type: "native_result",
                message: `✓ ${opKind}${reply?.id ? ` → ${reply.id}` : ""}`,
                data: { kind: opKind, reply, seq: d.seq },
                timestamp: new Date().toTimeString().slice(0, 8),
              });
            }).catch((e) => {
              appendAssistantThinking({
                type: "error",
                message: `Feature op failed: ${opKind} — ${e?.message || e}`,
                data: { kind: opKind, params: opParams, seq: d.seq },
                timestamp: new Date().toTimeString().slice(0, 8),
              });
            });
          } else if (kind === "native_op" && !bridge.isHosted) {
            // Standalone browser — can't execute native features. Emit a
            // friendly heads-up once, then continue rendering the tree.
            if (!streamRef.current?._nativeWarned) {
              appendAssistantThinking({
                type: "warning",
                message: "Native mode needs Fusion. Open this panel inside " +
                          "the Fusion ARIA add-in to stream features into " +
                          "the real feature tree.",
                timestamp: new Date().toTimeString().slice(0, 8),
              });
              if (streamRef.current) streamRef.current._nativeWarned = true;
            }
          }

          if (kind === "error") {
            // Don't close on single errors — the pipeline often retries.
            // Only terminate if the error message indicates a hard stop
            // (signal like "aborted", "fatal", "pipeline error:" prefix).
            const hardStop = typeof msg === "string" && (
              /^pipeline error:/i.test(msg) ||
              /fatal|aborted/i.test(msg));
            if (hardStop) {
              updateAssistant(m => ({ ...m, status: "error",
                error: typeof msg === "string" ? msg : JSON.stringify(msg) }));
              if (settleTimer) clearTimeout(settleTimer);
              es.close();
              if (streamRef.current === es) streamRef.current = null;
              setRunning(false);
              return;
            }
            // Soft error — StepTree shows it inline as a red row, pipeline
            // continues retrying.
          }
          if (payload.artifacts) {
            updateAssistant(m => ({ ...m,
              artifacts: [...(m.artifacts || []), ...payload.artifacts] }));
          }
        } catch {
          if (e.data && !e.data.startsWith(":")) {
            appendAssistantThinking(e.data);
          }
        }
      };
      es.onerror = () => {
        if (settleTimer) clearTimeout(settleTimer);
        es.close();
        if (streamRef.current === es) streamRef.current = null;
        updateAssistant(m => {
          if (m.status !== "running") return m;
          // If we never got a single event, the stream endpoint is probably
          // unavailable. If we got some events, the pipeline likely finished
          // (browsers close SSE on inactivity) — mark done, not error.
          return sawAnyEvent
            ? { ...m, status: "done",
                 content: m.content || "Done — check generated artifacts." }
            : { ...m, status: "error",
                 error: "Event stream unavailable — is the backend running?" };
        });
        setRunning(false);
      };
    } catch (e) {
      updateAssistant(m => ({ ...m, status: "error",
        error: e.message || String(e) }));
      setRunning(false);
    }
  }, [mode, model, attachments, appendAssistantThinking, updateAssistant]);

  const handleInsert = useCallback(async (artifact) => {
    if (!bridge.isHosted) return;
    const id = artifact.path || artifact.id || artifact.filename;
    setInsertingId(id);
    try {
      // Derive absolute URL for the host to download from
      const url = artifact.url ||
        (artifact.path
          ? `${API_BASE}/artifacts/download?path=${encodeURIComponent(artifact.path)}`
          : null);
      if (!url) throw new Error("artifact has no downloadable URL");
      await bridge.insertGeometry(url);
      bridge.showNotification(
        `Inserted ${artifact.filename || "part"}`, "success");
    } catch (e) {
      bridge.showNotification(`Insert failed: ${e.message}`, "error");
    } finally {
      setInsertingId(null);
    }
  }, []);

  // Post-creation actions — DFM, Quote, CAM, FEA, Drawing, Gerbers.
  // Each action POSTs to a dedicated backend endpoint that runs the
  // corresponding pipeline and emits events over SSE so the feature
  // tree fills in with progress. The panel just kicks them off.
  const handleAction = useCallback(async (actionId, artifact) => {
    try {
      const payload = {
        action: actionId,
        artifact: {
          filename: artifact.filename,
          path: artifact.path,
          stl_path: artifact.stl_path,
          kind: artifact.kind,
          bbox: artifact.bbox,
        },
      };
      const res = await fetch(api("/artifact_action"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const txt = await res.text().catch(() => "");
        bridge.showNotification(
          `${actionId} failed: ${res.status} ${txt.slice(0, 120)}`, "error");
        return;
      }
      bridge.showNotification(
        `${actionId} started — watch the feature tree for results.`, "info");
    } catch (err) {
      bridge.showNotification(
        `${actionId} error: ${err?.message || err}`, "error");
    }
  }, []);

  const handleOpen = useCallback((artifact) => {
    // Build a working download URL even if the artifact didn't ship one.
    const url = artifact.url ||
      (artifact.path
        ? `${API_BASE}/artifacts/download?path=${encodeURIComponent(artifact.path)}`
        : null);
    if (!url) {
      bridge.showNotification("No artifact URL to open", "error");
      return;
    }
    if (bridge.isHosted) {
      // In Fusion/Rhino, `insertGeometry(url)` downloads the STEP via HTTP
      // and calls the host's importManager. That actually works — unlike
      // `openFile(local_path)` which uses the wrong cloud-document API.
      bridge.insertGeometry(url).then(() => {
        bridge.showNotification(
          `Imported ${artifact.filename || "part"} into the active design`,
          "success");
      }).catch((e) => {
        // Last-ditch: open the URL in the browser (works in Rhino WebView2
        // for debugging, may be blocked in Fusion).
        bridge.showNotification(
          `Import failed: ${e?.message || e}. Opening in browser.`, "error");
        try { window.open(url, "_blank"); } catch { /* blocked */ }
      });
    } else {
      window.open(url, "_blank");
    }
  }, []);

  const onKeyDown = useCallback((e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  }, [submit]);

  const placeholder = useMemo(() => {
    if (bridge.kind === "fusion") return "Describe a part to generate into this Fusion design…";
    if (bridge.kind === "rhino")  return "Describe a part to drop into Rhino…";
    if (bridge.kind === "onshape")return "Describe a part to import into Onshape…";
    return "Describe a part (e.g. \"bracket 80×60×40mm, 4 M6 holes\")…";
  }, []);

  const newChat = useCallback(() => {
    setMessages([]);
    setInput("");
    setAttachments([]);
    if (streamRef.current) {
      try { streamRef.current.close(); } catch { /* noop */ }
      streamRef.current = null;
    }
    setRunning(false);
  }, []);

  return (
    <div style={ROOT_STYLE}>
      {/* keyframes + custom scrollbars (matches Claude's subtle thin rail).
          Scoped to `.aria-scroll` so we don't mutate the host CAD's scroll
          chrome. Uses WebKit selectors (Fusion's WebView2 / Rhino WebView2 /
          Chrome are all WebKit-family) plus the Firefox `scrollbar-*` props
          as a fallback. */}
      <style>{`
        @keyframes ariaPulse { 0%,100%{opacity:.4}50%{opacity:1} }
        @keyframes ariaSpin  { to { transform: rotate(360deg) } }
        .aria-scroll {
          scrollbar-width: thin;
          scrollbar-color: ${THEME.borderHi} transparent;
        }
        .aria-scroll::-webkit-scrollbar {
          width: 10px;
          height: 10px;
        }
        .aria-scroll::-webkit-scrollbar-track {
          background: transparent;
        }
        .aria-scroll::-webkit-scrollbar-thumb {
          background: rgba(0,0,0,0.12);
          border: 2px solid ${THEME.bg};
          border-radius: 10px;
          min-height: 40px;
        }
        .aria-scroll::-webkit-scrollbar-thumb:hover {
          background: rgba(0,0,0,0.22);
        }
        .aria-scroll::-webkit-scrollbar-thumb:active {
          background: ${THEME.accent};
        }
        /* Thinking block scrolls against the cream-code bg, so the
           thumb needs a darker track border. */
        .aria-scroll-raised::-webkit-scrollbar-thumb {
          background: rgba(0,0,0,0.16);
          border-color: ${THEME.bgCode};
        }
      `}</style>

      {/* Top bar — thin, serif wordmark left, new-chat button right */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "10px 14px",
        borderBottom: `1px solid ${THEME.border}`,
        flexShrink: 0,
      }}>
        <div style={{
          display: "inline-flex", alignItems: "center", gap: 8,
          color: THEME.text,
        }}>
          <span style={{
            width: 22, height: 22, borderRadius: 6,
            background: `linear-gradient(135deg, ${THEME.accent} 0%, #8a3e1e 100%)`,
            color: "#FFF",
            display: "inline-flex", alignItems: "center", justifyContent: "center",
            fontSize: 12, fontWeight: 700,
            fontFamily: FONT_SERIF, fontStyle: "italic",
          }}>A</span>
          <span style={{
            fontFamily: FONT_SERIF, fontStyle: "italic",
            fontSize: 15, fontWeight: 500, letterSpacing: "-0.01em",
          }}>ARIA</span>
          {bridge.kind && (
            <span style={{
              marginLeft: 2, fontSize: 11, color: THEME.mutedLo,
              fontStyle: "italic",
            }}>· {bridge.kind}</span>
          )}
        </div>
        <button onClick={newChat} title="New chat"
          style={{
            display: "inline-flex", alignItems: "center", gap: 6,
            padding: "5px 10px 5px 8px",
            background: "transparent",
            border: `1px solid ${THEME.border}`,
            borderRadius: 100,
            color: THEME.muted,
            fontFamily: FONT_SERIF, fontSize: 12,
            cursor: "pointer",
            fontStyle: "italic",
          }}
          onMouseEnter={e => {
            e.currentTarget.style.background = THEME.bgCode;
            e.currentTarget.style.color = THEME.text;
          }}
          onMouseLeave={e => {
            e.currentTarget.style.background = "transparent";
            e.currentTarget.style.color = THEME.muted;
          }}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2.2"
               strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 20h9"/>
            <path d="M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/>
          </svg>
          New chat
        </button>
        <button
          onClick={() => {
            const host = bridge.kind || "";
            const q = host ? `?host=${encodeURIComponent(host)}` : "";
            window.location.href = `/native-ops${q}`;
          }}
          title="Native CAD ops — enrichDrawing, runFea, sheet metal, surface, materials"
          style={{
            marginLeft: 8,
            display: "inline-flex", alignItems: "center", gap: 6,
            padding: "5px 10px 5px 8px",
            background: "transparent",
            border: `1px solid ${THEME.border}`,
            borderRadius: 100,
            color: THEME.muted,
            fontFamily: FONT_SERIF, fontSize: 12,
            cursor: "pointer",
            fontStyle: "italic",
          }}
          onMouseEnter={e => {
            e.currentTarget.style.background = THEME.bgCode;
            e.currentTarget.style.color = THEME.text;
          }}
          onMouseLeave={e => {
            e.currentTarget.style.background = "transparent";
            e.currentTarget.style.color = THEME.muted;
          }}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2.2"
               strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3"/>
            <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09a1.65 1.65 0 00-1-1.51 1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09a1.65 1.65 0 001.51-1 1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06a1.65 1.65 0 001.82.33h0a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82v0a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/>
          </svg>
          Native ops
        </button>
      </div>

      {/* Transcript — scrolls. Claude centers the empty state with
          a top bias so the composer isn't stranded at the bottom of
          a blank canvas. */}
      <div ref={transcriptRef} className="aria-scroll" style={{
        flex: 1,
        overflowY: "auto",
        padding: messages.length === 0 ? "0 16px" : "32px 20px 16px",
        display: "flex",
        flexDirection: "column",
        justifyContent: messages.length === 0 ? "center" : "flex-start",
      }}>
        <div style={{
          maxWidth: 740, width: "100%", margin: "0 auto",
          paddingBottom: messages.length === 0 ? 0 : 40,
        }}>
          {messages.length === 0 && (
            <EmptyState />
          )}
          {messages.map((m, i) => (
            <Message key={i} msg={m}
              onInsert={handleInsert}
              onOpen={handleOpen}
              onAction={handleAction}
              onClarifySubmit={submitWithClarifications}
              insertingId={insertingId} />
          ))}
        </div>
      </div>

      {/* Composer — pinned bottom, Claude-style: fat white card with
          attach / model / send icons and a disclaimer below */}
      <div style={{
        padding: "12px 16px 16px",
        background: THEME.bg,
      }}>
        <div style={{ maxWidth: 720, margin: "0 auto" }}>
          <div style={{
            display: "flex", flexDirection: "column",
            background: THEME.bgRaised,
            border: `1px solid ${composerFocused ? THEME.borderHi : THEME.border}`,
            borderRadius: 20,                         // fat rounded-2xl
            boxShadow: composerFocused
              ? `${THEME.shadowSm}, 0 0 0 3px ${THEME.accentBg}`
              : THEME.shadowSm,
            transition: "border-color 0.15s, box-shadow 0.15s",
            // NOT overflow:hidden — the model dropdown popover needs to
            // escape this container to render above the chat transcript.
            // The inner textarea has its own border-radius clipping via
            // the rounded composer box, so dropping overflow:hidden here
            // doesn't cause visible artifacts.
            overflow: "visible",
            position: "relative",
          }}>
            {/* Textarea — full width, generous padding, no visible border */}
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              onFocus={() => setComposerFocused(true)}
              onBlur={() => setComposerFocused(false)}
              placeholder={placeholder}
              rows={Math.min(6, Math.max(2, input.split("\n").length))}
              style={{
                width: "100%",
                border: "none", outline: "none", resize: "none",
                background: "transparent",
                color: THEME.text,
                fontFamily: FONT_SERIF,               // serif even in the input
                fontSize: 15,
                lineHeight: 1.55,
                padding: "16px 18px 8px",
                boxSizing: "border-box",
              }}
            />
            {/* Attachment chips (above the toolbar, inside the card) */}
            {attachments.length > 0 && (
              <div style={{
                display: "flex", flexWrap: "wrap", gap: 6,
                padding: "0 14px 10px",
              }}>
                {attachments.map((a, i) => (
                  <span key={i} style={{
                    display: "inline-flex", alignItems: "center", gap: 6,
                    padding: "4px 6px 4px 10px",
                    background: THEME.bgCode,
                    border: `1px solid ${THEME.border}`,
                    borderRadius: 100,
                    fontSize: 12, color: THEME.text,
                    fontStyle: "italic",
                  }}>
                    {a.name}
                    <button onClick={() =>
                        setAttachments(as => as.filter((_, j) => j !== i))}
                      style={{
                        width: 18, height: 18, borderRadius: 9, border: "none",
                        background: "transparent", color: THEME.muted,
                        cursor: "pointer", padding: 0,
                        display: "inline-flex", alignItems: "center",
                        justifyContent: "center",
                      }}
                      aria-label={`remove ${a.name}`}
                    >
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none"
                           stroke="currentColor" strokeWidth="2.8"
                           strokeLinecap="round" strokeLinejoin="round">
                        <line x1="18" y1="6" x2="6" y2="18"/>
                        <line x1="6" y1="6" x2="18" y2="18"/>
                      </svg>
                    </button>
                  </span>
                ))}
              </div>
            )}

            {/* Divider line — AdamCAD pattern: border-t on the toolbar row */}
            <div style={{
              display: "flex", alignItems: "center", gap: 4,
              padding: "8px 10px",
              borderTop: `1px solid ${THEME.border}`,
              flexWrap: "wrap",
            }}>
              {/* ATTACH — multi-type: the file extension routes to the
                  right pipeline (image → image-to-CAD, STL/PLY → scan-
                  to-CAD, audio → STT → prompt text). The user just drops
                  the file; no mode switching needed. */}
              <input
                ref={fileInputRef} type="file" multiple
                accept=".step,.stp,.stl,.ply,.obj,.dxf,.png,.jpg,.jpeg,.webp,.pdf,.wav,.m4a,.mp3,.webm"
                style={{ display: "none" }}
                onChange={async e => {
                  const files = Array.from(e.target.files || []);
                  e.target.value = "";
                  for (const f of files) {
                    const ext = f.name.split(".").pop().toLowerCase();
                    // Audio → transcribe, insert into prompt box
                    if (["wav", "m4a", "mp3", "webm"].includes(ext)) {
                      try {
                        const fd = new FormData();
                        fd.append("audio", f);
                        const r = await fetch(api("/stt/transcribe"),
                                               { method: "POST", body: fd });
                        const j = await r.json();
                        if (j.text) setInput(s => (s + " " + j.text).trim());
                      } catch (err) {
                        bridge.showNotification(
                          `STT failed: ${err?.message || err}`, "error");
                      }
                      continue;
                    }
                    // STL / PLY / OBJ / STEP → scan-to-CAD route
                    if (["stl", "ply", "obj", "step", "stp"].includes(ext)) {
                      setAttachments(as => [...as, {
                        name: f.name, size: f.size, file: f,
                        route: "scan_to_cad", kind: ext,
                      }]);
                      continue;
                    }
                    // Images → image-to-CAD route
                    if (["png", "jpg", "jpeg", "webp"].includes(ext)) {
                      setAttachments(as => [...as, {
                        name: f.name, size: f.size, file: f,
                        route: "image_to_cad", kind: ext,
                      }]);
                      continue;
                    }
                    // Everything else → plain reference attachment
                    setAttachments(as => [...as, {
                      name: f.name, size: f.size, file: f,
                      route: "reference", kind: ext,
                    }]);
                  }
                }}
              />
              <IconBtn title="Attach image/scan/audio — routes automatically"
                onClick={() => fileInputRef.current?.click()}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
                     stroke="currentColor" strokeWidth="1.8"
                     strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/>
                </svg>
              </IconBtn>

              {/* MIC — live speech-to-text. Uses the browser's
                  MediaRecorder API; posts the captured blob to the STT
                  endpoint which transcribes via Groq Whisper. */}
              <MicButton onTranscript={(text) =>
                setInput(s => (s ? (s + " " + text).trim() : text))} />

              {/* Compact mode picker — default Auto lets backend detect
                  domain from the prompt. Power users can override. */}
              <ModeSelector value={mode} onChange={setMode} />


              {/* spacer */}
              <div style={{ flex: 1 }} />

              {/* Prompt enhancer wand */}
              <IconBtn title={input.trim() ? "Enhance prompt" : "Generate prompt"}
                disabled={enhancing || running}
                onClick={async () => {
                  if (!input.trim()) return;
                  setEnhancing(true);
                  // Stub: upcase the first letter + add a material hint.
                  // Real enhancer would call the backend.
                  setTimeout(() => {
                    setInput(s => {
                      const trimmed = s.trim();
                      if (!trimmed) return s;
                      const cap = trimmed[0].toUpperCase() + trimmed.slice(1);
                      return /material|aluminum|steel|plastic/i.test(cap)
                        ? cap
                        : `${cap}, 6061 aluminum`;
                    });
                    setEnhancing(false);
                  }, 350);
                }}>
                {enhancing ? (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="2.4"
                       style={{ animation: "ariaSpin 1s linear infinite" }}>
                    <path d="M21 12a9 9 0 11-6.2-8.56" strokeLinecap="round"/>
                  </svg>
                ) : (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="1.8"
                       strokeLinecap="round" strokeLinejoin="round">
                    <path d="M15 4V2"/><path d="M15 16v-2"/>
                    <path d="M8 9h2"/><path d="M20 9h2"/>
                    <path d="M17.8 11.8L19 13"/><path d="M15 9h0"/>
                    <path d="M17.8 6.2L19 5"/><path d="M3 21l9-9"/>
                    <path d="M12.2 6.2L11 5"/>
                  </svg>
                )}
              </IconBtn>

              {/* MODEL SELECTOR — real dropdown */}
              <ModelSelector value={model} onChange={setModel} />

              {/* SEND */}
              <button
                onClick={submit}
                disabled={!input.trim() || running}
                aria-label={running ? "running" : "send"}
                style={{
                  width: 34, height: 34, borderRadius: 10,
                  marginLeft: 4,
                  background: running ? THEME.bgCode
                               : input.trim() ? THEME.accent : THEME.bgCode,
                  color: running ? THEME.muted
                          : input.trim() ? "#FFF" : THEME.mutedLo,
                  border: "none",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  cursor: (running || !input.trim()) ? "not-allowed" : "pointer",
                  flexShrink: 0,
                  transition: "background 0.15s, transform 0.08s",
                }}
                onMouseDown={e => e.currentTarget.style.transform = "scale(0.94)"}
                onMouseUp={e => e.currentTarget.style.transform = "none"}
                onMouseLeave={e => e.currentTarget.style.transform = "none"}
              >
                {running ? (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                    <rect x="6" y="6" width="12" height="12" rx="2"/>
                  </svg>
                ) : (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="2.4"
                       strokeLinecap="round" strokeLinejoin="round">
                    <line x1="12" y1="19" x2="12" y2="5"/>
                    <polyline points="5 12 12 5 19 12"/>
                  </svg>
                )}
              </button>
            </div>
          </div>
          {/* Tiny disclaimer — exactly mirrors claude.ai's footer line */}
          <div style={{
            marginTop: 10,
            fontSize: 11, color: THEME.mutedLo,
            textAlign: "center",
            fontFamily: FONT_SERIF,
            fontStyle: "italic",
          }}>
            ARIA can make mistakes — double-check generated dimensions before fabrication.
          </div>
        </div>
      </div>
    </div>
  );
}


/* ------------------------------------------------------------------------- */
/* Empty state — shown before any messages                                    */
/* ------------------------------------------------------------------------- */

/**
 * EmptyState — centered serif headline + suggestion grid. Matches the
 * claude.ai home-screen feel: one big question, four subtle cards below.
 * Each card has an icon glyph, a bold title, and a muted one-line hint.
 * Grid collapses to a single column below 480px so it stays readable in
 * the narrow Fusion / Rhino dock.
 */
function EmptyState() {
  const suggestions = [
    { icon: "◎", title: "Flange",
      prompt: "flange 100mm OD, 4 M6 holes on 80mm PCD, 6mm thick, 6061 aluminum" },
    { icon: "⌐", title: "Bracket",
      prompt: "L-bracket 80x60x40mm, 5mm wall, 4 M5 mounting holes" },
    { icon: "✷", title: "Gear",
      prompt: "involute gear 30 tooth, module 1.5, 10mm bore with keyway" },
    { icon: "❋", title: "Impeller",
      prompt: "impeller 120mm OD, 6 backward-curved blades, 20mm bore" },
    { icon: "◴", title: "Shaft",
      prompt: "stepped shaft 200mm long, 20mm dia center, 12mm dia ends, keyway" },
    { icon: "⬡", title: "Housing",
      prompt: "rectangular housing 120x80x40mm, 3mm wall, lid with M4 screw bosses" },
  ];

  const fillInput = (text) => {
    const ta = document.querySelector("textarea");
    if (!ta) return;
    ta.focus();
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, "value").set;
    setter.call(ta, text);
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  };

  return (
    <div style={{
      textAlign: "center",
      padding: "0 8px",
      // No vh-offset — let parent flex centering position this. The
      // earlier `marginTop: 12vh` collided with the parent's
      // justifyContent:center and pushed content off-screen in shorter
      // iframes (Onshape Element tab).
    }}>
      <h1 style={{
        margin: "0 0 28px",
        fontSize: "clamp(22px, 3.2vw, 32px)",
        fontWeight: 400,
        fontStyle: "italic",
        color: THEME.text,
        letterSpacing: "-0.02em",
        lineHeight: 1.15,
        fontFamily: FONT_SERIF,
      }}>
        What would you like to{" "}
        <span style={{ color: THEME.accent, fontStyle: "italic" }}>design</span>
        {bridge.isHosted ? `, ${bridge.kind}?` : "?"}
      </h1>

      {/* Horizontal pill chips — wrap to multiple rows on narrow panels.
          Each chip is icon + label, single line, low visual weight so
          the prompt input stays the focal point. */}
      <div style={{
        display: "flex",
        flexWrap: "wrap",
        justifyContent: "center",
        gap: 8,
        maxWidth: 560,
        margin: "0 auto",
      }}>
        {suggestions.map((s, i) => (
          <button key={i}
            onClick={() => fillInput(s.prompt)}
            title={s.prompt}
            style={{
              display: "inline-flex", alignItems: "center", gap: 8,
              padding: "7px 14px 7px 12px",
              background: THEME.bgRaised,
              border: `1px solid ${THEME.border}`,
              borderRadius: 100,
              color: THEME.text, cursor: "pointer",
              fontFamily: "inherit",
              fontSize: 13,
              transition: "border-color 0.15s, background 0.15s, transform 0.12s",
            }}
            onMouseEnter={e => {
              e.currentTarget.style.borderColor = THEME.borderHi;
              e.currentTarget.style.background = THEME.bgCode;
            }}
            onMouseLeave={e => {
              e.currentTarget.style.borderColor = THEME.border;
              e.currentTarget.style.background = THEME.bgRaised;
            }}
            onMouseDown={e => { e.currentTarget.style.transform = "scale(0.97)"; }}
            onMouseUp={e => { e.currentTarget.style.transform = "none"; }}
          >
            <span style={{
              fontSize: 14, lineHeight: 1, color: THEME.accent,
              flexShrink: 0, fontFamily: FONT_SERIF,
            }}>{s.icon}</span>
            <span style={{ fontWeight: 500 }}>{s.title}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
