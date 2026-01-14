"""Helper functions for environment variable and secret management."""

import os
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from models.diffs import EnvVarChanges
from models.secrets import Secret, SecretCollection
from rich.console import Console
from rich.text import Text

from cli.ui.colors import Colors
from cli.ui.components import Card

if TYPE_CHECKING:
    from responses.deployments import DiffResponse


class ImportMethod(StrEnum):
    """Method for importing environment variables."""

    FILE = "file"
    SHELL = "shell"
    NONE = "none"


# Constants
MAX_VARS_TO_DISPLAY = 5
MISSING_VARS_MESSAGE = "{count} variable(s) not found in {source}"


def find_env_file(project_dir: Path) -> Path | None:
    """Check if .env file exists in project directory."""
    env_file = project_dir / ".env"
    return env_file if env_file.exists() else None


def parse_env_file(file_path: Path) -> dict[str, str | None]:
    """Parse .env file into a dictionary."""
    env_vars = {}

    try:
        with open(file_path, "r") as f:
            for _, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()

                    # Remove quotes if present
                    if value and value[0] in ('"', "'") and value[-1] == value[0]:
                        value = value[1:-1]

                    env_vars[key] = value if value else None

    except FileNotFoundError:
        pass

    except Exception as e:
        Console().print(
            f"[yellow]Warning: Error parsing {file_path.name}: {e}[/yellow]"
        )

    return env_vars


def filter_secrets_with_values(secrets_dict: dict[str, Secret]) -> list[Secret]:
    """Filter secrets to only include those with actual values."""
    return [
        secret
        for secret in secrets_dict.values()
        if secret.value and secret.value.strip() != ""
    ]


def get_remaining_vars(
    secrets_dict: dict[str, Secret], loaded_keys: set[str]
) -> dict[str, str]:
    """Get variables that were not loaded from source."""
    return {
        key: secret.value
        for key, secret in secrets_dict.items()
        if key not in loaded_keys
    }


def show_missing_vars_error(
    console: Console,
    remaining_vars: dict[str, str],
    source_name: str,
) -> None:
    """Show error for missing environment variables."""
    missing_keys = list(remaining_vars.keys())

    # Build error message
    console.print()
    console.print(
        Text(
            f"Missing {len(missing_keys)} environment variable(s) from {source_name}",
            style=Colors.Ansi.error,
        )
    )
    console.print()

    # Show missing vars (truncated if too many)
    if len(missing_keys) <= MAX_VARS_TO_DISPLAY:
        for key in missing_keys:
            console.print(Text(f"  • {key}", style=Colors.Ansi.text_muted))
    else:
        for key in missing_keys[:MAX_VARS_TO_DISPLAY]:
            console.print(Text(f"  • {key}", style=Colors.Ansi.text_muted))
        console.print(
            Text(
                f"  ... and {len(missing_keys) - MAX_VARS_TO_DISPLAY} more",
                style=Colors.Ansi.text_muted,
            )
        )

    console.print()
    console.print(
        Text(
            "Add the missing variables to your env file and try again.",
            style=Colors.Ansi.text_muted,
        )
    )
    console.print()


def create_env_vars_detected_card(var_count: int) -> Card:
    """Create card displaying detected environment variables."""
    return Card(
        content=Text("\n").join(
            [
                Text(
                    f"Detected {var_count} environment variable(s)",
                    style=Colors.Ansi.text,
                ),
                Text(
                    "These will be encrypted and stored securely",
                    style=Colors.Ansi.text_muted,
                ),
            ]
        ),
        title="🔐 Environment Variables Detected",
        border_style=Colors.Ansi.primary,
    )


def load_from_shell_env(secrets_dict: dict[str, Secret]) -> set[str]:
    """Load environment variables from shell environment."""
    loaded_keys = set()
    for key in list(secrets_dict.keys()):
        value = os.environ.get(key)
        if value is not None:
            secrets_dict[key].value = value
            loaded_keys.add(key)
    return loaded_keys


def load_from_env_file(secrets_dict: dict[str, Secret], file_path: Path) -> set[str]:
    """Load environment variables from .env file."""
    loaded_keys = set()
    file_vars = parse_env_file(file_path)

    for key in list(secrets_dict.keys()):
        if key in file_vars and file_vars[key] is not None:
            secrets_dict[key].value = file_vars[key]
            loaded_keys.add(key)

    return loaded_keys


def mark_secrets_as_empty(secrets_dict: dict[str, Secret], keys: set[str]) -> None:
    """Mark specified secrets as empty (user chose to skip them)."""
    for key in keys:
        secrets_dict[key].value = ""


def count_collected_secrets(secrets: SecretCollection) -> int:
    """Count how many secrets have actual values."""
    return len([s for s in secrets.added if s.value and s.value.strip()])


def update_diff_with_collected_secrets(
    diff_response: "DiffResponse | None",
    secrets: SecretCollection,
    env: str | None,
) -> None:
    """Update diff response to reflect actually collected secrets."""

    if not diff_response:
        return

    # Clear env changes if user skipped collection
    if env and env.lower() == ImportMethod.NONE:
        diff_response.env_var_changes = None
        return

    if not diff_response.env_var_changes:
        return

    # Filter to only show vars that have values (were actually collected)
    collected_keys = {
        secret.key for secret in secrets.added if secret.value and secret.value.strip()
    }
    removed_keys = {secret.key for secret in secrets.removed}

    diff_response.env_var_changes = EnvVarChanges(
        added=list(collected_keys),
        removed=list(removed_keys),
        user_managed=diff_response.env_var_changes.user_managed or [],
    )
