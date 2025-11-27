import typer

from lazycloud_cli.commands.workspaces.activate import app as activate_app
from lazycloud_cli.commands.workspaces.create import app as create_app
from lazycloud_cli.commands.workspaces.destroy import app as destroy_app
from lazycloud_cli.commands.workspaces.list import app as list_app

workspace_app = typer.Typer(help="Workspace management commands")

workspace_app.add_typer(activate_app)
workspace_app.add_typer(create_app)
workspace_app.add_typer(list_app)
workspace_app.add_typer(destroy_app)
