import typer
from rich.console import Console

from cli.config import config
from cli.ui.components.info_cards import ErrorCard
from cli.ui.textual import run_dashboard

console = Console()


def dashboard():
    """Launch the LazyCloud dashboard."""
    try:
        run_dashboard()
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C
        pass

    except ConnectionError:
        error_card = ErrorCard(
            message=f"Cannot connect to server at {config.api_base_url}",
            title="🌐 Connection Error",
            suggestion="Check your network connection and server URL.",
        )
        console.print(error_card)
        raise typer.Exit(1)

    except Exception as e:
        error_card = ErrorCard(
            message=str(e),
            title="🔧 Dashboard Error",
            suggestion="Please check the error and try again.",
        )
        console.print(error_card)
        raise typer.Exit(1)
