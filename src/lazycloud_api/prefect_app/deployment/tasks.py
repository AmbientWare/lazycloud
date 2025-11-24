import asyncio
from datetime import UTC, datetime
from uuid import UUID

import yaml
from kubernetes.client.exceptions import ApiException
from loguru import logger
from prefect import task

from lazycloud_api.config import app_config
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.secrets import SecretPydantic
from lazycloud_api.prefect_app.deployment.models import ValidationResult
from lazycloud_api.prefect_app.deployment.utils import (
    delete_job_with_timeout,
    update_deployment_state,
    wait_for_secrets,
)
from lazycloud_api.prefect_app.utils import get_task_result
from lazycloud_api.services import get_subscription_service
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.k8s import get_chart_paths
from lazycloud_api.services.k8s.helm_manager import (
    DeploymentStrategy,
    HelmDeploymentConfig,
    HelmManager,
)
from lazycloud_api.services.k8s.helm_values_generator import HelmValuesGenerator
from shared.models.deployments import (
    DeploymentInfo,
    DeploymentResult,
    DeploymentStates,
)
from shared.models.helm import HelmNamespaceValues, HelmValues, NamespaceConfig
from shared.models.k8s import WorkloadType
from shared.models.secrets import SecretState
from shared.models.statuses import TaskStatus

charts = get_chart_paths()


@task(retries=0)
async def check_deployment_idempotency_task(
    deployment_id: str,
) -> DeploymentInfo | None:
    """Check if deployment is already in progress or completed. Returns deployment if should proceed, None if should skip."""
    async with db.compose_deployments.transaction() as session:
        deployment = await db.compose_deployments.get_by_id(
            deployment_id, with_lock=True, session=session
        )
        if not deployment:
            raise ValueError(f"Deployment {deployment_id} not found")

        # If already deployed and no pending changes, skip
        if (
            deployment.state == DeploymentStates.DEPLOYED
            and not deployment.pending_compose_yaml
        ):
            logger.info(
                f"Deployment {deployment_id} already deployed with no pending changes. Skipping."
            )
            return None

        # If in a non-terminal state (DEPLOYING, DELETING), verify the task is still running
        if deployment.state in (DeploymentStates.DEPLOYING, DeploymentStates.DELETING):
            if deployment.current_task_run_id:
                try:
                    task_status, _ = await get_task_result(
                        UUID(deployment.current_task_run_id)
                    )

                    # If task is completed or failed, reset state
                    if task_status in (TaskStatus.COMPLETED, TaskStatus.ERROR):
                        logger.warning(
                            f"Deployment {deployment_id} has task {deployment.current_task_run_id} "
                            f"in {task_status} state but deployment is {deployment.state}. Resetting state."
                        )
                        if deployment.state == DeploymentStates.DEPLOYING:
                            deployment.state = DeploymentStates.FAILED
                            deployment.status_message = "Previous deployment task completed but state was not updated"
                        elif deployment.state == DeploymentStates.DELETING:
                            deployment.state = DeploymentStates.DELETED
                            deployment.status_message = "Previous deletion task completed but state was not updated"
                        deployment.current_task_run_id = None
                        await db.compose_deployments.update(deployment, session=session)

                    else:
                        # Task is still running, block this operation
                        logger.info(
                            f"Deployment {deployment_id} has active task {deployment.current_task_run_id}. "
                            f"Current state: {deployment.state}, task status: {task_status}"
                        )
                        return None

                except Exception as e:
                    logger.warning(
                        f"Failed to check task state for {deployment.current_task_run_id}: {e}. "
                        f"Proceeding with deployment."
                    )

            else:
                # No task ID but in non-terminal state - likely stuck, reset
                logger.warning(
                    f"Deployment {deployment_id} is in {deployment.state} state but has no task_run_id. "
                    f"Resetting state."
                )
                if deployment.state == DeploymentStates.DEPLOYING:
                    deployment.state = DeploymentStates.FAILED
                    deployment.status_message = (
                        "Deployment was stuck in DEPLOYING state without active task"
                    )
                elif deployment.state == DeploymentStates.DELETING:
                    deployment.state = DeploymentStates.DELETED
                    deployment.status_message = (
                        "Deletion was stuck in DELETING state without active task"
                    )
                await db.compose_deployments.update(deployment, session=session)

        # Update state to deploying
        deployment.state = DeploymentStates.DEPLOYING
        deployment.status_message = "Starting deployment"
        await db.compose_deployments.update(deployment, session=session)

        return DeploymentInfo(
            id=deployment.id,
            workspace_id=deployment.workspace_id,
            name=deployment.name,
            namespace=deployment.namespace,
            current_helm_values=deployment.helm_values,
        )


@task(retries=0)
async def prepare_deployment_task(
    deployment_id: str, wait_for_secrets_flag: bool = False
) -> ValidationResult:
    """Prepare deployment data (validation already done upfront)."""
    if wait_for_secrets_flag:
        await wait_for_secrets(
            deployment_id, timeout=app_config.SECRETS_TIMEOUT_SECONDS
        )

    deployment = await db.compose_deployments.get_by_id(deployment_id)
    if not deployment:
        raise ValueError(f"Deployment {deployment_id} not found")

    compose_yaml = deployment.pending_compose_yaml or deployment.compose_yaml
    # Parse YAML (validation already done upfront, but we need to parse for helm generation)
    try:
        compose_data = yaml.safe_load(compose_yaml)
        compose_file = ComposeParser.parse_dict(compose_data)

    except Exception as e:
        await update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Failed to parse compose file: {str(e)}",
        )
        raise ValueError(f"Failed to parse compose file: {e}") from e

    secrets = await db.secrets.get_secrets(deployment_id)
    helm_generator = HelmValuesGenerator(deployment, secrets)
    helm_values, _ = helm_generator.generate_values(compose_file)
    helm_values.compose_yaml = compose_yaml

    return ValidationResult(
        deployment=deployment,
        compose_file=compose_file,
        helm_values=helm_values,
        secrets=secrets,
    )


@task(retries=1, retry_delay_seconds=5)
async def prepare_namespace_config_task(
    deployment: ComposeDeploymentPydantic,
) -> HelmDeploymentConfig:
    """Prepare namespace configuration with quota."""
    owner_user = await db.workspaces.get_owner_user(deployment.workspace_id)
    if not owner_user:
        raise ValueError(
            f"Workspace {deployment.workspace_id} has no owner. Cannot determine resource quota."
        )

    subscription_service = get_subscription_service()
    features = await subscription_service.get_user_features(owner_user.clerk_id)

    max_replicas = features.deployment.max_replicas_per_service
    # Set quotas much higher than limits as a safety net
    # Real enforcement happens in application layer (verify_quota_capacity)
    pods_limit = (
        features.workspace.deployment_limit
        * features.deployment.service_limit
        * max_replicas
        * 2
    )
    services_limit = (
        features.workspace.deployment_limit * features.deployment.service_limit * 2
    )
    pvcs_limit = (
        features.workspace.deployment_limit * features.deployment.volume_limit * 2
    )
    # Kubernetes count/deployments.apps counts Deployment resources (services), not compose deployments
    # So we need to account for all services across all compose deployments
    deployments_limit = (
        features.workspace.deployment_limit * features.deployment.service_limit * 2
    )
    ingresses_limit = services_limit

    quota_objects = {
        "pods": str(pods_limit),
        "services": str(services_limit),
        "persistentvolumeclaims": str(pvcs_limit),
        "configmaps": "500",
        "secrets": "500",
        "count/deployments.apps": str(deployments_limit),
        "count/ingresses.networking.k8s.io": str(ingresses_limit),
    }

    namespace = deployment.namespace
    namespace_values = HelmNamespaceValues(
        namespace=NamespaceConfig(
            name=namespace,
            labels={
                "lazycloud.io/managed": "true",
                "lazycloud.io/workspace-id": deployment.workspace_id,
            },
        ),
        resourceQuota={
            "enabled": True,
            "objects": quota_objects,
        },
    )

    namespace_config = HelmDeploymentConfig(
        release_name=namespace,
        namespace=namespace,
        chart_path=str(charts.namespace),
        values=namespace_values,
        timeout="2m",
        wait=True,
        create_namespace=True,
    )

    return namespace_config


@task(retries=2, retry_delay_seconds=10)
async def deploy_namespace_resources_task(
    namespace_config: HelmDeploymentConfig,
) -> None:
    """Deploy namespace resources (NetworkPolicy, ResourceQuota, etc.)."""
    helm_manager = HelmManager()
    result = helm_manager.deploy(namespace_config)
    if not result.success:
        raise Exception(f"Failed to deploy namespace: {result.error}")


@task(retries=2, retry_delay_seconds=5)
async def delete_existing_jobs_task(
    namespace: str,
    helm_values: HelmValues,
    current_helm_values: HelmValues | None = None,
) -> None:
    """Delete existing Jobs before deployment."""
    target_job_services = [
        service
        for service in helm_values.services
        if service.enabled and service.workloadType == WorkloadType.JOB
    ]

    current_job_services = []
    if current_helm_values and current_helm_values.services:
        current_job_services = [
            service
            for service in current_helm_values.services
            if service.enabled and service.workloadType == WorkloadType.JOB
        ]

    all_jobs = {}
    for service in target_job_services:
        all_jobs[service.resourceName] = service

    for service in current_job_services:
        all_jobs[service.resourceName] = service

    if not all_jobs:
        return

    logger.info(
        f"Deployment will affect {len(all_jobs)} Job(s), deleting existing Jobs before upgrade"
    )

    # Delete jobs in parallel
    async def delete_job(service):
        try:
            await delete_job_with_timeout(
                service.resourceName,
                namespace,
                app_config.ROLLBACK_JOB_DELETION_TIMEOUT_SECONDS,
            )
            logger.info(f"Deleted existing Job: {service.name}")

        except ApiException as e:
            if e.status == 404:
                logger.debug(f"Job {service.name} already deleted")
                return

            raise ValueError(f"Failed to delete Job {service.name}: {e}") from e

        except TimeoutError as e:
            raise ValueError(f"Job deletion timeout for {service.name}: {e}") from e

    await asyncio.gather(*[delete_job(service) for service in all_jobs.values()])


@task(retries=3, retry_delay_seconds=15)
async def deploy_application_task(
    name: str,
    namespace: str,
    helm_values: HelmValues,
) -> DeploymentResult:
    """Deploy application using Helm."""
    helm_manager = HelmManager()

    helm_app_config = HelmDeploymentConfig(
        release_name=name,
        namespace=namespace,
        chart_path=str(charts.compose),
        values=helm_values,
        timeout="5m",
        strategy=DeploymentStrategy.ROLLING_UPDATE,
        atomic=True,
        cleanup_on_fail=True,
    )

    app_result = helm_manager.deploy(helm_app_config)
    if not app_result.success:
        raise Exception(f"Failed to deploy application: {app_result.error}")

    return DeploymentResult(revision=app_result.revision)


@task(retries=2, retry_delay_seconds=5)
async def update_deployment_state_task(
    deployment_id: str,
    state: DeploymentStates,
    message: str | None = None,
) -> None:
    """Update deployment state in database with retry logic."""
    await update_deployment_state(deployment_id, state, message)


@task(retries=2, retry_delay_seconds=5)
async def sync_deployment_to_db_task(
    deployment_id: str,
    helm_values: HelmValues,
    helm_revision: int | None,
    secrets: list[SecretPydantic],
) -> None:
    """Sync deployment state to database after successful Helm deployment."""
    async with db.compose_deployments.transaction() as session:
        deployment = await db.compose_deployments.get_by_id(
            deployment_id, with_lock=True, session=session
        )
        if deployment is None:
            raise ValueError(
                f"Deployment {deployment_id} not found during deployment update"
            )

        if deployment.pending_compose_yaml:
            deployment.compose_yaml = deployment.pending_compose_yaml
            deployment.pending_compose_yaml = None

        deployment.helm_values = helm_values
        deployment.state = DeploymentStates.DEPLOYED
        deployment.status_message = (
            f"Deployment initiated successfully (revision: {helm_revision})"
        )
        deployment.deployed_at = datetime.now(UTC)
        if helm_revision:
            deployment.current_helm_revision = helm_revision

        deployment.current_task_run_id = None

        await db.compose_deployments.update(deployment, session=session)

    if secrets:
        async with db.secrets.transaction() as session:
            for secret in secrets:
                secret.state = SecretState.DEPLOYED
                await db.secrets.update(secret, session=session)
