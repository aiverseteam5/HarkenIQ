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
@click.option("--bmc-ip", default=None, help="BMC IP address")
def detect(bmc_ip):
    """Detect BMC vendor and controller generation."""
    click.echo("BMC detect not yet implemented.")


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


@skills.command(name="list")
def skills_list():
    """List installed skills."""
    click.echo("Skills list not yet implemented.")


@skills.command()
def validate():
    """Validate all skill YAML files."""
    click.echo("Skills validate not yet implemented.")


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
