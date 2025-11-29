"""Empty state component with ASCII art for when no deployments exist."""

from pyfiglet import Figlet
from textual.widgets import Static

from cli.ui.colors import Colors


class EmptyStateWidget(Static):
    """Widget displaying ASCII art and instructions when no deployments exist."""

    DEFAULT_CSS = """
    EmptyStateWidget {
        width: 100%;
        height: 100%;
        content-align: center middle;
    }
    """

    def __init__(self, **kwargs):
        """Initialize the empty state widget."""
        # Create ASCII art
        fig = Figlet(font="slant")
        ascii_art = fig.renderText("LazyCloud")

        # Build the complete message with proper colors
        message = (
            f"[{Colors.Hex.text_muted}]{ascii_art}[/]\n"
            f"[{Colors.Hex.text_muted}]Get started by deploying your first application:[/]\n\n"
            f"  [{Colors.Hex.secondary}]$[/] lazycloud deploy\n"
        )

        super().__init__(message, **kwargs)
