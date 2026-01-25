import typer
from models.feedback import FeedbackType
from rich.console import Console
from rich.live import Live
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from cli.api import APIError, api
from cli.ui.colors import Colors
from cli.ui.components.card import Card
from cli.ui.components.info_cards import ErrorCard
from cli.ui.textual.theme import Icons

console = Console()

FEEDBACK_TYPE_OPTIONS: dict[FeedbackType, tuple[str, str]] = {
    FeedbackType.BUG: ("Bug Report", "Something isn't working correctly"),
    FeedbackType.FEATURE: ("Feature Request", "Suggest a new feature or improvement"),
    FeedbackType.OTHER: ("Other", "General feedback or questions"),
}

MIN_FEEDBACK_LENGTH = 10


def _create_feedback_type_card() -> Card:
    """Create the feedback type selection card."""
    table = Table(show_header=False, box=None)
    table.add_column("Option", style=Colors.Ansi.secondary, width=10)
    table.add_column("Type", style=f"bold {Colors.Ansi.text}", width=18)
    table.add_column("Description", style=Colors.Ansi.text_muted)

    for feedback_type, (label, description) in FEEDBACK_TYPE_OPTIONS.items():
        table.add_row(feedback_type.value, label, description)

    return Card(
        content=table,
        title="Submit Feedback",
        subtitle="Help us improve LazyCloud",
        border_style=Colors.Ansi.info,
    )


def _create_input_card() -> Card:
    """Create the feedback input prompt card."""
    content = Text()
    content.append("Share your thoughts with us.\n", style=Colors.Ansi.text)
    content.append(
        f"Minimum {MIN_FEEDBACK_LENGTH} characters required.",
        style=Colors.Ansi.text_muted,
    )

    return Card(
        content=content,
        title="Your Feedback",
        border_style=Colors.Ansi.secondary,
    )


def _create_spinner_card() -> Card:
    """Create the submission spinner card."""
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    )
    progress.add_task("Submitting your feedback...", total=None)

    return Card(
        content=progress,
        title="Submitting Feedback",
        border_style=Colors.Ansi.info,
    )


def _create_success_card() -> Card:
    """Create the success card."""
    content = Text()
    content.append(
        "Thank you! Your feedback has been submitted successfully.",
        style=Colors.Ansi.success,
    )

    return Card(
        content=content,
        title=f"{Icons.CHECKMARK}  Feedback Submitted",
        border_style=Colors.Ansi.success,
    )


def feedback() -> None:
    """Submit feedback to the LazyCloud team."""
    try:
        # Display feedback type options
        console.print()
        console.print(_create_feedback_type_card())

        # Prompt for feedback type
        selected_type = Prompt.ask(
            "Select feedback type",
            choices=[t.value for t in FeedbackType],
            default=FeedbackType.OTHER.value,
        )

        console.print()

        # Show message input card
        console.print(_create_input_card())

        message = Prompt.ask(Text(">>", style=Colors.Ansi.secondary))

        if len(message.strip()) < MIN_FEEDBACK_LENGTH:
            console.print(
                ErrorCard(
                    message=f"Feedback message must be at least {MIN_FEEDBACK_LENGTH} characters.",
                    title="Invalid Input",
                )
            )
            raise typer.Exit(1)

        # Submit feedback with spinner
        console.print()

        with Live(
            _create_spinner_card(), console=console, refresh_per_second=10
        ) as live:
            api.feedback.submit_feedback(selected_type, message.strip())
            live.update(_create_success_card())

    except APIError as e:
        error_card = ErrorCard(
            message=str(e),
            title="Failed to Submit Feedback",
            suggestion="Please try again later or contact support@lazycloud.dev",
        )
        console.print(error_card)
        raise typer.Exit(1)

    except typer.Exit:
        raise

    except Exception as e:
        error_card = ErrorCard(
            message=f"An unexpected error occurred: {e}",
            title="Error",
        )
        console.print(error_card)
        raise typer.Exit(1)
