# ARIA-OS

**Natural-language engineering inside your CAD tool — Fusion 360, Rhino, Onshape, SolidWorks, KiCad.**

Type *"flange 100mm OD, 4 M6 holes on 80mm PCD, 6mm thick, 6061 aluminum"* → the part appears in your CAD's real feature tree, editable, parametric, with ISO-compliant engineering defaults applied automatically.

ARIA-OS wires an LLM-powered engineering agent pipeline into the native APIs of every major CAD, so you describe parts in English (or voice) and get real, editable geometry — not imported meshes, not one-shot STL files, but **live feature history** you can tweak parametrically.

```
"impeller 120mm OD, 6 backward-curved blades, 20mm bore"
    ↓  SpecAgent (regex + LLM enrichment)
    ↓  DeltaDetector (new vs modify vs extend)
    ↓  Planner (hardcoded fast-path OR LLM with engineering prompt)
    ↓  Validator (30+ semantic checks — catches impossible geometry)
    ↓  Stream ops → Fusion / Rhino / Onshape / SolidWorks feature tree
    ↓  EvalAgent (visual verify via Gemini/Claude vision) + DFM gate
    ↓  RefinerAgent (autonomous fix loop on FAIL)
    ↓  Post-creation actions: Drawing, DFM, Quote, CAM, FEA, Gerbers, BOM
```

---

## Quick start (~10 minutes)

### 1. Clone + install

```bash
git clone https://github.com/<you>/aria-os-export.git
cd aria-os-export
pip install -r requirements_aria_os.txt

# Frontend (the panel UI):
cd frontend
npm install
cd ..
```

### 2. Configure API keys

Create `.env` in the repo root:

```bash
# At least one of these (LLM for planner + clarifier):
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=AI...
GROQ_API_KEY=gsk_...

# For voice (mic button → Whisper transcription):
GROQ_API_KEY=gsk_...

# Optional: local LLM for fast tier
OLLAMA_HOST=http://localhost:11434
```

### 3. Start the backend + frontend dev server

Two terminals:

```bash
# Terminal 1 — backend (port 8001)
export ARIA_LAZY_STAGES=1
python -m uvicorn dashboard.aria_server:app --host 0.0.0.0 --port 8001

# Terminal 2 — panel dev server (port 5173)
cd frontend
npm run dev
```

### 4. Install the CAD add-in for your tool

Follow the section matching your CAD below.

---

## CAD plugin install

### Fusion 360 (most complete — all features work)

**Prereqs:** Fusion 360 installed (any active license).

```bash
# Copy the add-in to Fusion's AddIns directory
copy cad-plugins\fusion360\aria_panel\*.* ^
  "%AppData%\Autodesk\Autodesk Fusion 360\API\AddIns\aria_panel\"
```

In Fusion: **Tools → Scripts and Add-Ins → Add-Ins tab → ARIA Generate → Run** (tick *Run on Startup*).

The panel appears under **Solid → Create → ARIA Generate**.

**What works:** voice input (mic), prompt-to-part, modify/extend prompts, user parameters, motion studies, generative design launch, sheet metal mode, drawings, post-creation DFM/Quote/CAM/FEA actions.

---

### Rhino 8 (~10 min)

**Prereqs:**
- Rhino 8 installed
- .NET 7 SDK: https://dotnet.microsoft.com/download/dotnet/7.0

```bash
# Build the .rhp plugin
cd cad-plugins\rhino\AriaPanel
dotnet build -c Release

# Copy into Rhino's plugin folder
copy bin\Release\net7.0-windows\AriaPanel.rhp ^
  "%AppData%\McNeel\Rhinoceros\packages\8.0\"
```

In Rhino 8: **Tools → Options → Plug-ins** → tick **AriaPanel** → OK. Type `ARIA` in the command line to open the panel.

**What works:** prompt-to-part (sketches + extrudes on world planes), Make2D drawings, NURBS surfacing ops, file-format conversion via Rhino's importers/exporters. Feature-tree equivalent is the Layers panel (ARIA creates `ARIA::Sketches`, `ARIA::Bodies`, `ARIA::Patterns` layers).

---

### Onshape (~15 min — cloud)

**Prereqs:**
- Onshape account (free tier works): https://cad.onshape.com
- Onshape Developer Portal account: https://dev-portal.onshape.com

#### Option A: Direct API access (no OAuth, simplest)

1. **Dev Portal → API keys → New key** → copy both the access key and the secret key.
2. Add to `.env`:
   ```
   ONSHAPE_ACCESS_KEY=...
   ONSHAPE_SECRET_KEY=...
   ```
3. ARIA's backend now uses these to POST feature operations directly to any Onshape document you specify.

This path doesn't embed the panel inside Onshape's UI — you use ARIA's standalone panel and it talks to Onshape over REST.

#### Option B: Embedded Onshape tab (with OAuth)

1. **Dev Portal → OAuth applications → Create application**
   - Redirect URL: `https://<your-public-host>/onshape/callback`
   - Permissions: `OAuth2Read`, `OAuth2Write`, `OAuth2ReadPII`
2. Host `cad-plugins/onshape/aria-connector/` over HTTPS (use ngrok for dev):
   ```bash
   npx ngrok http 5174
   cd cad-plugins/onshape/aria-connector
   npx http-server -p 5174 --cors
   ```
3. In Onshape: **Document → + → Application** → paste the ngrok URL.

**What works:** feature tree streaming via REST, FeatureScript emission, configurations, branches.

---

### SolidWorks (scaffold — install when you have SW)

**Prereqs (for future build):**
- SolidWorks 2022+ installed
- Visual Studio 2022 with .NET desktop workload
- SolidWorks SDK (from your SW install)

The scaffold at `cad-plugins/solidworks/AriaSW/` implements the bridge contract. To build + register:

```bash
cd cad-plugins\solidworks\AriaSW
dotnet build -c Release
regasm /codebase bin\Release\AriaSW.dll
```

In SolidWorks: **Tools → Add-Ins → tick ARIA**.

**Status:** handler scaffolding is in place; SW-specific feature methods (`Toolbox`, `Weldments`, `DimXpert`, `eDrawings`, Sheet Metal) are declared but marked `todo` — they need filling out once you can test against a running SW instance.

---

### KiCad (for PCB work)

**Prereqs:** KiCad 7+ installed. No plugin to install — ARIA generates `.kicad_pcb` files that you open directly in KiCad.

Set `quality_tier=balanced` in the panel and prompt something like *"3.3V regulator board with USB-C, 30x20mm 2-layer"*. ARIA generates the PCB server-side via `pcbnew`, auto-routes through Freerouting, and emits a zip with gerbers + drill files ready for fab.

**Optional deps for full ECAD:**
- Freerouting JAR at `~/.tools/freerouting.jar` — enables `routeBoard` op
- `kicad-cli` on PATH — fallback gerber export

---

## Using the panel

### Prompting

- **Specific parts:** *"flange 100mm OD, 4 M6 holes on 80mm PCD, 6mm thick, 6061 aluminum"* → runs instantly (hardcoded planner)
- **Vague parts:** *"mount for a NEMA 17 stepper"* → LLM asks 1-3 clarifying questions, then generates
- **Modify existing:** *"make it thicker"* / *"set OD to 120mm"* → updates the matching User Parameter in Fusion, tree rebuilds
- **Extend existing:** *"add 4 relief pockets"* → LLM plans new features on top of the current part

### Voice input

Click the **mic** button, speak your prompt, click again to stop. Transcribed via Groq Whisper (large-v3-turbo) — handles engineering vocabulary (flange, PCD, 6061, M6) cleanly.

### Attachments

Drop files on the **paperclip**:
- `.png/.jpg/.webp` → image-to-CAD (vision LLM reads the image)
- `.stl/.ply/.obj/.step` → scan-to-CAD (mesh reconstruction)
- `.wav/.m4a/.mp3` → transcribed into the prompt box

### Mode selection

Default is **Auto** — backend detects domain from the prompt (mechanical part / PCB / drawing / assembly / sheet metal). Override via the small dropdown next to the model selector.

### Post-creation actions

After a part lands, click any chip on the artifact card:
- 📐 **Drawing** — multi-view engineering drawing with GD&T
- 🧪 **DFM** — manufacturability review (wall thickness, undercuts, tolerances)
- 💵 **Quote** — cost estimate + cycle time
- ⚙ **CAM** — G-code toolpaths (Fusion CAM script)
- 🔬 **FEA** — static stress analysis (CalculiX)
- 📤 **Gerbers** / 📋 **BOM** / 🔍 **DRC** (PCB artifacts)

---

## Engineering knowledge

ARIA applies ISO/ASTM/ASME standards automatically — *you don't have to tell it to*. See `aria_os/engineering/` for the canonical tables:

| Module | What it encodes |
|---|---|
| `iso_273.py` | Clearance + tap drill + counterbore sizes (M1.6 → M24) |
| `astm_mat.py` | Material grades with yield/UTS/density/machinability |
| `iso_2768.py` | General tolerances (fine/medium/coarse) by size |
| `iso_1302.py` | Surface-finish Ra values by process + feature |
| `iso_1101.py` | GD&T symbols + per-feature callout rules |

All of it flows into the LLM planner's system prompt so **arbitrary prompts** produce ISO-compliant output — flanges, brackets, housings, gears, whatever.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  PANEL (React)                                               │
│  ↕ bridge.executeFeature(op) ↔ host-CAD Python/C#/JS         │
└────────────────────┬─────────────────────────────────────────┘
                     │ HTTP + SSE
┌────────────────────▼─────────────────────────────────────────┐
│  ARIA BACKEND (FastAPI, port 8001)                           │
│  ├─ /api/generate       — pipeline entrypoint                │
│  ├─ /api/clarify        — missing-field detector             │
│  ├─ /api/native_eval    — visual-verify + DFM gate           │
│  ├─ /api/stt/transcribe — voice → Groq Whisper               │
│  └─ /api/artifact_action — DFM, Quote, CAM, FEA, Gerbers     │
└────────────────────┬─────────────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────────────┐
│  AGENT PIPELINE                                              │
│  Auto-detect mode → DeltaDetector → SpecAgent (regex + LLM)  │
│  → Planner (hardcoded fast-path OR LLM with engineering KB)  │
│  → Validator (structural + semantic)                         │
│  → Stream `native_op` events (panel dispatches per-op)       │
│  → EvalAgent (visual verify) + DFMAgent (manufacturability)  │
│  → RefinerAgent (auto-fix loop on FAIL, max 3 iterations)    │
└──────────────────────────────────────────────────────────────┘
```

---

## Project layout

```
aria-os-export/
├── aria_os/
│   ├── engineering/              # ISO/ASTM/ASME knowledge library
│   ├── agents/                   # SpecAgent, DFMAgent, QuoteAgent, ...
│   ├── native_planner/           # Dispatcher + planners + validator
│   ├── generators/               # CadQuery / Rhino Compute / SDF / Zoo
│   ├── ecad/                     # KiCad executor, gerber, BOM, DRC
│   ├── drawings/                 # FreeCAD TechDraw generator + auto-dim
│   ├── cam/                      # Fusion-compatible toolpath scripts
│   ├── fea/                      # CalculiX runner
│   └── llm_client.py             # Provider chain (Anthropic / Gemini / Ollama)
├── cad-plugins/
│   ├── fusion360/aria_panel/     # Python add-in (full features)
│   ├── rhino/AriaPanel/          # C# plugin + .csproj
│   ├── onshape/aria-connector/   # Browser-hosted JS connector
│   └── solidworks/AriaSW/        # C# scaffold (not yet built)
├── frontend/                     # React panel (Vite)
├── dashboard/aria_server.py      # FastAPI backend
├── tests/                        # 28 tests across planner + validator + detector
└── run_aria_os.py                # CLI entry point (alternative to panel)
```

---

## CLI alternative (no panel needed)

If you just want part files without a CAD tool open:

```bash
# Single part
python run_aria_os.py "flange 100mm OD, 4 M6 holes on 80mm PCD, 6mm thick"

# Full pipeline (CAD + FEA + DFM + Quote + CAM + Drawing)
python run_aria_os.py --full "ARIA cam collar, 80mm OD, 60mm bore, 45mm thick"

# Image-to-CAD
python run_aria_os.py --image part_sketch.jpg

# Scan-to-CAD
python run_aria_os.py --scan scan.stl
```

Output lands in `outputs/cad/step/` + `outputs/cad/stl/`.

---

## Troubleshooting

### Panel doesn't load in Fusion
Restart the add-in: **Tools → Scripts and Add-Ins → Add-Ins → ARIA Generate → Stop → Run**. The cache-buster query param in the URL forces a fresh bundle each time the add-in starts.

### "navigator.mediaDevices not available" on mic click
Expected in Fusion's WebView2. Click the mic anyway — ARIA falls back to a Python-side recorder via winmm + Groq Whisper. If the button still does nothing, fully stop/restart the add-in (Python module cache).

### Pipeline hangs for 30+ seconds before streaming
The LLM classifier is slow when the prompt doesn't match a hardcoded keyword. Add specific dimensions (`80x60x40mm`) and standard part names (`flange`, `bracket`, `impeller`) to hit the instant path.

### Exported STL is 84 bytes / EvalAgent returns 422
Geometry was consumed by a cut larger than the body. Check that cut dimensions are smaller than body dimensions (e.g. `bore < OD`).

### LLM planner returns invalid JSON
Tier escalation kicks in automatically (fast → balanced → premium). If all three fail, check `/tmp/aria_server.log` for the last LLM response — usually a provider outage or credit issue.

---

## Running tests

```bash
pytest tests/test_native_planner_pipeline.py -v
```

28 tests covering the validator, delta detector, modify-plan emitter, LLM planner JSON extraction, auto-mode routing, and auto-dimensioner.

---

## License

MIT — see [LICENSE](LICENSE).
