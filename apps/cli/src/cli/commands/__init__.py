import importlib.metadata

import typer
from pyfiglet import Figlet
from rich.console import Console

from cli.commands.compose.deploy import deploy
from cli.commands.compose.destroy import destroy
from cli.commands.compose.init import init_deployment
from cli.commands.compose.rollback import rollback
from cli.commands.dashboard import dashboard
from cli.commands.deployments import deployments_app
from cli.commands.login import login
from cli.commands.logout import logout
from cli.commands.usage import usage_command
from cli.commands.workspaces import workspace_app
from cli.config import config
from cli.ui.colors import Colors
from cli.ui.components.info_cards import ErrorCard

# Commands that don't require authentication
PUBLIC_COMMANDS = {"login", "logout", "version", "init", None}

try:
    __version__ = importlib.metadata.version("lazycloud")
except importlib.metadata.PackageNotFoundError:
    __version__ = "unknown"


def version_command():
    """Show the CLI version"""
    console = Console()

    # Create ASCII art
    fig = Figlet(font="slant")
    ascii_art = fig.renderText("LazyCloud")

    # Print with colors using Rich
    console.print(f"[{Colors.Ansi.text_muted}]{ascii_art}[/]")
    console.print(f"[{Colors.Ansi.secondary}]version {__version__}[/]")


# Create the main app
main_cli = typer.Typer(
    name="lazycloud",
    help="LazyCloud CLI - Deploy Docker Compose to the cloud",
    add_completion=False,
)


def version_callback(value: bool):
    if value:
        version_command()
        raise typer.Exit()


@main_cli.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit",
    ),
):
    """LazyCloud CLI"""
    console = Console()

    # Check authentication for protected commands
    if ctx.invoked_subcommand not in PUBLIC_COMMANDS:
        is_authenticated, error_message = config.check_authentication()
        if not is_authenticated:
            error_card = ErrorCard(
                message=error_message,
                title="Authentication Required",
                suggestion="Run 'lazycloud login' to authenticate.",
            )
            console.print(error_card)
            raise typer.Exit(1)

    # If no subcommand is provided, show ASCII art and help
    if ctx.invoked_subcommand is None:
        # Create ASCII art
        fig = Figlet(font="slant")
        ascii_art = fig.renderText("LazyCloud")

        # Print with colors
        console.print(f"[{Colors.Ansi.text_muted}]{ascii_art}[/]")
        console.print(
            f"[{Colors.Ansi.text_muted}]Deploy Docker Compose to the cloud[/]\n"
        )

        # Show quick start
        console.print(
            f"[{Colors.Ansi.text}]Get started:[/] [{Colors.Ansi.secondary}]https://lazycloud.dev/docs[/]"
        )
        console.print(
            f"[{Colors.Ansi.text}]Run[/] [{Colors.Ansi.secondary}]lazycloud --help[/] [{Colors.Ansi.text}]for all commands[/]"
        )
        console.print()


# Add command modules to the main app
main_cli.add_typer(workspace_app, name="workspaces")
main_cli.add_typer(deployments_app, name="deployments")
main_cli.add_typer(usage_command, name="usage")

# Add top-level shortcuts for compose commands
main_cli.command("init", help="Initialize a LazyCloud deployment configuration")(
    init_deployment
)
main_cli.command("deploy", help="Deploy or update a Docker Compose application")(deploy)
main_cli.command("destroy", help="Destroy a deployment")(destroy)
main_cli.command("rollback", help="Rollback a deployment to a previous revision")(
    rollback
)

# Add authentication commands
main_cli.command("login", help="Login to LazyCloud via browser")(login)
main_cli.command("logout", help="Logout and clear stored credentials")(logout)

# Add dashboard command to main app
main_cli.command("dashboard", help="Launch the LazyCloud dashboard")(dashboard)

# Add version command
main_cli.command("version", help="Show the CLI version")(version_command)
