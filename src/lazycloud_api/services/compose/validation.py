"""Validation functions for deployments that run before queuing tasks."""

import yaml

from lazycloud_api.config import app_config
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.prefect_app.deployment.utils import verify_quota_capacity
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.k8s import create_release_name
from lazycloud_api.services.k8s.helm_manager import HelmManager
from lazycloud_api.services.k8s.helm_values_generator import HelmValuesGenerator
from shared.models.deployments import ResourceRequirements
from shared.models.helm import HelmValues
from shared.models.k8s import WorkloadType


async def validate_deployment_request(
    deployment: ComposeDeploymentPydantic,
    existing_deployment: ComposeDeploymentPydantic | None = None,
) -> tuple[HelmValues, ResourceRequirements]:
    """Validate deployment request before queuing task.

    Validates compose YAML, generates Helm values, calculates resources, and checks quotas.
    Raises ValueError with user-friendly message if validation fails.

    Returns:
        Tuple of (helm_values, resource_requirements)
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
        secrets = await db.secrets.get_secrets(deployment.id)

    # Generate Helm values (this validates compose file)
    try:
        helm_generator = HelmValuesGenerator(deployment, secrets)
        helm_values, _ = helm_generator.generate_values(compose_file)
        helm_values.compose_yaml = compose_yaml

    except ValueError as e:
        # ComposeValidator errors are already user-friendly
        raise ValueError(str(e)) from e

    except Exception as e:
        raise ValueError(
            f"Failed to generate deployment configuration: {str(e)}"
        ) from e

    # Calculate resource requirements
    required_deployments = len(
        [
            s
            for s in helm_values.services
            if s.enabled and s.workloadType == WorkloadType.DEPLOYMENT
        ]
    )
    required_services = len([s for s in helm_values.services if s.enabled])
    required_pvcs = len(helm_values.volumes) if helm_values.volumes else 0

    requirements = ResourceRequirements(
        deployments=required_deployments,
        services=required_services,
        pvcs=required_pvcs,
    )

    # Check if updating existing deployment
    existing_requirements = None
    if existing_deployment:
        helm_manager = HelmManager()
        name = create_release_name(deployment.workspace_id, deployment.name)
        release_exists, _ = helm_manager.check_release_status(
            name, deployment.namespace
        )

        if release_exists:
            if existing_deployment.helm_values:
                existing_deployments = len(
                    [
                        s
                        for s in existing_deployment.helm_values.services
                        if s.enabled and s.workloadType == WorkloadType.DEPLOYMENT
                    ]
                )
                existing_services = len(
                    [s for s in existing_deployment.helm_values.services if s.enabled]
                )
                existing_pvcs = (
                    len(existing_deployment.helm_values.volumes)
                    if existing_deployment.helm_values.volumes
                    else 0
                )
                existing_requirements = ResourceRequirements(
                    deployments=existing_deployments,
                    services=existing_services,
                    pvcs=existing_pvcs,
                )
            else:
                # Release exists but no helm_values - account for 1 deployment being replaced
                existing_requirements = ResourceRequirements(
                    deployments=1, services=0, pvcs=0
                )

    # Verify quota capacity
    try:
        await verify_quota_capacity(
            deployment.workspace_id,
            required_deployments=requirements.deployments,
            required_services=requirements.services,
            required_pvcs=requirements.pvcs,
            existing_deployments=existing_requirements.deployments
            if existing_requirements
            else 0,
            existing_services=existing_requirements.services
            if existing_requirements
            else 0,
            existing_pvcs=existing_requirements.pvcs if existing_requirements else 0,
        )

    except ValueError as e:
        # verify_quota_capacity already provides user-friendly messages
        raise ValueError(str(e)) from e

    return helm_values, requirements
