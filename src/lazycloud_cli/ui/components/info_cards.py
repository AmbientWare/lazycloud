"""Information card components for replacing plain text messages."""

from typing import Optional

from rich.console import Group
from rich.text import Text

from lazycloud_cli.ui.components.card import Card
from lazycloud_cli.ui.theme import theme


class InfoCard(Card):
    """Informational message card with icon."""

    def __init__(self, message: str, title: str | None = None):
        """Initialize info card"""
        content = Text()
        content.append(message, style=theme.text_primary)

        super().__init__(
            content=content,
            title=title,
            border_style=theme.border_info,
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
        content.append("🔨  ", style=theme.info)
        content.append(f"{action} ", style=theme.text_primary)
        content.append(service_name, style=f"{theme.primary} bold")
        content.append("...", style=theme.text_primary)

        super().__init__(
            content=content,
            border_style=theme.border_info,
        )


class SuccessCard(Card):
    """Success message card."""

    def __init__(self, message: str, title: Optional[str] = None):
        """Initialize success card."""
        content = Text()
        content.append("✅  ", style=theme.success)
        content.append(message, style=theme.success)

        super().__init__(
            content=content,
            title=title,
            border_style=theme.border_success,
        )


class WarningCard(Card):
    """Warning message card."""

    def __init__(self, message: str, title: Optional[str] = None):
        """Initialize warning card."""
        content = Text()
        content.append("⚠️  ", style=theme.warning)
        content.append(message, style=theme.warning)

        super().__init__(
            content=content,
            title=title,
            border_style=theme.border_warning,
        )


class ErrorCard(Card):
    """Error message card."""

    def __init__(
        self,
        message: str,
        title: Optional[str] = None,
        suggestion: Optional[str] = None,
    ):
        """Initialize error card."""
        content = Text()
        content.append("❌  ", style=theme.error)
        content.append(message, style=theme.error)

        if suggestion:
            content.append("\n\n")
            content.append("💡  ", style=theme.warning)
            content.append(suggestion, style=theme.warning)

        super().__init__(
            content=content,
            title=title or "Error",
            border_style=theme.border_error,
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
            style = theme.success
            border = theme.border_success
        elif status == "warning":
            icon = "⚠️"
            style = theme.warning
            border = theme.border_warning
        elif status == "error":
            icon = "❌"
            style = theme.error
            border = theme.border_error
        else:
            icon = "ℹ️"
            style = theme.info
            border = theme.border_info

        content = Text()
        content.append(f"{icon}  ", style=style)
        content.append(message, style=theme.text_primary)

        super().__init__(
            content=content,
            border_style=border,
        )


class DeploymentActionCard(Card):
    """Card for deployment actions like creating, updating, etc."""

    def __init__(
        self, action: str, deployment_name: str, details: Optional[str] = None
    ):
        """Initialize deployment action card.

        Args:
            action: The action being performed (Creating, Updating, etc.)
            deployment_name: Name of the deployment
            details: Optional additional details
        """
        content = Group()

        # Main message
        main_text = Text()
        main_text.append("🚀  ", style=theme.info)
        main_text.append(f"{action} deployment ", style=theme.text_primary)
        main_text.append(f"'{deployment_name}'", style=f"{theme.primary} bold")

        parts = [main_text]

        if details:
            detail_text = Text(details, style=theme.text_secondary)
            parts.append(detail_text)

        content = Group(*parts)

        super().__init__(
            content=content,
            border_style=theme.border_info,
        )
