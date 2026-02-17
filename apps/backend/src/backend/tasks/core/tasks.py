"""Core task functions for deployment operations.

These functions contain the actual business logic for deployment operations.
They are called by SAQ jobs and can be composed together.
"""

import asyncio
from datetime import UTC, datetime
from typing import Sequence

import yaml
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger
from models.deployments import (
    DeploymentInfo,
    DeploymentResult,
    DeploymentStates,
)
from models.compose import ComposeFile
from models.helm import HelmNamespaceValues, HelmValues, NamespaceConfig, SecretValues
from models.k8s import WorkloadType
from models.secrets import SecretState
from models.statuses import TaskStatus

from backend.config import app_config
from backend.database import get_db_context
from backend.database.models import ComposeDeployment, SecretInDb
from backend.services import get_cloudflare_service, get_subscription_service
from backend.services.compose.parser import ComposeParser
from backend.services.k8s import get_chart_paths
from backend.services.k8s.helm_manager import (
    DeploymentStrategy,
    HelmDeploymentConfig,
    HelmManager,
)
from backend.services.k8s.generators.networking import transform_environment_urls
from backend.services.k8s.helm_values_generator import HelmValuesGenerator
from backend.tasks.core.schemas import DeploymentPreparationResult
from backend.tasks.core.utils import (
    delete_job_with_timeout,
    update_deployment_state,
    wait_for_secrets,
)
from backend.tasks.utils import get_task_result

charts = get_chart_paths()


async def check_deployment_idempotency(
    deployment_id: str,
) -> DeploymentInfo | None:
    """Check if deployment is already in progress or completed.

    Returns deployment info if should proceed, None if should skip.
    """
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

    # Step 2: If in non-terminal state, check task status
    should_reset_state = False
    reset_to_state = None
    reset_message = None

    if deployment.state in (DeploymentStates.DEPLOYING, DeploymentStates.DELETING):
        if deployment.current_task_run_id:
            try:
                # Check if previous task is still running
                task_status, _ = await get_task_result(deployment.current_task_run_id)

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


async def prepare_deployment(
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
            owner_user = await db.workspaces.get_owner_user(deployment.workspace_id)

        # Get features for instance class selection
        features = None
        if owner_user:
            subscription_service = get_subscription_service()
            try:
                features = await subscription_service.get_user_features(
                    owner_user.workos_id
                )
            except Exception as e:
                logger.warning(
                    f"Failed to get features for workspace {deployment.workspace_id}: {e}"
                )

        helm_generator = HelmValuesGenerator(deployment, secrets, features)
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

        # Merge fresh secrets into stored helm_values — secrets may have been
        # added after helm_values were generated (e.g. wait_for_secrets flow,
        # or validation used a temp deployment ID that couldn't find secrets)
        if secrets:
            secret_data = {s.key: s.value for s in secrets}

            # Apply .public URL transforms (same as HelmValuesGenerator does)
            if compose_file:
                service_names = {svc.name for svc in compose_file.services}
                secret_data = (
                    transform_environment_urls(
                        secret_data,
                        service_names,
                        deployment_id,
                        deployment.cluster_id,
                    )
                    or {}
                )

            secret_name = f"env-{deployment_id[:8]}"
            # Remove any stale env secrets, then add fresh one
            helm_values.secrets = [
                s for s in helm_values.secrets if not s.name.startswith("env-")
            ]
            helm_values.secrets.append(
                SecretValues(
                    name=secret_name,
                    enabled=True,
                    type="Opaque",
                    data=secret_data,
                )
            )

    return DeploymentPreparationResult(
        deployment=deployment,
        compose_file=compose_file,
        helm_values=helm_values,
        secrets=secrets,
    )


async def prepare_namespace_config(
    deployment: ComposeDeployment,
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

    max_replicas = features.max_replicas_per_service
    # Set quotas as generous safety nets
    # Real enforcement happens in application layer (subscription checks)
    # Services, volumes, networks are unlimited - only deployment count is limited
    # Use generous multipliers to avoid hitting K8s quotas before subscription limits
    base_services_per_deployment = 20  # Generous default for unlimited services
    base_volumes_per_deployment = 20  # Generous default for unlimited volumes

    pods_limit = (
        features.deployment_limit * base_services_per_deployment * max_replicas * 2
    )
    services_limit = features.deployment_limit * base_services_per_deployment * 2
    pvcs_limit = features.deployment_limit * base_volumes_per_deployment * 2
    # Kubernetes count/deployments.apps counts Deployment resources (services), not compose deployments
    deployments_limit = features.deployment_limit * base_services_per_deployment * 2
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


async def deploy_namespace_resources(
    namespace_config: HelmDeploymentConfig,
    cluster_id: str,
) -> None:
    """Deploy namespace resources (NetworkPolicy, ResourceQuota, etc.)."""
    helm_manager = HelmManager(cluster_id)
    result = await helm_manager.deploy(namespace_config)
    if not result.success:
        raise Exception(f"Failed to deploy namespace: {result.error}")


async def delete_existing_jobs(
    namespace: str,
    helm_values: HelmValues,
    cluster_id: str,
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
                cluster_id,
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


async def deploy_application(
    name: str,
    namespace: str,
    helm_values: HelmValues,
    cluster_id: str,
) -> DeploymentResult:
    """Deploy application using Helm."""
    helm_manager = HelmManager(cluster_id)

    helm_app_config = HelmDeploymentConfig(
        release_name=name,
        namespace=namespace,
        chart_path=str(charts.compose),
        values=helm_values,
        timeout="2m",  # Just for helm install itself, not pod readiness
        strategy=DeploymentStrategy.ROLLING_UPDATE,
        # No atomic/wait - deployment returns immediately, user monitors via dashboard
    )

    app_result = await helm_manager.deploy(helm_app_config)
    if not app_result.success:
        raise Exception(f"Failed to deploy application: {app_result.error}")

    return DeploymentResult(revision=app_result.revision)


def _normalize_domain(domain: str) -> str | None:
    """Normalize domain for consistent comparison and Cloudflare operations."""
    normalized = domain.strip().rstrip(".").lower()
    return normalized or None


def _normalize_domains(domains: list[str] | set[str]) -> list[str]:
    """Normalize, deduplicate, and sort domain collections."""
    normalized_domains = {_normalize_domain(domain) for domain in domains}
    return sorted(domain for domain in normalized_domains if domain)


def _extract_custom_domains_from_compose_file(compose_file: ComposeFile | None) -> set[str]:
    """Extract normalized custom domains from a parsed Compose file."""
    if not compose_file:
        return set()

    domains: set[str] = set()
    for service in compose_file.services:
        if not service.domain:
            continue

        normalized = _normalize_domain(service.domain)
        if normalized:
            domains.add(normalized)

    return domains


def _extract_custom_domains_from_compose_yaml(
    compose_yaml: str | None,
) -> tuple[set[str], bool]:
    """Extract normalized custom domains from compose YAML.

    Returns:
        Tuple of (domains, parse_success).
    """
    if not compose_yaml:
        return set(), True

    try:
        compose_data = yaml.safe_load(compose_yaml)
        if not isinstance(compose_data, dict):
            logger.warning("Skipping custom domain extraction: compose YAML root is invalid")
            return set(), False

        compose_file = ComposeParser.parse_dict(compose_data)
        return _extract_custom_domains_from_compose_file(compose_file), True

    except Exception as e:
        logger.warning(f"Failed to extract custom domains from compose YAML: {e}")
        return set(), False


async def _find_referenced_custom_domains(
    domains: set[str],
    exclude_deployment_id: str | None = None,
) -> set[str]:
    """Find domains still referenced by other active deployments.

    Checks both compose_yaml and pending_compose_yaml to avoid deleting domains that
    are in flight for another deployment update.
    """
    if not domains:
        return set()

    async with get_db_context() as db:
        active_deployments = await db.compose_deployments.find({})

    referenced_domains: set[str] = set()
    for deployment in active_deployments:
        if exclude_deployment_id and deployment.id == exclude_deployment_id:
            continue

        deployment_domains, compose_ok = _extract_custom_domains_from_compose_yaml(
            deployment.compose_yaml
        )
        pending_domains, pending_ok = _extract_custom_domains_from_compose_yaml(
            deployment.pending_compose_yaml
        )
        if not compose_ok or not pending_ok:
            logger.warning(
                f"Skipping stale domain cleanup: unable to parse compose config for deployment {deployment.id}"
            )
            return domains

        deployment_domains.update(pending_domains)

        overlap = domains & deployment_domains
        if overlap:
            referenced_domains.update(overlap)
            if referenced_domains == domains:
                return referenced_domains

    return referenced_domains


async def reconcile_custom_domains_for_deployment(
    deployment_id: str,
    previous_compose_yaml: str | None,
    current_compose_file: ComposeFile | None,
) -> tuple[list[str], list[str]]:
    """Remove stale domains no longer used by this deployment.

    Returns:
        Tuple of (removed_domains, skipped_domains_still_referenced_elsewhere).
    """
    previous_domains, _ = _extract_custom_domains_from_compose_yaml(
        previous_compose_yaml
    )
    current_domains = _extract_custom_domains_from_compose_file(current_compose_file)
    domains_to_remove = previous_domains - current_domains
    if not domains_to_remove:
        return [], []

    referenced_domains = await _find_referenced_custom_domains(
        domains_to_remove,
        exclude_deployment_id=deployment_id,
    )
    safe_to_remove = sorted(domains_to_remove - referenced_domains)
    skipped_domains = sorted(referenced_domains)

    if skipped_domains:
        logger.info(
            f"Skipping cleanup for referenced domains on deployment {deployment_id}: "
            f"{', '.join(skipped_domains)}"
        )

    if safe_to_remove:
        await unregister_custom_domains(safe_to_remove)

    return safe_to_remove, skipped_domains


async def register_custom_domains(
    domains: list[str],
) -> None:
    """Register custom domains with Cloudflare for SaaS SSL.

    Only runs in production when Cloudflare is configured.
    Skipped in local development.
    """
    normalized_domains = _normalize_domains(domains)
    if not normalized_domains:
        return

    # Skip if Cloudflare is not configured (local dev)
    if not app_config.CLOUDFLARE_API_KEY:
        logger.debug("Skipping Cloudflare domain registration (not configured)")
        return

    cloudflare = get_cloudflare_service()
    async with cloudflare:
        for domain in normalized_domains:
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


async def unregister_custom_domains(
    domains: list[str],
) -> None:
    """Remove custom domains from Cloudflare for SaaS SSL.

    Only runs in production when Cloudflare is configured.
    """
    normalized_domains = _normalize_domains(domains)
    if not normalized_domains:
        return

    if not app_config.CLOUDFLARE_API_KEY:
        logger.debug("Skipping Cloudflare domain removal (not configured)")
        return

    cloudflare = get_cloudflare_service()
    async with cloudflare:
        for domain in normalized_domains:
            try:
                await cloudflare.delete_saas_domain(domain)
                logger.info(f"Removed custom domain {domain} from Cloudflare")
            except Exception as e:
                logger.warning(f"Failed to remove domain {domain} from Cloudflare: {e}")


async def sync_deployment_to_db(
    deployment_id: str,
    helm_values: HelmValues,
    helm_revision: int | None,
    secrets: Sequence[SecretInDb],
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
