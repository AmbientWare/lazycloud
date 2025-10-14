import time
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.live import Live

from lazycloud_cli.api import api
from lazycloud_cli.config import config
from lazycloud_cli.lazycloud_file import LazyCloudFile
from lazycloud_cli.registry import RegistryType, create_registry
from lazycloud_cli.ui.views import DeployView
from shared.models.secrets import SecretCollection
from shared.models.statuses import TaskStatus
from shared.responses.deployments import DiffResponse

console = Console()


def deploy(
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation prompt"),
    warnings: bool = typer.Option(False, "--warnings", help="Show validation warnings"),
    timeout: int = typer.Option(
        5, "--timeout", "-t", help="Deployment timeout in minutes"
    ),
):
    """Deploy or update a Docker Compose application."""
    view = DeployView(console)

    # Load configuration
    lazycloud_file, lazycloud_config, compose_file_path = _load_configuration(view)
    deployment_name = lazycloud_config.deployment_name

    # Show deployment configuration using view
    view.show_configuration(
        deployment_name=deployment_name,
        compose_file=str(compose_file_path),
    )

    # Read compose file and env files
    with open(compose_file_path, "r") as f:
        compose_yaml = f.read()
    compose_data = yaml.safe_load(compose_yaml)
    env_files_content = _read_env_files(compose_data, compose_file_path.parent)

    # Check for existing deployment and show diff before building
    existing_deployment = _get_existing_deployment(deployment_name)

    # Always show changes/preview for non-dry-run deployments
    diff_response = None
    # Show diff/preview for both new and existing deployments
    diff_result = _show_and_confirm_changes(
        existing_deployment,
        deployment_name,
        compose_yaml,
        env_files_content,
        yes,
        warnings,
    )
    if not diff_result:
        return  # No changes or user cancelled

    diff_response = diff_result

    # Extract environment variables for secrets collection
    all_env_vars = _extract_env_variables(compose_data, env_files_content)

    # Determine which secrets to collect based on diff
    secrets = SecretCollection(added={}, removed=[])
    if diff_response and diff_response.env_var_changes:
        # Only collect values for added variables
        for key in diff_response.env_var_changes.added:
            if key in all_env_vars:
                secrets.added[key] = all_env_vars[key]

        # Only collect values for removed variables
        for key in diff_response.env_var_changes.removed:
            secrets.removed.append(key)

    else:
        # For new deployments or if no diff, collect all
        secrets = SecretCollection(added=all_env_vars, removed=[])

    # Collect secrets from user (unless dry run or auto-yes)
    has_secrets = bool(secrets.added or secrets.removed)
    if has_secrets and not yes:
        secrets = view.collect_secrets(secrets)

    elif has_secrets and yes:
        # dont update secrets if auto-yes
        secrets = None

    # Handle image building AFTER confirmation
    compose_yaml = _handle_builds(compose_data, compose_file_path, deployment_name, yes)

    # Deploy
    _deploy(
        deployment_name,
        compose_yaml,
        lazycloud_file,
        timeout,
        secrets=secrets if has_secrets else None,
    )


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


def _extract_env_variables(
    compose_data: dict, env_files_content: dict
) -> dict[str, str]:
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

                all_env_vars[key] = value

    # Then, add environment variables from services
    for _, service_config in compose_data.get("services", {}).items():
        env_config = service_config.get("environment", {})

        if isinstance(env_config, dict):
            for key, value in env_config.items():
                # Convert to string and handle None/null values
                all_env_vars[key] = str(value) if value is not None else ""
        elif isinstance(env_config, list):
            for env_var in env_config:
                if "=" in env_var:
                    key, value = env_var.split("=", 1)
                    all_env_vars[key] = value
                else:
                    # Environment variable without value
                    all_env_vars[env_var] = ""

    return all_env_vars


def _handle_builds(
    compose_data: dict, compose_file_path: Path, deployment_name: str, yes: bool = False
) -> str:
    """Handle building and pushing images if needed."""
    view = DeployView(console)
    services_to_build = []

    for service_name, service_config in compose_data.get("services", {}).items():
        if "build" in service_config:
            build_config = service_config.get("build", {})
            if isinstance(build_config, str):
                build_config = {"context": build_config}

            services_to_build.append(
                {
                    "service_name": service_name,
                    "image_name": service_config.get("image", f"{service_name}:latest"),
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

    try:
        # Use regular Live display for build status
        with Live(
            build_status, console=console, refresh_per_second=4, auto_refresh=True
        ):
            if not registry.setup():
                raise typer.Exit(1)

            for i, build_info in enumerate(services_to_build):
                # Update status to building
                build_status.update_service(
                    i, "building", f"Building {build_info['image_name']}"
                )

                context_path = compose_file_path.parent / build_info["context"]

                if not registry.build_image(
                    build_info["image_name"], context_path, build_info["dockerfile"]
                ):
                    build_status.update_service(i, TaskStatus.ERROR, "Build failed")
                    view.show_error(f"Failed to build {build_info['service_name']}")
                    raise typer.Exit(1)

                # Update status to pushing
                build_status.update_service(
                    i, TaskStatus.PENDING, "Pushing to registry"
                )

                if not registry.push_image(build_info["image_name"]):
                    build_status.update_service(i, TaskStatus.ERROR, "Push failed")
                    raise typer.Exit(1)

                if registry.credentials and registry.credentials.repository:
                    full_repo = registry.credentials.repository
                    if "/" in full_repo:
                        image_with_tag = full_repo.split("/")[-1]
                        service_config = compose_data["services"][
                            build_info["service_name"]
                        ]
                        service_config["image"] = image_with_tag

                build_status.update_service(i, TaskStatus.COMPLETED, "Ready")

    finally:
        registry.cleanup()

    return yaml.dump(compose_data, default_flow_style=False)


def _get_existing_deployment(deployment_name: str):
    """Get existing deployment if it exists."""
    try:
        return api.deployments.get_deployment(name=deployment_name)

    except Exception:
        return None


def _show_and_confirm_changes(
    existing_deployment,
    deployment_name: str,
    compose_yaml: str,
    env_files_content: dict[str, str],
    yes: bool,
    warnings: bool,
) -> DiffResponse | None:
    """Show diff and get confirmation for changes. Returns DiffResponse to proceed, None to cancel."""
    view = DeployView(console)

    if existing_deployment:
        deployment_id = existing_deployment.id
        deployment_name_param = None
    else:
        deployment_id = "new"
        deployment_name_param = deployment_name

    try:
        # Extract env keys from compose for diff
        compose_data = yaml.safe_load(compose_yaml)
        env_keys = _extract_env_variables(compose_data, env_files_content).keys()

        diff_response = api.diff.get_deployment_diff(
            deployment_id=deployment_id,
            compose_yaml=compose_yaml,
            deployment_name=deployment_name_param,
            env_keys=list(env_keys),
        )

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
            )

            if not confirmed:
                view.show_cancelled()
                raise typer.Exit(0)

        return diff_response

    except typer.Exit:
        # Re-raise Exit exceptions (they should not be caught)
        raise

    except Exception as e:
        view.show_warning(f"Could not generate diff: {e}")
        # Ask for confirmation anyway
        if not yes:
            message = (
                "Continue with deployment anyway?"
                if existing_deployment
                else "Create deployment anyway?"
            )
            if not view.confirm_continue(message):
                view.show_cancelled()
                raise typer.Exit(0)
        return diff_response


def _run_validation(
    deployment_name: str, compose_yaml: str, env_files_content: dict, warnings: bool
):
    """Run validation in dry-run mode."""
    view = DeployView(console)

    try:
        task_response = api.deployments.create_deployment(
            compose_yaml=compose_yaml,
            name=deployment_name,
        )

        if task_response:
            view.show_summary(
                deployment_name=deployment_name,
                status=task_response.status,
                duration=0,
                message=task_response.message,
            )
        else:
            view.show_summary(
                deployment_name=deployment_name,
                status=task_response.status,
                duration=0,
                message=task_response.message,
            )

    except Exception as e:
        view.show_error(f"Validation failed: {e}")
        raise typer.Exit(1)


def _deploy(
    deployment_name: str,
    compose_yaml: str,
    lazycloud_file: LazyCloudFile,
    timeout: int = 5,
    secrets: SecretCollection | None = None,
):
    """Perform the actual deployment."""
    view = DeployView(console)

    # Create a deployment creation progress card
    creation_progress = view.show_deployment_creation_progress(deployment_name)

    try:
        # Show the card while creating deployment
        with Live(creation_progress, console=console, refresh_per_second=4):
            # Create the deployment and get task ID
            creation_progress.update_status(
                "creating", "Sending configuration to server..."
            )
            task_response = api.deployments.create_deployment(
                compose_yaml=compose_yaml,
                name=deployment_name,
                secrets=bool(secrets),
            )

            if not task_response or not task_response.task_id:
                creation_progress.update_status(
                    "failed", "Failed to create deployment task"
                )
                time.sleep(1)
                raise Exception("Failed to create deployment task")

            # Store secrets if we have any
            if secrets:
                try:
                    api.secrets.store_secrets(task_response.deployment_id, secrets)

                except Exception as e:
                    view.show_error(f"Failed to store secrets: {e}")
                    raise typer.Exit(1)

            # Poll task status
            creation_progress.update_status(
                "creating", "Processing deployment request..."
            )
            start_time = time.time()

            # convert timeout to seconds
            timeout = timeout * 60

            while True:
                # Check timeout
                if time.time() - start_time > timeout:
                    creation_progress.update_status("failed", "Deployment timed out")
                    raise Exception("Deployment creation timed out")

                # Get task status
                task_status = api.tasks.get_task_status(task_response.task_id)

                if task_status.status == TaskStatus.COMPLETED:
                    creation_progress.update_status(
                        TaskStatus.COMPLETED, "Deployment created successfully!"
                    )
                    break
                elif task_status.status == TaskStatus.ERROR:
                    error_msg = getattr(task_status, "error", "Task failed")
                    creation_progress.update_status("failed", error_msg)
                    raise Exception(f"Deployment failed: {error_msg}")
                elif task_status.status == TaskStatus.PENDING:
                    creation_progress.update_status(
                        "creating", "Deployment in progress..."
                    )

                time.sleep(1)

        if task_status and not task_status.status == TaskStatus.COMPLETED:
            view.show_error(f"Deployment failed: {task_status.message}")
            raise typer.Exit(1)

        # Show final success message
        view.show_summary(
            deployment_name=deployment_name,
            status=task_status.status,
            duration=0,
            message="View deployment in the dashboard with 'lazycloud dashboard'",
        )

    except Exception as e:
        # The error was already shown in the progress card, just exit
        view.show_error(f"Deployment failed: {e}")
        raise typer.Exit(1)
