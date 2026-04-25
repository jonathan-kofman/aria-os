# W9 — ARIA-VR acceptance demos

Three short, recordable demos that prove the end-to-end VR loop
works. Each demo lasts ≤90s and answers a different "is this real?"
skeptic question.

## Prerequisites

- Quest 3 (or 3S/Pro) with Developer Mode on
- Quest + Windows host on the same Wi-Fi network
- ARIA-OS dashboard running: `python -m uvicorn dashboard.aria_server:app --host 0.0.0.0 --port 8001`
- aria-vr dev server running: `cd aria-vr && npm run dev`
  (Vite serves on port 5173 with HTTPS via mkcert)
- mkcert root cert installed in Meta Browser one-time
  (sideload via the Meta Quest Developer Hub or accept the cert
  prompt the first time you connect)

## Demo 1 — Voice-design a flange in 60s (model-sync round-trip)

**Question answered:** does the voice path actually generate new
geometry and reload it in-headset?

1. **Setup** (10s)
   - Quest 3 browser → `https://<pc-ip>:5173/`
   - aria-vr's WebSocket auto-connects to `wss://<pc-ip>:8001/ws/model_updates`
   - Empty scene + ground grid + controllers visible
2. **Voice command** (15s)
   - Hold Left A button. Speak: *"Generate a 100mm OD flange,
     6mm thick, 4 M6 bolt holes on 80mm PCD."*
   - Release A — the WAV uploads to `POST /api/voice_plan`
3. **Backend round-trip** (~20s)
   - Server: STT → planner.make_plan → executor exports `part.glb`
     → `broadcast_model_update("/outputs/runs/<id>/part.glb")`
4. **In-headset reload** (instant)
   - aria-vr's `model_sync.js` swaps the scene root with the new glTF
   - User walks around the flange at 1:1 scale
5. **Acceptance:** the loaded model has 4 holes on a circular pattern
   matching the spoken spec; no manual file transfer happens.

## Demo 2 — Walk through 1:1 aerospace bracket lattice

**Question answered:** can ARIA's W3 SDF lattice + W7 verifyPart
output actually be inspected in VR with the user moving through it?

1. **Setup** (5s)
   - From the dashboard (browser): generate
     *"Aerospace bracket 100×60×20mm with gyroid lattice infill at
     60% density, mounting holes preserved"*.
   - The pipeline emits `part.glb` and broadcasts to the VR client.
2. **In-headset** (60s)
   - Bracket appears at 1:1.
   - Use thumbstick teleport (locomotion.js) to walk INSIDE the
     lattice. The TPMS surface is visible from inside.
   - Right-trigger on lattice strut → measurement point. Place a
     second point on the next strut → distance label appears.
3. **Save** (5s)
   - ESC clears measurement. POST `/api/measurements/save` fires
     with the measurement points.
4. **Acceptance:** the lattice geometry is faithfully rendered (60%
   solid, not just an empty shell), the user can teleport within
   the volume without clipping, and the saved measurements
   round-trip into `outputs/vr/<run_id>/measurements.json`.

## Demo 3 — AR-overlay CAD on real stock to verify clearances

**Question answered:** can the user check fit against real-world
material before cutting?

1. **Setup** (10s)
   - Hold a piece of stock — e.g. 2"×1" × 12" extrusion — on a
     workbench.
   - In headset, switch to AR mode (button mapping: B button on
     right controller). aria-vr requests `immersive-ar` session.
2. **Anchor** (10s)
   - Aim controller at the workbench surface; trigger places the
     hit-test anchor.
   - The CAD model (e.g. a bracket designed for that extrusion)
     spawns at 1:1 scale at the anchor point.
3. **Verify clearance** (30s)
   - Walk around the workbench. The bracket sits on the real
     stock — passthrough shows real wood/aluminum, the model floats
     on top.
   - Right-trigger places a measurement endpoint on the real
     stock's edge. Second point on the model's mounting hole.
   - The distance label confirms the hole lines up with the stock's
     edge to within ±N mm.
4. **Acceptance:** model + reality align within visual tolerance,
   the measurement tool reads correctly, and the user can
   verify a clearance issue (e.g. "the M6 bolt won't clear my
   T-track") BEFORE machining.

## Failure modes worth recording

These are the demo "bugs" that genuinely happen in the field — show
them once, the audience trusts the rest of the demo more:

- **Network drop mid-record:** WebSocket reconnects with exponential
  backoff. Demo: kill Wi-Fi, see "ARIA disconnected" pill, restore,
  see "ARIA connected" + the queued model arrives.
- **STT misheard a number:** STT picks up "5mm" as "50mm". The
  resolved goal is shown on a billboard for 3 seconds before
  generation starts — user can A-button-cancel during that window.
- **Lattice mesh too dense for free-tier render:** the `_analyze_mesh`
  geometric audit reports >2M tris; client clamps to subdivision-
  reduced version. Demo: dense lattice generates, decimation kicks in,
  framerate stays at 90Hz.

## Recording checklist

For each demo, capture:
- Quest screen recording (in-headset POV, 1080p ≥)
- A side phone shot of the user gesturing (so viewers see voice/AR isn't
  happening at a screen)
- The dashboard log stream split-screen (so viewers see the planner
  + verifyPart run in real-time)

Total recorded length per demo: 90s. Total deliverable: ~5 minutes
of footage that closes the "does this actually work?" loop.
