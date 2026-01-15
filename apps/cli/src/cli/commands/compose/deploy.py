import asyncio
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml
from api_requests.deployments import DiffType
from models.build_args import BuildArg, BuildArgsCollection, ServiceBuildArgs
from models.diffs import ComposeDiff, EnvVarChanges, ResourceSection
from models.monitoring import DeployOverallPhase
from models.secrets import Secret, SecretCollection, SecretSource
from models.statuses import TaskStatus
from responses.builds import DepotTokenResponse, is_retryable_build_error
from responses.deployments import DiffResponse
from rich.console import Console
from rich.live import Live
from rich.prompt import Confirm

from cli.api import APIError, api
from cli.config import config
from cli.lazycloud_file import LazyCloudFile
from cli.ui.colors import Colors
from cli.ui.components.card import Card
from cli.ui.components.deploy_progress import ServiceStatusDisplay
from cli.ui.components.info_cards import ErrorCard
from cli.ui.views import DeployView
from cli.ui.views.helpers.env_helpers import (
    ImportMethod,
    count_collected_secrets,
    update_diff_with_collected_secrets,
)

console = Console()

# Regex to match depot build step lines (e.g., "#1 [depot] build:", "#10 DONE 2.4s")
DEPOT_BUILD_STEP_PATTERN = re.compile(r"^#\d+")


def _get_service_names_from_diff(diff_response: DiffResponse | None) -> list[str]:
    """Get all service names from the diff (added, modified, removed)."""
    if not diff_response:
        return []

    service_names = set()

    # Added services
    for added in diff_response.diff.services.added:
        if isinstance(added, dict) and "name" in added:
            service_names.add(added["name"])

    # Modified services
    for service_name in diff_response.diff.services.modified.keys():
        service_names.add(service_name)

    # Removed services (may still have env vars to clean up)
    for removed in diff_response.diff.services.removed:
        if isinstance(removed, dict) and "name" in removed:
            service_names.add(removed["name"])

    return list(service_names)


def _get_services_to_build_from_diff(
    diff_response: DiffResponse | None,
    compose_data: dict,
    user_target_services: list[str] | None = None,
) -> list[str]:
    """Get list of services that need to be built based on diff response"""
    if not diff_response:
        # No diff available, fall back to user targets or all buildable services
        return user_target_services or []

    # Get services from diff (added + modified)
    diff_services = set()

    # Added services
    for added in diff_response.diff.services.added:
        if isinstance(added, dict) and "name" in added:
            diff_services.add(added["name"])

    # Modified services
    for service_name in diff_response.diff.services.modified.keys():
        diff_services.add(service_name)

    # Filter to only services with build config
    buildable_services = []
    for service_name in diff_services:
        service_config = compose_data.get("services", {}).get(service_name, {})
        if "build" in service_config:
            buildable_services.append(service_name)

    # If user specified targets, intersect with diff services
    if user_target_services:
        buildable_services = [
            s for s in buildable_services if s in user_target_services
        ]

    return buildable_services


def deploy(
    service: list[str] | None = typer.Option(
        None,
        "-s",
        "--service",
        help="Deploy only these specific services (can be specified multiple times, e.g., -s api -s worker)",
    ),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation prompt"),
    warnings: bool = typer.Option(False, "--warnings", help="Show validation warnings"),
    env: str = typer.Option(
        None,
        "--env",
        help="Source for environment variables: path to .env file or 'shell'",
    ),
    build_arg: str = typer.Option(
        None,
        "--build-arg",
        help="Source for build arguments: path to .env file or 'shell'",
    ),
):
    """Deploy or update a Docker Compose application."""
    view = DeployView(console)

    try:
        # Load configuration
        _, lazycloud_config, compose_file_path = _load_configuration(view)
        deployment_name = lazycloud_config.deployment_name
    except typer.Exit:
        raise
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

    # Track if user explicitly specified services with -s flag
    user_specified_services = bool(service)
    target_services = list(service) if service else []

    # Validate services exist in compose file if specified
    if target_services:
        services_in_compose = compose_data.get("services", {})
        invalid_services = [s for s in target_services if s not in services_in_compose]
        if invalid_services:
            available_services = ", ".join(services_in_compose.keys())
            view.show_error(
                f"Service(s) '{', '.join(invalid_services)}' not found in compose file",
                suggestion=f"Available services: {available_services}",
            )
            raise typer.Exit(1)

    # For service-specific deployments, we need to get existing compose YAML first
    # to preserve non-target service image tags
    if target_services:
        try:
            # Get diff just to fetch existing compose YAML
            initial_compose_yaml = yaml.dump(compose_data, default_flow_style=False)
            initial_diff = _get_deployment_diff(
                deployment_name,
                initial_compose_yaml,
                env_files_content,
                is_service_specific=True,
            )

            # Preserve existing images for non-target services
            if initial_diff and initial_diff.existing_compose_yaml:
                existing_compose_data = yaml.safe_load(
                    initial_diff.existing_compose_yaml
                )
                # Preserve images for all non-target services
                for target_service in target_services:
                    _preserve_existing_images(
                        compose_data,
                        existing_compose_data,
                        target_service=target_service,
                    )
            elif initial_diff:
                # No existing compose means this is a new deployment - error already handled in validation
                pass

        except Exception as e:
            view.show_warning(
                f"Could not preserve existing image tags: {e}. "
                "Non-target services may be updated."
            )

    # Generate git-based tags for built images
    service_tags = _generate_service_tags(
        compose_data,
        compose_file_path.parent,
        target_services=target_services if target_services else None,
    )
    _apply_service_tags(compose_data, service_tags)

    # Regenerate YAML with tagged images (now with preserved + new tags)
    compose_yaml = yaml.dump(compose_data, default_flow_style=False)

    # Now get the REAL diff with the final compose YAML to show to user
    try:
        diff_response = _get_deployment_diff(
            deployment_name,
            compose_yaml,
            env_files_content,
            is_service_specific=bool(target_services),
        )

    except Exception as e:
        view.show_error(f"Error validating deployment: {e}")
        raise typer.Exit(1)

    # Extract environment variables for secrets collection
    # Only extract env vars for services in the diff (respects lazycloud.ignore)
    diff_service_names = _get_service_names_from_diff(diff_response)

    if target_services:
        # User specified targets - intersect with diff services
        services_for_env = (
            [s for s in target_services if s in diff_service_names]
            if diff_service_names
            else target_services
        )
    else:
        # No user targets - use all services from diff
        services_for_env = (
            diff_service_names
            if diff_service_names
            else list(compose_data.get("services", {}).keys())
        )

    all_env_vars = {}
    for service_name in services_for_env:
        service_env_vars = _extract_env_variables_for_service(
            compose_data, env_files_content, target_service=service_name
        )
        all_env_vars.update(service_env_vars)

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

    # Detect cross-service impacts if deploying specific services
    if target_services and diff_response:
        affected_services = _detect_cross_service_impacts(
            diff_response, compose_data, target_services, env_files_content
        )

        if affected_services:
            # Check if affected services are already in target list
            missing_services = [
                s for s in affected_services if s not in target_services
            ]

            if missing_services:
                suggested_services = sorted(
                    list(set(target_services + missing_services))
                )
                suggested_cmd = f"lazycloud deploy -s {' -s '.join(suggested_services)}"

                view.show_warning(
                    f"Changes to service(s) '{', '.join(target_services)}' would affect other services.",
                    suggestion=f"Affected services: {', '.join(missing_services)}\n"
                    f"Suggested command: {suggested_cmd}\n"
                    f"Proceeding with current selection...",
                )

    # Filter diff for target services (always filter, but for full deployments use all services)
    if diff_response:
        if not target_services:
            target_services = list(compose_data.get("services", {}).keys())

        diff_response = _filter_diff_for_services(
            diff_response,
            compose_data,
            target_services=target_services,
            env_files_content=env_files_content,
        )

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

    # Create deployment record BEFORE building images
    # This allows build token endpoint to find the deployment
    try:
        deployment = api.deployments.create_deployment(
            compose_yaml=compose_yaml,
            workspace_id=config.active_workspace_id,
            name=deployment_name,
        )

    except APIError as e:
        if e.status_code == 401:
            view.show_error(
                "Authentication failed",
                suggestion="Run 'lazycloud login' to authenticate",
            )
        else:
            view.show_error(f"Failed to create deployment: {e}")

        raise typer.Exit(1)

    except Exception as e:
        view.show_error(f"Failed to create deployment: {e}")
        raise typer.Exit(1)

    # Wrap build/deploy in try/except for cleanup on failure
    # If build or deploy fails, delete the PENDING deployment record to release quota
    try:
        # Handle image building AFTER deployment record created
        services_to_build = _get_services_to_build_from_diff(
            diff_response,
            compose_data,
            user_target_services=target_services if target_services else None,
        )

        # Extract and collect build args for services that need building
        build_args = None
        if services_to_build:
            # Extract user-defined build args from compose
            build_args = _extract_build_args(
                compose_data, env_files_content, services_to_build
            )

            # Collect any missing build arg values from user
            if build_args.has_args_to_collect():
                build_args = view.collect_build_args(
                    build_args,
                    project_dir=compose_file_path.parent,
                    build_arg_source=build_arg,
                    skip_prompts=yes or bool(build_arg),
                )

        compose_yaml, build_duration = _handle_builds(
            compose_data,
            compose_file_path,
            deployment_name,
            yes,
            target_services=services_to_build if services_to_build else None,
            build_args=build_args,
        )

        # Deploy (updates compose_yaml and triggers deployment in one call)
        _deploy(
            deployment_id=str(deployment.id),
            deployment_name=deployment_name,
            compose_yaml=compose_yaml,
            secrets=secrets if has_secrets else None,
            service_names=target_services if user_specified_services else None,
            build_duration=build_duration,
        )

    except typer.Exit:
        # Build or deploy failed - clean up the PENDING deployment record
        _cleanup_failed_deployment(str(deployment.id), view)
        raise

    except Exception as e:
        # Unexpected error - clean up and show error
        _cleanup_failed_deployment(str(deployment.id), view)
        view.show_error(f"Deployment failed: {e}")
        raise typer.Exit(1)


def _cleanup_failed_deployment(deployment_id: str, view: DeployView) -> None:
    """Clean up a failed deployment by deleting the PENDING record.

    This releases the quota slot that was reserved when the deployment
    record was created. Best-effort cleanup - errors are logged but don't
    prevent the original error from propagating.
    """
    try:
        api.deployments.delete_deployment_sync(deployment_id)
    except Exception as cleanup_error:
        # Log but don't fail - the original error is more important
        view.show_warning(f"Could not clean up deployment record: {cleanup_error}")


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
    except typer.Exit:
        raise
    except Exception as e:
        view.show_error(f"Failed to read .lazycloud file: {e}")
        raise typer.Exit(1)

    compose_file_path = lazycloud_file.get_compose_file_path()
    if not compose_file_path.exists():
        view.show_error(f"Compose file not found: {compose_file_path}")
        raise typer.Exit(1)

    return lazycloud_file, lazycloud_config, compose_file_path


def _normalize_env_file_entries(env_file_config: str | list) -> list[tuple[str, bool]]:
    """Normalize env_file config to list of (path, required) tuples"""
    if isinstance(env_file_config, str):
        env_file_config = [env_file_config]

    result = []
    for entry in env_file_config:
        if isinstance(entry, dict):
            path = entry.get("path", "")
            required = entry.get("required", True)
        else:
            path = entry
            required = True
        if path:
            result.append((path, required))
    return result


def _read_env_files(compose_data: dict, base_path: Path) -> dict[str, str]:
    """Read all env files referenced in the compose file."""
    env_files_content = {}

    for _, service_config in compose_data.get("services", {}).items():
        env_file_config = service_config.get("env_file", [])

        for env_path, required in _normalize_env_file_entries(env_file_config):
            env_file_path = base_path / env_path
            if env_file_path.exists() and env_path not in env_files_content:
                try:
                    env_files_content[env_path] = env_file_path.read_text()
                except Exception:
                    raise typer.Exit(1)

    return env_files_content


def _generate_build_timestamp() -> str:
    """Generate a unique timestamp for build tags."""
    now = datetime.now(UTC)
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond // 1000:03d}"


def _get_git_sha(context: Path) -> str | None:
    """Get current git commit SHA if available."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=context,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _has_uncommitted_changes(context: Path) -> bool:
    """Check if there are uncommitted changes in the git repo."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=context,
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(result.stdout.strip())

    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _generate_service_tags(
    compose_data: dict, context: Path, target_services: list[str] | None = None
) -> dict[str, str]:
    """Generate tags for each service based on git SHA or timestamp fallback."""
    git_sha = _get_git_sha(context)
    has_changes = _has_uncommitted_changes(context)

    # If git available, use SHA; otherwise fall back to timestamp for non-git projects
    if git_sha:
        if has_changes:
            # if dirty git - add timestamp to the tag
            base_tag = f"{git_sha}-{int(datetime.now(UTC).timestamp())}"
        else:
            base_tag = git_sha

    else:
        base_tag = _generate_build_timestamp()

    target_services_set = set(target_services) if target_services else None

    service_tags = {}
    for service_name, service_config in compose_data.get("services", {}).items():
        if "build" in service_config:
            # Only generate tags for services that will be built
            if target_services_set is None or service_name in target_services_set:
                # Per-service tags: {service}-{git-sha}
                service_tags[service_name] = f"{service_name}-{base_tag}"

    return service_tags


def _preserve_existing_images(
    compose_data: dict, existing_compose_data: dict, target_service: str
) -> None:
    """Preserve existing image tags for non-target services."""
    existing_services = existing_compose_data.get("services", {})

    for service_name, service_config in compose_data.get("services", {}).items():
        # Skip the target service - it will get a new timestamp
        if service_name == target_service:
            continue

        # If this service exists in the deployed version, preserve its image tag
        if service_name in existing_services:
            existing_image = existing_services[service_name].get("image")
            if existing_image:
                service_config["image"] = existing_image


def _apply_build_timestamps(
    compose_data: dict, timestamp: str, target_service: str | None = None
) -> None:
    """Apply timestamp tags to all services with build sections, or only target service if specified."""
    for service_name, service_config in compose_data.get("services", {}).items():
        if "build" in service_config:
            # Skip non-target services if target is specified
            if target_service and service_name != target_service:
                continue

            # Get original image and replace tag with timestamp
            original_image = service_config.get("image", f"{service_name}:latest")
            if ":" in original_image:
                image_base = original_image.rsplit(":", 1)[0]
            else:
                image_base = original_image

            # Update with timestamped image
            service_config["image"] = f"{image_base}:{timestamp}"


def _apply_service_tags(compose_data: dict, service_tags: dict[str, str]) -> None:
    """Apply per-service tags to services with build sections."""
    for service_name, service_config in compose_data.get("services", {}).items():
        if "build" in service_config and service_name in service_tags:
            # Get original image base
            original_image = service_config.get("image", f"{service_name}:latest")
            if ":" in original_image:
                image_base = original_image.rsplit(":", 1)[0]

            else:
                image_base = original_image

            # Apply service-specific tag
            service_config["image"] = f"{image_base}:{service_tags[service_name]}"


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


def _extract_service_volumes(compose_data: dict, service_name: str) -> set[str]:
    """Extract volume names used by a specific service."""
    service_config = compose_data.get("services", {}).get(service_name)
    if not service_config:
        return set()

    volumes = set()
    volumes_config = service_config.get("volumes", [])

    if isinstance(volumes_config, list):
        for volume_entry in volumes_config:
            if isinstance(volume_entry, str):
                # Format: "volume_name:/path" or "/host:/container"
                parts = volume_entry.split(":")
                if len(parts) >= 2:
                    source = parts[0]
                    # Check if it's a named volume (not a bind mount)
                    if not source.startswith("/") and not source.startswith("."):
                        volumes.add(source)

            elif isinstance(volume_entry, dict):
                # Format: {"type": "volume", "source": "volume_name", ...}
                source = volume_entry.get("source")
                if source and not source.startswith("/") and not source.startswith("."):
                    volumes.add(source)

    return volumes


def _extract_service_networks(compose_data: dict, service_name: str) -> set[str]:
    """Extract network names used by a specific service."""
    service_config = compose_data.get("services", {}).get(service_name)
    if not service_config:
        return set()

    networks = set()
    networks_config = service_config.get("networks", [])

    if isinstance(networks_config, list):
        # List format: ["network1", "network2"]
        for network_entry in networks_config:
            if isinstance(network_entry, str):
                networks.add(network_entry)
            elif isinstance(network_entry, dict):
                # Dict format: {"network1": {...}}
                networks.update(network_entry.keys())
    elif isinstance(networks_config, dict):
        # Dict format: {"network1": {...}, "network2": {...}}
        networks.update(networks_config.keys())

    return networks


def _get_services_using_network(compose_data: dict, network_name: str) -> list[str]:
    """Get all services that use a specific network."""
    services_using = []
    for service_name in compose_data.get("services", {}).keys():
        service_networks = _extract_service_networks(compose_data, service_name)
        if network_name in service_networks:
            services_using.append(service_name)
    return services_using


def _get_services_using_volume(compose_data: dict, volume_name: str) -> list[str]:
    """Get all services that use a specific volume."""
    services_using = []
    for service_name in compose_data.get("services", {}).keys():
        service_volumes = _extract_service_volumes(compose_data, service_name)
        if volume_name in service_volumes:
            services_using.append(service_name)
    return services_using


def _get_services_using_env_var(
    compose_data: dict, env_var_name: str, env_files_content: dict | None = None
) -> list[str]:
    """Get all services that use a specific environment variable."""
    if env_files_content is None:
        env_files_content = {}

    services_using = []
    for service_name in compose_data.get("services", {}).keys():
        service_env_vars = _extract_env_variables_for_service(
            compose_data, env_files_content, target_service=service_name
        )
        if env_var_name in service_env_vars:
            services_using.append(service_name)
    return services_using


def _filter_env_var_changes(
    env_var_changes: EnvVarChanges | None,
    compose_data: dict,
    target_services: list[str],
    env_files_content: dict | None = None,
) -> EnvVarChanges | None:
    """Filter env var changes to only include vars used by target services."""
    if not env_var_changes:
        return None

    if env_files_content is None:
        env_files_content = {}

    # Get all env vars used by target services
    target_env_vars = set()
    for service_name in target_services:
        service_env_vars = _extract_env_variables_for_service(
            compose_data, env_files_content, target_service=service_name
        )
        target_env_vars.update(service_env_vars.keys())

    # Filter added/removed to only target service vars
    filtered_added = [v for v in env_var_changes.added if v in target_env_vars]
    filtered_removed = [v for v in env_var_changes.removed if v in target_env_vars]

    return EnvVarChanges(
        added=filtered_added,
        removed=filtered_removed,
        existing=env_var_changes.existing,  # Keep all existing
        user_managed=env_var_changes.user_managed,  # Keep all user managed
    )


def _detect_cross_service_impacts(
    diff_response: DiffResponse,
    compose_data: dict,
    target_services: list[str],
    env_files_content: dict | None = None,
) -> list[str]:
    """Detect which other services would be affected by target service changes."""
    if not diff_response or not diff_response.diff:
        return []

    if env_files_content is None:
        env_files_content = {}

    target_services_set = set(target_services)
    all_services = set(compose_data.get("services", {}).keys())
    other_services = all_services - target_services_set
    affected_services = set()

    # Check networks
    if diff_response.diff.networks.has_changes():
        target_networks = set()
        for service_name in target_services:
            target_networks.update(
                _extract_service_networks(compose_data, service_name)
            )

        for network_name in target_networks:
            services_using = _get_services_using_network(compose_data, network_name)
            affected_services.update(s for s in services_using if s in other_services)

    # Check volumes
    if diff_response.diff.volumes.has_changes():
        target_volumes = set()
        for service_name in target_services:
            target_volumes.update(_extract_service_volumes(compose_data, service_name))

        for volume_name in target_volumes:
            services_using = _get_services_using_volume(compose_data, volume_name)
            affected_services.update(s for s in services_using if s in other_services)

    # Check env vars
    if diff_response.env_var_changes:
        target_env_vars = set()
        for service_name in target_services:
            service_env_vars = _extract_env_variables_for_service(
                compose_data, env_files_content, target_service=service_name
            )
            target_env_vars.update(service_env_vars.keys())

        changed_vars = set(diff_response.env_var_changes.added) | set(
            diff_response.env_var_changes.removed
        )
        for env_var in changed_vars & target_env_vars:
            services_using = _get_services_using_env_var(
                compose_data, env_var, env_files_content
            )
            affected_services.update(s for s in services_using if s in other_services)

    return sorted(list(affected_services))


def _filter_diff_for_services(
    diff_response: DiffResponse,
    compose_data: dict,
    target_services: list[str],
    env_files_content: dict | None = None,
) -> DiffResponse:
    """Filter diff to show only target services and related resources."""
    if not diff_response or not diff_response.diff:
        return diff_response

    if not target_services:
        return diff_response

    # Convert to set for efficient lookup
    target_services_set = set(target_services)

    # Get volumes used by all target services (union)
    used_volumes = set()
    for service_name in target_services:
        used_volumes.update(_extract_service_volumes(compose_data, service_name))

    # Filter services: only target services
    original_services = diff_response.diff.services
    filtered_services = ResourceSection(
        added=[
            s
            for s in original_services.added
            if isinstance(s, dict) and s.get("name") in target_services_set
        ],
        modified={
            k: v
            for k, v in original_services.modified.items()
            if k in target_services_set
        },
        removed=[
            s
            for s in original_services.removed
            if isinstance(s, dict) and s.get("name") in target_services_set
        ],
    )

    # Filter volumes: only used volumes (deduplicated automatically via set)
    original_volumes = diff_response.diff.volumes
    filtered_volumes = ResourceSection(
        added=[
            v
            for v in original_volumes.added
            if isinstance(v, dict) and v.get("name") in used_volumes
        ],
        modified={
            k: v for k, v in original_volumes.modified.items() if k in used_volumes
        },
        removed=[
            v
            for v in original_volumes.removed
            if isinstance(v, dict) and v.get("name") in used_volumes
        ],
    )

    # Networks: keep all (shared infrastructure)
    filtered_networks = diff_response.diff.networks

    # Filter env var changes to only target services
    filtered_env_var_changes = _filter_env_var_changes(
        diff_response.env_var_changes,
        compose_data,
        target_services,
        env_files_content,
    )

    # Create filtered compose diff
    filtered_compose_diff = ComposeDiff(
        services=filtered_services,
        volumes=filtered_volumes,
        networks=filtered_networks,
    )

    # Return new diff response with filtered compose diff
    return DiffResponse(
        deployment_id=diff_response.deployment_id,
        namespace=diff_response.namespace,
        has_changes=filtered_compose_diff.has_changes(),
        diff=filtered_compose_diff,
        env_var_changes=filtered_env_var_changes,
        errors=diff_response.errors,
        warnings=diff_response.warnings,
        can_deploy=diff_response.can_deploy,
        existing_compose_yaml=diff_response.existing_compose_yaml,
    )


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


def _extract_env_variables_for_service(
    compose_data: dict, env_files_content: dict, target_service: str
) -> dict[str, str | None]:
    """Extract environment variables only for target service."""
    service_config = compose_data.get("services", {}).get(target_service)
    if not service_config:
        return {}

    env_vars = {}

    # Parse env_file for this service only
    env_file_config = service_config.get("env_file", [])

    for env_path, _ in _normalize_env_file_entries(env_file_config):
        if env_path in env_files_content:
            content = env_files_content[env_path]
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
                    env_vars[key] = value if value else None

    # Parse environment section for this service
    env_config = service_config.get("environment", {})

    if isinstance(env_config, dict):
        for key, value in env_config.items():
            if value is None:
                env_vars[key] = None
            else:
                str_value = str(value)
                # Check if it's a placeholder like ${VAR} or $VAR
                if str_value.startswith("${") and str_value.endswith("}"):
                    env_vars[key] = None
                elif str_value.startswith("$"):
                    env_vars[key] = None
                elif str_value == "":
                    env_vars[key] = None
                else:
                    env_vars[key] = str_value

    elif isinstance(env_config, list):
        for env_var in env_config:
            if "=" in env_var:
                key, value = env_var.split("=", 1)
                # Check if it's a placeholder
                if value.startswith("${") and value.endswith("}"):
                    env_vars[key] = None
                elif value.startswith("$"):
                    env_vars[key] = None
                elif value == "":
                    env_vars[key] = None
                else:
                    env_vars[key] = value
            else:
                # Environment variable without value
                env_vars[env_var] = None

    return env_vars


def _extract_build_args(
    compose_data: dict,
    env_files_content: dict,
    services_to_build: list[str] | None = None,
) -> BuildArgsCollection:
    """Extract build arguments from compose services.

    Args:
        compose_data: Parsed compose.yaml data
        env_files_content: Contents of env files
        services_to_build: Optional list of services to extract args for

    Returns:
        BuildArgsCollection with args for each service
    """
    service_build_args: list[ServiceBuildArgs] = []

    for service_name, service_config in compose_data.get("services", {}).items():
        # Skip if not in the list of services to build
        if services_to_build and service_name not in services_to_build:
            continue

        build_config = service_config.get("build")
        if not build_config:
            continue

        # Extract args from build config
        args_config = {}
        if isinstance(build_config, dict):
            args_config = build_config.get("args", {})
        elif isinstance(build_config, str):
            # Simple string format (just context path), no args
            continue

        if not args_config:
            continue

        # Parse args - can be dict or list
        args: list[BuildArg] = []
        if isinstance(args_config, dict):
            for key, value in args_config.items():
                # Check if value needs to be resolved
                resolved_value = _resolve_build_arg_value(
                    value, env_files_content, compose_data
                )
                args.append(BuildArg(key=key, value=resolved_value))
        elif isinstance(args_config, list):
            for item in args_config:
                if isinstance(item, str):
                    if "=" in item:
                        key, value = item.split("=", 1)
                        resolved_value = _resolve_build_arg_value(
                            value, env_files_content, compose_data
                        )
                        args.append(BuildArg(key=key, value=resolved_value))
                    else:
                        # Just a key, needs value from user
                        args.append(BuildArg(key=item, value=None))

        if args:
            service_build_args.append(
                ServiceBuildArgs(service_name=service_name, args=args)
            )

    return BuildArgsCollection(services=service_build_args)


def _resolve_build_arg_value(
    value: str | None,
    env_files_content: dict,
    compose_data: dict,
) -> str | None:
    """Resolve a build arg value, handling variable substitution.

    Returns None if the value needs to be collected from user.
    """
    if value is None:
        return None

    if not isinstance(value, str):
        return str(value)

    # Check for variable substitution patterns
    if value.startswith("${") and value.endswith("}"):
        # Extract variable name (handle default values like ${VAR:-default})
        var_content = value[2:-1]
        var_name = var_content.split(":-")[0].split(":+")[0].split("-")[0]

        # Try to resolve from environment
        env_value = os.environ.get(var_name)
        if env_value:
            return env_value

        # Try to resolve from env files
        for _, content in env_files_content.items():
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    if key.strip() == var_name:
                        return val.strip().strip('"').strip("'")

        # If has default value, use it
        if ":-" in var_content:
            return var_content.split(":-", 1)[1]

        # Could not resolve, needs user input
        return None

    elif value.startswith("$"):
        # Simple variable reference
        var_name = value[1:]
        env_value = os.environ.get(var_name)
        if env_value:
            return env_value
        return None

    # Literal value
    return value


def _is_depot_available() -> bool:
    """Check if Depot CLI is installed and available."""
    return shutil.which("depot") is not None


def _get_depot_token(deployment_name: str) -> DepotTokenResponse | None:
    """Get Depot token from API. Returns None if Depot is not configured."""
    try:
        return api.builds.get_depot_token(deployment_name)
    except APIError as e:
        if e.status_code == 503:
            # Depot not configured on server
            return None
        raise


def _run_depot_build(
    build_info: dict,
    depot_token: DepotTokenResponse,
    compose_file_path: Path,
    build_state: dict,
    build_state_lock: threading.Lock,
    service_build_args: dict[str, str] | None = None,
    max_retries: int = 3,
) -> tuple[bool, str]:
    """Run a single depot build with retry logic. Returns (success, error_message)."""
    context_path = compose_file_path.parent / build_info["context"]
    dockerfile_path = context_path / build_info["dockerfile"]
    service_name = build_info["service_name"]

    # Construct the image tag for Depot registry
    # Format: registry.depot.dev/<project_id>:<tag>
    image_tag = build_info["image_name"]  # e.g., "api:api-08562c3-1768277453"
    # Extract just the tag part (after the colon) for save-tag
    tag_name = image_tag.split(":")[-1] if ":" in image_tag else image_tag

    # Build depot command with --save to store in Depot's registry
    depot_cmd = [
        "depot",
        "build",
        "--project",
        depot_token.project_id,
        "--platform",
        "linux/amd64",  # Build for amd64 architecture (EKS nodes)
        "--progress=plain",  # Force plain text output for pipes (not TTY)
        "--save",  # save to Depot's registry
        "--save-tag",  # custom tag for the saved image
        tag_name,
    ]

    # Add build args if provided
    if service_build_args:
        for key, value in service_build_args.items():
            if value is not None:
                depot_cmd.extend(["--build-arg", f"{key}={value}"])

    depot_cmd.extend(
        [
            "-f",
            str(dockerfile_path),
            str(context_path),
        ]
    )

    env = {
        **dict(os.environ),
        "DEPOT_TOKEN": depot_token.token,
    }

    # Retry loop for transient errors
    for attempt in range(1, max_retries + 1):
        try:
            # Stream output in real-time
            # Combine stderr into stdout so we capture all output
            process = subprocess.Popen(
                depot_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Redirect stderr to stdout
                text=True,
                env=env,
                bufsize=1,  # Line buffered
            )

            # Initialize/reset shared state for this service
            with build_state_lock:
                build_state[service_name] = {
                    "output_lines": [],
                    "error_lines": [],
                    "process": process,
                    "status": f"building (attempt {attempt}/{max_retries})"
                    if attempt > 1
                    else "building",
                }
            output_lines = build_state[service_name]["output_lines"]

            def should_filter_line(line: str) -> bool:
                """Only show depot build step lines (e.g., #1, #2, etc.)."""
                stripped = line.lstrip()
                return not DEPOT_BUILD_STEP_PATTERN.match(stripped)

            # Read output line by line until process completes
            while True:
                # Check if process finished
                returncode = process.poll()
                if returncode is not None:
                    break

                # Read line from stdout (non-blocking check)
                line = process.stdout.readline()
                if line:
                    line = line.rstrip()
                    if line and not should_filter_line(line):
                        with build_state_lock:
                            output_lines.append(line)
                else:
                    # No output available, wait a bit before checking again
                    time.sleep(0.1)

            # Wait for process to complete
            process.wait()

            # Read any remaining stdout
            remaining_stdout = process.stdout.read()
            if remaining_stdout:
                for line in remaining_stdout.splitlines():
                    line = line.rstrip()
                    if line and not should_filter_line(line):
                        with build_state_lock:
                            output_lines.append(line)

            # Stderr is redirected to stdout, so we don't need to read it separately
            # Any error messages will already be in output_lines

            # Check if build succeeded
            if process.returncode == 0:
                with build_state_lock:
                    build_state[service_name]["status"] = "complete"
                break  # Success - exit retry loop

            # Build failed - check if error is retryable
            error_text = "\n".join(output_lines) if output_lines else ""

            if is_retryable_build_error(error_text) and attempt < max_retries:
                # Retryable error - try again
                with build_state_lock:
                    build_state[service_name]["status"] = (
                        f"retrying ({attempt + 1}/{max_retries})..."
                    )
                    build_state[service_name]["output_lines"] = [
                        f"[Network error, retrying attempt {attempt + 1}/{max_retries}...]"
                    ]
                time.sleep(2)  # Brief pause before retry
                continue

            # Non-retryable error or last attempt - mark as failed
            with build_state_lock:
                build_state[service_name]["status"] = "failed"

            # Check for common error patterns and provide user-friendly messages
            error_text_lower = error_text.lower()
            if is_retryable_build_error(error_text):
                return (
                    False,
                    f"Network error after {attempt} attempt(s). Please try again.",
                )

            # Look for Dockerfile/build errors
            if (
                "dockerfile" in error_text_lower
                or "failed to solve" in error_text_lower
                or "error processing" in error_text_lower
            ):
                # Extract relevant error line if available
                relevant_lines = [
                    line
                    for line in output_lines
                    if any(
                        keyword in line.lower()
                        for keyword in ["error", "failed", "cannot"]
                    )
                ]

                if relevant_lines:
                    last_error = relevant_lines[-1]
                    # Clean up the error message
                    if len(last_error) > 200:
                        last_error = last_error[:197] + "..."
                    return (
                        False,
                        f"Build configuration error: {last_error}",
                    )

                return (
                    False,
                    "Build configuration error. Please check your Dockerfile and build context.",
                )

            # Generic build failure
            return (
                False,
                "Build failed. Please check your Dockerfile and build context for errors.",
            )

        except Exception:
            # Unexpected error during build
            if attempt < max_retries:
                with build_state_lock:
                    build_state[service_name]["status"] = (
                        f"retrying ({attempt + 1}/{max_retries})..."
                    )
                    build_state[service_name]["output_lines"] = [
                        f"[Unexpected error, retrying attempt {attempt + 1}/{max_retries}...]"
                    ]
                time.sleep(2)
                continue

            return False, "Build failed due to an unexpected error. Please try again."

    return True, ""


def _handle_builds_with_depot(
    compose_data: dict,
    compose_file_path: Path,
    deployment_name: str,
    services_to_build: list[dict],
    depot_token: DepotTokenResponse,
    build_args: BuildArgsCollection | None = None,
) -> str:
    """Handle builds using Depot remote builder with parallel execution."""
    view = DeployView(console)

    # Build all services - Depot handles layer caching internally
    builds_needed = [(i, build_info) for i, build_info in enumerate(services_to_build)]

    if not builds_needed:
        return yaml.dump(compose_data, default_flow_style=False)

    # Shared state for all builds (thread-safe dict)
    build_state = {}
    build_state_lock = threading.Lock()
    build_start_time = datetime.now()

    def render_build_status() -> Card:
        """Render a single panel showing all builds with latest 3 lines each."""
        with build_state_lock:
            content_lines = []
            LATEST_LINES = 3  # Show latest 3 lines per service
            elapsed = int((datetime.now() - build_start_time).total_seconds())

            # Calculate available width for log lines (console - borders - padding - indent)
            term_width = shutil.get_terminal_size().columns
            max_line_width = term_width - 10  # Account for borders, padding, indent

            for service_name, state in build_state.items():
                output_lines = state.get("output_lines", [])
                error_lines = state.get("error_lines", [])
                status = state.get("status", "building")

                # Determine status indicator and color
                if status == "complete":
                    status_indicator = (
                        f"[{Colors.Ansi.success}]✓[/{Colors.Ansi.success}]"
                    )
                    name_color = Colors.Ansi.success
                elif status == "failed":
                    status_indicator = f"[{Colors.Ansi.error}]✗[/{Colors.Ansi.error}]"
                    name_color = Colors.Ansi.error
                else:
                    status_indicator = f"[{Colors.Ansi.accent}]●[/{Colors.Ansi.accent}]"
                    name_color = Colors.Ansi.accent

                # Service header with matching color
                content_lines.append(
                    f"{status_indicator} [bold {name_color}]{service_name}[/bold {name_color}]"
                )

                # Show latest N lines
                display_lines = output_lines[-LATEST_LINES:]
                if display_lines:
                    for line in display_lines:
                        # Truncate long lines to fit terminal
                        if len(line) > max_line_width:
                            line = line[: max_line_width - 3] + "..."
                        content_lines.append(f"    {line}")

                else:
                    content_lines.append("    [dim]Waiting for output...[/dim]")

                # Show errors if any
                if error_lines:
                    for line in error_lines[-2:]:  # Last 2 error lines
                        if len(line) > max_line_width:
                            line = line[: max_line_width - 3] + "..."
                        content_lines.append(
                            f"    [{Colors.Ansi.warning}]⚠ {line}[/{Colors.Ansi.warning}]"
                        )

                content_lines.append("")  # Spacing between services

            # Add elapsed time at the bottom right
            elapsed_text = f"Elapsed: {elapsed}s"
            padding = max_line_width - len(elapsed_text)
            content_lines.append(
                f"[{Colors.Ansi.text_muted}]{' ' * padding}{elapsed_text}[/{Colors.Ansi.text_muted}]"
            )

            content = "\n".join(content_lines).rstrip()

            # Determine overall status for border
            all_complete = all(
                state.get("status") == "complete" for state in build_state.values()
            )
            any_failed = any(
                state.get("status") == "failed" for state in build_state.values()
            )

            if any_failed:
                border_style = Colors.Ansi.error
                title = "Build Status"
            elif all_complete:
                border_style = Colors.Ansi.success
                title = "Build Status"
            else:
                border_style = Colors.Ansi.accent
                title = "Build Status"

            return Card(
                content=content,
                title=f"🔨 {title}",
                border_style=border_style,
                padding=(1, 2),
            )

    # Initialize build state for all services
    with build_state_lock:
        for _, build_info in builds_needed:
            service_name = build_info["service_name"]
            build_state[service_name] = {
                "output_lines": [],
                "error_lines": [],
                "process": None,
                "status": "building",
            }

    # Run builds in parallel with shared state
    build_results = {}

    with ThreadPoolExecutor(max_workers=min(4, len(builds_needed))) as executor:
        futures = {
            executor.submit(
                _run_depot_build,
                build_info,
                depot_token,
                compose_file_path,
                build_state,
                build_state_lock,
                build_args.get_args_for_service(build_info["service_name"])
                if build_args
                else None,
            ): (i, build_info)
            for i, build_info in builds_needed
        }

        # Store errors to show after Live display closes
        build_errors = {}

        # Display all builds in a single Live view
        with Live(render_build_status(), console=console, refresh_per_second=4) as live:
            while futures:
                # Update display
                live.update(render_build_status())

                # Check for completed builds
                done_futures = []
                for future in futures:
                    if future.done():
                        done_futures.append(future)

                for future in done_futures:
                    idx, build_info = futures.pop(future)
                    service_name = build_info["service_name"]
                    try:
                        success, error_message = future.result()
                        build_results[idx] = success
                        if not success:
                            # Store error to show after Live closes
                            build_errors[service_name] = error_message

                            # Fail-fast: Cancel all remaining builds
                            if futures:
                                # Cancel all remaining futures
                                for remaining_future in futures:
                                    remaining_future.cancel()
                                # Terminate all running processes
                                with build_state_lock:
                                    for name, state in build_state.items():
                                        if name != service_name and "process" in state:
                                            proc = state.get("process")
                                            if proc and proc.poll() is None:
                                                try:
                                                    proc.terminate()
                                                    state["status"] = "cancelled"
                                                except Exception:
                                                    pass

                                # Clear remaining futures
                                futures.clear()
                                break

                    except Exception as e:
                        build_results[idx] = False
                        # Provide user-friendly error message
                        error_msg = str(e)
                        if "not found" in error_msg.lower():
                            build_errors[service_name] = (
                                "Build configuration error: Required files not found."
                            )
                        else:
                            build_errors[service_name] = (
                                "Build failed due to an unexpected error."
                            )

                        # Fail-fast: Cancel all remaining builds
                        if futures:
                            for remaining_future in futures:
                                remaining_future.cancel()
                            with build_state_lock:
                                for name, state in build_state.items():
                                    if name != service_name and "process" in state:
                                        proc = state.get("process")
                                        if proc and proc.poll() is None:
                                            try:
                                                proc.terminate()
                                                state["status"] = "cancelled"
                                            except Exception:
                                                pass
                            futures.clear()
                            break

                if futures:
                    time.sleep(0.25)  # Update every 250ms

        # Final update
        live.update(render_build_status())

        # Show errors after Live display is closed
        if build_errors:
            for service_name, error_message in build_errors.items():
                view.show_error(
                    f"Build failed for '{service_name}'", suggestion=error_message
                )

    # Check if any builds failed
    if not all(build_results.values()):
        raise typer.Exit(1)

    # Calculate total build duration
    build_duration = int((datetime.now() - build_start_time).total_seconds())

    return yaml.dump(compose_data, default_flow_style=False), build_duration


def _handle_builds(
    compose_data: dict,
    compose_file_path: Path,
    deployment_name: str,
    yes: bool = False,
    target_services: list[str] | None = None,
    build_args: BuildArgsCollection | None = None,
) -> tuple[str, int | None]:
    """Handle building and pushing images if needed, optionally for only target services."""
    services_to_build = []

    target_services_set = set(target_services) if target_services else None

    for service_name, service_config in compose_data.get("services", {}).items():
        if "build" in service_config:
            # Skip non-target services if targets are specified
            if target_services_set and service_name not in target_services_set:
                continue

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
        return yaml.dump(compose_data, default_flow_style=False), None

    # Remote builds are required - handle errors gracefully
    depot_available = _is_depot_available()

    if not depot_available:
        if platform.system() == "Windows":
            install_cmd = "irm https://lazycloud.dev/install.ps1 | iex"
        else:
            install_cmd = "curl -LsSf https://lazycloud.dev/install.sh | sh"

        error_card = ErrorCard(
            message="Remote build service is not available.",
            title="🔨 Build Error",
            suggestion=f"Install LazyCloud with all dependencies:\n  {install_cmd}",
        )
        console.print(error_card)
        raise typer.Exit(code=1)

    # Get build token - handle errors gracefully
    depot_token = None
    try:
        depot_token = _get_depot_token(deployment_name)
    except APIError as e:
        error_card = ErrorCard(
            message="Remote builds are not available.",
            title="🔨 Build Error",
            suggestion=f"Could not get build token: {e}\nPlease contact support if this issue persists.",
        )
        console.print(error_card)
        raise typer.Exit(code=1)

    # Build status will be shown in card below
    return _handle_builds_with_depot(
        compose_data=compose_data,
        compose_file_path=compose_file_path,
        deployment_name=deployment_name,
        services_to_build=services_to_build,
        depot_token=depot_token,
        build_args=build_args,
    )


def _get_deployment_diff(
    deployment_name: str,
    compose_yaml: str,
    env_files_content: dict[str, str],
    is_service_specific: bool = False,
) -> DiffResponse | None:
    """Get deployment diff without showing or confirming. Returns DiffResponse or None."""
    # For service-specific deploys, deployment must exist (use EXISTING)
    # For normal deploys, try EXISTING first, fall back to NEW
    view = DeployView(console)

    try:
        # Extract env keys from compose for diff
        compose_data = yaml.safe_load(compose_yaml)
        env_keys = _extract_env_variables(compose_data, env_files_content).keys()

        # Try EXISTING first (most deployments are updates)
        try:
            diff_response = api.diff.get_deployment_diff(
                diff_type=DiffType.EXISTING,
                workspace_id=config.active_workspace_id,
                deployment_name=deployment_name,
                compose_yaml=compose_yaml,
                env_keys=list(env_keys),
            )
            return diff_response

        except APIError as e:
            # If 404 and service-specific, error out
            if e.status_code == 404:
                if is_service_specific:
                    view.show_error(
                        f"Cannot deploy single service: deployment '{deployment_name}' does not exist",
                        suggestion="Run 'lazycloud deploy' first to create the deployment",
                    )
                    raise typer.Exit(1)

                # Otherwise, try as NEW deployment
                diff_response = api.diff.get_deployment_diff(
                    diff_type=DiffType.NEW,
                    workspace_id=config.active_workspace_id,
                    deployment_name=deployment_name,
                    compose_yaml=compose_yaml,
                    env_keys=list(env_keys),
                )
                return diff_response
            raise

    except typer.Exit:
        raise

    except Exception as e:
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
            raise typer.Exit(1)

        # Show storage type change warnings (EBS ↔ EFS transitions cause data loss)
        if diff_response.storage_type_changes:
            console.print()
            console.print("[bold yellow]⚠️  STORAGE TYPE CHANGES DETECTED[/bold yellow]")
            console.print(
                "The following volumes will change storage type. "
                "[bold red]DATA WILL BE LOST[/bold red] for these volumes:\n"
            )
            for change in diff_response.storage_type_changes:
                console.print(
                    f"  • [bold]{change.volume_name}[/bold]: "
                    f"[red]{change.old_type}[/red] → [green]{change.new_type}[/green]"
                )
                console.print(f"    Reason: {change.reason}")
            console.print()

            if not yes:
                confirmed_storage = Confirm.ask(
                    "[bold yellow]Do you understand that data will be lost for these volumes?[/bold yellow]",
                    default=False,
                    console=console,
                )
                if not confirmed_storage:
                    view.show_cancelled()
                    raise typer.Exit(0)

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
    deployment_id: str,
    deployment_name: str,
    compose_yaml: str | None = None,
    secrets: SecretCollection | None = None,
    service_names: list[str] | None = None,
    build_duration: int | None = None,
):
    """Trigger deployment and wait for completion with real-time status."""
    view = DeployView(console)

    # Create a deployment creation progress card
    creation_progress = view.show_deployment_creation_progress(deployment_name)
    task_response = None
    final_status = None

    try:
        # Phase 1: Store secrets and trigger deployment
        with Live(creation_progress, console=console, refresh_per_second=4):
            # Store secrets first (BEFORE triggering deploy task!)
            if secrets:
                try:
                    creation_progress.update_status(
                        "creating", "Storing environment variables..."
                    )

                    # Handle new secrets (from diff's "added" list - these should all be new)
                    if secrets.added:
                        try:
                            # Create new secrets - these keys are brand new per the diff
                            api.secrets.store_secrets(deployment_id, secrets.added)
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
                            api.secrets.delete_secrets(deployment_id, secrets.removed)
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

            # Trigger deployment (with updated compose_yaml if provided)
            creation_progress.update_status("creating", "Triggering deployment...")

            try:
                task_response = api.deployments.deploy_deployment(
                    deployment_id=deployment_id,
                    compose_yaml=compose_yaml,
                    secrets=bool(secrets),
                    service_names=service_names,
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
                raise Exception(f"Failed to trigger deployment: {e}")

            creation_progress.update_status(
                "creating", "Deployment started, monitoring services..."
            )

        # Phase 2: Wait for task and stream service status
        service_display = ServiceStatusDisplay(deployment_name)
        stream_error: Exception | None = None

        async def monitor_deployment():
            nonlocal final_status, stream_error

            def on_progress(data: dict):
                service_display.update(data)

            def on_stream_error(error: Exception):
                nonlocal stream_error
                stream_error = error

            # Start both the task wait and service status stream
            task_wait = asyncio.create_task(
                api.deployments.wait_for_deployment(task_response.task_id)
            )

            try:
                # Stream service status until complete or task finishes
                progress_stream = asyncio.create_task(
                    api.deployments.stream_deploy_progress(
                        deployment_id=deployment_id,
                        on_progress=on_progress,
                        on_error=on_stream_error,
                    )
                )

                # Wait for task completion (this is the authoritative signal)
                final_status = await task_wait

                # Give the progress stream a moment to receive final status update
                # before cancelling (the server may have one more update pending)
                if final_status and final_status.status == TaskStatus.COMPLETED:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(progress_stream), timeout=1.0
                        )
                    except asyncio.TimeoutError:
                        pass
                    except asyncio.CancelledError:
                        pass

                # Cancel progress stream
                progress_stream.cancel()
                try:
                    await progress_stream

                except asyncio.CancelledError:
                    pass

                # Set display state based on task completion
                if final_status:
                    if final_status.status == TaskStatus.COMPLETED:
                        service_display.overall = DeployOverallPhase.COMPLETED
                    elif final_status.status == TaskStatus.ERROR:
                        service_display.overall = DeployOverallPhase.FAILED

            except Exception as e:
                stream_error = e
                # Still try to get task result
                if not task_wait.done():
                    task_wait.cancel()

                    try:
                        await task_wait
                    except asyncio.CancelledError:
                        pass

        # Run with Live display showing service status
        with Live(service_display, console=console, refresh_per_second=4) as live:
            # Run the async monitoring
            try:
                asyncio.run(monitor_deployment())
            except Exception as e:
                if stream_error is None:
                    stream_error = e

            # Keep refreshing until we have final status
            if final_status is None and stream_error:
                service_display.overall = DeployOverallPhase.FAILED
                service_display.failure_message = str(stream_error)
                live.update(service_display)

        # Handle results
        if stream_error and final_status is None:
            raise Exception(f"Error monitoring deployment: {stream_error}")

        if final_status is None:
            raise Exception("Deployment monitoring ended without status")

        # Update UI based on actual status
        if final_status.status == TaskStatus.COMPLETED:
            # Update last_deployed timestamp in .lazycloud file
            try:
                lazycloud_file = LazyCloudFile.find_and_load(Path.cwd())
                if lazycloud_file:
                    lazycloud_file.update(last_deployed=datetime.now(UTC))

            except Exception:
                # Don't fail deployment if we can't update the timestamp
                pass

        elif final_status.status == TaskStatus.ERROR:
            error_msg = final_status.message or "Task failed"
            # Add failure message from service display if available
            if service_display.failure_message:
                error_msg = f"{error_msg}\n{service_display.failure_message}"
            raise Exception(f"Deployment failed: {error_msg}")

        else:
            raise Exception(
                f"Deployment ended with unexpected status: {final_status.status}"
            )

        # Show final success message
        view.show_summary(
            deployment_name=deployment_name,
            status=final_status.status,
            duration=service_display.elapsed_seconds,
            build_duration=build_duration,
            message="View deployment in the dashboard with 'lazycloud dashboard'",
        )

    except typer.Exit:
        raise

    except Exception as e:
        view.show_error(str(e))
        raise typer.Exit(1)
