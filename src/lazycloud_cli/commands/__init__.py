import typer

# Import command modules
from lazycloud_cli.commands.machines import machine_app
from lazycloud_cli.commands.ssh import ssh_app
from lazycloud_cli.commands.auth import auth_app
from lazycloud_cli.commands.file_systems import fs_app

# Create the main app
cli = typer.Typer(
    name="lazycloud",
    help="LazyCloud CLI",
    add_completion=False,
)

# Add command modules to the main app
cli.add_typer(machine_app, name="machine")
cli.add_typer(ssh_app, name="ssh")
cli.add_typer(auth_app, name="auth")
cli.add_typer(fs_app, name="fs")
