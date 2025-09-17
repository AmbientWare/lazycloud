"""Badge components for status display."""

from rich.text import Text

from shared.models.statuses import TaskStatus


class DeploymentStatusBadge:
    """A badge component for displaying deployment status with color and emoji."""

    def __init__(self, status: TaskStatus):
        """Initialize the badge with a status.

        Args:
            status: The deployment status as string or TaskStatus enum
        """
        self.status = status

    def render(self) -> Text:
        """Render the status badge as Rich Text.

        Returns:
            Rich Text object with colored status
        """
        color = {
            TaskStatus.COMPLETED: "green",
            TaskStatus.PENDING: "yellow",
            TaskStatus.ERROR: "red",
        }.get(self.status, "white")

        text = Text(self.status.value.upper(), style=f"bold {color}")
        return text

    def __rich__(self) -> Text:
        """Rich protocol support for direct console printing."""
        return self.render()


class StatusBadge:
    """A generic badge component for displaying status with custom color and text."""

    def __init__(self, status_type: str, text: str):
        """Initialize the badge with a status type and text.

        Args:
            status_type: The type of status (success, error, warning, info, etc.)
            text: The text to display
        """
        self.status_type = status_type
        self.text = text

        # Map status types to colors
        self.color_map = {
            "success": "green",
            "error": "red",
            "warning": "yellow",
            "info": "blue",
            "pending": "yellow",
            "running": "cyan",
            "stopped": "dim",
            "unknown": "dim red",
        }

    def render(self) -> Text:
        """Render the status badge as Rich Text.

        Returns:
            Rich Text object with colored status
        """
        color = self.color_map.get(self.status_type, "white")
        return Text(self.text, style=f"bold {color}")

    def __rich__(self) -> Text:
        """Rich protocol support for direct console printing."""
        return self.render()
