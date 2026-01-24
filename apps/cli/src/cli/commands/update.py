import platform
import subprocess

import typer
from rich.console import Console

from cli.ui.components.info_cards import ErrorCard, SuccessCard

console = Console()


def update():
    """Update the LazyCloud CLI to the latest version."""
    is_windows = platform.system() == "Windows"

    console.print()
    console.print("[bold]Updating LazyCloud CLI...[/bold]")
    console.print()

    try:
        if is_windows:
            # Run PowerShell install script
            result = subprocess.run(
                [
                    "powershell",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "irm https://lazycloud.dev/install.ps1 | iex",
                ],
                check=False,
            )
        else:
            # Run bash install script
            result = subprocess.run(
                ["sh", "-c", "curl -LsSf https://lazycloud.dev/install.sh | sh"],
                check=False,
            )

        if result.returncode != 0:
            error_card = ErrorCard(
                message="The update script exited with an error.",
                title="Update Failed",
                suggestion="Check the output above for details.",
            )
            console.print(error_card)
            raise typer.Exit(1)

        console.print()
        success_card = SuccessCard(
            message="LazyCloud CLI has been updated successfully.",
            title="Update Complete",
        )
        console.print(success_card)

    except FileNotFoundError:
        if is_windows:
            error_card = ErrorCard(
                message="PowerShell is required but was not found.",
                title="Update Failed",
                suggestion="Run manually: irm https://lazycloud.dev/install.ps1 | iex",
            )
        else:
            error_card = ErrorCard(
                message="curl is required but was not found.",
                title="Update Failed",
                suggestion="Run manually: curl -LsSf https://lazycloud.dev/install.sh | sh",
            )
        console.print(error_card)
        raise typer.Exit(1)

    except Exception as e:
        error_card = ErrorCard(
            message=str(e),
            title="Update Failed",
            suggestion="Please try again or update manually.",
        )
        console.print(error_card)
        raise typer.Exit(1)
