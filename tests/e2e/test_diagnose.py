"""`harken diagnose` e2e (QA-007; doc 12 §4.1's five missing tests).

These are the tests whose absence let a TypeError ship in the command:
diagnose is run as a real subprocess against a live MockSimulator so the
Nagios exit codes (doc 06 §6.3: 0 healthy / 1 warning / 2 critical /
3 unknown) are asserted on the actual process, not a mock.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from harkeniq.mock.simulator import MockSimulator

pytestmark = pytest.mark.asyncio

_SKILLS_DIR = str(Path(__file__).resolve().parents[2] / "skills")


async def run_diagnose(*args: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["HARKENIQ_SKILLS_DIRECTORY"] = _SKILLS_DIR
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "harkeniq", "diagnose", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode()


async def test_diagnose_healthy_exit_0():
    sim = MockSimulator(device="dell-r750", port=0)
    await sim.start()
    try:
        code, out = await run_diagnose(
            "--bmc-ip", sim.url, "--bmc-user", "admin", "--bmc-pass", "password",
        )
    finally:
        await sim.stop()
    assert code == 0, out
    assert "No issues found" in out


async def test_diagnose_critical_exit_2():
    sim = MockSimulator(device="dell-r750", port=0)
    await sim.start()
    await sim.inject_fault("fan", "Fan1A", {"health": "Critical", "speed_rpm": 0})
    try:
        code, out = await run_diagnose(
            "--bmc-ip", sim.url, "--bmc-user", "admin", "--bmc-pass", "password",
        )
    finally:
        await sim.stop()
    assert code == 2, out
    assert "Fan1A" in out


async def test_diagnose_warning_exit_1():
    # One-shot diagnose runs at baseline confidence 0 (nothing learned),
    # so the A2.3 gate keeps it in BMC-health pass-through: it reports
    # what the BMC itself asserts. A Warning health rolls out as exit 1;
    # expression-only signals (e.g. smart_alert without a health change)
    # deliberately do NOT fire without baselines.
    sim = MockSimulator(device="dell-r750", port=0)
    await sim.start()
    await sim.inject_fault("fan", "Fan2A", {"health": "Warning"})
    try:
        code, out = await run_diagnose(
            "--bmc-ip", sim.url, "--bmc-user", "admin", "--bmc-pass", "password",
        )
    finally:
        await sim.stop()
    assert code == 1, out


async def test_diagnose_json_output():
    sim = MockSimulator(device="dell-r750", port=0)
    await sim.start()
    await sim.inject_fault("fan", "Fan1A", {"health": "Critical", "speed_rpm": 0})
    try:
        code, out = await run_diagnose(
            "--bmc-ip", sim.url, "--bmc-user", "admin", "--bmc-pass", "password",
            "--json",
        )
    finally:
        await sim.stop()
    assert code == 2, out
    payload = json.loads(out)
    assert payload["device"]["vendor"] == "Dell"
    assert payload["worst_severity"] == "CRITICAL"
    assert payload["diagnoses"], "critical fault produced no diagnosis"
    # Honest confidence: one-shot poll has no learned baseline.
    assert payload["diagnoses"][0]["confidence"][0]["name"] == "baseline"


async def test_diagnose_bmc_unreachable_exit_3():
    code, out = await run_diagnose(
        "--bmc-ip", "https://127.0.0.1:1", "--bmc-user", "x", "--bmc-pass", "y",
    )
    assert code == 3, out
    assert "UNKNOWN" in out
