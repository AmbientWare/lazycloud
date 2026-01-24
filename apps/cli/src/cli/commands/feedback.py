import typer
from models.feedback import FeedbackType
from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from cli.api import APIError, api
from cli.ui.colors import Colors
from cli.ui.components.info_cards import ErrorCard, SuccessCard

console = Console()

FEEDBACK_TYPE_DESCRIPTIONS = {
    FeedbackType.BUG: "Bug Report - Something isn't working correctly",
    FeedbackType.FEATURE: "Feature Request - Suggest a new feature or improvement",
    FeedbackType.OTHER: "Other - General feedback or questions",
}


def feedback():
    """Submit feedback to the LazyCloud team."""
    try:
        # Display feedback type options
        console.print()
        console.print(
            Text("What type of feedback do you have?", style=f"bold {Colors.Ansi.text}")
        )
        console.print()

        for feedback_type, description in FEEDBACK_TYPE_DESCRIPTIONS.items():
            console.print(
                f"  [{Colors.Ansi.secondary}]{feedback_type.value}[/] - {description}"
            )

        console.print()

        # Prompt for feedback type
        selected_type = Prompt.ask(
            Text("Select feedback type", style=Colors.Ansi.text_muted),
            choices=[t.value for t in FeedbackType],
            default=FeedbackType.OTHER.value,
        )

        console.print()

        # Prompt for message
        console.print(
            Text(
                "Enter your feedback (minimum 10 characters):",
                style=Colors.Ansi.text_muted,
            )
        )
        message = Prompt.ask(Text(">>", style=Colors.Ansi.secondary))

        if len(message.strip()) < 10:
            error_card = ErrorCard(
                message="Feedback message must be at least 10 characters.",
                title="Invalid Input",
            )
            console.print(error_card)
            raise typer.Exit(1)

        # Submit feedback
        console.print()
        console.print(
            Text("Submitting feedback...", style=f"italic {Colors.Ansi.text_muted}")
        )

        api.feedback.submit_feedback(selected_type, message.strip())

        success_card = SuccessCard(
            message="Thank you! Your feedback has been submitted successfully.",
            title="Feedback Submitted",
        )
        console.print(success_card)

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
