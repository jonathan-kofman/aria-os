/**
 * AgentTab — the hackathon face of aria_os.self_extend.
 *
 * Three sub-tabs:
 *   live    — prompt box + live SSE stream of orchestrator stages
 *   trust   — HITL registry (quarantined / review_required / trusted)
 *   history — recent agent runs
 *
 * The live tab is the judging demo: user types an English request, the
 * stream shows dispatch → hypothesis → implement → contract → physics
 * → review → PR as each stage fires. Every event is a row with status
 * pill + elapsed + stage-specific metadata.
 */
import { useState, useEffect, useRef } from "react";
import { useViewport, spacing } from "../responsive.js";
import { T } from "../aria/theme.js";
import { Panel } from "../aria/uiPrimitives.jsx";


const STAGE_COLORS = {
  dispatch:       T.ai,
  template_match: T.blue,
  hypothesis:     T.brand,
  implement:      T.amber,
  contract:       "#F0B400",
  physics:        T.green,
  review:         "#9B59E6",
  pr:             "#2ECC71",
  trust:          "#17A2B8",
};
const STAGE_ORDER = [
  "dispatch", "template_match", "hypothesis",
  "implement", "contract", "physics",
  "review", "pr", "trust",
];


function StatusPill({ status }) {
  const color = { start: T.ai, done: T.green, fail: T.red,
                  skip: T.text4 }[status] || T.text3;
  const glyph = { start: "◐", done: "✓", fail: "✗",
                  skip: "·" }[status] || "?";
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: "4px",
      fontSize: "10px", padding: "2px 7px", borderRadius: "4px",
      background: `${color}18`, color, fontWeight: 700,
      border: `1px solid ${color}40`, letterSpacing: "0.04em",
    }}>
      <span style={{ fontWeight: 900,
                     animation: status === "start" ? "pulse 1s infinite" : "none" }}>
        {glyph}
      </span>
      {status.toUpperCase()}
    </span>
  );
}


function EventRow({ event }) {
  const color = STAGE_COLORS[event.stage] || T.text3;
  const { stage, status, elapsed_s, ...rest } = event;
  const extraKeys = Object.keys(rest).filter(
    k => !["request_id"].includes(k));
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "auto 100px 1fr 60px",
      gap: "10px", alignItems: "center",
      padding: "7px 10px",
      borderBottom: `1px solid ${T.border}`,
      fontSize: "11px",
    }}>
      <span style={{ width: "5px", height: "18px",
                     background: color, borderRadius: "2px",
                     boxShadow: `0 0 6px ${color}60` }} />
      <span style={{ color: T.text1, fontFamily: "JetBrains Mono, monospace",
                     fontWeight: 600, fontSize: "10px",
                     letterSpacing: "0.05em", textTransform: "uppercase" }}>
        {stage}
      </span>
      <span style={{ display: "flex", gap: "6px", alignItems: "center",
                     flexWrap: "wrap" }}>
        <StatusPill status={status} />
        {extraKeys.length > 0 && (
          <span style={{ color: T.text3, fontSize: "10px",
                         fontFamily: "JetBrains Mono, monospace" }}>
            {extraKeys.map(k => `${k}=${JSON.stringify(rest[k])}`).join(" ")}
          </span>
        )}
      </span>
      <span style={{ color: T.text4, fontSize: "9px",
                     fontFamily: "JetBrains Mono, monospace",
                     textAlign: "right" }}>
        {elapsed_s.toFixed(1)}s
      </span>
    </div>
  );
}


function StagePipeline({ events }) {
  /* A compact pipeline-pill row showing each stage in STAGE_ORDER with
     its latest status. Sits above the event log. */
  const latest = {};
  for (const e of events) { latest[e.stage] = e.status; }

  return (
    <div style={{ display: "flex", gap: "6px", flexWrap: "wrap",
                  padding: "10px 0", borderBottom: `1px solid ${T.border}` }}>
      {STAGE_ORDER.map((s) => {
        const st = latest[s];
        const color = STAGE_COLORS[s];
        const bg = st === "done" ? `${color}25`
                 : st === "start" ? `${color}15`
                 : st === "fail" ? `${T.red}20`
                 : "rgba(255,255,255,0.03)";
        const fg = st ? (st === "fail" ? T.red : color) : T.text4;
        const glyph = st === "done" ? "✓" : st === "fail" ? "✗"
                    : st === "skip" ? "·" : st === "start" ? "◐" : "○";
        return (
          <div key={s} style={{
            display: "flex", alignItems: "center", gap: "5px",
            padding: "4px 9px", borderRadius: "6px",
            background: bg, color: fg,
            border: `1px solid ${st ? color + "40" : T.border}`,
            fontSize: "10px", fontWeight: 600,
            letterSpacing: "0.05em", textTransform: "uppercase",
          }}>
            <span style={{ fontWeight: 900,
                           animation: st === "start" ? "pulse 1s infinite" : "none" }}>
              {glyph}
            </span>
            {s.replace("_", " ")}
          </div>
        );
      })}
    </div>
  );
}


function LiveRunPanel() {
  const vp = useViewport();
  const S = spacing(vp);
  const [goal, setGoal] = useState(
    "NEMA17 stepper mount, 3mm aluminum plate, 4x M4 holes on 40mm square pattern, 50g target mass, must survive 10N load at 200Hz excitation");
  const [dryRun, setDryRun] = useState(true);
  const [requestId, setRequestId] = useState(null);
  const [events, setEvents] = useState([]);
  const [status, setStatus] = useState(null);  // running/done/failed/error
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const esRef = useRef(null);

  async function startRun() {
    if (!goal.trim() || status === "running") return;
    setError(null);
    setEvents([]);
    setResult(null);
    setStatus("running");
    try {
      const r = await fetch("/api/extend", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ goal, dry_run: dryRun }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = await r.json();
      setRequestId(body.request_id);
      openStream(body.request_id);
    } catch (e) {
      setError(String(e));
      setStatus("error");
    }
  }

  function openStream(id) {
    if (esRef.current) { try { esRef.current.close(); } catch {} }
    const es = new EventSource(`/api/extend/${id}/stream`);
    esRef.current = es;
    es.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data.terminal) {
          setStatus(data.status);
          setResult(data.result);
          if (data.error) setError(data.error);
          es.close();
          return;
        }
        setEvents(prev => [...prev, data]);
      } catch {}
    };
    es.onerror = () => { try { es.close(); } catch {} };
  }

  useEffect(() => {
    return () => { if (esRef.current) try { esRef.current.close(); } catch {} };
  }, []);

  return (
    <div style={{ padding: `${S.pageY} ${S.pageX}`, display: "flex",
                  flexDirection: "column", gap: "12px" }}>
      <Panel title="SELF-EXTENSION AGENT">
        <div style={{ padding: "14px 16px", display: "flex",
                      flexDirection: "column", gap: "10px" }}>
          <textarea
            value={goal} onChange={e => setGoal(e.target.value)}
            placeholder="Describe the engineering problem…"
            style={{ width: "100%", minHeight: "70px",
                     background: "rgba(0,0,0,0.3)",
                     border: `1px solid ${T.border}`, borderRadius: "8px",
                     padding: "10px 12px", color: T.text1,
                     fontSize: vp.isMobile ? "16px" : "12px",
                     fontFamily: "inherit", resize: "vertical",
                     outline: "none", lineHeight: 1.5,
                     boxSizing: "border-box" }} />
          <div style={{ display: "flex", gap: "12px", alignItems: "center" }}>
            <label style={{ display: "flex", gap: "6px", alignItems: "center",
                            color: T.text3, fontSize: "11px" }}>
              <input type="checkbox" checked={dryRun}
                     onChange={e => setDryRun(e.target.checked)} />
              Dry-run (no credits)
            </label>
            <span style={{ flex: 1 }} />
            <button onClick={startRun}
                    disabled={!goal.trim() || status === "running"}
                    style={{ padding: "9px 24px", borderRadius: "8px",
                             border: "none",
                             background: status === "running"
                               ? `${T.ai}25`
                               : `linear-gradient(135deg, ${T.ai}, ${T.brand})`,
                             color: "#fff", fontSize: "12px", fontWeight: 700,
                             cursor: status === "running"
                               ? "not-allowed" : "pointer",
                             letterSpacing: "0.06em" }}>
              {status === "running" ? "RUNNING…" : "RUN AGENT →"}
            </button>
          </div>
          {error && (
            <div style={{ padding: "8px 12px", borderRadius: "6px",
                          background: `${T.red}12`, color: T.red,
                          fontSize: "11px", fontFamily: "JetBrains Mono, monospace" }}>
              {error}
            </div>
          )}
        </div>
      </Panel>

      <Panel title={requestId ? `PIPELINE — ${requestId}` : "PIPELINE"}>
        <div style={{ padding: "10px 16px" }}>
          <StagePipeline events={events} />
          <div style={{ marginTop: "6px",
                        maxHeight: "420px", overflowY: "auto" }}>
            {events.length === 0 ? (
              <div style={{ padding: "36px 10px", textAlign: "center",
                            color: T.text4, fontSize: "11px",
                            fontStyle: "italic" }}>
                {status === "running" ? "Waiting for first event…"
                 : "Run the agent to see the pipeline."}
              </div>
            ) : (
              events.map((ev, i) => <EventRow key={i} event={ev} />)
            )}
          </div>
        </div>
      </Panel>

      {result && (
        <Panel title="RESULT">
          <div style={{ padding: "14px 16px", display: "flex",
                        flexDirection: "column", gap: "6px",
                        fontSize: "11px", fontFamily: "JetBrains Mono, monospace" }}>
            <div><span style={{ color: T.text3 }}>success:</span>{" "}
              <span style={{ color: result.success ? T.green : T.red,
                             fontWeight: 700 }}>
                {String(result.success)}
              </span>
            </div>
            <div><span style={{ color: T.text3 }}>merged_module:</span>{" "}
              <span style={{ color: T.text1 }}>{result.merged_module || "—"}</span></div>
            <div><span style={{ color: T.text3 }}>pr_url:</span>{" "}
              {result.pr_url ? (
                <a href={result.pr_url} target="_blank" rel="noreferrer"
                   style={{ color: T.ai }}>{result.pr_url}</a>
              ) : <span style={{ color: T.text4 }}>—</span>}
            </div>
            <div><span style={{ color: T.text3 }}>trust_state:</span>{" "}
              <span style={{ color: T.amber }}>{result.trust_state || "—"}</span></div>
            <div><span style={{ color: T.text3 }}>candidates_tried:</span>{" "}
              <span style={{ color: T.text1 }}>{result.candidates_tried}</span></div>
            {result.winner_metrics && Object.keys(result.winner_metrics).length > 0 && (
              <div><span style={{ color: T.text3 }}>metrics:</span>{" "}
                <span style={{ color: T.text1 }}>
                  {JSON.stringify(result.winner_metrics)}
                </span></div>
            )}
            {result.error && (
              <div style={{ color: T.red }}>error: {result.error}</div>
            )}
          </div>
        </Panel>
      )}
    </div>
  );
}


function TrustPanel() {
  const vp = useViewport();
  const S = spacing(vp);
  const [records, setRecords] = useState([]);

  useEffect(() => {
    async function load() {
      try {
        const r = await fetch("/api/extend/trust/list");
        const d = await r.json();
        setRecords(d.modules || []);
      } catch {}
    }
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, []);

  async function approve(path) {
    try {
      await fetch("/api/extend/trust/approve", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ module_path: path }),
      });
    } catch {}
  }

  return (
    <div style={{ padding: `${S.pageY} ${S.pageX}` }}>
      <Panel title="MODULE TRUST REGISTRY">
        <div style={{ padding: "12px 16px" }}>
          {records.length === 0 ? (
            <div style={{ color: T.text4, fontSize: "11px",
                          textAlign: "center", padding: "30px" }}>
              No agent-generated modules yet. Run the Agent tab first.
            </div>
          ) : records.map(r => (
            <div key={r.module_path} style={{
              display: "flex", alignItems: "center", gap: "10px",
              padding: "8px 10px", borderBottom: `1px solid ${T.border}`,
              fontSize: "11px",
            }}>
              <span style={{ flex: 1, fontFamily: "JetBrains Mono, monospace",
                             color: T.text1 }}>{r.module_path}</span>
              <span style={{
                padding: "3px 8px", borderRadius: "4px", fontSize: "10px",
                fontWeight: 700, letterSpacing: "0.04em",
                color: r.state === "trusted" ? T.green
                       : r.state === "review_required" ? T.amber : T.red,
                background: r.state === "trusted" ? `${T.green}18`
                       : r.state === "review_required" ? `${T.amber}18`
                       : `${T.red}18`,
              }}>
                {r.state.toUpperCase()}
              </span>
              <span style={{ color: T.text3, fontSize: "10px" }}>
                ✓{r.successful_runs} · ✗{r.failed_runs}
              </span>
              {r.state !== "trusted" && (
                <button onClick={() => approve(r.module_path)}
                        style={{ padding: "4px 10px", borderRadius: "5px",
                                 border: `1px solid ${T.green}60`,
                                 background: `${T.green}15`, color: T.green,
                                 fontSize: "10px", cursor: "pointer",
                                 fontWeight: 700, letterSpacing: "0.04em" }}>
                  APPROVE
                </button>
              )}
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}


function HistoryPanel() {
  const vp = useViewport();
  const S = spacing(vp);
  const [runs, setRuns] = useState([]);

  useEffect(() => {
    async function load() {
      try {
        const r = await fetch("/api/extend");
        const d = await r.json();
        setRuns(d.runs || []);
      } catch {}
    }
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  return (
    <div style={{ padding: `${S.pageY} ${S.pageX}` }}>
      <Panel title="RECENT AGENT RUNS">
        <div style={{ padding: "12px 16px" }}>
          {runs.length === 0 ? (
            <div style={{ color: T.text4, fontSize: "11px",
                          textAlign: "center", padding: "30px" }}>
              No runs yet.
            </div>
          ) : runs.map(r => (
            <div key={r.request_id} style={{
              padding: "8px 10px", borderBottom: `1px solid ${T.border}`,
              fontSize: "11px",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <span style={{
                  padding: "2px 7px", borderRadius: "4px", fontSize: "9px",
                  fontWeight: 700, letterSpacing: "0.04em",
                  color: r.success ? T.green : r.status === "running" ? T.ai : T.red,
                  background: r.success ? `${T.green}18`
                             : r.status === "running" ? `${T.ai}18`
                             : `${T.red}18`,
                }}>{r.status.toUpperCase()}</span>
                <span style={{ flex: 1, fontFamily: "JetBrains Mono, monospace",
                               fontSize: "10px", color: T.text4 }}>
                  {r.request_id}
                </span>
                <span style={{ color: T.text4, fontSize: "9px" }}>
                  {r.n_events} events
                </span>
              </div>
              <div style={{ marginTop: "4px", color: T.text2,
                            fontSize: "10px" }}>{r.goal}</div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}


export default function AgentTab({ subsection = "live" }) {
  if (subsection === "trust") return <TrustPanel />;
  if (subsection === "history") return <HistoryPanel />;
  return <LiveRunPanel />;
}
