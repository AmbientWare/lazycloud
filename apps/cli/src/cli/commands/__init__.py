import importlib.metadata

import typer

from cli.commands.compose.deploy import deploy
from cli.commands.compose.destroy import destroy
from cli.commands.compose.init import init_deployment
from cli.commands.compose.rollback import rollback
from cli.commands.dashboard import dashboard
from cli.commands.deployments import deployments_app
from cli.commands.login import login
from cli.commands.usage import usage_command
from cli.commands.workspaces import workspace_app

try:
    __version__ = importlib.metadata.version("lazycloud")
except importlib.metadata.PackageNotFoundError:
    __version__ = "unknown"


def version_command():
    """Show the CLI version"""
    typer.echo(f"lazycloud version {__version__}")


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
    pass


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

# Add authentication command
main_cli.command("login", help="Login with your LazyCloud API key")(login)

# Add dashboard command to main app
main_cli.command("dashboard", help="Launch the LazyCloud dashboard")(dashboard)

# Add version command
main_cli.command("version", help="Show the CLI version")(version_command)
