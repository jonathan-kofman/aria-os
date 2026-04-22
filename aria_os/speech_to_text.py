"""
Speech-to-text for ARIA-OS pipeline input at 3 user-facing stages:
  1. Initial goal  (run_aria_os.py --voice "...")
  2. Refine        (run_aria_os.py --refine --voice)
  3. Modify        (run_aria_os.py --modify <path> --voice)

Thin wrapper over `manufacturing_core.voice`. The shared module centralizes
the mic recording + transcription-provider chain so ariaOS, MillForge-AI,
and StructSight share one implementation (see manufacturing-core CLAUDE.md
rule: "shared-across-projects code lives in manufacturing-core").

If manufacturing-core isn't installed, we fall back to a local inline copy
so the CLI still works on boxes that haven't run `pip install -e
../manufacturing-core` yet.
"""
from __future__ import annotations

import os
import sys
import time
import wave
from pathlib import Path


try:
    from manufacturing_core.voice import (
        voice_input, transcribe, record_wav as _record_wav,
    )
    _USING_SHARED = True
except Exception:
    _USING_SHARED = False
    _SAMPLE_RATE = 16000
    _CHANNELS = 1

    def _record_wav(out_path: Path, *, max_seconds: int = 30,
                     silence_trail_s: float = 1.5) -> bool:
        try:
            import sounddevice as sd
            import numpy as np
        except Exception as exc:
            print(f"[stt] sounddevice/numpy unavailable: {exc}")
            return False
        print(f"[stt] recording (max {max_seconds}s) — speak now...")
        try:
            block_s = 0.1
            block_n = int(_SAMPLE_RATE * block_s)
            frames = []
            silence_n = 0
            silence_limit = int(silence_trail_s / block_s)
            ambient: list[float] = []
            started = False
            t_start = time.time()

            def _rms(a):
                a = a.astype("float64")
                return float((a * a).mean() ** 0.5)

            stream = sd.InputStream(samplerate=_SAMPLE_RATE, channels=_CHANNELS,
                                     dtype="int16", blocksize=block_n)
            with stream:
                while time.time() - t_start < max_seconds:
                    block, _ = stream.read(block_n)
                    frames.append(block.copy())
                    r = _rms(block)
                    if len(ambient) < 5:
                        ambient.append(r); continue
                    base = max(250.0, sum(ambient) / len(ambient))
                    if r > base * 2.5:
                        started = True; silence_n = 0
                    elif started:
                        silence_n += 1
                        if silence_n >= silence_limit:
                            break
            if not frames:
                return False
            pcm = np.concatenate(frames, axis=0).astype("int16").tobytes()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(out_path), "wb") as w:
                w.setnchannels(_CHANNELS); w.setsampwidth(2)
                w.setframerate(_SAMPLE_RATE); w.writeframes(pcm)
            return True
        except Exception as exc:
            print(f"[stt] record failed: {type(exc).__name__}: {exc}")
            return False

    def _t_groq(wav_path):
        key = os.environ.get("GROQ_API_KEY")
        if not key: return None
        try: from groq import Groq
        except Exception: return None
        try:
            with open(wav_path, "rb") as f:
                r = Groq(api_key=key).audio.transcriptions.create(
                    model="whisper-large-v3",
                    file=(wav_path.name, f.read()), response_format="text")
            return r if isinstance(r, str) else getattr(r, "text", None)
        except Exception: return None

    def _t_openai(wav_path):
        key = os.environ.get("OPENAI_API_KEY")
        if not key: return None
        try: from openai import OpenAI
        except Exception: return None
        try:
            with open(wav_path, "rb") as f:
                r = OpenAI(api_key=key).audio.transcriptions.create(
                    model="whisper-1", file=f, response_format="text")
            return r if isinstance(r, str) else getattr(r, "text", None)
        except Exception: return None

    def _t_fw(wav_path):
        try: from faster_whisper import WhisperModel
        except Exception: return None
        try:
            m = WhisperModel("tiny.en", device="cpu", compute_type="int8")
            segs, _ = m.transcribe(str(wav_path), vad_filter=True)
            return "".join(s.text for s in segs).strip()
        except Exception: return None

    def transcribe(wav_path: Path) -> str | None:
        for fn in (_t_groq, _t_openai, _t_fw):
            r = fn(wav_path)
            if r: return r.strip()
        return None

    def voice_input(stage: str, *, max_seconds: int = 30,
                    out_dir: str | Path = "outputs/voice") -> str | None:
        out_dir = Path(out_dir)
        ts = time.strftime("%Y%m%dT%H%M%S")
        wav = out_dir / f"{ts}_{stage}.wav"
        print(f"[stt] --- stage: {stage} (local fallback) ---")
        if not _record_wav(wav, max_seconds=max_seconds):
            return None
        text = transcribe(wav)
        if not text:
            print(f"[stt] transcription failed; audio at {wav}")
            return None
        try:
            wav.with_suffix(".txt").write_text(text, encoding="utf-8")
        except Exception:
            pass
        print(f"[stt] transcribed: {text!r}")
        return text


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    r = voice_input("smoke", max_seconds=n)
    print("RESULT:", r)
    print("USING SHARED manufacturing_core.voice:", _USING_SHARED)
