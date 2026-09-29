"""S3-E1 pre-merge remediation (A30.38): a TEST-ONLY global safety member is
IMPOSSIBLE to enable through ordinary production configuration.

Codex's review of PR #67 at `6057a24` found the TEST-ONLY probe registered from
`CCConfig.global_safety_test_probe`, which the ordinary loader fills from YAML
and from `HARKEN_CC_GLOBAL_SAFETY_TEST_PROBE`. Fail-closed -- it can only
narrow -- but test machinery one configuration line away from changing
production execution eligibility. This module proves the boundary:

1. no configuration reaches the registry: `CCConfig` has no probe field, and
   YAML, the environment and both together register nothing, a stale key being
   ignored by the loader's ordinary rules;
2. production startup: a REAL `runtime.run()` -- in-process and as
   `python -m harkeniq_cc` -- started with the stale key in its environment
   AND its YAML and the old trigger file present serves an empty registry, a
   clear gate and no WARNING; a fresh interpreter importing the startup path
   sees an empty registry;
3. the test harness (`tests/gate/`) registers the probe explicitly, onto the
   production registry and nothing else, only AFTER the production runtime
   has recorded the production registry, and drives the SHIPPED main;
4. the probe only narrows, fails closed when it raises, and its identity
   reaches no projection;
5. structural guards, so a configuration-to-test-member path cannot come back
   without failing the suite.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import logging
import os
import pathlib
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import httpx
import pytest
import yaml

import harkeniq_cc
from harkeniq_cc import global_safety as G
from harkeniq_cc.autonomy import (
    AUTONOMOUS,
    DENIED,
    NOT_BUDGET_MAPPED,
    REQUIRES_APPROVAL,
    build_autonomy,
)
from harkeniq_cc.config import CCConfig, _ENV_MAP, load_cc_config

from tests.gate import cc_global_safety_probe as H
from tests.unit.cc.s3e1_support import fresh_report, gate_with

ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = pathlib.Path(harkeniq_cc.__file__).parent
GATE_DIR = ROOT / "tests" / "gate"
HARNESS = GATE_DIR / "cc_global_safety_probe.py"
ENTRYPOINT = GATE_DIR / "entrypoint-cc-probe.sh"
OVERRIDE = ROOT / "scripts" / "e2e-compose-gate.override.yml"
GATE_SCRIPT = ROOT / "scripts" / "e2e-compose-gate.sh"
SHIPPED_ENTRYPOINT = ROOT / "deploy" / "full-stack" / "entrypoint-cc.sh"
CC_DOCKERFILE = ROOT / "deploy" / "r2b" / "Dockerfile.cc"

#: A30.37's key, gone from production. It must stay inert wherever it appears.
STALE_ENV = "HARKEN_CC_GLOBAL_SAFETY_TEST_PROBE"
STALE_FIELD = "global_safety_test_probe"

#: Names that would mean the probe, its key, its trigger or its harness had
#: found their way back into shipped code.
FORBIDDEN_IN_PRODUCTION = (
    "TestOnlyProbe",
    STALE_ENV,
    STALE_FIELD,
    H.TEST_PROBE_MEMBER_ID,
    "harken-gate-global-safety-probe",
    "cc_global_safety_probe",
    "entrypoint-cc-probe",
    "tests.gate",
    "tests/gate",
    "harken-test",
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
LOCALS = (AUTONOMOUS, REQUIRES_APPROVAL, DENIED, NOT_BUDGET_MAPPED)
_RANK = {DENIED: 0, NOT_BUDGET_MAPPED: 1, REQUIRES_APPROVAL: 1, AUTONOMOUS: 2}


@pytest.fixture(autouse=True)
def _registry_restored(monkeypatch):
    """Every test starts on the production registry and leaves it there."""
    assert G.active_members() == G.PRODUCTION_MEMBERS == ()
    monkeypatch.setattr(G, "_ACTIVE", G._ACTIVE)
    yield


def _stale_yaml(tmp_path, trigger, **extra) -> str:
    """A YAML file carrying A30.37's key -- and every other name a careless
    edit might try -- beside ordinary settings."""
    data = {
        STALE_FIELD: str(trigger),
        "_ACTIVE": ["probe"],
        "PRODUCTION_MEMBERS": ["probe"],
        "global_safety": {"test_probe": str(trigger)},
        "test_probe": str(trigger),
    }
    data.update(extra)
    path = tmp_path / "cc.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _lab_settings(tmp_path, port: int) -> dict:
    return {
        "insecure": True,
        "tenant_id": "lab",
        "http_host": "127.0.0.1",
        "http_port": port,
        "dsn": f"sqlite+aiosqlite:///{tmp_path}/cc.db",
    }


# ---------------------------------------------------------------------------
# 1. No configuration reaches the registry
# ---------------------------------------------------------------------------


class TestNoConfigurationReachesTheRegistry:
    def test_ordinary_ccconfig_has_no_probe_field(self):
        names = {f.name for f in dataclasses.fields(CCConfig)}
        assert STALE_FIELD not in names
        for name in names:
            for word in ("probe", "global_safety", "safety", "member"):
                assert word not in name.lower(), name
        with pytest.raises(TypeError):
            CCConfig(**{STALE_FIELD: "/tmp/trigger"})

    def test_no_environment_variable_maps_to_the_gate(self):
        assert STALE_ENV not in _ENV_MAP
        fields = {f.name for f in dataclasses.fields(CCConfig)}
        for var, attr in _ENV_MAP.items():
            assert attr in fields, (var, attr)
            for word in ("PROBE", "GLOBAL_SAFETY", "SAFETY", "MEMBER"):
                assert word not in var, var

    def test_yaml_cannot_activate_the_probe(self, tmp_path):
        trigger = tmp_path / "trigger"
        trigger.write_text("")  # the old probe's "constrain every class"
        config = load_cc_config(env={}, yaml_path=_stale_yaml(tmp_path, trigger))
        assert not hasattr(config, STALE_FIELD)
        assert G.active_members() == ()

    def test_the_environment_cannot_activate_the_probe(self, tmp_path):
        trigger = tmp_path / "trigger"
        trigger.write_text("")
        config = load_cc_config(env={STALE_ENV: str(trigger)})
        assert not hasattr(config, STALE_FIELD)
        assert G.active_members() == ()

    def test_yaml_and_environment_together_cannot_activate_it(self, tmp_path):
        trigger = tmp_path / "trigger"
        trigger.write_text("")
        env = {STALE_ENV: str(trigger),
               "HARKEN_CC_CONFIG": _stale_yaml(tmp_path, trigger)}
        config = load_cc_config(env=env)
        assert not hasattr(config, STALE_FIELD)
        assert G.active_members() == ()

    def test_a_stale_key_is_ignored_by_the_ordinary_rules(self, tmp_path):
        """D: ignored, not special-cased -- the configuration built with the
        stale key is EQUAL to the one built without it."""
        trigger = tmp_path / "trigger"
        plain = tmp_path / "plain.yaml"
        plain.write_text(yaml.safe_dump({"tenant_id": "lab"}))
        stale = _stale_yaml(tmp_path, trigger, tenant_id="lab")
        without = load_cc_config(env={}, yaml_path=str(plain))
        with_key = load_cc_config(env={STALE_ENV: str(trigger)}, yaml_path=stale)
        assert dataclasses.asdict(with_key) == dataclasses.asdict(without)

    def test_nothing_is_left_to_configure_the_registry(self):
        for gone in ("configure", "TestOnlyProbeMember", "PROBE_ENV",
                     "PROBE_RAISE", "TEST_PROBE_MEMBER_ID"):
            assert not hasattr(G, gone), gone


# ---------------------------------------------------------------------------
# 2. Production startup, for real
# ---------------------------------------------------------------------------


def _gate_records(caplog):
    return [r for r in caplog.records if r.name == "harkeniq.cc.global_safety"]


class TestProductionStartup:
    async def test_a_real_startup_with_the_stale_key_everywhere_serves_the_empty_registry(
        self, tmp_path, caplog,
    ):
        """A: `runtime.run()` started from a configuration whose environment AND
        YAML carry A30.37's key, with the old trigger file present -- the
        registry stays empty, the gate is clear for every class, startup says
        "production" at INFO and no WARNING is emitted."""
        from harkeniq_cc.runtime import make_state, run

        trigger = tmp_path / "trigger"
        trigger.write_text("")
        yaml_path = _stale_yaml(tmp_path, trigger, **_lab_settings(tmp_path, 0))
        config = load_cc_config(env={STALE_ENV: str(trigger),
                                     "HARKEN_CC_CONFIG": yaml_path})
        caplog.set_level(logging.INFO)
        state = await make_state(config)
        task = asyncio.create_task(run(config, state=state))
        try:
            await asyncio.wait_for(state.started.wait(), timeout=20)
            assert G.active_members() == G.PRODUCTION_MEMBERS == ()
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{state.http_port}", timeout=20,
            ) as client:
                contract = (await client.get("/api/autonomy/")).json()
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseExceptionGroup):
                pass
        rows = contract["action_classes"]
        assert rows
        for row in rows:
            assert row["global_safety"] == {"state": "clear", "reason_codes": []}, row
            assert row["final_execution_eligibility"]["disposition"] == row["disposition"]
        records = _gate_records(caplog)
        assert [r.getMessage() for r in records] == [
            "global safety gate: production registry (0 members)"
        ]
        assert all(r.levelno == logging.INFO for r in records)
        assert "TEST-ONLY" not in caplog.text
        assert "NON-PRODUCTION" not in caplog.text

    def test_a_non_production_registry_is_announced_loudly(self, monkeypatch, caplog):
        """Production cannot produce this; if anything ever installs a member
        in-process, startup says so at WARNING, naming it."""

        class Installed:
            member_id = "installed_member"

        monkeypatch.setattr(G, "_ACTIVE", (Installed(),))
        with caplog.at_level(logging.INFO, logger="harkeniq.cc.global_safety"):
            G.announce_registry()
        (record,) = _gate_records(caplog)
        assert record.levelno == logging.WARNING
        assert "NON-PRODUCTION registry" in record.getMessage()
        assert "installed_member" in record.getMessage()

    def test_a_fresh_interpreter_on_the_startup_path_sees_an_empty_registry(self, tmp_path):
        trigger = tmp_path / "trigger"
        trigger.write_text("")
        env = dict(os.environ, **{STALE_ENV: str(trigger),
                                  "HARKEN_CC_CONFIG": _stale_yaml(tmp_path, trigger)})
        env.pop("PYTHONPATH", None)
        script = (
            "import json\n"
            "import harkeniq_cc.__main__, harkeniq_cc.runtime\n"
            "from harkeniq_cc.config import load_cc_config\n"
            "from harkeniq_cc import global_safety as G\n"
            "c = load_cc_config()\n"
            "print(json.dumps({'members': len(G.active_members()),\n"
            "  'production': G.active_members() is G.PRODUCTION_MEMBERS,\n"
            f"  'field': hasattr(c, {STALE_FIELD!r}),\n"
            "  'configure': hasattr(G, 'configure'),\n"
            "  'probe': hasattr(G, 'TestOnlyProbeMember')}))\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", script], env=env, cwd=str(tmp_path),
            capture_output=True, text=True, timeout=60, check=True,
        )
        assert json.loads(out.stdout.strip().splitlines()[-1]) == {
            "members": 0, "production": True, "field": False,
            "configure": False, "probe": False,
        }

    def test_the_shipped_main_with_the_stale_key_everywhere_never_constrains(self, tmp_path):
        """`python -m harkeniq_cc` -- the shipped entrypoint's own command --
        with the stale key in its environment and its YAML and the trigger
        present: every class clear, no TEST-ONLY or NON-PRODUCTION line."""
        trigger = tmp_path / "trigger"
        trigger.write_text("")
        port = _free_port()
        yaml_path = _stale_yaml(tmp_path, trigger, **_lab_settings(tmp_path, port))
        env = dict(os.environ, **{STALE_ENV: str(trigger), "HARKEN_CC_CONFIG": yaml_path})
        env.pop("PYTHONPATH", None)
        contract, log = _serve_and_read(
            [sys.executable, "-m", "harkeniq_cc"], env, tmp_path, port,
        )
        for row in contract["action_classes"]:
            assert row["global_safety"]["state"] == "clear", row
        assert "global safety gate: production registry (0 members)" in log
        assert "TEST-ONLY" not in log and "NON-PRODUCTION" not in log


def _serve_and_read(cmd, env, tmp_path, port, *, after_start=None):
    """Start a Central Command process, read /api/autonomy/, stop it."""
    log_path = tmp_path / "cc.log"
    with open(log_path, "w") as log_fh:
        proc = subprocess.Popen(cmd, env=env, cwd=str(tmp_path),
                                stdout=log_fh, stderr=subprocess.STDOUT)
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 60
            while True:
                assert proc.poll() is None, log_path.read_text()
                try:
                    if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.25)
            if after_start is not None:
                after_start(base)
            contract = httpx.get(f"{base}/api/autonomy/", timeout=20).json()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    return contract, log_path.read_text()


# ---------------------------------------------------------------------------
# 3. The test harness registers it -- explicitly, and only it
# ---------------------------------------------------------------------------


class TestTheHarness:
    def test_install_registers_the_probe_onto_the_production_registry(
        self, tmp_path, caplog,
    ):
        with caplog.at_level(logging.WARNING, logger="harkeniq.test.global_safety_probe"):
            members = H.install(str(tmp_path / "trigger"))
        assert members == G.active_members()
        assert len(members) == len(G.PRODUCTION_MEMBERS) + 1
        assert members[: len(G.PRODUCTION_MEMBERS)] == G.PRODUCTION_MEMBERS
        assert isinstance(members[-1], H.TestOnlyProbeMember)
        assert "TEST-ONLY global safety probe INSTALLED" in caplog.text

    def test_install_refuses_a_registry_that_is_not_the_production_one(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setattr(G, "_ACTIVE", (object(),))
        with pytest.raises(RuntimeError, match="not the production one"):
            H.install(str(tmp_path / "trigger"))

    @pytest.mark.parametrize("trigger", ["", "   "])
    def test_install_needs_a_trigger(self, trigger):
        with pytest.raises(ValueError):
            H.install(trigger)
        assert G.active_members() == ()

    async def test_the_harness_installs_only_after_the_production_runtime_started(
        self, tmp_path, caplog,
    ):
        """1 -> 2: the production runtime records the PRODUCTION registry at
        startup; the harness installs the probe afterwards, onto it."""
        trigger = tmp_path / "trigger"
        config = CCConfig(**_lab_settings(tmp_path, 0))
        caplog.set_level(logging.INFO)
        task = asyncio.create_task(H.serve_then_install(config, str(trigger)))
        try:
            deadline = time.monotonic() + 20
            while G.active_members() == ():
                assert not task.done(), task
                assert time.monotonic() < deadline
                await asyncio.sleep(0.05)
            (probe,) = G.active_members()
            assert isinstance(probe, H.TestOnlyProbeMember)
            assert probe.trigger_path == str(trigger)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseExceptionGroup):
                pass
        messages = [r.getMessage() for r in caplog.records]
        announced = messages.index("global safety gate: production registry (0 members)")
        installed = next(i for i, m in enumerate(messages)
                         if m.startswith("TEST-ONLY global safety probe INSTALLED"))
        assert announced < installed
        assert not any("NON-PRODUCTION" in m for m in messages)

    async def test_a_failed_install_stops_the_harness(self, tmp_path, monkeypatch):
        """A harness that could not install must not go on serving as if the
        proof's member were there."""
        monkeypatch.setattr(G, "_ACTIVE", (object(),))
        config = CCConfig(**_lab_settings(tmp_path, 0))
        with pytest.raises(RuntimeError, match="not the production one"):
            await asyncio.wait_for(
                H.serve_then_install(config, str(tmp_path / "trigger")), timeout=30,
            )

    def test_the_harness_process_constrains_on_its_trigger(self, tmp_path):
        """The harness exactly as the gate runs it, minus the shell wrapper:
        the shipped main, the production runtime, the probe installed after
        startup, the trigger file driving the constraint."""
        trigger = tmp_path / "trigger"
        port = _free_port()
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        for key, value in _lab_settings(tmp_path, port).items():
            env[f"HARKEN_CC_{key.upper()}"] = str(value).lower() if value is True else str(value)

        def constrain(base):
            deadline = time.monotonic() + 30
            while "TEST-ONLY global safety probe INSTALLED" not in (tmp_path / "cc.log").read_text():
                assert time.monotonic() < deadline
                time.sleep(0.1)
            trigger.write_text("BMC_RESET\n")

        contract, log = _serve_and_read(
            [sys.executable, str(HARNESS), str(trigger)], env, tmp_path, port,
            after_start=constrain,
        )
        rows = {r["action_type"]: r for r in contract["action_classes"]}
        assert rows["BMC_RESET"]["global_safety"] == {
            "state": "constrained", "reason_codes": [G.GLOBAL_SAFETY_CONSTRAINT],
        }
        assert rows["BMC_RESET"]["final_execution_eligibility"] == {
            "disposition": DENIED, "reason_codes": [G.GLOBAL_SAFETY_CONSTRAINT],
        }
        for action, row in rows.items():
            if action != "BMC_RESET":
                assert row["global_safety"]["state"] == "clear", row
        text = json.dumps(contract)
        assert H.TEST_PROBE_MEMBER_ID not in text and str(trigger) not in text
        assert log.index("global safety gate: production registry (0 members)") < \
            log.index("TEST-ONLY global safety probe INSTALLED")

    def test_the_harness_needs_exactly_one_trigger_argument(self):
        assert H.main([]) == 2
        assert H.main([""]) == 2
        assert H.main(["a", "b"]) == 2
        assert G.active_members() == ()

    def test_the_shipped_main_calls_run_by_its_module_global_name(self):
        """The harness's one substitution is `harkeniq_cc.__main__.run`; it is
        honoured only while the shipped main() calls `run(config)` by name."""
        import harkeniq_cc.__main__ as shipped

        tree = ast.parse(pathlib.Path(shipped.__file__).read_text())
        main = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "run"]
        assert len(calls) == 1
        assert any(isinstance(n, ast.ImportFrom) and n.module == "harkeniq_cc.runtime"
                   and any(a.name == "run" and a.asname is None for a in n.names)
                   for n in tree.body)


class TestTheEntrypointWrapper:
    def test_it_is_the_shipped_entrypoint_with_only_its_final_line_replaced(self):
        derived = subprocess.run(
            ["sh", str(ENTRYPOINT), "/tmp/harken-gate-global-safety-probe",
             str(SHIPPED_ENTRYPOINT), "--print"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        shipped = SHIPPED_ENTRYPOINT.read_text().splitlines()
        assert derived[:-1] == shipped[:-1]
        assert shipped[-1] == "exec python -m harkeniq_cc"
        assert derived[-1] == (
            "exec python /opt/harken-test/cc_global_safety_probe.py "
            "/tmp/harken-gate-global-safety-probe"
        )
        assert any(line.strip() == "alembic upgrade head" for line in derived)

    def test_it_refuses_an_entrypoint_it_would_not_be_replacing_a_line_of(self, tmp_path):
        for body in ("#!/bin/sh\nexec python -m harkeniq_cc\necho late\n",
                     "#!/bin/sh\nexec python -m harkeniq_cc\nexec python -m harkeniq_cc\n",
                     "#!/bin/sh\nexec python -m something_else\n"):
            bad = tmp_path / "entrypoint.sh"
            bad.write_text(body)
            out = subprocess.run(["sh", str(ENTRYPOINT), "/t", str(bad), "--print"],
                                 capture_output=True, text=True)
            assert out.returncode != 0, body

    def test_the_gate_override_runs_the_wrapper_and_sets_no_environment(self):
        service = yaml.safe_load(OVERRIDE.read_text())["services"]["central-command"]
        assert set(service) == {"volumes", "entrypoint"}, service
        assert service["volumes"] == ["../../tests/gate:/opt/harken-test:ro"]
        assert service["entrypoint"] == [
            "/bin/sh", "/opt/harken-test/entrypoint-cc-probe.sh",
            "/tmp/harken-gate-global-safety-probe",
        ]
        assert "e2e-compose-gate.override.yml" in GATE_SCRIPT.read_text()


# ---------------------------------------------------------------------------
# 4. The probe only narrows, fails closed, and never names itself
# ---------------------------------------------------------------------------


TRIGGER_STATES = {
    "absent": None,
    "empty": "",
    "listed": "SEL_CLEAR\n",
    "unlisted": "BMC_RESET\n",
    "raise": "raise\n",
}


def _probe_at(tmp_path, content):
    trigger = tmp_path / "trigger"
    if content is not None:
        trigger.write_text(content)
    return H.TestOnlyProbeMember(str(trigger)), trigger


class TestTheProbe:
    def test_the_probe_semantics(self, tmp_path):
        trigger = tmp_path / "probe"
        probe = H.TestOnlyProbeMember(str(trigger))
        ctx = G.GlobalSafetyContext("t", "SEL_CLEAR", "s1", (), NOW)
        assert probe.evaluate(ctx) is G.MemberVerdict.CLEAR          # absent
        trigger.write_text("")
        assert probe.evaluate(ctx) is G.MemberVerdict.CONSTRAIN      # every class
        trigger.write_text("BMC_RESET\n")
        assert probe.evaluate(ctx) is G.MemberVerdict.CLEAR          # not listed
        trigger.write_text("sel_clear\n")
        assert probe.evaluate(ctx) is G.MemberVerdict.CONSTRAIN      # listed
        trigger.write_text("raise\n")
        with pytest.raises(RuntimeError):
            probe.evaluate(ctx)

    @pytest.mark.parametrize("state", sorted(TRIGGER_STATES))
    @pytest.mark.parametrize("local", LOCALS)
    def test_it_only_narrows(self, tmp_path, state, local):
        probe, _ = _probe_at(tmp_path, TRIGGER_STATES[state])
        verdict = G.GlobalSafetyGate(members=(probe,), tenant_id="t", estate=()).verdict(
            "SEL_CLEAR", "s1")
        final = G.final_execution_eligibility(local, verdict)
        assert _RANK[final] <= _RANK[local]
        assert final in (local, DENIED)
        if verdict.clear:
            assert final == local
        expected_clear = state in ("absent", "unlisted")
        assert verdict.clear is expected_clear

    def test_a_probe_that_raises_fails_closed(self, tmp_path):
        probe, _ = _probe_at(tmp_path, "raise\n")
        verdict = G.GlobalSafetyGate(members=(probe,), tenant_id="t", estate=()).verdict(
            "SEL_CLEAR")
        assert verdict.state == G.STATE_UNKNOWN and not verdict.clear
        assert verdict.as_dict() == {
            "state": "unknown", "reason_codes": [G.GLOBAL_SAFETY_CONSTRAINT],
        }

    @pytest.mark.parametrize("state", ["empty", "listed", "raise"])
    def test_its_identity_reaches_no_projection(self, tmp_path, state):
        probe, trigger = _probe_at(tmp_path, TRIGGER_STATES[state])
        contract = build_autonomy(
            tenant_id="t", actor_id="op-agent:a@v1", actor_species="agent",
            permissions=["fleet.view"],
            budgets=[NS(device_type="*", level=2, budget_limit=10,
                        budget_period="daily", actions_used=0)],
            stop_switch=NS(active=False, changed_by="", updated_at=NOW),
            outcomes=[], safety_rows=[fresh_report("s1", now=NOW)],
            sites=[NS(id="s1", site_name="DC-s1")], now=NOW, action_type="SEL_CLEAR",
            global_safety=gate_with(probe, safety_rows=[fresh_report("s1", now=NOW)],
                                    target_site_id="s1", now=NOW),
        )
        text = json.dumps(contract, default=str)
        row = next(c for c in contract["action_classes"] if c["action_type"] == "SEL_CLEAR")
        assert row["global_safety"]["reason_codes"] == [G.GLOBAL_SAFETY_CONSTRAINT]
        for secret in (H.TEST_PROBE_MEMBER_ID, str(trigger), "TestOnlyProbe",
                       "TEST-ONLY probe asked to fail"):
            assert secret not in text, secret


# ---------------------------------------------------------------------------
# 5. Structural guards -- the path cannot come back quietly
# ---------------------------------------------------------------------------


def _package_trees():
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path, ast.parse(path.read_text())


def _generated(path: pathlib.Path) -> bool:
    """Build output a local `pip install -e .` writes into a source tree, and
    nothing else: setuptools' `*.egg-info` (gitignored) and `__pycache__`.
    Not source and never shipped -- CI's root `SOURCES.txt` lists every
    tracked file, `tests/gate/` included, while the image's own egg-info is
    generated from a build context that has no `tests/` at all."""
    return "__pycache__" in path.parts or any(
        part.endswith(".egg-info") for part in path.parts
    )


def _production_files():
    for base in [ROOT / "src", *sorted((ROOT / "services").glob("*/src"))]:
        for path in sorted(base.rglob("*")):
            if _generated(path):
                continue
            if path.is_file() and path.suffix in {
                ".py", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".json", ".txt",
            }:
                yield path


class TestStructuralGuards:
    def test_the_registry_is_bound_once_to_the_production_members(self):
        bindings, globals_, references = [], [], []
        for path, tree in _package_trees():
            rel = str(path.relative_to(PACKAGE))
            for node in ast.walk(tree):
                if isinstance(node, ast.Global) and "_ACTIVE" in node.names:
                    globals_.append(rel)
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    targets = [node.target]
                for target in targets:
                    for sub in ast.walk(target):
                        if (isinstance(sub, ast.Name) and sub.id == "_ACTIVE") or (
                            isinstance(sub, ast.Attribute) and sub.attr == "_ACTIVE"
                        ):
                            bindings.append((rel, node, node in tree.body))
                if (isinstance(node, ast.Name) and node.id == "_ACTIVE") or (
                    isinstance(node, ast.Attribute) and node.attr == "_ACTIVE"
                ):
                    references.append(rel)
                if isinstance(node, ast.Call) and any(
                    isinstance(a, ast.Constant) and a.value == "_ACTIVE"
                    for a in node.args
                ):
                    references.append(rel + ":setattr")
        assert globals_ == [], globals_
        assert len(bindings) == 1, bindings
        rel, node, module_level = bindings[0]
        assert rel == "global_safety.py"
        assert module_level, "the one binding is module-level"
        assert isinstance(node, ast.AnnAssign)
        assert isinstance(node.value, ast.Name) and node.value.id == "PRODUCTION_MEMBERS"
        assert set(references) == {"global_safety.py"}, references

    def test_the_production_members_are_an_empty_literal(self):
        tree = ast.parse((PACKAGE / "global_safety.py").read_text())
        (node,) = [n for n in tree.body if isinstance(n, ast.AnnAssign)
                   and getattr(n.target, "id", "") == "PRODUCTION_MEMBERS"]
        assert isinstance(node.value, ast.Tuple) and node.value.elts == []

    def test_the_gate_module_performs_no_environment_file_or_dynamic_import_io(self):
        tree = ast.parse((PACKAGE / "global_safety.py").read_text())
        allowed = {"__future__", "enum", "logging", "dataclasses", "datetime",
                   "typing", "harkeniq_cc.autonomy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name in allowed, alias.name
            if isinstance(node, ast.ImportFrom):
                assert node.module in allowed, node.module
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                assert name not in {"open", "getenv", "__import__", "import_module",
                                    "exec", "eval", "compile", "load_cc_config",
                                    "safe_load", "setattr"}, name
            if isinstance(node, ast.Name):
                assert node.id not in {"os", "environ", "importlib", "sys"}, node.id

    def test_configuration_and_the_gate_never_import_each_other(self):
        config = ast.parse((PACKAGE / "config.py").read_text())
        gate = ast.parse((PACKAGE / "global_safety.py").read_text())
        for tree, forbidden in ((config, "global_safety"), (gate, "config")):
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert forbidden not in (node.module or ""), node.module
                    assert all(a.name != forbidden for a in node.names)
                if isinstance(node, ast.Import):
                    assert all(forbidden not in a.name for a in node.names)

    def test_startup_only_reads_the_registry(self):
        """No module of the package WRITES into `global_safety`, and runtime
        asks it one thing: to announce itself."""
        for path, tree in _package_trees():
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Attribute):
                            owner = getattr(target.value, "id", "")
                            assert owner not in {"global_safety", "G"}, (path, target.attr)
        runtime = ast.parse((PACKAGE / "runtime.py").read_text())
        used = {n.attr for n in ast.walk(runtime) if isinstance(n, ast.Attribute)
                and getattr(n.value, "id", "") == "global_safety"}
        assert used == {"announce_registry"}, used

    def test_no_production_source_names_the_probe_its_key_or_its_harness(self):
        offenders = []
        for path in _production_files():
            text = path.read_text(errors="ignore")
            for needle in FORBIDDEN_IN_PRODUCTION:
                if needle in text:
                    offenders.append((str(path.relative_to(ROOT)), needle))
        assert offenders == []

    def test_only_generated_build_metadata_is_skipped(self):
        """The skip is exactly setuptools' egg-info and bytecode caches: no
        tracked file lives under either, so no source can hide there."""
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True,
            check=True,
        ).stdout.splitlines()
        assert tracked
        assert [f for f in tracked if _generated(pathlib.Path(f))] == []
        assert _generated(ROOT / "src" / "harkeniq.egg-info" / "SOURCES.txt")
        assert not _generated(PACKAGE / "global_safety.py")

    def test_the_harness_is_not_part_of_any_shipped_package(self):
        import importlib.util

        assert importlib.util.find_spec("harkeniq_cc.cc_global_safety_probe") is None
        for src in [ROOT / "src", *(ROOT / "services").glob("*/src")]:
            assert not HARNESS.resolve().is_relative_to(src.resolve())

    def test_the_image_copies_no_tests(self):
        for line in CC_DOCKERFILE.read_text().splitlines():
            if line.strip().upper().startswith(("COPY", "ADD")):
                assert "tests" not in line, line

    def test_no_shipped_compose_file_carries_the_key_the_mount_or_an_entrypoint(self):
        for compose in sorted((ROOT / "deploy").rglob("*compose*.y*ml")):
            text = compose.read_text()
            for needle in (STALE_ENV, "tests/gate", "harken-test",
                           "cc_global_safety_probe", "entrypoint-cc-probe"):
                assert needle not in text, (compose, needle)
            services = (yaml.safe_load(text) or {}).get("services") or {}
            cc = services.get("central-command")
            if cc is None:
                continue
            assert "entrypoint" not in cc and "command" not in cc, compose
            for volume in cc.get("volumes") or []:
                assert "tests" not in str(volume), (compose, volume)
            env = cc.get("environment") or {}
            keys = env if isinstance(env, dict) else [e.split("=", 1)[0] for e in env]
            assert not any("PROBE" in k or "GLOBAL_SAFETY" in k for k in keys), compose

    def test_only_the_gate_names_the_harness_outside_the_test_tree(self):
        """Nothing under deploy/, scripts/, .github/ or a shipped source tree
        names the harness except the gate's own override and the gate script
        that asserts on it; the stale key appears only where the gate asserts
        it is ABSENT."""
        allowed = {OVERRIDE, GATE_SCRIPT}
        for base in ("deploy", "scripts", ".github", "src", "services"):
            for path in (ROOT / base).rglob("*"):
                if not path.is_file() or path in allowed or _generated(path):
                    continue
                text = path.read_text(errors="ignore")
                for needle in ("cc_global_safety_probe", "entrypoint-cc-probe",
                               "tests/gate", STALE_ENV):
                    assert needle not in text, (path, needle)
        assert STALE_ENV not in OVERRIDE.read_text()
