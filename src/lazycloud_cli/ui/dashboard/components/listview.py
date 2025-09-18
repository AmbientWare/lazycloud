from dataclasses import dataclass
from typing import Any, Callable, Optional

from textual.app import ComposeResult
from textual.widgets import Label as TextualLabel
from textual.widgets import ListItem as TextualListItem
from textual.widgets import ListView as TextualListView

from lazycloud_cli.ui.dashboard.theme import theme


@dataclass
class ListItemData:
    """Data for a list item."""

    id: str
    name: str
    status: Optional[str] = None
    extra_text: Optional[str] = None
    data: Optional[Any] = None  # Store any additional data


class ListItem(TextualListItem):
    """A styled list item for LazyCloud."""

    def __init__(self, item_data: ListItemData):
        super().__init__()
        self.item_data = item_data

    def compose(self) -> ComposeResult:
        """Create the list item layout."""
        # Status dot if status provided
        if self.item_data.status:
            status_symbol = "○"
            dot = TextualLabel(status_symbol)
            dot.styles.width = 2
            dot.styles.color = theme.get_status_color(self.item_data.status)
            yield dot

        # Item name with ellipsis truncation
        name = TextualLabel(self.item_data.name)
        name.styles.width = "1fr"
        name.styles.overflow = "ellipsis"
        name.styles.text_overflow = "ellipsis"
        yield name

        # Extra text on the right (e.g., replicas count)
        if self.item_data.extra_text:
            extra = TextualLabel(self.item_data.extra_text)
            extra.styles.width = 8
            extra.styles.text_align = "right"
            extra.styles.color = theme.text_dim
            yield extra

    def on_mount(self) -> None:
        """Apply minimal styling."""
        self.styles.layout = "horizontal"
        self.styles.height = 1
        self.styles.padding = (0, 1)


class ListView(TextualListView):
    """A reusable list view component with consistent styling."""

    def __init__(
        self,
        items: Optional[list[ListItemData]] = None,
        on_select: Optional[Callable[[ListItemData], None]] = None,
        empty_message: str = "No items found",
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._on_select = on_select
        self._empty_message = empty_message
        self._items = items or []

    def on_mount(self) -> None:
        """Apply consistent styling and render items on mount."""
        self.styles.background = theme.background
        self.styles.border = None
        self.styles.padding = 0
        self.styles.scrollbar_size = 1

        # Render initial items if provided
        if self._items:
            self.update_items(self._items)
        elif self._empty_message:
            self.show_empty_message()

    def update_items(self, items: list[ListItemData]) -> None:
        """Update the list with new items."""
        self.clear()
        self._items = items

        if not items:
            self.show_empty_message()
            return

        for item_data in items:
            list_item = ListItem(item_data)
            self.append(list_item)

    def show_empty_message(self) -> None:
        """Show the empty message."""
        self.clear()
        self.append(TextualListItem(TextualLabel(self._empty_message)))

    def show_loading(self, message: str = "Loading...") -> None:
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
