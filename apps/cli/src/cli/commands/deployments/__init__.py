import typer

from cli.commands.deployments.list import app as list_app

deployments_app = typer.Typer(help="Deployment management commands")

deployments_app.add_typer(list_app)
