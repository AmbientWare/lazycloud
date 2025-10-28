from lazycloud_cli.ui.textual.components import Container


class SectionContainer(Container):
    """A bordered section container for Textual UI."""

    def __init__(self, title: str, **kwargs):
        super().__init__(**kwargs)
        self.border_title = title
