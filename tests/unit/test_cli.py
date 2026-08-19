"""CLI tests (Doc 12 §4.1)."""

import asyncio
import sys

import pytest
from click.testing import CliRunner

from harkeniq import __version__
from harkeniq.cli import main
from harkeniq.mock.simulator import MockSimulator


class TestVersion:
    def test_version(self):
        result = CliRunner().invoke(main, ["version"])
        assert result.exit_code == 0
        assert __version__ in result.output


class TestBmcDetect:
    async def _run_cli(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "harkeniq", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode, out.decode()

    async def test_bmc_detect_dell(self):
        sim = MockSimulator(device="dell-r750", port=0)
        await sim.start()
        try:
            code, out = await self._run_cli(
                "bmc", "detect", "--bmc-ip", sim.url,
                "--username", "admin", "--password", "password",
            )
        finally:
            await sim.stop()

        assert code == 0, out
        assert "Dell PowerEdge R750 (iDRAC9)" in out

    async def test_bmc_detect_hpe_ilo6(self):
        sim = MockSimulator(device="hpe-dl380-gen11", port=0)
        await sim.start()
        try:
            code, out = await self._run_cli(
                "bmc", "detect", "--bmc-ip", sim.url,
                "--username", "admin", "--password", "password",
            )
        finally:
            await sim.stop()

        assert code == 0, out
        assert "HPE ProLiant DL380 Gen11 (iLO6)" in out

    async def test_bmc_detect_bad_credentials(self):
        sim = MockSimulator(device="dell-r750", port=0)
        await sim.start()
        try:
            code, out = await self._run_cli(
                "bmc", "detect", "--bmc-ip", sim.url,
                "--username", "admin", "--password", "wrong",
            )
        finally:
            await sim.stop()

        assert code != 0
        assert "Error" in out

    def test_bmc_detect_requires_password(self):
        result = CliRunner().invoke(main, ["bmc", "detect", "--bmc-ip", "https://x"])
        assert result.exit_code != 0
        assert "password" in result.output.lower()


VALID_SKILL = (
    "name: cli-test\nversion: 1\ntarget: fan\n"
    "rules:\n  - condition: \"health == 'Critical'\"\n"
    "    verdict: CRITICAL\n    message: bad\n"
)


class TestSkillsValidate:
    def test_bundled_skills_valid_exit_0(self):
        result = CliRunner().invoke(main, ["skills", "validate", "--dir", "skills"])
        assert result.exit_code == 0, result.output
        assert "All 5 skill files valid" in result.output

    def test_invalid_skill_exit_4(self, tmp_path):
        (tmp_path / "good.yaml").write_text(VALID_SKILL)
        (tmp_path / "bad.yaml").write_text(
            "name: broken\nversion: 1\ntarget: gpu\n"
            "rules:\n  - condition: \"health == 'Critical'\"\n"
            "    verdict: CRITICAL\n    message: bad\n"
        )
        result = CliRunner().invoke(main, ["skills", "validate", "--dir", str(tmp_path)])
        assert result.exit_code == 4
        assert "ERROR bad.yaml" in result.output
        assert "OK    good.yaml" in result.output

    def test_missing_directory_exit_4(self, tmp_path):
        result = CliRunner().invoke(
            main, ["skills", "validate", "--dir", str(tmp_path / "nope")]
        )
        assert result.exit_code == 4
        assert "not found" in result.output

    def test_empty_directory_exit_4(self, tmp_path):
        result = CliRunner().invoke(main, ["skills", "validate", "--dir", str(tmp_path)])
        assert result.exit_code == 4
        assert "No skill files" in result.output


class TestSkillsList:
    def test_lists_bundled_skills(self):
        result = CliRunner().invoke(main, ["skills", "list", "--dir", "skills"])
        assert result.exit_code == 0, result.output
        for name in ("fan-health", "disk-health", "memory-health",
                     "psu-health", "thermal-health"):
            assert name in result.output

    def test_bad_directory_errors(self, tmp_path):
        result = CliRunner().invoke(
            main, ["skills", "list", "--dir", str(tmp_path / "nope")]
        )
        assert result.exit_code != 0
