from dataclasses import dataclass
from typing import Any, Callable

from textual.app import ComposeResult
from textual.widgets import Label as TextualLabel
from textual.widgets import ListItem as TextualListItem
from textual.widgets import ListView as TextualListView


@dataclass
class ListItemData:
    """Data for a list item."""

    id: str
    name: str
    status: str | None = None
    extra_text: str | None = None
    data: Any | None = None


class ListItem(TextualListItem):
    """A styled list item for LazyCloud."""

    def __init__(self, item_data: ListItemData):
        super().__init__()
        self.item_data = item_data

    def compose(self) -> ComposeResult:
        """Create the list item layout."""
        # Status dot if status provided
        if self.item_data.status:
            status_symbol = "●"  # Filled circle for better visibility
            dot = TextualLabel(status_symbol, classes="status-dot")

            # Add CSS class based on status
            status_lower = self.item_data.status.lower()
            if (
                "running" in status_lower
                or "ready" in status_lower
                or "active" in status_lower
            ):
                dot.add_class("status-running")
            elif "pending" in status_lower or "waiting" in status_lower:
                dot.add_class("status-pending")
            elif "error" in status_lower or "failed" in status_lower:
                dot.add_class("status-error")
            else:
                dot.add_class("status-pending")  # Default to warning/pending color
            yield dot
        else:
            # Add empty space for alignment when no status dot
            yield TextualLabel("", classes="status-dot")

        # Item name with ellipsis truncation
        yield TextualLabel(self.item_data.name, classes="item-name text-primary")

        # Extra text on the right (e.g., replicas count) - dimmed
        if self.item_data.extra_text:
            yield TextualLabel(
                self.item_data.extra_text, classes="item-extra text-muted"
            )


class ListView(TextualListView):
    """A reusable list view component with consistent styling."""

    def __init__(
        self,
        items: list[ListItemData] | None = None,
        on_select: Callable[[ListItemData], None] | None = None,
        on_highlight: Callable[[ListItemData], None] | None = None,
        empty_message: str = "No items found",
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._on_select = on_select
        self._on_highlight = on_highlight
        self._empty_message = empty_message
        self._items = items or []
        self._last_index = 0

    def on_mount(self) -> None:
        """Render items on mount."""
        # Render initial items if provided
        if self._items:
            self.update_items(self._items)
        elif self._empty_message:
            self.show_empty_message()

    def update_items(self, items: list[ListItemData]) -> None:
        """Update the list with new items."""
        # Remember current index before clearing
        current_index = self.index if self.index is not None else self._last_index

        self.clear()
        self._items = items

        if not items:
            self.show_empty_message()
            return

        for item_data in items:
            list_item = ListItem(item_data)
            self.append(list_item)

        # Remember the position for later restoration
        if len(self.children) > 0:
            self._last_index = min(current_index, len(self.children) - 1)

    def show_empty_message(self) -> None:
        """Show the empty message."""
        self.clear()
        self.append(TextualListItem(TextualLabel(self._empty_message)))

    def show_loading(self, _message: str = "Loading...") -> None:
        """Show a loading message."""
        self.clear()
        self.loading = True

    def hide_loading(self) -> None:
        """Hide the loading indicator."""
        self.loading = False

    def on_list_view_selected(self, event: TextualListView.Selected) -> None:
        """Handle item selection."""
        if self._on_select and isinstance(event.item, ListItem):
            self._on_select(event.item.item_data)
            # Remember the selected index
            self._last_index = self.index

    def ensure_highlighted(self) -> None:
        """Ensure an item is highlighted when the list gains focus."""
        if len(self.children) > 0 and self.index is None:
            # If no item is highlighted, highlight the last selected or first item
            self.index = min(self._last_index, len(self.children) - 1)

    def on_list_view_highlighted(self, event: TextualListView.Highlighted) -> None:
        """Handle item highlight change (when navigating with arrows)."""
        if self._on_highlight and isinstance(event.item, ListItem):
            self._on_highlight(event.item.item_data)
            self._last_index = self.index
