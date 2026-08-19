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


@agent.command()
def start():
    """Start the HarkenIQ agent."""
    click.echo("Agent start not yet implemented.")


@agent.command()
def stop():
    """Stop the HarkenIQ agent."""
    click.echo("Agent stop not yet implemented.")


@agent.command()
def status():
    """Show agent status."""
    click.echo("Agent status not yet implemented.")


@main.command()
def diagnose():
    """One-shot diagnosis: poll BMC, evaluate skills, print results."""
    click.echo("Diagnose not yet implemented.")


@main.command()
def demo():
    """Run the 60-second automated showcase."""
    click.echo("Demo not yet implemented.")


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
@click.option("--bmc-ip", default=None, help="BMC IP address")
def test(bmc_ip):
    """Test BMC connectivity and authentication."""
    click.echo("BMC test not yet implemented.")


@main.group()
def mock():
    """Redfish mock simulator commands."""
    pass


@mock.command()
@click.option("--device", default="dell-r750", help="Device profile")
@click.option("--port", default=8443, type=int, help="HTTPS port")
@click.option("--no-auth", is_flag=True, help="Disable session authentication")
def start(device, port, no_auth):
    """Start the Redfish mock simulator."""
    click.echo("Mock start not yet implemented.")


@mock.command(name="stop")
def mock_stop():
    """Stop the mock simulator."""
    click.echo("Mock stop not yet implemented.")


@main.group()
def config():
    """Configuration commands."""
    pass


@config.command()
def show():
    """Print effective configuration."""
    click.echo("Config show not yet implemented.")


@config.command()
def validate():
    """Validate configuration file."""
    click.echo("Config validate not yet implemented.")


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
def peers_list():
    """Show configured peers and their liveness."""
    click.echo("Peers list not yet implemented.")


@main.group()
def action():
    """Action management commands."""
    pass


@action.command(name="list")
def action_list():
    """Show pending actions."""
    click.echo("Action list not yet implemented.")


@action.command()
@click.argument("action_id")
def approve(action_id):
    """Approve a pending action."""
    click.echo(f"Action approve {action_id} not yet implemented.")


@action.command()
@click.argument("action_id")
def deny(action_id):
    """Deny a pending action."""
    click.echo(f"Action deny {action_id} not yet implemented.")
