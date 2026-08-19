"""E2E: `harken demo --mock --speed 10` showcase (Doc 12 §4.2).

Runs the demo as a real subprocess (no TTY, so line mode) and asserts
the Doc 09 acceptance criteria: clean exit within the wall-clock bound
and evidence of every showcased capability in the output.
"""

import subprocess
import sys
import time

import pytest


@pytest.fixture(scope="module")
def demo_result():
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", "from harkeniq.cli import main; main()",
         "demo", "--mock", "--speed", "10"],
        capture_output=True, text=True, timeout=30,
    )
    return proc, time.monotonic() - start


def test_exits_cleanly_within_bound(demo_result):
    proc, elapsed = demo_result
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert elapsed < 10, f"demo took {elapsed:.1f}s (bound 10s)"


def test_output_shows_all_capabilities(demo_result):
    out = demo_result[0].stdout
    for keyword in ("CRITICAL", "WARNING", "TRENDING", "UNRESPONSIVE",
                    "PENDING ACTIONS"):
        assert keyword in out, f"{keyword!r} missing from demo output:\n{out}"


def test_scripted_narrative_present(demo_result):
    out = demo_result[0].stdout
    assert "baselines pre-seeded" in out
    assert "rack-12-server-03" in out  # dead peer named in the witness line
    assert "demo complete" in out  # summary screen
