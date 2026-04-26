"""W13 — autoroute.py SES re-import fix.

The audit caught that the original code did:
    shutil.copyfile(pcb_path, routed_path)
in place of actually importing the SES file. The "routed" board was
just a copy of the unrouted input. This test pins down that:

  1. When KiCad bundled python is missing, autoroute returns a clear
     {available: False, error: ...} with the KiCad install hint.
  2. When freerouting.jar is missing (with KiCad present), same.
  3. When BOTH the DSN export AND SES import succeed (mocked), the
     final routed_path is NOT a byte-equal copy of the input pcb_path
     -- proving the new pcbnew round-trip is wired in.

Real end-to-end verification requires Java + freerouting.jar +
KiCad's bundled python. That's a manual step (`scripts/run_autoroute_demo.py`).
"""
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from aria_os.ecad import autoroute


def test_autoroute_unavailable_when_kicad_python_missing(tmp_path):
    pcb = tmp_path / "fake.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108))\n")
    with mock.patch.object(autoroute, "_find_kicad_python", return_value=None), \
         mock.patch.object(autoroute, "_find_freerouting_jar", return_value="/fake.jar"), \
         mock.patch.object(autoroute, "_find_java", return_value="/fake/java"):
        result = autoroute.run_autoroute(pcb, tmp_path)
    assert result["available"] is False
    assert "KiCad bundled python.exe" in result["error"]
    assert result["routed_pcb_path"] is None


def test_autoroute_unavailable_when_jar_missing(tmp_path):
    pcb = tmp_path / "fake.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n")
    with mock.patch.object(autoroute, "_find_kicad_python", return_value="/k/python.exe"), \
         mock.patch.object(autoroute, "_find_freerouting_jar", return_value=None), \
         mock.patch.object(autoroute, "_find_java", return_value="/fake/java"):
        result = autoroute.run_autoroute(pcb, tmp_path)
    assert result["available"] is False
    assert "freerouting.jar" in result["error"]


def test_autoroute_routed_pcb_is_not_byte_copy_of_input(tmp_path, monkeypatch):
    """The audit bug: routed_path was a byte-for-byte copy of pcb_path.
    Mock subprocess.run so the DSN export, freerouting, and SES import
    all 'succeed', but write a DIFFERENT bytestring to routed_path than
    is in pcb_path. The new code must produce that different file
    rather than copying the input over the output."""
    pcb = tmp_path / "in.kicad_pcb"
    in_bytes = b"(kicad_pcb (version 20240108) (UNROUTED))\n"
    pcb.write_bytes(in_bytes)
    out_dir = tmp_path / "out"

    routed_bytes = b"(kicad_pcb (version 20240108) (ROUTED 100 traces))\n"

    call_log = []

    def fake_run(cmd, *args, **kwargs):
        call_log.append(cmd[0:2])
        # Match each leg by what the cmd contains:
        cmd_str = " ".join(str(c) for c in cmd)
        if "ExportSpecctraDSN" in cmd_str:
            # [kicad_py, '-c', script, pcb_path, dsn_path] -> dsn is cmd[4]
            Path(cmd[4]).write_text("(pcb (dsn placeholder))")
            return subprocess.CompletedProcess(cmd, 0, "OK\n", "")
        if any("-jar" in str(c) for c in cmd):
            # java -jar freerouting.jar -de dsn -do ses -mp 1
            ses_idx = cmd.index("-do") + 1
            Path(cmd[ses_idx]).write_text("(ses placeholder)")
            return subprocess.CompletedProcess(cmd, 0, "routed\n", "")
        if "ImportSpecctraSES" in cmd_str:
            # [kicad_py, '-c', script, pcb, ses, out_pcb] -> out is cmd[5]
            Path(cmd[5]).write_bytes(routed_bytes)
            return subprocess.CompletedProcess(cmd, 0, "OK\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected cmd")

    monkeypatch.setattr(autoroute.subprocess, "run", fake_run)
    monkeypatch.setattr(autoroute, "_find_kicad_python",
                        lambda: "/fake/kicad/python.exe")
    monkeypatch.setattr(autoroute, "_find_freerouting_jar",
                        lambda: "/fake/freerouting.jar")
    monkeypatch.setattr(autoroute, "_find_java", lambda: "/fake/java.exe")

    result = autoroute.run_autoroute(pcb, out_dir)

    assert result["available"] is True, f"unexpected: {result}"
    assert result["routed_pcb_path"], f"no routed_pcb_path in {result}"
    routed = Path(result["routed_pcb_path"])
    assert routed.is_file()

    # THE audit bug: routed used to be byte-equal to input. Now it
    # must be the bytes that the SES import produced.
    assert routed.read_bytes() == routed_bytes, (
        "routed_path bytes don't match the SES import output -- the "
        "old shutil.copyfile bug may have regressed.")
    assert routed.read_bytes() != in_bytes, (
        "routed_path is byte-equal to the input PCB -- the audit bug "
        "(`shutil.copyfile(pcb_path, routed_path)`) is back.")
