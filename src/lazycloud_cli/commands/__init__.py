import typer

from lazycloud_cli.commands.compose.deploy import deploy
from lazycloud_cli.commands.compose.destroy import destroy
from lazycloud_cli.commands.compose.init import init_deployment
from lazycloud_cli.commands.dashboard import dashboard
from lazycloud_cli.commands.login import login
from lazycloud_cli.commands.usage import usage_command
from lazycloud_cli.commands.workspace import workspace_app

# Create the main app
main_cli = typer.Typer(
    name="lazycloud",
    help="LazyCloud CLI",
    add_completion=False,
)

# Add command modules to the main app
main_cli.add_typer(workspace_app, name="workspace")
main_cli.add_typer(usage_command, name="usage")

# Add top-level shortcuts for compose commands
main_cli.command("init", help="Initialize a LazyCloud deployment configuration")(
    init_deployment
)
main_cli.command("deploy", help="Deploy or update a Docker Compose application")(deploy)
main_cli.command("destroy", help="Destroy a deployment")(destroy)

# Add authentication command
main_cli.command("login", help="Login with your LazyCloud API key")(login)

# Add dashboard command to main app
main_cli.command("dashboard", help="Launch the LazyCloud dashboard")(dashboard)
