from textual.widget import Widget

from cli.ui.textual.components import Container


class SectionContainer(Container):
    """A bordered section container for Textual UI."""

    def __init__(self, title: str | None = None, *children: Widget, **kwargs):
        super().__init__(*children, **kwargs)
        if title:
            self.border_title = title
