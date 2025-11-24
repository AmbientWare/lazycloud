import asyncio
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.live import Live

from lazycloud_cli.api import APIError, api
from lazycloud_cli.config import config
from lazycloud_cli.lazycloud_file import LazyCloudFile
from lazycloud_cli.registry import RegistryType, create_registry
from lazycloud_cli.ui.views import DeployView
from lazycloud_cli.ui.views.helpers.env_helpers import (
    ImportMethod,
    count_collected_secrets,
    update_diff_with_collected_secrets,
)
from shared.models.secrets import Secret, SecretCollection, SecretSource
from shared.models.statuses import TaskStatus
from shared.requests.deployments import DiffType
from shared.responses.deployments import DiffResponse

console = Console()


def deploy(
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation prompt"),
    warnings: bool = typer.Option(False, "--warnings", help="Show validation warnings"),
    env: str = typer.Option(
        None,
        "--env",
        help="Source for environment variables: path to .env file or 'shell'",
    ),
):
    """Deploy or update a Docker Compose application."""
    view = DeployView(console)

    try:
        # Load configuration
        _, lazycloud_config, compose_file_path = _load_configuration(view)
        deployment_name = lazycloud_config.deployment_name
    except Exception as e:
        view.show_error(f"Configuration error: {e}")
        raise typer.Exit(1)

    # Show deployment configuration
    view.show_configuration(
        deployment_name=deployment_name,
        compose_file=str(compose_file_path),
    )

    try:
        # Read compose file and env files
        with open(compose_file_path, "r") as f:
            compose_yaml = f.read()
        compose_data = yaml.safe_load(compose_yaml)
        env_files_content = _read_env_files(compose_data, compose_file_path.parent)
    except FileNotFoundError as e:
        view.show_error(f"File not found: {e}")
        raise typer.Exit(1)
    except yaml.YAMLError as e:
        view.show_error(f"Invalid YAML in compose file: {e}")
        raise typer.Exit(1)
    except Exception as e:
        view.show_error(f"Error reading compose file: {e}")
        raise typer.Exit(1)

    # Generate timestamp for built images
    timestamp = _generate_build_timestamp()
    _apply_build_timestamps(compose_data, timestamp)

    # Regenerate YAML with timestamped images
    compose_yaml = yaml.dump(compose_data, default_flow_style=False)

    try:
        # Check for existing deployment and get diff (don't confirm yet)
        existing_deployment = _get_existing_deployment(deployment_name)

        # Get diff response to know what changed
        diff_response = _get_deployment_diff(
            existing_deployment,
            deployment_name,
            compose_yaml,
            env_files_content,
        )

    except Exception as e:
        view.show_error(f"Error validating deployment: {e}")
        raise typer.Exit(1)

    # Extract environment variables for secrets collection
    all_env_vars = _extract_env_variables(compose_data, env_files_content)

    # Determine which secrets to collect based on diff (unless --env none)
    secrets = _prepare_secrets_for_collection(env, diff_response, all_env_vars)

    # Collect secrets from user (unless --env none)
    has_secrets = bool(secrets.added or secrets.removed)
    if has_secrets:
        total_to_collect = len(secrets.added)

        secrets = view.collect_secrets(
            secrets,
            project_dir=compose_file_path.parent,
            env_source=env,
            skip_prompts=yes or bool(env),
        )

        # Show collection summary
        collected_count = count_collected_secrets(secrets)
        if collected_count > 0 or total_to_collect > 0:
            view.show_collection_summary(collected_count, total_to_collect)

        # Re-check after collection - user might have skipped some
        has_secrets = bool(secrets.added or secrets.removed)

    # Update diff response to reflect what was actually collected
    update_diff_with_collected_secrets(diff_response, secrets, env)

    # Now show diff and get confirmation
    try:
        _show_diff_and_confirm(
            diff_response,
            deployment_name,
            yes,
            warnings,
            _get_env_source_display(env),
        )
    except typer.Exit:
        # Re-raise typer.Exit (including cancellation) so it propagates correctly
        raise

    except Exception as e:
        view.show_error(f"Error during confirmation: {e}")
        raise typer.Exit(1)

    # Check if validation passed (diff endpoint already did full validation)
    if diff_response and not diff_response.can_deploy:
        view.show_error("Validation failed - cannot proceed with deployment")
        if diff_response.errors:
            for error in diff_response.errors:
                view.show_error(f"  • {error}")
        raise typer.Exit(1)

    try:
        # Handle image building AFTER validation passes
        compose_yaml = _handle_builds(
            compose_data, compose_file_path, deployment_name, yes
        )
    except typer.Exit:
        # Re-raise typer.Exit from build failures
        raise
    except Exception as e:
        view.show_error(f"Build stage failed: {e}")
        raise typer.Exit(1)

    # Deploy
    try:
        _deploy(
            deployment_name,
            compose_yaml,
            secrets=secrets if has_secrets else None,
        )
    except typer.Exit:
        # Re-raise typer.Exit from deployment failures
        raise

    except Exception as e:
        view.show_error(f"Deployment stage failed: {e}")
        raise typer.Exit(1)


def _load_configuration(view: DeployView):
    """Load and validate .lazycloud configuration."""
    lazycloud_file = LazyCloudFile.find_and_load(Path.cwd())
    if not lazycloud_file:
        view.show_error(
            "No .lazycloud file found",
            suggestion="Run 'lazycloud init' to create a deployment configuration",
        )
        raise typer.Exit(1)

    try:
        lazycloud_config = lazycloud_file.read()

    except Exception as e:
        view.show_error(f"Failed to read .lazycloud file: {e}")
        raise typer.Exit(1)

    compose_file_path = lazycloud_file.get_compose_file_path()
    if not compose_file_path.exists():
        view.show_error(f"Compose file not found: {compose_file_path}")
        raise typer.Exit(1)

    return lazycloud_file, lazycloud_config, compose_file_path


def _read_env_files(compose_data: dict, base_path: Path) -> dict[str, str]:
    """Read all env files referenced in the compose file."""
    env_files_content = {}

    for _, service_config in compose_data.get("services", {}).items():
        env_file_config = service_config.get("env_file", [])
        if isinstance(env_file_config, str):
            env_file_config = [env_file_config]

        for env_file in env_file_config:
            env_file_path = base_path / env_file
            if env_file_path.exists() and env_file not in env_files_content:
                try:
                    env_files_content[env_file] = env_file_path.read_text()

                except Exception:
                    raise typer.Exit(1)

    return env_files_content


def _generate_build_timestamp() -> str:
    """Generate a unique timestamp for build tags."""
    now = datetime.now(UTC)
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond // 1000:03d}"


def _apply_build_timestamps(compose_data: dict, timestamp: str) -> None:
    """Apply timestamp tags to all services with build sections."""
    for service_name, service_config in compose_data.get("services", {}).items():
        if "build" in service_config:
            # Get original image and replace tag with timestamp
            original_image = service_config.get("image", f"{service_name}:latest")
            if ":" in original_image:
                image_base = original_image.rsplit(":", 1)[0]
            else:
                image_base = original_image

            # Update with timestamped image
            service_config["image"] = f"{image_base}:{timestamp}"


def _prepare_secrets_for_collection(
    env: str | None,
    diff_response: DiffResponse | None,
    all_env_vars: dict[str, str | None],
) -> SecretCollection:
    """Prepare secrets for collection based on diff and env source."""
    # Skip collection if user explicitly chose 'none'
    if env and env.lower() == ImportMethod.NONE:
        return SecretCollection(added=[], removed=[])

    if diff_response and diff_response.env_var_changes:
        # Update deployment - collect only changed variables
        added_secrets = [
            Secret(
                key=key,
                value=all_env_vars[key] or "",
                source=SecretSource.COMPOSE,
            )
            for key in diff_response.env_var_changes.added
            if key in all_env_vars
        ]

        removed_secrets = [
            Secret(
                key=key,
                value="",  # Empty value for removal
                source=SecretSource.COMPOSE,
            )
            for key in diff_response.env_var_changes.removed
        ]

        return SecretCollection(added=added_secrets, removed=removed_secrets)
    else:
        # New deployment - collect all variables
        added_secrets = [
            Secret(
                key=key,
                value=value or "",
                source=SecretSource.COMPOSE,
            )
            for key, value in all_env_vars.items()
        ]
        return SecretCollection(added=added_secrets, removed=[])


def _get_env_source_display(env: str | None) -> str:
    """Get human-readable env source description for UI display."""
    if not env:
        return "interactive input"

    env_lower = env.lower()
    if env_lower == ImportMethod.SHELL:
        return "shell environment"
    elif env_lower == ImportMethod.NONE:
        return "none (skipped)"
    else:
        return f"file: {env}"


def _extract_env_variables(
    compose_data: dict, env_files_content: dict
) -> dict[str, str | None]:
    """Extract all environment variables from compose data and env files."""
    all_env_vars = {}

    # First, parse env files
    for _, content in env_files_content.items():
        for line in content.splitlines():
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue

            # Parse KEY=value format
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                # Remove quotes if present
                if value and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]

                # Store None for empty values
                all_env_vars[key] = value if value else None

    # Then, add environment variables from services
    for _, service_config in compose_data.get("services", {}).items():
        env_config = service_config.get("environment", {})

        if isinstance(env_config, dict):
            for key, value in env_config.items():
                if value is None:
                    all_env_vars[key] = None
                else:
                    str_value = str(value)
                    # Check if it's a placeholder like ${VAR} or $VAR
                    if str_value.startswith("${") and str_value.endswith("}"):
                        all_env_vars[key] = None
                    elif str_value.startswith("$"):
                        all_env_vars[key] = None
                    elif str_value == "":
                        all_env_vars[key] = None
                    else:
                        all_env_vars[key] = str_value
        elif isinstance(env_config, list):
            for env_var in env_config:
                if "=" in env_var:
                    key, value = env_var.split("=", 1)
                    # Check if it's a placeholder
                    if value.startswith("${") and value.endswith("}"):
                        all_env_vars[key] = None
                    elif value.startswith("$"):
                        all_env_vars[key] = None
                    elif value == "":
                        all_env_vars[key] = None
                    else:
                        all_env_vars[key] = value
                else:
                    # Environment variable without value
                    all_env_vars[env_var] = None

    return all_env_vars


def _handle_builds(
    compose_data: dict, compose_file_path: Path, deployment_name: str, yes: bool = False
) -> str:
    """Handle building and pushing images if needed"""
    view = DeployView(console)
    services_to_build = []

    for service_name, service_config in compose_data.get("services", {}).items():
        if "build" in service_config:
            build_config = service_config.get("build", {})
            if isinstance(build_config, str):
                build_config = {"context": build_config}

            # Image already has timestamp from _apply_build_timestamps()
            image_name = service_config.get("image", f"{service_name}:latest")

            services_to_build.append(
                {
                    "service_name": service_name,
                    "image_name": image_name,
                    "context": build_config.get("context", "."),
                    "dockerfile": build_config.get("dockerfile", "Dockerfile"),
                }
            )

    if not services_to_build:
        return yaml.dump(compose_data, default_flow_style=False)

    # Build and push images
    registry = create_registry(
        RegistryType(config.registry_type), deployment_name=deployment_name
    )

    # Create build status tracker (don't print the build info card separately)
    build_status = view.show_build_status(services_to_build)

    with Live(build_status, console=console, refresh_per_second=4, auto_refresh=True):
        setup_response = registry.setup()
        if not setup_response.success:
            view.show_error(f"Registry setup failed: {setup_response.error_message}")
            raise typer.Exit(1)

        for i, build_info in enumerate(services_to_build):
            # Update status to building
            build_status.update_service(
                i, "building", f"Building {build_info['image_name']}"
            )

            context_path = compose_file_path.parent / build_info["context"]

            build_response = registry.build_image(
                build_info["image_name"], context_path, build_info["dockerfile"]
            )

            if not build_response.success:
                build_status.update_service(i, TaskStatus.ERROR, "Build failed")
                view.show_error(
                    f"Failed to build {build_info['service_name']}:\n\n{build_response.error_message}"
                )
                raise typer.Exit(1)

            # Update status to pushing
            build_status.update_service(i, TaskStatus.PENDING, "Pushing to registry")

            push_response = registry.push_image(build_info["image_name"])

            if not push_response.success:
                build_status.update_service(i, TaskStatus.ERROR, "Push failed")
                view.show_error(
                    f"Failed to push {build_info['service_name']}:\n\n{push_response.error_message}"
                )
                raise typer.Exit(1)

            build_status.update_service(i, TaskStatus.COMPLETED, "Ready")

    return yaml.dump(compose_data, default_flow_style=False)


def _get_existing_deployment(deployment_name: str):
    """Get existing deployment if it exists."""
    try:
        return api.deployments.get_deployment(name=deployment_name)

    except Exception:
        return None


def _get_deployment_diff(
    existing_deployment,
    deployment_name: str,
    compose_yaml: str,
    env_files_content: dict[str, str],
) -> DiffResponse | None:
    """Get deployment diff without showing or confirming. Returns DiffResponse or None."""
    # Determine diff type based on whether deployment exists
    diff_type = DiffType.EXISTING if existing_deployment else DiffType.NEW

    try:
        # Extract env keys from compose for diff
        compose_data = yaml.safe_load(compose_yaml)
        env_keys = _extract_env_variables(compose_data, env_files_content).keys()

        diff_response = api.diff.get_deployment_diff(
            diff_type=diff_type,
            workspace_id=config.active_workspace_id,
            deployment_name=deployment_name,
            compose_yaml=compose_yaml,
            env_keys=list(env_keys),
        )

        return diff_response

    except Exception as e:
        view = DeployView(console)
        view.show_warning(f"Could not generate diff: {e}")
        return None


def _show_diff_and_confirm(
    diff_response: DiffResponse | None,
    deployment_name: str,
    yes: bool,
    warnings: bool,
    env_source_info: str | None = None,
) -> None:
    """Show diff and get confirmation for deployment."""
    view = DeployView(console)

    if diff_response:
        # Show diff using view components
        has_changes = view.show_diff(diff_response, show_warnings=warnings)

        if not has_changes:
            view.show_no_changes()

        if diff_response.errors:
            view.show_error("Cannot proceed due to errors")
            raise typer.Exit(1)

    if not yes:
        # Show confirmation using view
        confirmed = view.confirm_deployment(
            deployment_name=deployment_name,
            default=False,
            env_source_info=env_source_info,
        )

        if not confirmed:
            view.show_cancelled()
            raise typer.Exit(0)


def _deploy(
    deployment_name: str,
    compose_yaml: str,
    secrets: SecretCollection | None = None,
):
    """Perform the actual deployment."""
    view = DeployView(console)

    # Create a deployment creation progress card
    creation_progress = view.show_deployment_creation_progress(deployment_name)

    try:
        # Show the card while creating deployment
        with Live(creation_progress, console=console, refresh_per_second=4):
            # Create the deployment
            creation_progress.update_status(
                "creating", "Sending configuration to server..."
            )

            try:
                # Create deployment
                task_response = api.deployments.create_deployment(
                    compose_yaml=compose_yaml,
                    workspace_id=config.active_workspace_id,
                    name=deployment_name,
                    secrets=bool(secrets),
                )

                if not task_response or not task_response.task_id:
                    creation_progress.update_status(
                        "failed", "Failed to create deployment task"
                    )
                    raise Exception("Server did not return a task ID")
            except APIError as e:
                creation_progress.update_status("failed", "API request failed")
                if e.status_code == 401:
                    raise Exception(
                        "Authentication failed. Please run 'lazycloud login'"
                    )
                elif e.status_code and 500 <= e.status_code < 600:
                    raise Exception(f"Server error: {e}")
                else:
                    raise Exception(f"API error: {e}")
            except Exception as e:
                creation_progress.update_status("failed", "Request failed")
                raise Exception(f"Failed to create deployment: {e}")

            # Store secrets if we have any (BEFORE waiting for task!)
            if secrets:
                try:
                    creation_progress.update_status(
                        "creating", "Storing environment variables..."
                    )

                    # Handle new secrets (from diff's "added" list - these should all be new)
                    if secrets.added:
                        try:
                            # Create new secrets - these keys are brand new per the diff
                            api.secrets.store_secrets(
                                task_response.deployment_id, secrets.added
                            )
                        except APIError as e:
                            creation_progress.update_status(
                                "failed", "Failed to create secrets"
                            )
                            if e.status_code == 409:
                                raise Exception(
                                    f"Secrets already exist (unexpected): {e}\n"
                                    "The diff indicated these are new keys, but they already exist."
                                )
                            else:
                                raise Exception(f"Failed to create secrets: {e}")

                    # Handle removed secrets
                    if secrets.removed:
                        try:
                            api.secrets.delete_secrets(
                                task_response.deployment_id, secrets.removed
                            )
                        except Exception as delete_error:
                            creation_progress.update_status(
                                "failed", "Failed to delete secrets"
                            )
                            raise Exception(f"Failed to delete secrets: {delete_error}")

                except Exception as e:
                    if "Failed to" not in str(
                        e
                    ):  # Don't double-wrap our own exceptions
                        creation_progress.update_status(
                            "failed", "Failed to manage secrets"
                        )
                        raise Exception(f"Failed to manage secrets: {e}")
                    else:
                        raise

            # NOW wait for task completion via streaming
            creation_progress.update_status(
                "creating", "Processing deployment request..."
            )

            try:
                # Get the final task status from the stream
                final_status = asyncio.run(
                    api.deployments.wait_for_deployment(task_response.task_id)
                )
            except Exception as e:
                creation_progress.update_status(
                    "failed", "Failed to monitor deployment"
                )
                raise Exception(f"Error monitoring deployment: {e}")

            # Update UI based on actual status
            if final_status.status == TaskStatus.COMPLETED:
                creation_progress.update_status(
                    TaskStatus.COMPLETED, "Deployment created successfully!"
                )
            elif final_status.status == TaskStatus.ERROR:
                error_msg = final_status.message or "Task failed"
                creation_progress.update_status("failed", error_msg)
                raise Exception(f"Deployment task failed: {error_msg}")
            else:
                creation_progress.update_status(
                    "failed", f"Unexpected status: {final_status.status}"
                )
                raise Exception(
                    f"Deployment ended with unexpected status: {final_status.status}"
                )

        # Show final success message
        view.show_summary(
            deployment_name=deployment_name,
            status=final_status.status,
            duration=0,
            message="View deployment in the dashboard with 'lazycloud dashboard'",
        )

    except typer.Exit:
        raise
    except Exception as e:
        view.show_error(str(e))
        raise typer.Exit(1)
