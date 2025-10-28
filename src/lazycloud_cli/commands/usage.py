"""Usage and billing dashboard command"""

import typer

from lazycloud_cli.ui.dashboard.usage import UsageDashboard

usage_command = typer.Typer(name="usage", help="View workspace usage and billing")


@usage_command.callback(invoke_without_command=True)
def usage(ctx: typer.Context):
    """Show workspace usage and billing dashboard"""
    if ctx.invoked_subcommand is None:
        app = UsageDashboard()
        app.run()
