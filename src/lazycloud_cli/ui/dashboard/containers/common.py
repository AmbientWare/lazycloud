"""
Common UI components for dashboard containers.
"""

from textual.containers import Container

from lazycloud_cli.ui.dashboard.theme import theme


class SectionContainer(Container):
    """A bordered section container for Textual UI."""

    def __init__(self, title: str, **kwargs):
        super().__init__(**kwargs)
        self.border_title = title

    def on_mount(self) -> None:
        """Style the section container."""
        self.styles.border = ("round", theme.primary)
        self.styles.background = theme.background
        self.styles.padding = (0, 1)
        self.styles.height = "auto"