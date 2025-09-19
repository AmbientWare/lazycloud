import typer

from lazycloud_cli.commands.auth import auth_app
from lazycloud_cli.commands.compose.deploy import deploy
from lazycloud_cli.commands.compose.destroy import destroy
from lazycloud_cli.commands.compose.init import init_deployment
from lazycloud_cli.commands.dashboard import dashboard

# Create the main app
main_cli = typer.Typer(
    name="lazycloud",
    help="LazyCloud CLI",
    add_completion=False,
)

# Create service sub-app
service_app = typer.Typer(
    name="service",
    help="Service management commands",
    add_completion=False,
)

# Add command modules to the main app
main_cli.add_typer(auth_app, name="auth")
main_cli.add_typer(service_app, name="service")

# Add top-level shortcuts for compose commands
main_cli.command("init", help="Initialize a LazyCloud deployment configuration")(
    init_deployment
)
main_cli.command("deploy", help="Deploy or update a Docker Compose application")(deploy)
main_cli.command("destroy", help="Destroy a deployment")(destroy)

# Add dashboard command to main app
main_cli.command("dashboard", help="Launch the LazyCloud dashboard")(dashboard)
