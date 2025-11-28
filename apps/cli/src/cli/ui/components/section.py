from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.rule import Rule
from rich.text import Text


class Section:
    """A section component with a header and content.

    Similar to HTML sections, this provides structure and visual hierarchy
    to CLI output.
    """

    def __init__(
        self,
        title: str,
        content: RenderableType | None = None,
        icon: str | None = None,
        style: str = "bright_cyan",
        rule: bool = True,
    ):
        """Initialize a Section component.

        Args:
            title: The section title
            content: Optional content to display under the section
            icon: Optional emoji/icon to prepend to the title
            style: Style for the section header
            rule: Whether to show a rule/divider for the section
        """
        self.title = title
        self.content = content
        self.icon = icon
        self.style = style
        self.rule = rule

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Render the section."""
        # Build title with icon
        title_text = ""
        if self.icon:
            title_text = f"{self.icon} {self.title}"
        else:
            title_text = self.title

        # Render header
        if self.rule:
            yield Rule(title_text, style=self.style)
        else:
            yield Text(title_text, style=f"bold {self.style}")

        # Render content if provided
        if self.content:
            yield self.content


class SectionGroup:
    """A container for multiple sections."""

    def __init__(self, sections: list[Section], spacing: int = 1):
        """Initialize a SectionGroup.

        Args:
            sections: List of Section components
            spacing: Number of blank lines between sections
        """
        self.sections = sections
        self.spacing = spacing

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Render the section group."""
        for i, section in enumerate(self.sections):
            if i > 0:
                for _ in range(self.spacing):
                    yield ""
            yield section
