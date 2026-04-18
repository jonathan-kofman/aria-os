"""
scripts/augment_goals.py

Paraphrase every goal in a synthetic-dataset JSONL using Gemini.
Each original row is expanded to 4 rows: 1 original + 3 paraphrases.

Voices requested from Gemini:
  1. Terse engineering spec     ("80mm OD aluminium flange, 4xM8, 21mm thick")
  2. Customer-request tone      ("I need a flange that fits an 80mm shaft...")
  3. Hobbyist / maker tone      ("Looking to 3D-print a flange for my project...")

Numbers are kept exact. Malformed Gemini responses are skipped.

Usage:
    python scripts/augment_goals.py --in PATH_TO_JSONL [--out PATH] [--batch N]
                                     [--model gemini-2.0-flash]

Prerequisites:
    - GEMINI_API_KEY env var set (or GOOGLE_API_KEY)
    - pip install google-generativeai  (already in requirements.txt)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Gemini client setup (lazy — only imported when actually running)
# ---------------------------------------------------------------------------

def _get_gemini_model(model_name: str):
    """Return a google.generativeai GenerativeModel, or raise on missing key."""
    try:
        import google.generativeai as genai  # type: ignore
    except ImportError:
        raise RuntimeError(
            "google-generativeai is not installed. "
            "Run: pip install google-generativeai"
        )
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Set GEMINI_API_KEY or GOOGLE_API_KEY environment variable."
        )
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(model_name)


_PARAPHRASE_PROMPT = textwrap.dedent("""\
Rewrite this engineering part description in exactly 3 different natural voices.
Keep ALL numbers, units, and technical specifications exactly as-is.

Original: {goal}

Return ONLY a JSON array with exactly 3 strings, one per voice:
[
  "<terse spec: concise datasheet language, symbols like OD/ID/M6, all numbers preserved>",
  "<customer request: conversational, first-person, all numbers preserved>",
  "<hobbyist/maker: informal, enthusiastic, numbers preserved>"
]

No explanation, no markdown fences. Just the JSON array.
""")

import textwrap  # noqa: E402  (needed for the dedent above)


def _paraphrase(model, goal: str) -> list[str] | None:
    """Call Gemini to produce 3 paraphrases. Returns list of 3 strings or None."""
    prompt = _PARAPHRASE_PROMPT.format(goal=goal)
    try:
        response = model.generate_content(prompt)
        raw = response.text.strip()
    except Exception as exc:
        print(f"  [WARN] Gemini error: {exc}", file=sys.stderr)
        return None

    # Strip optional markdown code fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try to find the array in the response
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(parsed, list) or len(parsed) < 3:
        return None

    # Validate each element is a non-empty string
    result = []
    for item in parsed[:3]:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        else:
            return None

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Augment synthetic dataset goals with Gemini paraphrases."
    )
    parser.add_argument(
        "--in", dest="input_path", required=True,
        help="Input JSONL (output of build_synthetic_dataset.py)"
    )
    parser.add_argument(
        "--out", dest="output_path", default="",
        help="Output JSONL path (default: <input>_augmented.jsonl)"
    )
    parser.add_argument(
        "--model", default="gemini-2.0-flash",
        help="Gemini model name (default: gemini-2.0-flash)"
    )
    parser.add_argument(
        "--batch", type=int, default=1,
        help="Requests per second rate limit denominator (default: 1 = no batching)"
    )
    parser.add_argument(
        "--delay", type=float, default=0.1,
        help="Seconds to sleep between API calls (default: 0.1)"
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Process only first N rows (0 = all, default: 0)"
    )
    args = parser.parse_args()

    input_path = Path(args.input_path)
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        return 1

    if args.output_path:
        out_path = Path(args.output_path)
    else:
        out_path = input_path.with_name(input_path.stem + "_augmented.jsonl")

    print(f"[augment_goals] Loading {input_path}...")
    rows: list[dict[str, Any]] = []
    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    if args.limit > 0:
        rows = rows[: args.limit]

    print(f"  {len(rows)} rows to process")
    print(f"  Model: {args.model}")
    print(f"  Output: {out_path}")

    model = _get_gemini_model(args.model)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    skipped = 0
    expanded = 0
    t0 = time.monotonic()

    with out_path.open("w", encoding="utf-8") as fout:
        for i, row in enumerate(rows):
            # Always write the original
            fout.write(json.dumps(row, ensure_ascii=False) + "\n")
            expanded += 1

            goal = row.get("goal", "")
            paraphrases = _paraphrase(model, goal)

            if paraphrases is None:
                skipped += 1
            else:
                voices = ["terse_spec", "customer_request", "hobbyist"]
                for voice, paraphrase in zip(voices, paraphrases):
                    aug_row = dict(row)
                    aug_row["goal"] = paraphrase
                    aug_row["goal_voice"] = voice
                    aug_row["goal_original"] = goal
                    fout.write(json.dumps(aug_row, ensure_ascii=False) + "\n")
                    expanded += 1

            if (i + 1) % 100 == 0:
                elapsed = time.monotonic() - t0
                rate = (i + 1) / elapsed
                eta = (len(rows) - i - 1) / rate if rate > 0 else 0
                print(
                    f"  [{i + 1}/{len(rows)}] {expanded} rows written, "
                    f"{skipped} skipped | ETA {eta:.0f}s",
                    flush=True,
                )

            if args.delay > 0:
                time.sleep(args.delay)

    elapsed = time.monotonic() - t0
    print()
    print("=" * 60)
    print("AUGMENTATION SUMMARY")
    print("=" * 60)
    print(f"  Input rows       : {len(rows)}")
    print(f"  Output rows      : {expanded}")
    print(f"  Expansion factor : {expanded / max(len(rows), 1):.2f}x")
    print(f"  Skipped (bad)    : {skipped}")
    print(f"  Elapsed          : {elapsed:.1f}s")
    print(f"  Written to       : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
