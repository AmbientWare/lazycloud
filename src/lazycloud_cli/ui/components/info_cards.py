from rich.table import Table
from rich.text import Text

from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.components.card import Card


class InfoCard(Card):
    """Informational message card with icon"""

    def __init__(self, message: str, title: str | None = None):
        """Initialize info card"""
        table = Table(show_header=False, box=None)
        table.add_column(style=Colors.Ansi.text)

        content = Text()
        content.append(message, style=Colors.Ansi.text)
        table.add_row(content)

        super().__init__(
            content=table,
            title=title,
            border_style=Colors.Ansi.info,
        )


class BuildInfoCard(Card):
    """Card for build-related messages"""

    def __init__(self, service_name: str, action: str = "Building"):
        """Initialize build info card"""
        table = Table(show_header=False, box=None)
        table.add_column(style=Colors.Ansi.info)

        content = Text()
        content.append("🔨  ", style=Colors.Ansi.info)
        content.append(f"{action} ", style=Colors.Ansi.text)
        content.append(service_name, style=f"{Colors.Ansi.primary} bold")
        content.append("...", style=Colors.Ansi.text)
        table.add_row(content)

        super().__init__(
            content=table,
            border_style=Colors.Ansi.info,
        )


class SuccessCard(Card):
    """Success message card."""

    def __init__(self, message: str, title: str | None = None):
        """Initialize success card"""
        table = Table(show_header=False, box=None)
        table.add_column(style=Colors.Ansi.success)

        content = Text()
        content.append("✅  ", style=Colors.Ansi.success)
        content.append(message, style=Colors.Ansi.success)
        table.add_row(content)

        super().__init__(
            content=table,
            title=title,
            border_style=Colors.Ansi.success,
        )


class WarningCard(Card):
    """Warning message card."""

    def __init__(self, message: str, title: str | None = None):
        """Initialize warning card"""
        table = Table(show_header=False, box=None)
        table.add_column(style=Colors.Ansi.warning)

        content = Text()
        content.append("⚠️  ", style=Colors.Ansi.warning)
        content.append(message, style=Colors.Ansi.warning)
        table.add_row(content)

        super().__init__(
            content=table,
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
        """Initialize error card"""
        table = Table(show_header=False, box=None)
        table.add_column(style=Colors.Ansi.error)

        error_content = Text()
        error_content.append("❌  ", style=Colors.Ansi.error)
        error_content.append(message, style=Colors.Ansi.error)
        table.add_row(error_content)

        if suggestion:
            suggestion_content = Text()
            suggestion_content.append("💡  ", style=Colors.Ansi.warning)
            suggestion_content.append(suggestion, style=Colors.Ansi.warning)
            table.add_row(suggestion_content)

        super().__init__(
            content=table,
            title=title or "Error",
            border_style=Colors.Ansi.error,
        )


class StatusMessageCard(Card):
    """Compact status message card for inline updates."""

    def __init__(self, message: str, status: str = "info"):
        """Initialize status message card"""
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

        table = Table(show_header=False, box=None)
        table.add_column(style=style)

        content = Text()
        content.append(f"{icon}  ", style=style)
        content.append(message, style=Colors.Ansi.text)
        table.add_row(content)

        super().__init__(
            content=table,
            border_style=border,
        )


class DeploymentActionCard(Card):
    """Card for deployment actions like creating, updating, etc."""

    def __init__(self, action: str, deployment_name: str, details: str | None = None):
        """Initialize deployment action card"""
        table = Table(show_header=False, box=None)
        table.add_column(style=Colors.Ansi.info)

        # Main message
        main_text = Text()
        main_text.append("🚀  ", style=Colors.Ansi.info)
        main_text.append(f"{action} deployment ", style=Colors.Ansi.text)
        main_text.append(f"'{deployment_name}'", style=f"{Colors.Ansi.primary} bold")
        table.add_row(main_text)

        if details:
            detail_text = Text(details, style=Colors.Ansi.text_muted)
            table.add_row(detail_text)

        super().__init__(
            content=table,
            border_style=Colors.Ansi.info,
        )


class SuccessDetailsCard(Card):
    """Success card with key-value details."""

    def __init__(
        self,
        title: str,
        message: str,
        details: dict[str, str] | None = None,
        icon: str = "✓",
    ):
        """Initialize success details card"""
        table = Table(show_header=False, box=None)
        table.add_column(style=Colors.Ansi.success)

        message_text = Text()
        message_text.append(f"{icon} {message}", style=Colors.Ansi.success)
        table.add_row(message_text)

        if details:
            for key, value in details.items():
                detail_text = Text()
                detail_text.append(f"{key}: ", style=Colors.Ansi.text_muted)
                detail_text.append(value, style=Colors.Ansi.primary)
                table.add_row(detail_text)

        super().__init__(
            content=table,
            title=title,
            border_style=Colors.Ansi.success,
        )


class NotFoundCard(ErrorCard):
    """Standard not found error card."""

    def __init__(self, resource_type: str, resource_name: str, icon: str = ""):
        """Initialize not found card"""
        title = f"{icon} Not Found" if icon else "Not Found"
        message = f"{resource_type} '{resource_name}' not found"
        super().__init__(message=message, title=title)


class ActionProgressCard(InfoCard):
    """Card showing action in progress."""

    def __init__(self, action: str, resource_name: str = "", icon: str = ""):
        """Initialize action progress card"""
        title = f"{icon} {action}" if icon else action
        if resource_name:
            message = f"{action} '{resource_name}'..."
        else:
            message = f"{action}..."
        super().__init__(message=message, title=title)
