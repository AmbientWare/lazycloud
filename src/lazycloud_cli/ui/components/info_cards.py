from rich.console import Group
from rich.text import Text

from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.components.card import Card


class InfoCard(Card):
    """Informational message card with icon."""

    def __init__(self, message: str, title: str | None = None):
        """Initialize info card"""
        content = Text()
        content.append(message, style=Colors.Ansi.text)

        super().__init__(
            content=content,
            title=title,
            border_style=Colors.Ansi.info,
        )


class BuildInfoCard(Card):
    """Card for build-related messages."""

    def __init__(self, service_name: str, action: str = "Building"):
        """Initialize build info card.

        Args:
            service_name: Name of the service
            action: Current action (Building, Pushing, etc.)
        """
        content = Text()
        content.append("🔨  ", style=Colors.Ansi.info)
        content.append(f"{action} ", style=Colors.Ansi.text)
        content.append(service_name, style=f"{Colors.Ansi.primary} bold")
        content.append("...", style=Colors.Ansi.text)

        super().__init__(
            content=content,
            border_style=Colors.Ansi.info,
        )


class SuccessCard(Card):
    """Success message card."""

    def __init__(self, message: str, title: str | None = None):
        """Initialize success card."""
        content = Text()
        content.append("✅  ", style=Colors.Ansi.success)
        content.append(message, style=Colors.Ansi.success)

        super().__init__(
            content=content,
            title=title,
            border_style=Colors.Ansi.success,
        )


class WarningCard(Card):
    """Warning message card."""

    def __init__(self, message: str, title: str | None = None):
        """Initialize warning card."""
        content = Text()
        content.append("⚠️  ", style=Colors.Ansi.warning)
        content.append(message, style=Colors.Ansi.warning)

        super().__init__(
            content=content,
            title=title,
            border_style=Colors.Ansi.warning,
        )


class ErrorCard(Card):
    """Error message card."""

    def __init__(
        self,
        message: str,
        title: str | None = None,
        suggestion: str | None = None,
    ):
        """Initialize error card."""
        content = Text()
        content.append("❌  ", style=Colors.Ansi.error)
        content.append(message, style=Colors.Ansi.error)

        if suggestion:
            content.append("\n\n")
            content.append("💡  ", style=Colors.Ansi.warning)
            content.append(suggestion, style=Colors.Ansi.warning)

        super().__init__(
            content=content,
            title=title or "Error",
            border_style=Colors.Ansi.error,
        )


class StatusMessageCard(Card):
    """Compact status message card for inline updates."""

    def __init__(self, message: str, status: str = "info"):
        """Initialize status message card.

        Args:
            message: The message to display
            status: Status type (info, success, warning, error)
        """
        # Choose icon and style based on status
        if status == "success":
            icon = "✅"
            style = Colors.Ansi.success
            border = Colors.Ansi.success
        elif status == "warning":
            icon = "⚠️"
            style = Colors.Ansi.warning
            border = Colors.Ansi.warning
        elif status == "error":
            icon = "❌"
            style = Colors.Ansi.error
            border = Colors.Ansi.error
        else:
            icon = "ℹ️"
            style = Colors.Ansi.info
            border = Colors.Ansi.info

        content = Text()
        content.append(f"{icon}  ", style=style)
        content.append(message, style=Colors.Ansi.text)

        super().__init__(
            content=content,
            border_style=border,
        )


class DeploymentActionCard(Card):
    """Card for deployment actions like creating, updating, etc."""

    def __init__(self, action: str, deployment_name: str, details: str | None = None):
        """Initialize deployment action card.

        Args:
            action: The action being performed (Creating, Updating, etc.)
            deployment_name: Name of the deployment
            details: Optional additional details
        """
        content = Group()

        # Main message
        main_text = Text()
        main_text.append("🚀  ", style=Colors.Ansi.info)
        main_text.append(f"{action} deployment ", style=Colors.Ansi.text)
        main_text.append(f"'{deployment_name}'", style=f"{Colors.Ansi.primary} bold")

        parts = [main_text]

        if details:
            detail_text = Text(details, style=Colors.Ansi.text_muted)
            parts.append(detail_text)

        content = Group(*parts)

        super().__init__(
            content=content,
            border_style=Colors.Ansi.info,
        )
