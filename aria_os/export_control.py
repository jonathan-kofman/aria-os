"""
Export control enforcement layer.

Tags assemblies and BOMs with the most-restrictive export classification
across all their components. Provides policy checks that:

- Warn when an ITAR-controlled part is about to be sent to a non-US endpoint
- Refuse to upload technical data (STEP/STL/drawings) to external services
  when any component is flagged ITAR
- Tag BOM output + MillForge handoff with the controlling classification

The rules encoded here are starting-point guidance. You, as the responsible
engineer, are legally accountable for actual export compliance decisions.
This module does not replace counsel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Hierarchy: most-restrictive wins
# ---------------------------------------------------------------------------

# Lower number = LESS restrictive. An assembly's classification is max() across all components.
_HIERARCHY = {
    "EAR99": 0,
    "EAR-": 1,
    "ITAR-": 2,
    "controlled-other": 2,
}


def classification_rank(tag: str) -> int:
    """Lower = less restrictive. EAR99 = 0, ITAR-* = 2."""
    if not tag or tag == "EAR99":
        return 0
    if tag.upper().startswith("ITAR"):
        return 2
    if tag.upper().startswith("EAR") and tag != "EAR99":
        return 1
    return 2  # unknown → treat as most restrictive


def most_restrictive(tags: list[str]) -> str:
    """Return the strictest tag from a list."""
    if not tags:
        return "EAR99"
    return max(tags, key=classification_rank)


# ---------------------------------------------------------------------------
# Assembly classification
# ---------------------------------------------------------------------------

@dataclass
class ExportControlReport:
    overall_classification: str = "EAR99"
    is_itar: bool = False
    is_controlled: bool = False
    component_tags: dict[str, str] = field(default_factory=dict)
    flagged_components: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def classify_assembly(bom: dict[str, Any]) -> ExportControlReport:
    """
    Determine the overall export classification for an assembly from its BOM.

    bom: output of assembly_bom.generate_bom()
    """
    tags: list[str] = []
    component_tags: dict[str, str] = {}
    flagged: list[str] = []

    for row in bom.get("purchased", []):
        tag = row.get("export_control", "EAR99")
        tags.append(tag)
        component_tags[row["designation"]] = tag
        if classification_rank(tag) > 0:
            flagged.append(row["designation"])

    overall = most_restrictive(tags)
    warnings: list[str] = []

    if overall.upper().startswith("ITAR"):
        warnings.append(
            f"Assembly contains ITAR-controlled components ({overall}). "
            "Technical data (CAD files, STEP, drawings, BOMs) must not be shared "
            "with non-US persons or exported without a license. "
            "Confirm design storage, cloud backups, and collaborators all meet "
            "22 CFR 120-130 requirements."
        )
    elif overall.upper().startswith("EAR") and overall != "EAR99":
        warnings.append(
            f"Assembly contains EAR-controlled components ({overall}). "
            "Check destination country + end-use against 15 CFR 734 before export."
        )

    return ExportControlReport(
        overall_classification=overall,
        is_itar=overall.upper().startswith("ITAR"),
        is_controlled=classification_rank(overall) > 0,
        component_tags=component_tags,
        flagged_components=flagged,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Policy enforcement — used at integration boundaries
# ---------------------------------------------------------------------------

import os as _os


def _allowlisted_endpoints() -> set[str]:
    """Read explicit allow-list of cleared MillForge endpoints from env.

    MANUFACTURING_CORE_ITAR_ALLOWED_ENDPOINTS — comma-separated URL prefixes
    that the deploying engineer has personally verified are US-hosted,
    US-personnel-only, and contractually appropriate for ITAR technical data.

    Default allow-list contains only localhost — anything routable across the
    network requires explicit allow-listing.
    """
    raw = _os.environ.get("MANUFACTURING_CORE_ITAR_ALLOWED_ENDPOINTS", "")
    base = {
        "http://localhost", "https://localhost",
        "http://127.0.0.1", "https://127.0.0.1",
    }
    if raw:
        for entry in raw.split(","):
            entry = entry.strip().rstrip("/")
            if entry:
                base.add(entry)
    return base


def check_millforge_destination_ok(
    report: ExportControlReport,
    millforge_url: str,
) -> tuple[bool, str]:
    """
    Returns (ok, reason).

    For ITAR-controlled assemblies, refuses submission unless the destination
    URL prefix-matches an explicit allow-list (env var
    MANUFACTURING_CORE_ITAR_ALLOWED_ENDPOINTS). Default allow-list is localhost
    only.

    NOTE: This function is a guardrail, not legal compliance. The deploying
    engineer remains accountable under 22 CFR 120-130 for actual export
    decisions (license requirements, supplier certification, end-use review).
    Do not treat passing this check as authorization to export.
    """
    if not report.is_controlled:
        return True, ""

    if report.is_itar:
        if not _url_in_allowlist(millforge_url, _allowlisted_endpoints()):
            flagged_msg = ", ".join(report.flagged_components[:3])
            if len(report.flagged_components) > 3:
                flagged_msg += "..."
            return False, (
                f"ITAR-controlled assembly ({report.overall_classification}) "
                f"submission BLOCKED to {millforge_url}. Destination not in "
                "ITAR endpoint allow-list. To approve a destination (only "
                "after verifying it meets 22 CFR 120-130 requirements), add "
                "to MANUFACTURING_CORE_ITAR_ALLOWED_ENDPOINTS env var. "
                f"Flagged components: {flagged_msg}"
            )
    return True, ""


def _url_in_allowlist(target_url: str, allowed: set[str]) -> bool:
    """
    Strict URL host+port match for the allow-list.

    Compares parsed (scheme, hostname, port, path) tuples — NOT raw substrings
    or string-prefix. Prevents prefix-confusion attacks where a malicious
    domain like "https://us-cleared.example.com.evil.com/" would match
    "https://us-cleared.example.com" under naive startswith logic.
    """
    from urllib.parse import urlsplit
    try:
        target = urlsplit(target_url)
    except ValueError:
        return False
    target_host = (target.hostname or "").lower()
    target_port = target.port
    target_scheme = (target.scheme or "").lower()

    for entry in allowed:
        try:
            allowed_parsed = urlsplit(entry.rstrip("/"))
        except ValueError:
            continue
        a_host = (allowed_parsed.hostname or "").lower()
        a_port = allowed_parsed.port
        a_scheme = (allowed_parsed.scheme or "").lower()

        if a_host != target_host:
            continue
        if a_scheme and a_scheme != target_scheme:
            continue
        if a_port is not None and a_port != target_port:
            continue
        # Path tightening: if allow-list entry has a path, target must
        # match exactly OR start with allowed_path + "/"
        a_path = (allowed_parsed.path or "").rstrip("/")
        if a_path:
            t_path = (target.path or "").rstrip("/")
            if not (t_path == a_path or t_path.startswith(a_path + "/")):
                continue
        return True
    return False


def check_cloud_llm_ok(report: ExportControlReport) -> tuple[bool, str]:
    """
    Returns (ok, reason). Refuses to send ITAR-controlled geometry to cloud
    LLM services (vision verifiers, cloud code generators). For ITAR work,
    stay on-prem with Ollama.
    """
    if not report.is_itar:
        return True, ""
    return False, (
        f"ITAR-controlled assembly ({report.overall_classification}) must not "
        "be sent to cloud LLMs (Anthropic, Gemini, etc.). Use local Ollama or "
        "a cleared on-premise LLM only. Technical data of a defense article "
        "is itself export-controlled."
    )


# ---------------------------------------------------------------------------
# BOM annotation helper
# ---------------------------------------------------------------------------

def annotate_bom_with_export_control(bom: dict[str, Any]) -> dict[str, Any]:
    """Add a 'export_control' block to a BOM dict. Returns the same dict."""
    report = classify_assembly(bom)
    bom["export_control"] = {
        "overall_classification": report.overall_classification,
        "is_itar": report.is_itar,
        "is_controlled": report.is_controlled,
        "flagged_components": report.flagged_components,
        "warnings": report.warnings,
    }
    return bom
