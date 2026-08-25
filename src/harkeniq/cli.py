"""HarkenIQ CLI entry point."""

import click

from harkeniq import __version__


@click.group()
def main():
    """HarkenIQ — autonomous hardware diagnostics."""
    pass


@main.command()
def version():
    """Print version information."""
    click.echo(f"harkeniq {__version__}")


@main.group()
def agent():
    """Agent lifecycle commands."""
    pass


DEFAULT_PIDFILE = "~/.harkeniq/agent.pid"


def _pidfile_path(pidfile):
    from pathlib import Path

    return Path(pidfile or DEFAULT_PIDFILE).expanduser()


@agent.command()
@click.option("--config", "config_path", default=None, help="Config file path")
@click.option("--bmc-ip", default=None, help="BMC IP address or URL")
@click.option("--bmc-user", default=None, help="BMC username")
@click.option("--bmc-pass", default=None, help="BMC password")
@click.option("--peers", "peers_opt", default=None,
              help="Comma-separated peer list: host[:port],host[:port]")
@click.option("--site-manager-ip", default=None, help="Site Manager host")
@click.option("--checkpoint", "checkpoint_path", default=None, help="Checkpoint DB path")
@click.option("--log-level", default=None,
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]))
@click.option("--tui", is_flag=True, help="Run with the terminal console UI")
@click.option("--foreground", is_flag=True, default=False,
              help="Run in the foreground (R1 always runs foreground; use systemd to daemonize)")
@click.option("--pidfile", default=None, help=f"Pidfile path [default: {DEFAULT_PIDFILE}]")
@click.pass_context
def start(ctx, config_path, bmc_ip, bmc_user, bmc_pass, peers_opt,
          site_manager_ip, checkpoint_path, log_level, tui, foreground, pidfile):
    """Start the HarkenIQ agent (runs until SIGTERM/SIGINT)."""
    import asyncio
    import logging
    import os

    from harkeniq.config import load_config, validate_config
    from harkeniq.errors import ConfigError, HarkenIQError

    overrides = {}
    if bmc_ip:
        overrides.setdefault("bmc", {})["host"] = bmc_ip
    if bmc_user:
        overrides.setdefault("bmc", {})["username"] = bmc_user
    if bmc_pass:
        overrides.setdefault("bmc", {})["password"] = bmc_pass
    if site_manager_ip:
        overrides.setdefault("site_manager", {})["host"] = site_manager_ip
    if checkpoint_path:
        overrides.setdefault("checkpoint", {})["path"] = checkpoint_path
    if log_level:
        overrides.setdefault("agent", {})["log_level"] = log_level
    if peers_opt:
        peers = []
        for entry in peers_opt.split(","):
            entry = entry.strip()
            if not entry:
                continue
            host, _, port = entry.partition(":")
            peers.append({"host": host, "port": int(port) if port else 5150})
        overrides["peers"] = peers

    try:
        cfg = load_config(config_path, cli_overrides=overrides)
    except ConfigError as e:
        raise click.ClickException(str(e))
    errors = validate_config(cfg)
    if errors:
        for err in errors:
            click.echo(f"ERROR {err}", err=True)
        ctx.exit(3)

    logging.basicConfig(
        level=getattr(logging, cfg["agent"]["log_level"]),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    pid_path = _pidfile_path(pidfile)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))

    async def _run():
        from harkeniq.agent import Agent

        agent_obj = Agent(cfg)
        await agent_obj.start()
        if tui:
            from harkeniq.reporting.console import ConsoleUI

            ui = ConsoleUI(agent_obj)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(agent_obj.run())
                tg.create_task(ui.run())
        else:
            await agent_obj.run()

    try:
        asyncio.run(_run())
    except HarkenIQError as e:
        raise click.ClickException(str(e))
    finally:
        pid_path.unlink(missing_ok=True)


@agent.command()
@click.option("--pidfile", default=None, help=f"Pidfile path [default: {DEFAULT_PIDFILE}]")
def stop(pidfile):
    """Stop a running agent (sends SIGTERM via pidfile)."""
    import os
    import signal as signal_mod

    pid_path = _pidfile_path(pidfile)
    if not pid_path.is_file():
        raise click.ClickException(f"No pidfile at {pid_path} (agent not running?)")
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, signal_mod.SIGTERM)
    except (ValueError, ProcessLookupError) as e:
        pid_path.unlink(missing_ok=True)
        raise click.ClickException(f"Stale pidfile {pid_path}: {e}")
    click.echo(f"Sent SIGTERM to agent (pid {pid})")


@agent.command()
@click.option("--checkpoint", "checkpoint_path", required=True, help="Checkpoint DB path")
def status(checkpoint_path):
    """Show last known agent status from the checkpoint DB."""
    async def _load(cp):
        return await cp.load_checkpoint(), await cp.load_actions()

    state, actions = _run_on_checkpoint(checkpoint_path, _load)
    meta = state["agent_meta"]
    click.echo(f"Agent:   {meta.get('agent_id', '?')}")
    click.echo(f"State:   {meta.get('state', '?')}")
    click.echo(f"Sensors: {len(state['sensor_readings'])} readings, "
               f"{len(state['baselines'])} baselines")
    for verdict in state["verdicts"]:
        click.echo(f"  {verdict.severity.value:<9} {verdict.sensor_id}: {verdict.message}")
    for peer in state["peers"]:
        click.echo(f"Peer:    {peer.peer_id or peer.host} {peer.status.value} "
                   f"(last heartbeat {peer.last_heartbeat or 'never'})")
    from harkeniq.models import ActionStatus

    pending = [a for a in actions if a.status == ActionStatus.PENDING]
    if pending:
        click.echo(f"Pending actions: {len(pending)} (see 'harken action list')")


@agent.command(name="checkpoint")
@click.option("--checkpoint", "checkpoint_path", required=True, help="Checkpoint DB path")
def agent_checkpoint(checkpoint_path):
    """Show checkpoint DB contents summary."""
    async def _load(cp):
        counts = {}
        for table in ("sensor_readings", "baselines", "verdicts", "peers",
                      "actions", "audit_log"):
            counts[table] = cp._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()["n"]
        updated = cp._conn.execute(
            "SELECT MAX(updated_at) AS ts FROM agent_meta"
        ).fetchone()["ts"]
        return counts, updated

    counts, updated = _run_on_checkpoint(checkpoint_path, _load)
    click.echo(f"Checkpoint: {checkpoint_path}")
    click.echo(f"Last written: {updated or 'never'}")
    for table, n in counts.items():
        click.echo(f"  {table:<16} {n}")


@main.command()
@click.option("--config", "config_path", default=None, help="Config file path")
@click.option("--bmc-ip", default=None, help="BMC IP/URL (overrides config)")
@click.option("--bmc-user", default=None, help="BMC username")
@click.option("--bmc-pass", default=None, help="BMC password")
@click.option("--verbose", is_flag=True, help="Show healthy subsystems too")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.pass_context
def diagnose(ctx, config_path, bmc_ip, bmc_user, bmc_pass, verbose, json_output):
    """One-shot diagnosis: poll BMC, evaluate skills, print structured results.

    Exit codes (Doc 06 §6.3, Nagios-compatible): 0 healthy, 1 warning,
    2 critical, 3 unknown/unreachable.
    """
    import asyncio
    import logging

    from harkeniq.config import load_config, validate_config
    from harkeniq.errors import ConfigError, HarkenIQError

    logging.basicConfig(level=logging.WARNING)

    overrides: dict = {}
    if bmc_ip:
        overrides.setdefault("bmc", {})["host"] = bmc_ip
    if bmc_user:
        overrides.setdefault("bmc", {})["username"] = bmc_user
    if bmc_pass:
        overrides.setdefault("bmc", {})["password"] = bmc_pass

    try:
        config = load_config(config_path, cli_overrides=overrides)
    except ConfigError as e:
        click.echo(f"UNKNOWN: {e}", err=True)
        ctx.exit(3)
    errors = validate_config(config)
    if errors:
        click.echo("UNKNOWN: " + "; ".join(errors), err=True)
        ctx.exit(3)

    async def _run():
        from harkeniq.autonomy.diagnosis import ConfidenceDimension, Diagnosis
        from harkeniq.autonomy.tier import TierLevel
        from harkeniq.models import VerdictSeverity
        from harkeniq.poller import Poller
        from harkeniq.redfish.client import RedfishClient
        from harkeniq.skills.engine import SkillEngine
        from harkeniq.skills.loader import load_skills
        from harkeniq.skills.trending import TrendingEngine

        bmc = config["bmc"]
        client = RedfishClient(host=bmc["host"], verify_ssl=bmc.get("verify_ssl", False))
        try:
            await client.connect(bmc.get("username", ""), bmc.get("password", ""))
            poller = Poller(client)
            identity = await poller.detect()
            device = await poller.poll_sensors()

            skills = load_skills(
                (config.get("skills") or {}).get("directory", "skills")
            )
            trending = TrendingEngine(config)
            # One-shot: a single observation is all there is — debounce
            # windows (built for the continuous poll loop) would swallow
            # every first-sight fault and report healthy.
            one_shot_debounce = {
                "critical": [1, 1], "warning": [1, 1], "recovery": [1, 1],
            }
            engine = SkillEngine(
                list(skills.values()), one_shot_debounce, trending
            )
            verdicts = await engine.evaluate(device)
        finally:
            await client.close()

        # One proposed action per sensor, when a matched rule carried one.
        actions_by_sensor = {
            a.sensor_id: a.type.value for a in engine.get_pending_actions()
        }

        diagnoses = []
        for v in verdicts:
            if v.severity == VerdictSeverity.HEALTHY and not verbose:
                continue
            evidence = v.evidence[0] if v.evidence else None
            baseline_confidence = (
                evidence.baseline_confidence if evidence is not None else 0.0
            )
            d = Diagnosis(
                device_id=identity.service_tag or identity.system_id or "unknown",
                component=v.sensor_id,
                summary=v.message,
                tier=TierLevel.T2,  # standalone one-shot = no peers = T2
            )
            d.add_evidence("redfish", v.sensor_id, v.severity.value)
            # Honest confidence: a one-shot poll has no learned baseline, so
            # baseline confidence is what the engine actually had — never
            # asserted as 1.0 (that was the pre-QA bug's claim).
            d.confidence = [
                ConfidenceDimension(
                    name="baseline",
                    value=baseline_confidence,
                    detail="one-shot poll; baselines not learned",
                ),
            ]
            d.recommended_action = actions_by_sensor.get(v.sensor_id, "")
            d.add_reasoning_step(
                f"Skill '{v.skill_name}' verdict {v.severity.value}"
            )
            diagnoses.append(d)

        worst = max(
            (v.severity for v in verdicts), default=VerdictSeverity.UNKNOWN
        )
        return identity, diagnoses, worst

    try:
        identity, diagnoses, worst = asyncio.run(_run())
    except HarkenIQError as e:
        click.echo(f"UNKNOWN: {e}", err=True)
        ctx.exit(3)
    except OSError as e:
        click.echo(f"UNKNOWN: BMC unreachable: {e}", err=True)
        ctx.exit(3)

    vendor = {"dell": "Dell", "hpe": "HPE"}.get(identity.vendor, identity.vendor)

    # Doc 06 §6.3 Nagios-compatible exit code from the worst verdict.
    from harkeniq.models import VerdictSeverity as _Sev

    exit_code = {
        _Sev.CRITICAL: 2, _Sev.WARNING: 1,
        _Sev.TRENDING: 0, _Sev.HEALTHY: 0,
    }.get(worst, 3)

    if json_output:
        import json as json_mod
        click.echo(json_mod.dumps({
            "device": {"vendor": vendor, "model": identity.model,
                       "service_tag": identity.service_tag},
            "worst_severity": worst.value,
            "diagnoses": [d.to_dict() for d in diagnoses],
        }, indent=2))
        ctx.exit(exit_code)

    click.echo(f"Device: {vendor} {identity.model} ({identity.service_tag})")
    click.echo(f"Diagnoses: {len(diagnoses)}")
    click.echo()
    if not diagnoses:
        click.echo("  No issues found. All subsystems healthy.")
    for d in diagnoses:
        conf = f"{d.overall_confidence:.0%}" if d.confidence else "?"
        click.echo(f"  [{d.tier.value.upper()}] {d.component}")
        click.echo(f"    {d.summary}")
        click.echo(f"    Confidence: {conf} | Evidence: {len(d.evidence)} | Tier: {d.tier.value}")
        if d.recommended_action:
            click.echo(f"    Action: {d.recommended_action}")
        click.echo()
    ctx.exit(exit_code)


@main.command()
@click.option("--mock/--no-mock", default=True,
              help="Run against the built-in mock simulator (R1: mock only)")
@click.option("--speed", default=1.0, show_default=True, type=float,
              help="Time compression factor (10 = 6-second run)")
@click.option("--plain", is_flag=True, help="Line-mode output instead of the TUI")
def demo(mock, speed, plain):
    """Run the 60-second automated showcase (Doc 09)."""
    import asyncio
    import logging
    import sys

    from harkeniq.errors import HarkenIQError

    if not mock:
        raise click.ClickException("R1 supports --mock only (no real-BMC demo)")
    if speed <= 0:
        raise click.ClickException("--speed must be > 0")
    logging.basicConfig(level=logging.ERROR)

    from harkeniq.demo import DemoRunner

    tui = (not plain) and sys.stdout.isatty()
    try:
        asyncio.run(DemoRunner(speed=speed, tui=tui).run())
    except KeyboardInterrupt:
        pass
    except HarkenIQError as e:
        raise click.ClickException(str(e))


@main.group()
def bmc():
    """BMC utility commands."""
    pass


@bmc.command()
@click.option("--bmc-ip", default=None, help="BMC IP address or URL (auto-detect if omitted)")
@click.option("--port", default=443, type=int, help="BMC HTTPS port")
@click.option("--username", "-u", envvar="HARKEN_BMC_USERNAME", default="admin", show_default=True, help="BMC username [env: HARKEN_BMC_USERNAME]")
@click.option("--password", "-p", envvar="HARKEN_BMC_PASSWORD", required=True, help="BMC password [env: HARKEN_BMC_PASSWORD]")
@click.option("--verify-ssl", is_flag=True, default=False, help="Verify BMC TLS certificate")
def detect(bmc_ip, port, username, password, verify_ssl):
    """Detect BMC vendor and controller generation."""
    import asyncio

    async def _run():
        from harkeniq.redfish.client import RedfishClient
        from harkeniq.redfish.discovery import auto_detect_bmc, detect_identity

        host = bmc_ip
        bmc_port = port
        if host is None:
            click.echo("No --bmc-ip given, probing known BMC addresses...")
            host, bmc_port = await auto_detect_bmc(verify_ssl=verify_ssl)

        client = RedfishClient(host=host, port=bmc_port, verify_ssl=verify_ssl)
        try:
            await client.connect(username, password)
            identity = await detect_identity(client)
        finally:
            await client.close()

        vendor_display = {"dell": "Dell", "hpe": "HPE"}.get(identity.vendor, identity.vendor)
        version = identity.controller_version if identity.controller_version is not None else "?"
        click.echo(f"{vendor_display} {identity.model} ({identity.controller_type}{version})")
        click.echo(f"  Firmware:    {identity.firmware_version}")
        click.echo(f"  Service tag: {identity.service_tag}")
        click.echo(f"  System ID:   {identity.system_id}")
        click.echo(f"  Chassis ID:  {identity.chassis_id}")
        click.echo(f"  Manager ID:  {identity.manager_id}")

    try:
        asyncio.run(_run())
    except Exception as e:
        raise click.ClickException(str(e))


@bmc.command()
@click.option("--bmc-ip", default=None, help="BMC IP address or URL (auto-detect if omitted)")
@click.option("--port", default=443, type=int, help="BMC HTTPS port")
@click.option("--username", "-u", envvar="HARKEN_BMC_USERNAME", default="admin", show_default=True, help="BMC username [env: HARKEN_BMC_USERNAME]")
@click.option("--password", "-p", envvar="HARKEN_BMC_PASSWORD", required=True, help="BMC password [env: HARKEN_BMC_PASSWORD]")
@click.option("--verify-ssl", is_flag=True, default=False, help="Verify BMC TLS certificate")
@click.pass_context
def test(ctx, bmc_ip, port, username, password, verify_ssl):
    """Test BMC connectivity and authentication.

    Exit codes (D10): 0 = OK, 3 = network failure, 4 = auth failure.
    """
    import asyncio
    import time

    from harkeniq.errors import RedfishAuthError, RedfishError

    async def _run():
        from harkeniq.redfish.client import RedfishClient
        from harkeniq.redfish.discovery import auto_detect_bmc, detect_identity

        host = bmc_ip
        bmc_port = port
        if host is None:
            click.echo("No --bmc-ip given, probing known BMC addresses...")
            host, bmc_port = await auto_detect_bmc(verify_ssl=verify_ssl)

        client = RedfishClient(host=host, port=bmc_port, verify_ssl=verify_ssl)
        started = time.monotonic()
        try:
            await client.connect(username, password)
            latency_ms = (time.monotonic() - started) * 1000
            identity = await detect_identity(client)
        finally:
            await client.close()
        return host, bmc_port, latency_ms, identity

    try:
        host, bmc_port, latency_ms, identity = asyncio.run(_run())
    except RedfishAuthError as e:
        click.echo(f"Reachability: OK\nAuthentication: FAILED ({e})", err=True)
        ctx.exit(4)
    except RedfishError as e:
        click.echo(f"Reachability: FAILED ({e})", err=True)
        ctx.exit(3)

    vendor_display = {"dell": "Dell", "hpe": "HPE"}.get(identity.vendor, identity.vendor)
    click.echo(f"Reachability:   OK ({host}:{bmc_port})")
    click.echo(f"Authentication: OK ({username})")
    click.echo(f"Vendor:         {vendor_display}")
    click.echo(f"Model:          {identity.model}")
    click.echo(f"Latency:        {latency_ms:.0f} ms")


@main.group()
def mock():
    """Redfish mock simulator commands."""
    pass


@mock.command(name="start")
@click.option("--device", default="dell-r750", help="Device profile")
@click.option("--port", default=8443, type=int, help="HTTPS port")
@click.option("--host", default="127.0.0.1",
              help="Bind address (0.0.0.0 for container use)")
@click.option("--no-auth", is_flag=True, help="Disable session authentication")
def mock_start(device, port, host, no_auth):
    """Start the Redfish mock simulator (runs until Ctrl+C)."""
    import asyncio

    async def _run():
        from harkeniq.mock.simulator import MockSimulator

        sim = MockSimulator(device=device, port=port, host=host, no_auth=no_auth)
        await sim.start()
        click.echo(f"Mock {device} listening on https://{host}:{sim.port} "
                   f"(auth {'disabled' if no_auth else 'admin:password'})")
        try:
            await asyncio.Event().wait()
        finally:
            await sim.stop()

    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, ValueError) as e:
        if isinstance(e, ValueError):
            raise click.ClickException(str(e))


@mock.command(name="switch")
@click.option("--ports", default=8, type=int, help="Number of front-panel ports")
@click.option("--port", default=50051, type=int, help="gNMI listen port")
@click.option("--host", default="127.0.0.1",
              help="Bind address (0.0.0.0 for container use)")
@click.option("--writable", is_flag=True,
              help="Enable translib write + client_auth none (P6 action testing)")
def mock_switch(ports, port, host, writable):
    """Start the switch simulator's gNMI server (R6-P2; runs until Ctrl+C).

    Serves the P0-fixture-faithful gNMI surface on an insecure channel —
    pair with the agent's `bmc.protocol: gnmi` + `plaintext` for compose
    testing without a real SONiC pod.
    """
    import asyncio

    async def _run():
        from harkeniq.mock.switch_sim import SwitchSimulator

        sim = SwitchSimulator(
            num_ports=ports, port=port, host=host,
            translib_write=writable,
            client_auth="none" if writable else "password",
        )
        await sim.start()
        click.echo(f"Switch simulator gNMI on port {sim.port} "
                   f"({ports} ports, writes {'ON' if writable else 'off'})")
        try:
            while True:
                sim.state.tick(1.0)
                await asyncio.sleep(1.0)
        finally:
            await sim.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


@main.group()
def config():
    """Configuration commands."""
    pass


@config.command()
@click.option("--config", "config_path", default=None, help="Config file path")
@click.option("--no-redact", is_flag=True, help="Show secrets in cleartext")
def show(config_path, no_redact):
    """Print effective configuration (defaults + file + env)."""
    from harkeniq.config import load_config, render_config
    from harkeniq.errors import ConfigError

    try:
        effective = load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e))
    click.echo(render_config(effective, redact=not no_redact), nl=False)


@config.command(name="validate")
@click.option("--config", "config_path", default=None, help="Config file path")
@click.pass_context
def config_validate(ctx, config_path):
    """Validate configuration file. Exit 0 if valid, 3 if not."""
    from harkeniq.config import load_config, validate_config
    from harkeniq.errors import ConfigError

    try:
        effective = load_config(config_path)
    except ConfigError as e:
        click.echo(f"ERROR {e}", err=True)
        ctx.exit(3)
    errors = validate_config(effective)
    if errors:
        for err in errors:
            click.echo(f"ERROR {err}", err=True)
        click.echo(f"{len(errors)} configuration error(s).", err=True)
        ctx.exit(3)
    click.echo("Configuration valid.")


@config.command()
@click.option("--output", "-o", default="config.yaml", show_default=True, help="Output path")
@click.option("--bmc-host", prompt="BMC host (IP or URL)", help="BMC IP address or URL")
@click.option("--bmc-user", prompt="BMC username", default="admin", help="BMC username")
@click.option("--bmc-pass", prompt="BMC password", hide_input=True, help="BMC password")
@click.option("--name", prompt="Agent name", default="", help="Human-readable agent name")
@click.option("--force", is_flag=True, help="Overwrite existing file")
def init(output, bmc_host, bmc_user, bmc_pass, name, force):
    """First-time setup: generate a config.yaml."""
    import copy
    from pathlib import Path

    import yaml

    from harkeniq.config import DEFAULTS

    out = Path(output)
    if out.exists() and not force:
        raise click.ClickException(f"{out} already exists (use --force to overwrite)")
    data = copy.deepcopy(DEFAULTS)
    data["bmc"]["host"] = bmc_host
    data["bmc"]["username"] = bmc_user
    data["bmc"]["password"] = bmc_pass
    data["agent"]["name"] = name
    out.write_text(yaml.safe_dump(data, default_flow_style=None, sort_keys=False))
    click.echo(f"Wrote {out}")


@main.group()
def skills():
    """Skill management commands."""
    pass


def _resolve_skills_dir(directory):
    """Resolve the skills directory: --dir > /etc/harkeniq/skills > bundled ./skills."""
    import os

    if directory:
        return directory
    from harkeniq.skills.loader import DEFAULT_SKILLS_DIR

    if os.path.isdir(DEFAULT_SKILLS_DIR):
        return DEFAULT_SKILLS_DIR
    return "skills"


@skills.command(name="list")
@click.option("--dir", "directory", default=None, help="Skills directory")
def skills_list(directory):
    """List installed skills."""
    from harkeniq.errors import SkillError
    from harkeniq.skills.loader import load_skills

    try:
        loaded = load_skills(_resolve_skills_dir(directory))
    except SkillError as e:
        raise click.ClickException(str(e))
    for name, skill in sorted(loaded.items()):
        click.echo(
            f"{name:<20} target={skill.target:<8} rules={len(skill.rules)} "
            f"trending={len(skill.trending)}  {skill.description}"
        )


@skills.command()
@click.option("--dir", "directory", default=None, help="Skills directory")
@click.pass_context
def validate(ctx, directory):
    """Validate all skill YAML files. Exit 0 if valid, 4 if not (Doc 07 §8.3)."""
    from pathlib import Path

    from harkeniq.errors import SkillError
    from harkeniq.skills.loader import load_skill_file

    skills_dir = Path(_resolve_skills_dir(directory))
    if not skills_dir.is_dir():
        click.echo(f"Skills directory not found: {skills_dir}", err=True)
        ctx.exit(4)

    errors = 0
    count = 0
    for path in sorted(skills_dir.glob("*.yaml")):
        count += 1
        try:
            skill = load_skill_file(path)
            click.echo(f"OK    {path.name} ({skill.name}, target={skill.target}, {len(skill.rules)} rules)")
        except SkillError as e:
            errors += 1
            click.echo(f"ERROR {path.name}: {e}", err=True)

    if count == 0:
        click.echo(f"No skill files found in {skills_dir}", err=True)
        ctx.exit(4)
    if errors:
        click.echo(f"{errors} of {count} skill files invalid.", err=True)
        ctx.exit(4)
    click.echo(f"All {count} skill files valid.")


@main.group()
def peers():
    """Peer management commands."""
    pass


@peers.command(name="list")
@click.option("--config", "config_path", default=None, help="Config file path")
@click.option("--checkpoint", "checkpoint_path", default=None, help="Checkpoint DB path")
def peers_list(config_path, checkpoint_path):
    """Show configured peers and their liveness.

    Merges the static peer list from the config with the last-known peer
    state from the checkpoint database (when given).
    """
    from harkeniq.config import load_config
    from harkeniq.errors import ConfigError

    try:
        effective = load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e))

    hb_port = (effective.get("heartbeat") or {}).get("port", 5150)
    rows = {}  # host -> row dict
    for entry in effective.get("peers") or []:
        host = entry.get("host", "")
        rows[host] = {
            "peer_id": "-",
            "host": host,
            "port": entry.get("port", hb_port),
            "status": "UNKNOWN",
            "last_heartbeat": "-",
        }

    if checkpoint_path:
        async def _load(cp):
            return (await cp.load_checkpoint())["peers"]

        for peer in _run_on_checkpoint(checkpoint_path, _load):
            row = rows.setdefault(peer.host, {"host": peer.host})
            row["peer_id"] = peer.peer_id or "-"
            row["port"] = peer.port
            row["status"] = peer.status.value
            row["last_heartbeat"] = peer.last_heartbeat or "-"

    if not rows:
        click.echo("No peers configured.")
        return

    click.echo(f"{'PEER ID':<24} {'HOST':<20} {'PORT':<6} {'STATUS':<14} LAST HEARTBEAT")
    for host in sorted(rows):
        row = rows[host]
        click.echo(
            f"{row.get('peer_id', '-'):<24} {row['host']:<20} "
            f"{row.get('port', hb_port):<6} {row.get('status', 'UNKNOWN'):<14} "
            f"{row.get('last_heartbeat', '-')}"
        )


@main.group()
def action():
    """Action management commands."""
    pass


def _run_on_checkpoint(checkpoint_path, fn):
    """Open the checkpoint DB, run async fn(checkpoint), and close it."""
    import asyncio

    from harkeniq.errors import ActionError, CheckpointError
    from harkeniq.state.checkpoint import CheckpointManager

    async def _run():
        cp = CheckpointManager(checkpoint_path)
        try:
            return await fn(cp)
        finally:
            await cp.close()

    try:
        return asyncio.run(_run())
    except (ActionError, CheckpointError) as e:
        raise click.ClickException(str(e))


@action.command(name="list")
@click.option("--checkpoint", "checkpoint_path", required=True, help="Checkpoint DB path")
@click.option("--all", "show_all", is_flag=True, help="Include completed/denied actions")
def action_list(checkpoint_path, show_all):
    """Show pending actions (use --all for full history)."""
    from harkeniq.models import ActionStatus

    async def _list(cp):
        return await cp.load_actions()

    actions = _run_on_checkpoint(checkpoint_path, _list)
    if not show_all:
        actions = [a for a in actions if a.status == ActionStatus.PENDING]
    if not actions:
        click.echo("No pending actions." if not show_all else "No actions.")
        return
    for a in actions:
        click.echo(
            f"{a.id}  {a.status.value:<9}  {a.type.value:<20}  "
            f"{a.sensor_id}  ({a.skill_name}, proposed {a.proposed_at})"
        )


def _decide_action(checkpoint_path, action_id, decision, approved_by):
    """Approve or deny a pending action in the checkpoint DB."""
    import json

    from harkeniq.actions.queue import ActionQueue

    async def _decide(cp):
        queue = ActionQueue()
        queue.restore(await cp.load_actions())
        if decision == "approved":
            act = queue.approve(action_id)
        else:
            act = queue.deny(action_id)
        await cp.save_actions([act])
        await cp.save_audit_entry(
            action=act.type.value,
            target=act.params.get("target", "") or act.sensor_id,
            outcome=decision,
            authorization=approved_by,
            evidence_json=json.dumps({"action_id": act.id, "sensor_id": act.sensor_id}),
        )
        return act

    return _run_on_checkpoint(checkpoint_path, _decide)


@action.command()
@click.argument("action_id")
@click.option("--checkpoint", "checkpoint_path", required=True, help="Checkpoint DB path")
@click.option("--by", "approved_by", default="operator", show_default=True, help="Approver identity for the audit log")
def approve(action_id, checkpoint_path, approved_by):
    """Approve a pending action (takes effect on the agent's next cycle)."""
    act = _decide_action(checkpoint_path, action_id, "approved", approved_by)
    click.echo(f"Approved {act.id} ({act.type.value} on {act.sensor_id})")


@action.command()
@click.argument("action_id")
@click.option("--checkpoint", "checkpoint_path", required=True, help="Checkpoint DB path")
@click.option("--by", "approved_by", default="operator", show_default=True, help="Denier identity for the audit log")
def deny(action_id, checkpoint_path, approved_by):
    """Deny a pending action (denied is final)."""
    act = _decide_action(checkpoint_path, action_id, "denied", approved_by)
    click.echo(f"Denied {act.id} ({act.type.value} on {act.sensor_id})")
