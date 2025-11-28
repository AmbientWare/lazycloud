import re
from pathlib import Path

import typer
from rich.console import Console

from cli.api import api
from cli.config import config
from cli.lazycloud_file import LazyCloudConfig, LazyCloudFile
from cli.ui.components.confirmation import SimpleConfirmationDialog
from cli.ui.views.init import InitView

console = Console()
view = InitView(console)


def validate_deployment_name(name: str) -> tuple[str, bool]:
    """Validate deployment name follows LazyCloud naming conventions"""
    pattern = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
    if not re.match(pattern, name) or len(name) > 63:
        raise typer.BadParameter(
            "Deployment name must be lowercase alphanumeric, may contain hyphens, "
            "must start/end with alphanumeric, and be 63 characters or less"
        )

    # check if the name is already taken
    existing_deployment = api.deployments.get_deployment(name=name)
    if existing_deployment:
        # deployment already exists, ask if we want to sync it locally
        details = [
            f"Deployment '{name}' already exists in the cloud",
            "This will create a local .lazycloud file that references it",
            "You can then manage this deployment from this directory",
        ]
        if existing_deployment.namespace:
            details.insert(1, f"Namespace: {existing_deployment.namespace}")
        if existing_deployment.state:
            state_display = existing_deployment.state.value.title()
            details.insert(-1, f"Current state: {state_display}")

        dialog = SimpleConfirmationDialog(
            action="sync this existing deployment locally",
            details=details,
            title="🔄 Existing Deployment Found",
        )

        if dialog.show(console):
            # User wants to sync - return the name and flag
            return name, True

        else:
            # User doesn't want to sync - raise an exception to exit
            raise typer.BadParameter(
                f"Deployment '{name}' already exists. Choose a different name or sync the existing deployment."
            )

    return name, False


def find_compose_files(directory: Path = Path.cwd()) -> list[str]:
    """Find potential docker-compose files in the directory."""
    compose_patterns = [
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "docker-compose.*.yml",
        "docker-compose.*.yaml",
    ]

    found_files = []
    for pattern in compose_patterns:
        if "*" in pattern:
            # Handle glob patterns
            for file in directory.glob(pattern):
                if file.is_file():
                    found_files.append(file.name)
        else:
            # Direct file check
            if (directory / pattern).exists():
                found_files.append(pattern)

    return sorted(set(found_files))


def suggest_deployment_name(directory: Path = Path.cwd()) -> str:
    """Suggest a deployment name based on directory name."""
    dir_name = directory.name.lower()
    # Replace non-alphanumeric with hyphens
    clean_name = re.sub(r"[^a-z0-9]+", "-", dir_name)
    # Remove leading/trailing hyphens
    clean_name = clean_name.strip("-")
    # Ensure it starts with a letter
    if clean_name and clean_name[0].isdigit():
        clean_name = "app-" + clean_name

    # Truncate if too long
    if len(clean_name) > 63:
        clean_name = clean_name[:63].rstrip("-")

    return clean_name or "default"


def init_deployment(
    deployment_name: str | None = typer.Option(
        None, "--name", "-n", help="Deployment name (will prompt if not provided)"
    ),
    compose_file: str | None = typer.Option(
        None, "--file", "-f", help="Docker Compose file to use"
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing .lazycloud file"
    ),
) -> None:
    """Initialize a LazyCloud deployment configuration."""

    lazycloud_file = LazyCloudFile(Path.cwd())

    # Check if .lazycloud already exists
    if lazycloud_file.exists() and not force:
        if not view.show_existing_file_warning():
            raise typer.Abort()

    # Get deployment name
    if not deployment_name:
        suggested_name = suggest_deployment_name()
        deployment_name = view.prompt_deployment_name(suggested_name)

    # Validate deployment name
    try:
        deployment_name, is_sync = validate_deployment_name(deployment_name)
    except typer.BadParameter as e:
        view.show_validation_error(str(e))
        raise typer.Exit(1)

    # Find compose files
    compose_files = find_compose_files()

    if not compose_file:
        if not compose_files:
            compose_file = view.show_no_compose_files()
        elif len(compose_files) == 1:
            compose_file = compose_files[0]
            view.show_single_compose_file(compose_file)
        else:
            # Let user choose from found files
            view.show_compose_files(compose_files)
            compose_file = view.prompt_compose_file_selection(compose_files)

    # Verify compose file exists
    compose_path = Path(compose_file)
    if not compose_path.exists():
        view.show_compose_file_not_found(compose_file)
        raise typer.Exit(1)

    # Show configuration summary before creating
    view.show_configuration_summary(deployment_name, compose_file, is_sync=is_sync)

    # Create config
    lazycloud_config = LazyCloudConfig(
        deployment_name=deployment_name,
        compose_file=compose_file,
        last_deployed=None,
    )

    # Write .lazycloud file
    try:
        lazycloud_file.write(lazycloud_config)
        if is_sync:
            # Get deployment info to show in success message
            deployment_info = api.deployments.get_deployment(
                name=deployment_name, workspace_id=config.active_workspace_id
            )
            view.show_sync_success(deployment_name, compose_file, deployment_info)

        else:
            view.show_success(deployment_name, compose_file)

    except Exception as e:
        view.show_error(f"Failed to create .lazycloud file: {e}")
        raise typer.Exit(1)
