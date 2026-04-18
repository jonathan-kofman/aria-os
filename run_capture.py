#!/usr/bin/env python3
"""Wrapper: run aria_os pipeline and flush all output to a file."""
import subprocess, sys, time, pathlib

out_file = pathlib.Path("aria_run_output.txt")
goal = "aluminium flanged pipe coupling 60mm OD 40mm bore 4 bolt holes on 80mm PCD"

with open(out_file, "w", encoding="utf-8", errors="replace") as fh:
    proc = subprocess.Popen(
        [sys.executable, "-u", "run_aria_os.py", goal],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    start = time.time()
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(line, flush=True)
            fh.write(line + "\n")
            fh.flush()
            elapsed = time.time() - start
            if elapsed > 280:
                fh.write(f"\n--- TIMEOUT after {elapsed:.0f}s ---\n")
                proc.kill()
                break
    except Exception as e:
        fh.write(f"\n--- WRAPPER ERROR: {e} ---\n")
    rc = proc.wait()
    fh.write(f"\n--- EXIT CODE: {rc} ---\n")
    print(f"\n--- EXIT CODE: {rc} ---")

print(f"Output saved to: {out_file.resolve()}")
