import asyncio
import subprocess
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
from shared.models.diffs import ComposeDiff, EnvVarChanges, ResourceSection
from shared.models.secrets import Secret, SecretCollection, SecretSource
from shared.models.statuses import TaskStatus
from shared.requests.deployments import DiffType
from shared.responses.deployments import DiffResponse

console = Console()


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

    # Normalize service to list (empty list means deploy all)
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
    # If service-specific deploy, only extract env vars for target services
    if target_services:
        all_env_vars = {}
        for target_service in target_services:
            service_env_vars = _extract_env_variables_for_service(
                compose_data, env_files_content, target_service=target_service
            )
            all_env_vars.update(service_env_vars)
    else:
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

    try:
        # Handle image building AFTER validation passes
        compose_yaml = _handle_builds(
            compose_data,
            compose_file_path,
            deployment_name,
            yes,
            target_services=target_services if target_services else None,
        )
    except typer.Exit:
        # Re-raise typer.Exit from build failures
        raise
    except Exception as e:
        view.show_error(f"Build stage failed: {e}")
        raise typer.Exit(1)

    # Deploy
    try:
        # For API, pass first service if multiple (API currently expects single service)
        # TODO: Update API to accept multiple services
        _deploy(
            deployment_name,
            compose_yaml,
            secrets=secrets if has_secrets else None,
            service_name=target_services[0]
            if target_services and len(target_services) == 1
            else None,
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

    # If git available, use SHA; otherwise fall back to timestamp
    if git_sha:
        if has_changes:
            # Add dirty flag with timestamp for uniqueness
            base_tag = f"{git_sha}-dirty-{int(datetime.now(UTC).timestamp())}"
        else:
            base_tag = git_sha

    else:
        # Fall back to timestamp for non-git repos
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
    if isinstance(env_file_config, str):
        env_file_config = [env_file_config]

    for env_file in env_file_config:
        if env_file in env_files_content:
            content = env_files_content[env_file]
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


def _handle_builds(
    compose_data: dict,
    compose_file_path: Path,
    deployment_name: str,
    yes: bool = False,
    target_services: list[str] | None = None,
) -> str:
    """Handle building and pushing images if needed, optionally for only target services"""
    view = DeployView(console)
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

        # Batch check all images at once
        all_image_names = [build_info["image_name"] for build_info in services_to_build]
        image_exists_map = registry.images_exist(all_image_names)

        for i, build_info in enumerate(services_to_build):
            # Check if image already exists in registry
            image_name = build_info["image_name"]
            if image_exists_map.get(image_name, False):
                build_status.update_service(
                    i, TaskStatus.COMPLETED, f"Using cached image: {image_name}"
                )
                continue

            # Update status to building
            build_status.update_service(i, "building", f"Building {image_name}")

            context_path = compose_file_path.parent / build_info["context"]

            build_response = registry.build_image(
                image_name, context_path, build_info["dockerfile"]
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
    service_name: str | None = None,
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
                    service_name=service_name,
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
