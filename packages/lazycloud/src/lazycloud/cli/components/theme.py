"""Semantic Rich styles for the ``lazycloud`` and ``lazycloud-admin`` commands.

One small set of semantic styles replaces per-command color literals, and one
mapping ties domain states (task/build/container status, machine/worker/pool
state, AWS connection phases) to those styles.
"""

from __future__ import annotations

from enum import Enum

from rich.style import Style
from rich.text import Text

SUCCESS = Style(color="green")
ERROR = Style(color="red")
WARNING = Style(color="yellow")
PENDING = Style(color="magenta")
RUNNING = Style(color="cyan")
INFO = Style(color="blue")
MUTED = Style(dim=True)
EMPHASIS = Style(bold=True)
PLAIN = Style()
BORDER = Style(color="bright_black")
TABLE_HEADER = Style(color="cyan", bold=True)
ROW_ALT = Style(dim=True)

_STATE_STYLES: dict[str, Style] = {
    # Terminal success.
    "active": SUCCESS,
    "approved": SUCCESS,
    "complete": SUCCESS,
    "healthy": SUCCESS,
    "ready": SUCCESS,
    "reused": SUCCESS,
    # Terminal failure.
    "action_required": ERROR,
    "denied": ERROR,
    "error": ERROR,
    "failed": ERROR,
    "timeout": ERROR,
    # Degraded or winding down.
    "cancelled": WARNING,
    "cordoned": WARNING,
    "degraded": WARNING,
    "disconnect_draining": WARNING,
    "draining": WARNING,
    "expired": WARNING,
    "retiring": WARNING,
    "retiring_authorization": WARNING,
    "revoking": WARNING,
    "stopped": WARNING,
    # Queued or waiting.
    "awaiting_authorization": PENDING,
    "created": PENDING,
    "pending": PENDING,
    "reconnect_pending": PENDING,
    "retry": PENDING,
    "submitted": PENDING,
    # In flight.
    "planning": RUNNING,
    "running": RUNNING,
    "validating": RUNNING,
    "verifying": RUNNING,
    "verifying_revocation": RUNNING,
    # Inert or historical.
    "deleted": MUTED,
    "exited": MUTED,
    "released": MUTED,
    "retired": MUTED,
    "revoked": MUTED,
}


def state_style(state: object) -> Style:
    """Return the semantic style for a domain state value or enum member."""
    raw = state.value if isinstance(state, Enum) else state
    return _STATE_STYLES.get(str(raw).strip().lower().replace("-", "_"), PLAIN)


def styled(message: str, style: Style = PLAIN) -> Text:
    """Build text carrying one semantic style without markup interpretation."""
    return Text(message, style=style)


__all__ = [
    "BORDER",
    "EMPHASIS",
    "ERROR",
    "INFO",
    "MUTED",
    "PENDING",
    "PLAIN",
    "ROW_ALT",
    "RUNNING",
    "SUCCESS",
    "TABLE_HEADER",
    "WARNING",
    "state_style",
    "styled",
]
