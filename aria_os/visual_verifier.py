"""
aria_os/visual_verifier.py

Visual verification of generated CAD parts using vision AI.

Renders 3 views of the STL (top, front, isometric) via matplotlib (headless),
then sends them to a vision LLM with a feature checklist derived from the
goal string and spec dict.  Returns a structured verification result.

Priority: Gemini 2.5 Flash (fast/cheap) -> Anthropic Claude (fallback) -> skip.

Dependencies: trimesh, matplotlib (both already in requirements_aria_os.txt).
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Rendering helpers (matplotlib Agg — works headless on Windows)
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Convert goal text to a filesystem-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:60] if slug else "part"


def _render_views(stl_path: str, goal: str, out_dir: Path) -> list[str]:
    """Render top, front, and isometric PNGs from an STL file.

    Returns list of PNG file paths.
    """
    import trimesh
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: F401

    mesh = trimesh.load(stl_path)
    if hasattr(mesh, "geometry"):
        # Scene — concatenate all meshes
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

    slug = _slugify(goal)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Cap face count for rendering performance
    max_faces_2d = 6000
    max_faces_3d = 3000
    face_idx_2d = list(range(min(len(mesh.faces), max_faces_2d)))
    face_idx_3d = list(range(min(len(mesh.faces), max_faces_3d)))

    short_goal = goal[:50]
    paths: list[str] = []

    # --- Top view (XY plane) ------------------------------------------------
    top_path = str(out_dir / f"verify_{slug}_top.png")
    fig, ax = plt.subplots(figsize=(6, 6))
    verts = [mesh.vertices[mesh.faces[i]][:, :2] for i in face_idx_2d]
    ax.add_collection(
        PolyCollection(
            verts,
            edgecolor="gray",
            facecolor=[0.75, 0.82, 0.92],
            linewidth=0.1,
        )
    )
    ax.autoscale_view()
    ax.set_aspect("equal")
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")
    ax.set_title(f"Top View (XY) \u2014 {short_goal}")
    plt.savefig(top_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    paths.append(top_path)

    # --- Front view (XZ plane) ----------------------------------------------
    front_path = str(out_dir / f"verify_{slug}_front.png")
    fig, ax = plt.subplots(figsize=(6, 6))
    verts = [mesh.vertices[mesh.faces[i]][:, [0, 2]] for i in face_idx_2d]
    ax.add_collection(
        PolyCollection(
            verts,
            edgecolor="gray",
            facecolor=[0.75, 0.82, 0.92],
            linewidth=0.1,
        )
    )
    ax.autoscale_view()
    ax.set_aspect("equal")
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Z (mm)")
    ax.set_title(f"Front View (XZ) \u2014 {short_goal}")
    plt.savefig(front_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    paths.append(front_path)

    # --- Isometric (3D) -----------------------------------------------------
    iso_path = str(out_dir / f"verify_{slug}_iso.png")
    fig = plt.figure(figsize=(8, 6))
    ax3 = fig.add_subplot(111, projection="3d")
    polys = mesh.vertices[mesh.faces[face_idx_3d]]
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection as P3D
    ax3.add_collection3d(
        P3D(
            polys,
            alpha=0.7,
            edgecolor="gray",
            facecolor=[0.7, 0.8, 0.9],
            linewidth=0.1,
        )
    )
    # Auto-scale the 3D axes
    all_pts = mesh.vertices
    x_min, x_max = all_pts[:, 0].min(), all_pts[:, 0].max()
    y_min, y_max = all_pts[:, 1].min(), all_pts[:, 1].max()
    z_min, z_max = all_pts[:, 2].min(), all_pts[:, 2].max()
    ax3.set_xlim(x_min, x_max)
    ax3.set_ylim(y_min, y_max)
    ax3.set_zlim(z_min, z_max)
    ax3.set_xlabel("X")
    ax3.set_ylabel("Y")
    ax3.set_zlabel("Z")
    ax3.view_init(elev=25, azim=45)
    ax3.set_title(f"Isometric \u2014 {short_goal}")
    plt.savefig(iso_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    paths.append(iso_path)

    return paths


# ---------------------------------------------------------------------------
# Feature checklist builder
# ---------------------------------------------------------------------------

# keyword -> (description pattern, view hint)
_FEATURE_KEYWORDS: list[tuple[str, str, str]] = [
    ("bore", "large center hole / bore opening visible", "top view"),
    ("hole", "circular hole(s) visible", "top view"),
    ("fin", "parallel fin-like protrusions visible", "front/side view"),
    ("heat sink", "heat-sink body with parallel fins visible", "front/side view"),
    ("l-bracket", "L-shaped profile visible (two plates at ~90 degrees)", "front view"),
    ("l bracket", "L-shaped profile visible (two plates at ~90 degrees)", "front view"),
    ("angle bracket", "angled profile visible", "front view"),
    ("shell", "visible wall thickness indicating hollow/shell body", "isometric view"),
    ("hollow", "visible wall thickness indicating hollow interior", "isometric view"),
    ("sweep", "curved swept profile visible", "isometric view"),
    ("curve", "curved geometry visible", "isometric view"),
    ("bend", "bent/curved profile visible", "front view"),
    ("thread", "surface texture or helical thread pattern visible", "isometric view"),
    ("knurl", "knurled surface texture visible", "isometric view"),
    ("slot", "rectangular slot or cutout visible", "top view"),
    ("groove", "groove/channel cut into surface visible", "front view"),
    ("prong", "protruding prong/tab features visible", "isometric view"),
    ("clip", "clip/snap feature visible", "isometric view"),
    ("tab", "protruding tab feature visible", "isometric view"),
    ("flange", "flange rim/lip visible around body", "front view"),
    ("rib", "reinforcing rib(s) visible", "isometric view"),
    ("chamfer", "chamfered edge(s) visible", "isometric view"),
    ("fillet", "rounded fillet edge(s) visible", "isometric view"),
    ("gear", "gear teeth visible around circumference", "top view"),
    ("teeth", "tooth features visible", "top view"),
    ("ratchet", "asymmetric ratchet teeth visible", "top view"),
    ("keyway", "keyway slot visible in bore/shaft", "top view"),
    ("spline", "spline features visible", "top view"),
    ("mount", "mounting features (holes/tabs/flanges) visible", "isometric view"),
    ("nozzle", "convergent-divergent nozzle profile visible", "front view"),
    ("impeller", "curved vane/blade features visible", "top view"),
    ("blade", "blade/airfoil profile visible", "front view"),
    ("vane", "vane features visible", "top view"),
]


def _build_checklist(goal: str, spec: dict) -> list[str]:
    """Build a visual feature checklist from goal text and extracted spec."""
    checks: list[str] = []
    goal_lower = goal.lower()

    # Keyword-based checks
    for keyword, description, view in _FEATURE_KEYWORDS:
        if keyword in goal_lower:
            checks.append(f"{description} (check {view})")

    # Repeated feature pattern: "4x holes", "8 fins", "6 holes", "3 prongs"
    # Requires the count to be directly followed by the feature (with optional x/X)
    # Excludes patterns like "M3" where the digit is part of a metric size
    nx_pattern = re.findall(r"(?<![mM])(\d+)\s*[xX×]\s*(hole|fin|bolt|prong|tab|slot|groove|rib|blade|vane|teeth|tooth|spoke|arm|leg|pin|screw)s?", goal_lower)
    # Also match "N features" without x separator: "8 fins", "24 teeth"
    # Allow optional adjectives between count and feature: "8 parallel fins"
    # Exclude metric sizes: "M5" → skip (the (?<![mM]) prevents "M5 bolt" matching)
    nx_pattern += re.findall(r"(?<![mM])(\d+)\s+(?:\w+\s+)?(hole|fin|bolt|prong|tab|slot|groove|rib|blade|vane|teeth|tooth|spoke|arm|leg|pin|screw)s?", goal_lower)
    seen_nx: set[str] = set()
    # Skip regex bolt/teeth counts when spec has exact values (spec is authoritative)
    _spec_overrides = set()
    if spec.get("n_bolts"):
        _spec_overrides.add("bolt")
        _spec_overrides.add("hole")
    if spec.get("n_teeth"):
        _spec_overrides.add("teeth")
        _spec_overrides.add("tooth")

    for count, feature in nx_pattern:
        if feature in _spec_overrides:
            continue  # spec has the correct count, skip regex guess
        key = f"{count}_{feature}"
        if key not in seen_nx:
            seen_nx.add(key)
            checks.append(f"{count} distinct {feature} features visible")

    # Spec-driven checks (authoritative counts)
    if spec.get("n_teeth"):
        checks.append(f"approximately {spec['n_teeth']} teeth visible around circumference (top view)")
    if spec.get("n_bolts"):
        checks.append(f"{spec['n_bolts']} bolt holes visible in a circular pattern (top view)")
    if spec.get("bore_mm") or spec.get("id_mm"):
        bore = spec.get("bore_mm") or spec.get("id_mm")
        checks.append(f"center bore (~{bore}mm) visible as a large circular opening (top view)")
    if spec.get("wall_mm"):
        checks.append(f"wall thickness visible (part should appear hollow/shelled, not solid)")
    if spec.get("od_mm") and spec.get("bore_mm"):
        checks.append("part appears as a ring/annular shape (top view)")

    # Angle check
    angle_match = re.search(r"(\d+)\s*degrees?", goal_lower)
    if angle_match:
        checks.append(f"angled surface at approximately {angle_match.group(1)} degrees visible")

    # If no checks found, add generic shape checks
    if not checks:
        checks.append("overall shape appears reasonable for the described part")
        checks.append("no obvious defects (missing features, floating geometry, zero-thickness walls)")

    return checks


# ---------------------------------------------------------------------------
# Vision API call
# ---------------------------------------------------------------------------

def _encode_image(path: str) -> str:
    """Read an image file and return its base64-encoded content."""
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("ascii")


def _build_vision_prompt(goal: str, checks: list[str]) -> str:
    """Build the vision verification prompt (shared across backends)."""
    checklist_text = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(checks))
    view_labels = ["Top view (XY projection)", "Front view (XZ projection)", "Isometric (3D)"]

    return (
        f"You are verifying a CAD model. The user asked for: \"{goal}\"\n\n"
        f"Here are 3 rendered views of the generated part.\n"
        f"Image 1: {view_labels[0]}\n"
        f"Image 2: {view_labels[1]}\n"
        f"Image 3: {view_labels[2]}\n\n"
        f"Check each of the following features:\n{checklist_text}\n\n"
        f"For each check, determine PASS or FAIL based on what you see in the images.\n"
        f"Also note any obvious defects: missing geometry, floating parts, "
        f"zero-thickness walls, or shapes that clearly do not match the description.\n\n"
        f"Respond with ONLY valid JSON (no markdown, no code fences):\n"
        f'{{"checks": [{{"feature": "...", "found": true/false, "notes": "..."}}], '
        f'"overall_match": true/false, "confidence": 0.0-1.0, "issues": ["..."]}}'
    )


def _parse_vision_json(text: str) -> dict | None:
    """Parse a vision model response into a dict, stripping code fences."""
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        print("[VISUAL] could not parse JSON from vision response")
        return None


def _call_vision_gemini(
    image_paths: list[str],
    prompt: str,
    repo_root: Path | None = None,
) -> dict | None:
    """Try Gemini vision API for verification. Returns parsed dict or None."""
    from .llm_client import get_google_key, _gemini_model

    api_key = get_google_key(repo_root)
    if not api_key:
        return None

    # Try new google-genai SDK
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError:
        return None

    client = genai.Client(api_key=api_key)

    # Read image bytes + prompt as plain string (not Part.from_text which varies by SDK version)
    parts: list = []
    for img_path in image_paths:
        with open(img_path, "rb") as f:
            img_bytes = f.read()
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))
    parts.append(prompt)  # plain string — google-genai accepts str in contents list

    cfg = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=4096,
    )

    # Model preference: gemini-2.5-flash first, then configured model, then 2.0-flash
    configured = _gemini_model(repo_root)
    model_candidates = ["gemini-2.5-flash"]
    if configured not in model_candidates:
        model_candidates.append(configured)
    if "gemini-2.0-flash" not in model_candidates:
        model_candidates.append("gemini-2.0-flash")

    for try_model in model_candidates:
        # Retry up to 2 times per model (Gemini sometimes returns short/malformed JSON)
        for attempt in range(2):
            try:
                # Use higher temperature on retry to get different output
                retry_cfg = cfg if attempt == 0 else types.GenerateContentConfig(
                    temperature=0.3, max_output_tokens=4096)
                response = client.models.generate_content(
                    model=try_model,
                    contents=parts,
                    config=retry_cfg,
                )
                text = (response.text or "").strip()
                if not text:
                    continue
                print(f"[VISUAL] vision response from gemini/{try_model} ({len(text)} chars)")
                parsed = _parse_vision_json(text)
                if parsed and parsed.get("checks"):
                    return parsed
                if attempt == 0:
                    print(f"[VISUAL] malformed response, retrying...")
                    continue
                return parsed  # return whatever we got on 2nd attempt
            except Exception as exc:
                err_str = str(exc)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    if try_model != model_candidates[-1]:
                        break  # try next model
                    print("[VISUAL] gemini quota exhausted")
                    return None
                if "model" in err_str.lower():
                    break  # try next model
                print(f"[VISUAL] gemini vision error ({try_model}): {exc}")
                break  # don't retry on unknown errors
            return None

    return None


def _call_vision_anthropic(
    image_paths: list[str],
    prompt: str,
    repo_root: Path | None = None,
) -> dict | None:
    """Try Anthropic Claude vision API for verification. Returns parsed dict or None."""
    from .llm_client import get_anthropic_key

    api_key = get_anthropic_key(repo_root)
    if not api_key:
        return None

    try:
        import anthropic  # type: ignore
    except ImportError:
        return None

    # Build content blocks: images + text
    content: list[dict[str, Any]] = []
    for img_path in image_paths:
        b64 = _encode_image(img_path)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64,
            },
        })
    content.append({"type": "text", "text": prompt})

    client = anthropic.Anthropic(api_key=api_key)

    for model in ("claude-sonnet-4-6", "claude-3-5-sonnet-20241022"):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=1024,
                temperature=0,
                messages=[{"role": "user", "content": content}],
            )
            text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
            if not text:
                continue
            print(f"[VISUAL] vision response from anthropic/{model} ({len(text)} chars)")
            return _parse_vision_json(text)
        except Exception as exc:
            err = str(exc).lower()
            if "model" in err or "not_found" in err:
                continue
            print(f"[VISUAL] anthropic vision error ({model}): {exc}")
            return None

    return None


def _call_vision(
    image_paths: list[str],
    goal: str,
    checks: list[str],
    repo_root: Path | None = None,
) -> dict | None:
    """Send rendered views to vision AI and parse the verification result.

    Priority: Gemini 2.5 Flash (fast/cheap) -> Anthropic Claude (fallback) -> None.
    Returns parsed dict or None if no API is available.
    """
    prompt = _build_vision_prompt(goal, checks)

    # 1. Try Gemini (primary — fast and cheap)
    try:
        result = _call_vision_gemini(image_paths, prompt, repo_root)
        if result is not None:
            return result
    except Exception as exc:
        print(f"[VISUAL] gemini unexpected error: {exc}")

    # 2. Try Anthropic (fallback)
    try:
        result = _call_vision_anthropic(image_paths, prompt, repo_root)
        if result is not None:
            return result
    except Exception as exc:
        print(f"[VISUAL] anthropic unexpected error: {exc}")

    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def verify_visual(
    step_path: str,
    stl_path: str,
    goal: str,
    spec: dict,
    *,
    repo_root: Path | None = None,
) -> dict:
    """Render the part from 3 angles, send to vision AI, get verification.

    Parameters
    ----------
    step_path : str
        Path to the STEP file (used for metadata only; rendering uses STL).
    stl_path : str
        Path to the STL file to render.
    goal : str
        The natural-language goal that was used to generate the part.
    spec : dict
        Extracted spec dict (from spec_extractor.extract_spec).
    repo_root : Path | None
        Repository root for .env lookup.

    Returns
    -------
    dict with keys:
        verified     : bool | None   — True if all checks pass, None if API unavailable
        confidence   : float          — 0.0-1.0 confidence score
        checks       : list[dict]     — per-feature check results
        issues       : list[str]      — detected issues
        screenshots  : list[str]      — paths to rendered PNGs
        reason       : str | None     — explanation if verification was skipped
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    result: dict[str, Any] = {
        "verified": None,
        "confidence": 0.0,
        "checks": [],
        "issues": [],
        "screenshots": [],
        "reason": None,
    }

    # Validate STL exists
    stl = Path(stl_path)
    if not stl.exists():
        result["reason"] = f"STL file not found: {stl_path}"
        print(f"[VISUAL] {result['reason']}")
        return result

    # --- Step 1: Render 3 views ---------------------------------------------
    screenshot_dir = repo_root / "outputs" / "screenshots"
    try:
        paths = _render_views(stl_path, goal, screenshot_dir)
        result["screenshots"] = paths
        print(f"[VISUAL] rendered {len(paths)} views to {screenshot_dir}")
    except Exception as exc:
        result["reason"] = f"rendering failed: {exc}"
        print(f"[VISUAL] {result['reason']}")
        return result

    # --- Step 2: Build checklist --------------------------------------------
    checks = _build_checklist(goal, spec or {})
    print(f"[VISUAL] built {len(checks)} feature checks from goal + spec")

    # --- Step 3: Send to vision API -----------------------------------------
    vision_result = _call_vision(paths, goal, checks, repo_root)

    if vision_result is None:
        result["reason"] = "vision API unavailable"
        print("[VISUAL] vision API unavailable — skipping visual verification")
        return result

    # --- Step 4: Parse and return -------------------------------------------
    parsed_checks = vision_result.get("checks", [])
    issues = vision_result.get("issues", [])
    overall = vision_result.get("overall_match", False)
    confidence = vision_result.get("confidence", 0.0)

    # Ensure confidence is a float in [0, 1]
    try:
        confidence = float(confidence)
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.0

    # Count passes/fails
    n_pass = sum(1 for c in parsed_checks if c.get("found", False))
    n_total = len(parsed_checks)

    result["verified"] = bool(overall) and n_pass == n_total
    result["confidence"] = confidence
    result["checks"] = parsed_checks
    result["issues"] = [i for i in issues if i]  # filter empty strings

    status = "PASS" if result["verified"] else "FAIL"
    print(
        f"[VISUAL] verification {status}: "
        f"{n_pass}/{n_total} checks passed, "
        f"confidence={confidence:.2f}, "
        f"{len(result['issues'])} issue(s)"
    )
    for check in parsed_checks:
        flag = "OK" if check.get("found") else "XX"
        print(f"  [{flag}] {check.get('feature', '?')}: {check.get('notes', '')}")
    for issue in result["issues"]:
        print(f"  [!!] {issue}")

    return result
