import re
from datetime import datetime

from responses.deployments import Revision
from rich.console import Console
from rich.live import Live
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from cli.ui.colors import Colors
from cli.ui.components import Card, ConfirmationDialog, ErrorCard
from cli.ui.views.helpers.formatters import format_timestamp


class RollbackView:
    """View orchestrator for the rollback command."""

    def __init__(self, console: Console):
        """Initialize the rollback view."""
        self.console = console

    def show_error(self, message: str, suggestion: str | None = None) -> None:
        """Show rollback error message."""
        error_card = ErrorCard(
            message=message,
            suggestion=suggestion,
            title="⏪ Rollback Failed",
        )
        self.console.print(error_card)

    def show_cancelled(self) -> None:
        """Show cancellation message."""
        self.console.print("\n[dim]Rollback cancelled.[/dim]")

    def show_history(
        self, latest_revision: Revision, rollbackable_revisions: list[Revision]
    ) -> None:
        """Display rollback history in a table."""
        table = Table(show_header=True, box=None)
        table.add_column("Revision", style=Colors.Ansi.primary, width=10)
        table.add_column("Status", width=12)
        table.add_column("Description", width=30)
        table.add_column("Updated", width=30)

        reversed_revisions = list(reversed(rollbackable_revisions))
        helm_to_display = {
            rev.revision: len(rollbackable_revisions) - idx + 1
            for idx, rev in enumerate(rollbackable_revisions, start=1)
        }

        def format_description(desc: str) -> str:
            """Format description: map revision numbers and truncate long error messages."""
            formatted = desc

            pattern = r"Rollback to (\d+)"
            match = re.search(pattern, formatted)
            if match:
                helm_rollback_rev = int(match.group(1))
                if helm_rollback_rev in helm_to_display:
                    display_num = helm_to_display[helm_rollback_rev]
                    formatted = formatted.replace(
                        f"Rollback to {helm_rollback_rev}", f"Rollback to {display_num}"
                    )

            if len(formatted) > 60:
                if "failed:" in formatted.lower():
                    error_match = re.search(
                        r"failed:\s*(.+?)(?:core\.|ObjectMeta|$)",
                        formatted,
                        re.IGNORECASE | re.DOTALL,
                    )
                    if error_match:
                        error_msg = error_match.group(1).strip()
                        error_msg = re.sub(r"\s+", " ", error_msg)
                        error_msg = (
                            error_msg.split(":")[0]
                            if ":" in error_msg and len(error_msg.split(":")[0]) < 60
                            else error_msg[:60]
                        )
                        if len(error_msg) > 60:
                            error_msg = error_msg[:57] + "..."
                        return error_msg

                first_line = formatted.split("\n")[0].strip()
                if len(first_line) > 60:
                    first_line = first_line[:57] + "..."
                return first_line

            return formatted

        table.add_row(
            "[bold]Latest[/bold]",
            latest_revision.status,
            format_description(latest_revision.description),
            format_timestamp(latest_revision.updated),
        )

        for idx, rev in enumerate(reversed_revisions, start=1):
            table.add_row(
                str(idx),
                rev.status,
                format_description(rev.description),
                format_timestamp(rev.updated),
            )

        card = Card(
            content=table,
            title="📜 Rollback History",
            border_style=Colors.Ansi.primary,
        )
        self.console.print(card)

    def select_revision(
        self, latest_revision: Revision, rollbackable_revisions: list[Revision]
    ) -> tuple[int | None, int | None]:
        """Prompt user to select a revision."""
        self.show_history(latest_revision, rollbackable_revisions)

        reversed_revisions = list(reversed(rollbackable_revisions))
        revision_map = {
            idx: rev.revision for idx, rev in enumerate(reversed_revisions, start=1)
        }

        relative_numbers = [
            str(idx) for idx in range(1, len(rollbackable_revisions) + 1)
        ]
        selected_relative = Prompt.ask(
            "[bold]Select revision to rollback to[/bold]",
            choices=relative_numbers,
            default=None,
        )
        self.console.print()

        if selected_relative:
            relative_num = int(selected_relative)
            rev_number = revision_map.get(relative_num)
            return rev_number, relative_num

        return None, None

    def confirm_rollback(self, deployment_name: str, display_revision: int) -> bool:
        """Confirm rollback action."""
        dialog = ConfirmationDialog(
            title="⏪ Confirm Rollback",
            question=f"Rollback deployment '{deployment_name}' to revision {display_revision}?",
            details=[
                f"The deployment will be rolled back to revision {display_revision}",
                "Current deployment state will be replaced",
                "Services will be restarted with the previous configuration",
                "You can rollback again if needed",
            ],
            warning_message="This will replace the current deployment state",
            danger=False,
            default=False,
            border_style=Colors.Ansi.primary,
        )
        return dialog.show(self.console)

    def show_rollback_progress(self, deployment_name: str, display_revision: int):
        """Show rollback progress with elapsed time.

        Args:
            deployment_name: Name of deployment being rolled back
            display_revision: Display revision number

        Returns:
            Context manager for progress display
        """

        class RollbackRenderable:
            """Custom renderable that updates with elapsed time."""

            def __init__(self, deployment_name, display_revision):
                self.deployment_name = deployment_name
                self.display_revision = display_revision
                self.start_time = datetime.now()
                self.progress = Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    transient=False,
                )
                self.task_id = self.progress.add_task(
                    "Rolling back to previous revision..."
                )

            def __rich__(self):
                """Rich protocol - called each time the display updates."""
                table = Table(show_header=False, box=None)
                table.add_column()

                table.add_row(self.progress)

                elapsed = int((datetime.now() - self.start_time).total_seconds())
                elapsed_text = Text(
                    f"Elapsed time: {elapsed}s", style=Colors.Ansi.text_muted
                )
                table.add_row(elapsed_text)

                return Card(
                    content=table,
                    title=f"⏪ Rolling back '{self.deployment_name}' to revision {self.display_revision}",
                    border_style=Colors.Ansi.primary,
                )

        class ProgressContext:
            def __init__(self, console, deployment_name, display_revision):
                self.console = console
                self.deployment_name = deployment_name
                self.display_revision = display_revision
                self.live = None
                self.renderable = None

            def __enter__(self):
                self.renderable = RollbackRenderable(
                    self.deployment_name, self.display_revision
                )
                self.live = Live(
                    self.renderable, console=self.console, refresh_per_second=4
                )
                self.live.start()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                if self.live:
                    self.live.stop()
                if self.renderable:
                    self.renderable.progress.stop()

        return ProgressContext(self.console, deployment_name, display_revision)

    def show_success(
        self,
        deployment_name: str,
        display_revision: int,
    ) -> None:
        """Show rollback success message."""
        table = Table(show_header=False, box=None)
        table.add_column(style=Colors.Ansi.success)
        table.add_row(
            f"Successfully rolled back '{deployment_name}' to revision {display_revision}"
        )

        card = Card(
            content=table,
            title="⏪ Rollback Complete",
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)
