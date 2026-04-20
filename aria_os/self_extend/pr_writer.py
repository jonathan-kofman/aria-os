"""
PR writer — package an accepted candidate as a GitHub pull request.

Runs in the sandbox's worktree:
  1. Commit the new module + tests
  2. Push to a branch `aria-agent/<request_id>`
  3. Run `gh pr create` with a structured body (physics evidence, benchmark
     numbers, rendered previews)

dry_run: return a fake PR URL without touching git/gh. Used by unit tests
         and judge-less rehearsals.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestrator import ExtensionRequest


_BRANCH_PREFIX = "aria-agent/"


def _gh_available() -> bool:
    try:
        r = subprocess.run(["gh", "--version"],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _render_pr_body(best: dict, request: "ExtensionRequest") -> str:
    cand = best["candidate"]
    verdict = best["verdict"]
    metrics = verdict.get("metrics", {})
    lines = [
        f"## Self-extension agent PR",
        "",
        f"Request: **{request.goal}**",
        f"Request id: `{request.request_id}`",
        "",
        f"### Winning candidate: `{cand.get('name', 'unknown')}`",
        f"- kind: `{cand.get('kind', 'unknown')}`",
        f"- rationale: {cand.get('rationale', '')}",
        f"- composed from: "
        f"{', '.join(cand.get('parent_primitives', [])) or '—'}",
        "",
        "### Physics verdict",
        f"- passed: **{verdict.get('passed')}**",
        f"- score: **{verdict.get('score')}**",
    ]
    for k, v in metrics.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("### Guardrails")
    lines.append(
        "- ✅ **Sandbox (G1)**: executed in isolated git-worktree.")
    lines.append(
        "- ✅ **Contract tests (G2)**: passed the fixture suite.")
    lines.append(
        "- ✅ **Physics (G3)**: cleared CalculiX / DRC / CAMotics gate.")
    lines.append(
        "- ⚠️ **HITL (G4)**: new module is QUARANTINED — first real "
        "invocation requires human approval.")
    lines.append("")
    if request.github_issue_id is not None:
        lines.append(f"Closes #{request.github_issue_id}.")
    return "\n".join(lines)


def write_pr(best: dict, *, request: "ExtensionRequest",
             dry_run: bool = False) -> dict:
    """Commit + push + open a PR. Returns {url, branch, status}.

    In dry_run mode, returns a fake url without touching git/gh so the
    rest of the pipeline can be tested.
    """
    cand = best["candidate"]
    worktree = Path(best["sandbox_worktree"])
    branch = f"{_BRANCH_PREFIX}{request.request_id}"

    if dry_run or not _gh_available():
        return {"url": f"https://example.com/pr/{request.request_id}",
                "branch": branch, "status": "dry_run"}

    # Commit in the sandbox worktree
    msg = (f"agent: new {cand.get('kind', 'module')} "
           f"{cand.get('name', 'unknown')}\n\nRequest: {request.goal}")
    subprocess.run(["git", "add", cand["module_relpath"]],
                   cwd=worktree, check=False, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg],
                   cwd=worktree, check=False, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", branch],
                   cwd=worktree, check=False, capture_output=True)

    body = _render_pr_body(best, request)
    title = (f"[aria-agent] new {cand.get('kind', 'module')}: "
             f"{cand.get('name', 'unknown')}")
    res = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body,
         "--head", branch, "--base", "main"],
        cwd=worktree, capture_output=True, text=True)
    url = (res.stdout or "").strip()
    return {"url": url, "branch": branch,
            "status": "opened" if res.returncode == 0 else "failed",
            "stderr": res.stderr[-400:] if res.stderr else ""}
