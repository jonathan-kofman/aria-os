"""Few-shot plan library.

Each few-shot is a hand-written, validator-passing native plan paired
with the goal it answers. At LLM-call time, the retriever picks the
top-K by tag overlap (intent: "show the model 1-2 working examples
of the exact ops it'll need") and injects them into the system prompt.

Layout:
    fewshots/
        __init__.py           — registry + retrieval API
        catalog.json          — generated index of all snippets
        sweep_along_helix.json
        loft_round_to_rect.json
        revolve_with_spline.json
        … etc.

Each .json file matches:
    {
        "id":     str,
        "goal":   str,         # natural language goal this answers
        "tags":   [str, …],    # vocabulary keywords, used for retrieval
        "ops_used": [str, …],  # validator op kinds used
        "plan":   [{kind, params, label}, …]
    }

The intentional choice of one-file-per-shot (vs a single mega-JSON):
  - Easy to add/remove shots in PRs.
  - The plan reads as JSON, not Python, so it can't drift from the
    actual op schema.
  - Retrieval bypasses any code-loading machinery — `json.loads` is
    enough.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z_]+")


@dataclass
class FewShot:
    id: str
    goal: str
    tags: list[str]
    ops_used: list[str]
    plan: list[dict]
    path: Path

    def render_for_prompt(self) -> str:
        """Format this shot as a markdown block for the LLM context."""
        return (f"### {self.goal}\n"
                f"Ops used: {', '.join(self.ops_used)}\n"
                f"```json\n{json.dumps(self.plan, indent=2)}\n```")


def _load_all() -> list[FewShot]:
    shots: list[FewShot] = []
    for p in sorted(_DIR.glob("*.json")):
        if p.name == "catalog.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        shots.append(FewShot(
            id=data.get("id", p.stem),
            goal=data.get("goal", ""),
            tags=list(data.get("tags") or []),
            ops_used=list(data.get("ops_used") or []),
            plan=list(data.get("plan") or []),
            path=p,
        ))
    return shots


_CACHE: list[FewShot] | None = None


def all_shots() -> list[FewShot]:
    """Return every registered few-shot. Cached after first call."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _load_all()
    return _CACHE


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def retrieve(goal: str, *, k: int = 2,
              prefer_ops: list[str] | None = None) -> list[FewShot]:
    """Return up to k shots scored by:
      - tag/word overlap with goal
      - +5 per op-kind in `prefer_ops` that this shot's plan uses

    `prefer_ops` is the planner's best-guess of what ops the LLM will
    need (passed by the caller after running a quick keyword scan).
    Empty goal → no shots."""
    if not goal:
        return []
    q = _tokenize(goal)
    prefer = set(prefer_ops or [])
    scored: list[tuple[float, FewShot]] = []
    for s in all_shots():
        score = 0.0
        # Tag overlap (each tag is a phrase; tokenize)
        for tag in s.tags:
            t = _tokenize(tag)
            score += len(q & t)
        # Goal-text overlap (the original natural-language goal of the shot)
        score += len(q & _tokenize(s.goal)) * 0.5
        # Op-kind preference
        if prefer:
            for op in s.ops_used:
                if op in prefer:
                    score += 5.0
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda r: r[0], reverse=True)
    return [s for _, s in scored[:k]]


def render_for_prompt(shots: list[FewShot]) -> str:
    if not shots:
        return ""
    out = ["## Working examples (validated plans for similar goals)\n"]
    for s in shots:
        out.append(s.render_for_prompt())
        out.append("")
    return "\n".join(out).strip()


__all__ = ["FewShot", "all_shots", "retrieve", "render_for_prompt"]
