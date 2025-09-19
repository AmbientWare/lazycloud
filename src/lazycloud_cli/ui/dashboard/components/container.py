from typing import TYPE_CHECKING

from textual.containers import Container as TextualContainer

if TYPE_CHECKING:
    from lazycloud_cli.ui.dashboard.components.app import StatefulApp


class Container(TextualContainer):
    """Custom container for the dashboard"""

    if TYPE_CHECKING:

        @property
        def app(self) -> "StatefulApp": ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
