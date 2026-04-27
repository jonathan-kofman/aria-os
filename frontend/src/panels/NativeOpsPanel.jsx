/**
 * NativeOpsPanel — exposes the recent SW addin / cross-CAD ops
 * (enrichDrawing, runFea, sheetMetalBaseFlange, surfaceLoft, materials)
 * as buttons in the in-CAD React panel.
 *
 * Works in any host the bridge supports: SolidWorks, Rhino, Fusion 360,
 * Onshape — the bridge.executeFeature(kind, params) call routes to
 * whichever host is loaded. KiCad has no React panel; its server-side
 * /op endpoint is targeted from the dashboard GUI instead.
 *
 * UI is intentionally compact (vertical button stack) so it fits the
 * narrow CAD task panes (typical width ~280-360 px on the Rhino /
 * SolidWorks side panels).
 */
import React, { useState } from "react";
import bridge from "../aria/bridge";

const COLORS = {
  bg:      "#F5F5F0",
  panel:   "#FFFFFF",
  border:  "rgba(0,0,0,0.10)",
  text:    "#1A1A18",
  muted:   "#6B6864",
  accent:  "#AE5630",
  ok:      "#2E7D32",
  err:     "#C62828",
};

const BTN = {
  width: "100%",
  textAlign: "left",
  padding: "8px 10px",
  margin: "4px 0",
  background: COLORS.panel,
  border: `1px solid ${COLORS.border}`,
  borderRadius: 4,
  fontFamily: "ui-sans-serif, system-ui, sans-serif",
  fontSize: 13,
  cursor: "pointer",
};

function statusLabel(s) {
  if (!s) return null;
  const ok = s.ok !== false && !s.error;
  return (
    <div style={{
      fontSize: 11,
      color: ok ? COLORS.ok : COLORS.err,
      marginTop: 2,
      whiteSpace: "pre-wrap",
      wordBreak: "break-word",
    }}>
      {ok ? "✓ " : "✗ "}
      {(s.summary || s.error || JSON.stringify(s)).slice(0, 240)}
    </div>
  );
}

export function NativeOpsPanel() {
  const [busy, setBusy] = useState(null);
  const [results, setResults] = useState({});

  const run = async (label, kind, params) => {
    setBusy(label);
    try {
      const r = await bridge.executeFeature(kind, params || {});
      setResults((prev) => ({
        ...prev,
        [label]: { ok: r?.ok !== false, summary: JSON.stringify(r).slice(0, 200) },
      }));
    } catch (e) {
      setResults((prev) => ({
        ...prev,
        [label]: { ok: false, error: e.message || String(e) },
      }));
    } finally {
      setBusy(null);
    }
  };

  const sectionTitle = (txt) => (
    <div style={{
      fontSize: 10,
      letterSpacing: 1.4,
      textTransform: "uppercase",
      color: COLORS.muted,
      marginTop: 14,
      marginBottom: 4,
    }}>{txt}</div>
  );

  if (!bridge.isHosted) {
    return (
      <div style={{ padding: 12, color: COLORS.muted, fontSize: 12 }}>
        Native ops are only available inside a CAD host (SolidWorks, Rhino,
        Fusion 360, or Onshape). When standalone, use the dashboard GUI at
        <code> /gui</code> instead.
      </div>
    );
  }

  return (
    <div style={{
      padding: 12,
      fontFamily: "ui-sans-serif, system-ui, sans-serif",
      color: COLORS.text,
      fontSize: 13,
    }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        Native ops · <span style={{ color: COLORS.muted, fontWeight: 400 }}>{bridge.kind}</span>
      </div>
      <div style={{ fontSize: 11, color: COLORS.muted, marginBottom: 8 }}>
        Each button issues a single op against the active CAD doc. Status appears below the button.
      </div>

      {sectionTitle("Drawing enrichment")}
      <button style={BTN} disabled={busy !== null}
        onClick={() => run("enrichDrawing",
          "enrichDrawing",
          { gdt: true, section_view: true, exploded_view: true })}>
        {busy === "enrichDrawing" ? "running…" : "↳ enrichDrawing (GD&T + section + exploded)"}
      </button>
      {statusLabel(results.enrichDrawing)}

      <button style={BTN} disabled={busy !== null}
        onClick={() => run("createDrawing",
          "createDrawing",
          { sheet_size: "A3", add_bom: true })}>
        {busy === "createDrawing" ? "running…" : "↳ createDrawing (active doc → .slddrw)"}
      </button>
      {statusLabel(results.createDrawing)}

      {sectionTitle("Simulation")}
      <button style={BTN} disabled={busy !== null}
        onClick={() => run("runFea",
          "runFea",
          {
            iterations: [
              { name: "baseline", load_n: 50, thickness_mm: 3, span_mm: 200, width_mm: 50, e_gpa: 69 },
              { name: "thicker",  load_n: 50, thickness_mm: 5, span_mm: 200, width_mm: 50, e_gpa: 69 },
              { name: "shorter",  load_n: 50, thickness_mm: 3, span_mm: 100, width_mm: 50, e_gpa: 69 },
            ],
            target_max_stress_mpa: 200,
          })}>
        {busy === "runFea" ? "running…" : "↳ runFea (3-iter cantilever sweep)"}
      </button>
      {statusLabel(results.runFea)}

      {sectionTitle("Sheet metal & surface")}
      <button style={BTN} disabled={busy !== null}
        onClick={() => run("sheetMetalBaseFlange",
          "sheetMetalBaseFlange",
          { thickness_mm: 1.5, bend_radius_mm: 1.0, k_factor: 0.5 })}>
        {busy === "sheetMetalBaseFlange" ? "running…" : "↳ sheetMetalBaseFlange (1.5mm, k=0.5)"}
      </button>
      {statusLabel(results.sheetMetalBaseFlange)}

      <button style={BTN} disabled={busy !== null}
        onClick={() => run("surfaceLoft",
          "surfaceLoft",
          { profile_sketches: ["Sketch1", "Sketch2"] })}>
        {busy === "surfaceLoft" ? "running…" : "↳ surfaceLoft (Sketch1 → Sketch2)"}
      </button>
      {statusLabel(results.surfaceLoft)}

      {sectionTitle("Materials")}
      <button style={BTN} disabled={busy !== null}
        onClick={() => run("setMaterial",
          "setMaterial",
          { name: "6061 Alloy", database: "SOLIDWORKS Materials" })}>
        {busy === "setMaterial" ? "running…" : "↳ set 6061 Alloy on active part"}
      </button>
      {statusLabel(results.setMaterial)}

      <button style={BTN} disabled={busy !== null}
        onClick={() => run("setMaterial",
          "setMaterial",
          { name: "ABS PC", database: "SOLIDWORKS Materials" })}>
        {busy === "setMaterial" ? "running…" : "↳ set ABS PC (PCB stand-in)"}
      </button>

      {sectionTitle("View")}
      <button style={BTN} disabled={busy !== null}
        onClick={() => run("setView", "setView", { view: "*Isometric" })}>
        {busy === "setView" ? "…" : "↳ Isometric view"}
      </button>
      <button style={BTN} disabled={busy !== null}
        onClick={() => run("zoomToFit", "zoomToFit", {})}>
        {busy === "zoomToFit" ? "…" : "↳ Zoom to fit"}
      </button>
    </div>
  );
}

export default NativeOpsPanel;
