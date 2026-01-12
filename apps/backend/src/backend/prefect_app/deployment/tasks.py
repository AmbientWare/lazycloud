import asyncio
from datetime import UTC, datetime
from uuid import UUID

import yaml
from kubernetes.client.exceptions import ApiException
from loguru import logger
from models.deployments import (
    DeploymentInfo,
    DeploymentResult,
    DeploymentStates,
)
from models.helm import HelmNamespaceValues, HelmValues, NamespaceConfig
from models.k8s import WorkloadType
from models.secrets import SecretState
from models.statuses import TaskStatus
from prefect import task

from backend.config import app_config
from backend.database import get_db_context
from backend.database.compose import ComposeDeploymentPydantic
from backend.database.secrets import SecretPydantic
from backend.prefect_app.deployment.schemas import DeploymentPreparationResult
from backend.prefect_app.deployment.utils import (
    delete_job_with_timeout,
    update_deployment_state,
    wait_for_secrets,
)
from backend.prefect_app.utils import get_task_result
from backend.services import get_cloudflare_service, get_subscription_service
from backend.services.compose.parser import ComposeParser
from backend.services.k8s import get_chart_paths
from backend.services.k8s.helm_manager import (
    DeploymentStrategy,
    HelmDeploymentConfig,
    HelmManager,
)
from backend.services.k8s.helm_values_generator import HelmValuesGenerator

charts = get_chart_paths()


@task(retries=0)
async def check_deployment_idempotency_task(
    deployment_id: str,
) -> DeploymentInfo | None:
    """Check if deployment is already in progress or completed. Returns deployment if should proceed, None if should skip."""

    # Step 1: Quick DB read to get current state
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(deployment_id)

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

    # Step 2: If in non-terminal state, check task status (external Prefect API call - NO DB)
    should_reset_state = False
    reset_to_state = None
    reset_message = None

    if deployment.state in (DeploymentStates.DEPLOYING, DeploymentStates.DELETING):
        if deployment.current_task_run_id:
            try:
                # External call to Prefect API - outside DB transaction
                task_status, _ = await get_task_result(
                    UUID(deployment.current_task_run_id)
                )

                if task_status in (TaskStatus.COMPLETED, TaskStatus.ERROR):
                    # Task finished but state wasn't updated - need to reset
                    logger.warning(
                        f"Deployment {deployment_id} has task {deployment.current_task_run_id} "
                        f"in {task_status} state but deployment is {deployment.state}. Resetting state."
                    )
                    should_reset_state = True
                    if deployment.state == DeploymentStates.DEPLOYING:
                        reset_to_state = DeploymentStates.FAILED
                        reset_message = "Previous deployment task completed but state was not updated"
                    elif deployment.state == DeploymentStates.DELETING:
                        reset_to_state = DeploymentStates.DELETED
                        reset_message = (
                            "Previous deletion task completed but state was not updated"
                        )
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
            # No task ID but in non-terminal state - likely stuck
            logger.warning(
                f"Deployment {deployment_id} is in {deployment.state} state but has no task_run_id. "
                f"Resetting state."
            )

            should_reset_state = True
            if deployment.state == DeploymentStates.DEPLOYING:
                reset_to_state = DeploymentStates.FAILED
                reset_message = (
                    "Deployment was stuck in DEPLOYING state without active task"
                )

            elif deployment.state == DeploymentStates.DELETING:
                reset_to_state = DeploymentStates.DELETED
                reset_message = (
                    "Deletion was stuck in DELETING state without active task"
                )

    # Step 3: Quick DB write to update state (with lock for safety)
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(
            deployment_id, with_lock=True
        )

        if not deployment:
            raise ValueError(f"Deployment {deployment_id} not found")

        # Apply reset if needed
        if should_reset_state and reset_to_state:
            deployment.state = reset_to_state
            deployment.status_message = reset_message
            deployment.current_task_run_id = None
            await db.compose_deployments.update(deployment)

        # Re-check state after potential reset (another process might have changed it)
        if deployment.state in (DeploymentStates.DEPLOYING, DeploymentStates.DELETING):
            if deployment.current_task_run_id:
                # Someone else started a task while we were checking
                logger.info(
                    f"Deployment {deployment_id} now has active task. Skipping."
                )
                return None

        # Update state to deploying
        deployment.state = DeploymentStates.DEPLOYING
        deployment.status_message = "Starting deployment"
        await db.compose_deployments.update(deployment)

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
) -> DeploymentPreparationResult:
    """Prepare deployment data (validation already done upfront)."""
    if wait_for_secrets_flag:
        await wait_for_secrets(
            deployment_id, timeout=app_config.SECRETS_TIMEOUT_SECONDS
        )

    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(deployment_id)

    if not deployment:
        raise ValueError(f"Deployment {deployment_id} not found")

    compose_yaml = deployment.pending_compose_yaml or deployment.compose_yaml

    helm_values = deployment.helm_values
    compose_file = None

    if not helm_values:
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

        async with get_db_context() as db:
            secrets = await db.secrets.get_secrets(deployment_id)

        helm_generator = HelmValuesGenerator(deployment, secrets)
        helm_values, _ = helm_generator.generate_values(compose_file)
        helm_values.compose_yaml = compose_yaml
    else:
        if compose_yaml:
            try:
                compose_data = yaml.safe_load(compose_yaml)
                compose_file = ComposeParser.parse_dict(compose_data)
            except Exception as e:
                logger.warning(f"Failed to parse compose_file for return value: {e}")
                compose_file = None

        async with get_db_context() as db:
            secrets = await db.secrets.get_secrets(deployment_id)

    return DeploymentPreparationResult(
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
    async with get_db_context() as db:
        owner_user = await db.workspaces.get_owner_user(deployment.workspace_id)

    if not owner_user:
        raise ValueError(
            f"Workspace {deployment.workspace_id} has no owner. Cannot determine resource quota."
        )

    subscription_service = get_subscription_service()
    features = await subscription_service.get_user_features(owner_user.workos_id)

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
                "lazycloud.dev/managed": "true",
                "lazycloud.dev/workspace-id": deployment.workspace_id,
            },
        ),
        resourceQuota={
            "enabled": True,
            "objects": quota_objects,
        },
    )

    namespace_config = HelmDeploymentConfig(
        release_name=namespace,
        namespace="default",  # Deploy to default, chart creates target namespace
        chart_path=str(charts.namespace),
        values=namespace_values,
        timeout="2m",
        wait=True,
        create_namespace=False,
    )

    return namespace_config


@task(retries=2, retry_delay_seconds=10)
async def deploy_namespace_resources_task(
    namespace_config: HelmDeploymentConfig,
) -> None:
    """Deploy namespace resources (NetworkPolicy, ResourceQuota, etc.)."""
    helm_manager = HelmManager()
    result = await helm_manager.deploy(namespace_config)
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

    app_result = await helm_manager.deploy(helm_app_config)
    if not app_result.success:
        raise Exception(f"Failed to deploy application: {app_result.error}")

    return DeploymentResult(revision=app_result.revision)


@task(retries=2, retry_delay_seconds=5)
async def register_custom_domains_task(
    domains: list[str],
) -> None:
    """Register custom domains with Cloudflare for SaaS SSL.

    Only runs in production when Cloudflare is configured.
    Skipped in local development.
    """
    if not domains:
        return

    # Skip if Cloudflare is not configured (local dev)
    if not app_config.CLOUDFLARE_API_KEY:
        logger.debug("Skipping Cloudflare domain registration (not configured)")
        return

    cloudflare = get_cloudflare_service()
    async with cloudflare:
        for domain in domains:
            try:
                # Check if domain already exists
                try:
                    await cloudflare.get_domain_status(domain)
                    logger.info(
                        f"Custom domain {domain} already registered with Cloudflare"
                    )
                    continue

                except Exception:
                    pass  # Domain doesn't exist, proceed to create

                await cloudflare.add_saas_domain(domain)
                logger.info(f"Registered custom domain {domain} with Cloudflare")
            except Exception as e:
                logger.error(f"Failed to register domain {domain} with Cloudflare: {e}")
                # Don't fail deployment for Cloudflare errors - domain can be retried
                # Customer will see SSL pending until they add CNAME


@task(retries=2, retry_delay_seconds=5)
async def unregister_custom_domains_task(
    domains: list[str],
) -> None:
    """Remove custom domains from Cloudflare for SaaS SSL.

    Only runs in production when Cloudflare is configured.
    """
    if not domains:
        return

    if not app_config.CLOUDFLARE_API_KEY:
        logger.debug("Skipping Cloudflare domain removal (not configured)")
        return

    cloudflare = get_cloudflare_service()
    async with cloudflare:
        for domain in domains:
            try:
                await cloudflare.delete_saas_domain(domain)
                logger.info(f"Removed custom domain {domain} from Cloudflare")
            except Exception as e:
                logger.warning(f"Failed to remove domain {domain} from Cloudflare: {e}")


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
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(
            deployment_id, with_lock=True
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

        await db.compose_deployments.update(deployment)

        if secrets:
            for secret in secrets:
                secret.state = SecretState.DEPLOYED
                await db.secrets.update(secret)
