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
        assert "All 6 skill files valid" in result.output

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


class TestBmcTest:
    """R2a: `harken bmc test` — reachability/auth/vendor/latency (D10 codes)."""

    async def _run_cli(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "harkeniq", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode, out.decode()

    async def test_success(self):
        sim = MockSimulator(device="dell-r750", port=0)
        await sim.start()
        try:
            code, out = await self._run_cli(
                "bmc", "test", "--bmc-ip", sim.url,
                "--username", "admin", "--password", "password",
            )
        finally:
            await sim.stop()

        assert code == 0, out
        assert "Reachability:   OK" in out
        assert "Authentication: OK" in out
        assert "Dell" in out
        assert "PowerEdge R750" in out
        assert "Latency:" in out

    async def test_network_failure_exit_3(self):
        code, out = await self._run_cli(
            "bmc", "test", "--bmc-ip", "https://127.0.0.1:1",
            "--username", "admin", "--password", "password",
        )
        assert code == 3, out
        assert "Reachability: FAILED" in out

    async def test_auth_failure_exit_4(self):
        sim = MockSimulator(device="dell-r750", port=0)
        await sim.start()
        try:
            code, out = await self._run_cli(
                "bmc", "test", "--bmc-ip", sim.url,
                "--username", "admin", "--password", "wrong",
            )
        finally:
            await sim.stop()

        assert code == 4, out
        assert "Reachability: OK" in out
        assert "Authentication: FAILED" in out


class TestPeersList:
    """R2a: `harken peers list` — config peers merged with checkpoint state."""

    def _config(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "peers:\n"
            "  - host: 10.0.0.11\n"
            "  - host: 10.0.0.12\n"
            "    port: 5250\n"
            "heartbeat:\n"
            "  secret: s3cret\n"
        )
        return str(path)

    def test_config_only(self, tmp_path):
        result = CliRunner().invoke(
            main, ["peers", "list", "--config", self._config(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert "10.0.0.11" in result.output
        assert "10.0.0.12" in result.output
        assert "5250" in result.output
        assert "UNKNOWN" in result.output

    def test_no_peers(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("agent:\n  name: lonely\n")
        result = CliRunner().invoke(main, ["peers", "list", "--config", str(path)])
        assert result.exit_code == 0
        assert "No peers configured" in result.output

    def test_merged_with_checkpoint(self, tmp_path):
        from harkeniq.models import Peer, PeerStatus
        from harkeniq.state.checkpoint import CheckpointManager

        cp_path = str(tmp_path / "cp.db")

        async def _seed():
            cp = CheckpointManager(cp_path)
            try:
                await cp.save_checkpoint(
                    {}, {}, [],
                    [Peer(peer_id="rack-1-srv-2", host="10.0.0.11", port=5150,
                          last_heartbeat="2026-08-19T10:00:00Z",
                          status=PeerStatus.ALIVE)],
                    {}, {},
                )
            finally:
                await cp.close()

        asyncio.run(_seed())

        result = CliRunner().invoke(
            main,
            ["peers", "list", "--config", self._config(tmp_path),
             "--checkpoint", cp_path],
        )
        assert result.exit_code == 0, result.output
        assert "rack-1-srv-2" in result.output
        assert "ALIVE" in result.output
        assert "2026-08-19T10:00:00Z" in result.output
        # Config-only peer still listed as UNKNOWN.
        assert "10.0.0.12" in result.output
        assert "UNKNOWN" in result.output
