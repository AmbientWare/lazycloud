from typing import TYPE_CHECKING, Callable

from textual.containers import Container as TextualContainer
from textual.timer import Timer

if TYPE_CHECKING:
    from lazycloud_cli.ui.dashboard.main import DashboardApp


class Container(TextualContainer):
    """Custom container for the dashboard"""

    if TYPE_CHECKING:

        @property
        def app(self) -> "DashboardApp": ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._selection_timer = None

    def handle_debounce(
        self, timer: Timer | None, callback: Callable[[], None], delay: float = 0.2
    ) -> Timer:
        """Helper to debounce rapid events like navigation"""
        # Stop existing timer if it exists
        if timer:
            timer.stop()

        # Create new timer with callback
        return self.set_timer(delay, callback)
