from models.statuses import StatusPhase

from cli.ui.colors import Colors


def get_status_class(status: StatusPhase) -> str:
    """Get CSS class for status display in widgets."""
    if status == StatusPhase.RUNNING:
        return "status-running"
    elif status in (
        StatusPhase.PENDING,
        StatusPhase.CREATING,
        StatusPhase.HEALTH_CHECK,
        StatusPhase.UPDATING,
        StatusPhase.STOPPING,
        StatusPhase.RESTARTING,
    ):
        return "status-pending"
    elif status == StatusPhase.EXITED:
        return "status-stopped"
    else:
        return "status-error"


def get_status_color(status: StatusPhase) -> str:
    """Get Rich color hex for status in markup text (matches CSS theme)."""
    if status == StatusPhase.RUNNING:
        return Colors.Hex.success
    elif status in (
        StatusPhase.PENDING,
        StatusPhase.CREATING,
        StatusPhase.HEALTH_CHECK,
        StatusPhase.UPDATING,
        StatusPhase.STOPPING,
        StatusPhase.RESTARTING,
    ):
        return Colors.Hex.warning
    elif status == StatusPhase.EXITED:
        return f"{Colors.Hex.primary} 50%"
    else:
        return Colors.Hex.error


def get_status_color_from_string(status: str) -> str:
    """Get Rich color hex from string status (for volumes/networks)."""
    status_lower = status.lower()
    if status_lower in ("running", "ready", "active"):
        return Colors.Hex.success
    elif status_lower in (
        "pending",
        "waiting",
        "terminating",
        "updating",
        "creating",
        "health_check",
        "stopping",
        "restarting",
    ):
        return Colors.Hex.warning
    elif status_lower in ("error", "failed"):
        return Colors.Hex.error
    elif status_lower in ("stopped", "exited"):
        return f"{Colors.Hex.primary} 50%"
    else:
        return Colors.Hex.text
