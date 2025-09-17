"""
Dashboard command for LazyCloud CLI.
"""

import typer

from lazycloud_cli.ui.dashboard import run_dashboard


def dashboard():
    """Launch the LazyCloud dashboard."""
    try:
        run_dashboard()
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C
        pass
    except Exception as e:
        typer.secho(f"Error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
