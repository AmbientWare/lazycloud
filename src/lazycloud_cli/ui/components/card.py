"""Card component for displaying information in a bordered panel."""

from typing import Optional, Union

from rich.box import ROUNDED, Box
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.panel import Panel
from rich.text import Text


class Card:
    def __init__(
        self,
        content: RenderableType,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        border_style: Optional[str] = None,
        padding: Union[int, tuple[int, int]] = (0, 1),
        expand: bool = True,
        box: Box = ROUNDED,
    ):
        self.content = content
        self.title = title
        self.subtitle = subtitle
        self.border_style = border_style or "bright_blue"
        self.padding = padding
        self.expand = expand
        self.box = box

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        if self.title:
            title_text = Text(self.title, style="bold")
            if self.subtitle:
                title_text.append(" ", style="")
                title_text.append(f"({self.subtitle})", style="dim")
            title_element = title_text
        else:
            title_element = None

        yield Panel(
            self.content,
            title=title_element,
            title_align="left",
            border_style=self.border_style,
            padding=self.padding,
            expand=self.expand,
            box=self.box,
        )


class CardGroup:
    def __init__(self, cards: list[Card], spacing: int = 1):
        self.cards = cards
        self.spacing = spacing

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        for card in self.cards:
            yield card
