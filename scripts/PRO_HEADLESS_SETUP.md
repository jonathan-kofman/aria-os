# Pro-Quality Headless Setup

Everything in ariaOS's `build_pipeline` runs headless — no GUI apps required — once the OSS tool stack below is installed. Each stage skips gracefully if its tool is missing, so you can install them incrementally and watch pipeline stages light up one by one.

## What each install unlocks

| Stage | Tool | Quality gain |
|---|---|---|
| `drc` | `kicad-cli` (KiCad 8+) | PCBs validated against real fab rules. Catches clearance, trace-width, unconnected-pin violations before gerbers ship. |
| `autoroute` | Freerouting JAR + Java 17+ | Replaces naive star-routing with a real negotiated-congestion router. Produces routable boards at higher density. |
| `fea` | gmsh (✅ auto-installed) + CalculiX | Static-linear stress analysis per metal part. Asserts safety factor ≥ 2 against yield. Catches under-designed brackets before you machine them. |
| `cam_headless` | FreeCAD 1.0+ | `freecadcmd`-based toolpath generation (replaces Fusion 360 scripts — no more app-dependency). |
| `nc_sim` | CAMotics 1.2+ | Simulates generated G-code against STL stock; catches rapids-into-material, wrong Z heights, broken tools. |
| `drawings_mbd` | FreeCAD TechDraw (bundled with FreeCAD) | Real GD&T annotations on drawings, not just projection SVGs. |

## One-shot install commands (Windows, user-scope)

```powershell
# KiCad 8+ → enables drc + erc + pcb/sch export
winget install --id KiCad.KiCad --source winget

# FreeCAD 1.0+ → enables headless CAM + MBD drawings
winget install --id FreeCAD.FreeCAD --source winget

# Java 21 LTS → required for Freerouting
winget install --id EclipseAdoptium.Temurin.21.JDK --source winget

# CalculiX → static-linear FEA solver
winget install --id bConverged.CalculiX --source winget
# If not in winget: download http://www.dhondt.de/ccx_2.22.win64.zip
# unzip to C:\CalculiX\, then add C:\CalculiX\bin\ to PATH

# CAMotics → G-code simulator / collision checker
winget install --id CAMotics.CAMotics --source winget
```

## Freerouting JAR (manual, no winget yet)

```bash
# Windows (Git Bash):
mkdir -p /c/Users/$USER/Downloads/workspace/aria-os-export/.tools
curl -L \
  https://github.com/freerouting/freerouting/releases/latest/download/freerouting-2.1.0-executable.jar \
  -o /c/Users/$USER/Downloads/workspace/aria-os-export/.tools/freerouting.jar
```

Or set the env var if you store it elsewhere:

```powershell
setx ARIA_FREEROUTING_JAR "D:\tools\freerouting-2.1.0-executable.jar"
```

## Verify

After installs, restart your shell so PATH picks up the new binaries. Then:

```bash
cd aria-os-export
python -c "
from aria_os.ecad.drc_check import _find_kicad_cli
from aria_os.ecad.autoroute import _find_freerouting_jar, _find_java
from aria_os.fea.calculix_stage import _find_ccx, _have_gmsh
print('kicad-cli:     ', _find_kicad_cli() or 'MISSING')
print('freerouting:   ', _find_freerouting_jar() or 'MISSING')
print('java:          ', _find_java() or 'MISSING')
print('ccx:           ', _find_ccx() or 'MISSING')
print('gmsh (python): ', _have_gmsh())
"
```

Once everything prints a path (not MISSING), run a preset build and the `drc`, `autoroute`, and `fea` stages will light up instead of skipping.

## Python-only deps (auto-installed in the env)

```
gmsh     # FEA mesh generator
meshio   # mesh format conversion
```

Both were installed by the setup step. Upgrade with:

```bash
pip install --upgrade gmsh meshio
```

## What you still can't do headless

- **Altium Situs-grade autorouting** — Freerouting is ~70% as good; enough for consumer/hobby/industrial, not enterprise.
- **HFSS-grade signal integrity** — no open tool does 10+ GHz; not relevant for drones/robotics.
- **Nonlinear FEA at Ansys quality** — CalculiX does linear static well; nonlinear is rougher.
- **Parasolid boolean robustness** — OCCT (CadQuery's kernel) has known ceilings on shelling thin walls and complex unions. Real but uncommon in target verticals.

Everything else — routing, DRC, ERC, static FEA, CAM, NC simulation, GD&T MBD — is fully achievable with this stack.
