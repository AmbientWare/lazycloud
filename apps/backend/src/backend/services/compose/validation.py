"""Validation functions for deployments that run before queuing tasks."""

import yaml
from loguru import logger
from models.helm import HelmValues

from backend.billing.product_details.features import BaseFeatures
from backend.config import app_config
from backend.database import get_db_context
from backend.database.models import ComposeDeployment
from backend.services.compose.parser import ComposeParser
from backend.services.k8s.helm_values_generator import HelmValuesGenerator
from backend.tasks.core.utils import verify_quota_capacity


async def validate_deployment_request(
    deployment: ComposeDeployment,
    existing_deployment: ComposeDeployment | None = None,
    service_names: list[str] | None = None,
    features: BaseFeatures | None = None,
) -> tuple[HelmValues, list[str]]:
    """Validate deployment request before queuing task.

    Validates compose YAML, generates Helm values, and checks quotas.
    Raises ValueError with user-friendly message if validation fails.

    Returns:
        Tuple of (helm_values, warnings)
    """
    compose_yaml = deployment.pending_compose_yaml or deployment.compose_yaml

    # Parse YAML
    try:
        compose_data = yaml.safe_load(compose_yaml)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML format: {str(e)}") from e

    # Parse compose file
    try:
        compose_file = ComposeParser.parse_dict(compose_data)
    except Exception as e:
        raise ValueError(f"Failed to parse compose file: {str(e)}") from e

    # Validate service_names if specified
    if service_names:
        available_services = [service.name for service in compose_file.services]
        invalid_services = [s for s in service_names if s not in available_services]
        if invalid_services:
            available = ", ".join(available_services)
            raise ValueError(
                f"Service(s) '{', '.join(invalid_services)}' not found in compose file. "
                f"Available services: {available}"
            )

    # Check size
    compose_yaml_size = len(compose_yaml.encode("utf-8"))
    if compose_yaml_size > app_config.COMPOSE_YAML_MAX_SIZE_BYTES:
        raise ValueError(
            f"Compose file is too large ({compose_yaml_size} bytes). "
            f"Maximum size is {app_config.COMPOSE_YAML_MAX_SIZE_BYTES} bytes."
        )

    # Get secrets (only if deployment exists)
    secrets = []
    if deployment.id:
        async with get_db_context() as db:
            secrets = await db.secrets.get_secrets(deployment.id)

    # Generate Helm values (this validates compose file)
    try:
        helm_generator = HelmValuesGenerator(deployment, secrets, features)
        helm_values, warnings = helm_generator.generate_values(compose_file)
        helm_values.compose_yaml = compose_yaml

    except ValueError as e:
        # ComposeValidator errors are already user-friendly
        logger.exception(f"ComposeValidator error: {e}")
        raise ValueError(str(e)) from e

    except Exception as e:
        logger.exception(f"Helm generation failed: {e}")
        raise ValueError(
            f"Failed to generate deployment configuration: {str(e)}"
        ) from e

    # Verify quota capacity (only for new deployments, not updates)
    try:
        await verify_quota_capacity(
            deployment.workspace_id,
            is_update=existing_deployment is not None,
        )
    except ValueError as e:
        raise ValueError(str(e)) from e

    return helm_values, warnings
