import typer

# Import command modules
from lazycloud_cli.commands.ssh.add import app as add_app
from lazycloud_cli.commands.ssh.list import app as list_app
from lazycloud_cli.commands.ssh.remove import app as remove_app


# Create the ssh app
ssh_app = typer.Typer(help="SSH connection and configuration commands")

# Add command modules to the ssh app
ssh_app.add_typer(add_app)
ssh_app.add_typer(list_app)
ssh_app.add_typer(remove_app)
