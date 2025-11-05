from lazycloud_cli.ui.textual.components import Container


class SectionContainer(Container):
    """A bordered section container for Textual UI."""

    def __init__(self, title: str | None = None, **kwargs):
        super().__init__(**kwargs)
        if title:
            self.border_title = title
