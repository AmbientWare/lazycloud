import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import yaml
from kubernetes.client.exceptions import ApiException
from loguru import logger
from prefect import flow, task

from lazycloud_api.config import app_config
from lazycloud_api.database import db
from lazycloud_api.services import get_ecr_auth_service, get_subscription_service
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.k8s import (
    create_release_name,
    get_chart_paths,
)
from lazycloud_api.services.k8s.client import get_batch_v1_api
from lazycloud_api.services.k8s.helm_manager import (
    DeploymentStrategy,
    HelmDeploymentConfig,
    HelmManager,
)
from lazycloud_api.services.k8s.helm_values_generator import HelmValuesGenerator
from shared.models.deployments import DeploymentStates
from shared.models.helm import HelmNamespaceValues, NamespaceConfig
from shared.models.k8s import WorkloadType
from shared.models.secrets import SecretState

charts = get_chart_paths()


async def _delete_job_with_timeout(
    job_name: str, namespace: str, timeout_seconds: int
) -> None:
    """Delete a Kubernetes Job and wait for it to be fully removed."""
    batch_v1 = get_batch_v1_api()

    def _delete_job() -> None:
        batch_v1.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            propagation_policy="Foreground",
        )

    def _check_job_exists() -> bool:
        try:
            batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    # Initiate deletion
    try:
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            await asyncio.wait_for(
                loop.run_in_executor(executor, _delete_job),
                timeout=timeout_seconds,
            )
    except ApiException as e:
        if e.status == 404:
            logger.debug(f"Job {job_name} does not exist (already deleted)")
            return
        raise
    except asyncio.TimeoutError:
        logger.error(
            f"Timeout ({timeout_seconds}s) initiating deletion of Job {job_name}"
        )
        raise TimeoutError(
            f"Job {job_name} deletion initiation timed out after {timeout_seconds} seconds"
        ) from None

    # Wait for Job to be fully deleted (poll until 404)
    start_time = time.time()
    poll_interval = 0.5
    while time.time() - start_time < timeout_seconds:
        try:
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as executor:
                exists = await loop.run_in_executor(executor, _check_job_exists)
            if not exists:
                logger.info(f"Job {job_name} successfully deleted and removed")
                return
            await asyncio.sleep(poll_interval)
        except Exception as e:
            logger.warning(f"Error checking Job {job_name} deletion status: {e}")
            await asyncio.sleep(poll_interval)

    # Timeout waiting for deletion
    logger.error(
        f"Timeout ({timeout_seconds}s) waiting for Job {job_name} to be fully deleted. "
        "Job deletion may still be in progress."
    )
    raise TimeoutError(
        f"Job {job_name} deletion timed out after {timeout_seconds} seconds"
    ) from None


async def _update_deployment_state(
    deployment_id: str,
    state: DeploymentStates,
    message: str | None = None,
) -> None:
    """Update deployment status in database."""
    try:
        deployment = await db.compose_deployments.get_by_id(deployment_id)
        if deployment:
            deployment.state = state
            if message:
                deployment.status_message = message[:500]  # Truncate to fit
            await db.compose_deployments.update(deployment)

    except Exception as e:
        logger.error(f"Failed to update deployment status: {e}")


async def _wait_for_secrets(deployment_id: str, timeout: int | None = None) -> None:
    """Wait for secrets to be stored for a deployment."""
    if timeout is None:
        timeout = app_config.SECRETS_TIMEOUT_SECONDS

    start_time = time.time()
    secrets = []
    while time.time() - start_time < timeout:
        await asyncio.sleep(1)
        secrets = await db.secrets.get_secrets(deployment_id)
        if secrets:
            logger.info(f"Found {len(secrets)} secrets for deployment {deployment_id}")
            break

    if not secrets:
        raise TimeoutError(
            f"Secrets not found for deployment {deployment_id} after {timeout} seconds"
        )


@task
async def deploy_compose_task(
    deployment_id: str, wait_for_secrets: bool = False
) -> None:
    """Deploy a Docker Compose file to Kubernetes using improved Helm management."""
    logger.info(f"Starting deployment {deployment_id}")
    secrets = []

    if wait_for_secrets:
        try:
            await _wait_for_secrets(
                deployment_id, timeout=app_config.SECRETS_TIMEOUT_SECONDS
            )
        except TimeoutError as e:
            logger.error(f"Timeout waiting for secrets: {e}")
            await _update_deployment_state(
                deployment_id,
                DeploymentStates.FAILED,
                f"Timeout waiting for secrets: {str(e)}",
            )
            raise

    # get deployment
    deployment = await db.compose_deployments.get_by_id(deployment_id)
    if not deployment:
        raise ValueError(f"Deployment {deployment_id} not found")

    # Parse the compose YAML (use pending if available, otherwise use current)
    compose_yaml = deployment.pending_compose_yaml or deployment.compose_yaml
    try:
        compose_data = yaml.safe_load(compose_yaml)
    except yaml.YAMLError as e:
        logger.error(f"YAML parsing error for deployment {deployment_id}: {e}")
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Invalid compose YAML: {str(e)}",
        )
        raise ValueError(f"Invalid YAML in deployment: {e}") from e

    compose_file = ComposeParser.parse_dict(compose_data)

    compose_yaml_size = len(compose_yaml.encode("utf-8"))
    if compose_yaml_size > app_config.COMPOSE_YAML_MAX_SIZE_BYTES:
        error_msg = (
            f"Compose YAML size ({compose_yaml_size} bytes) exceeds maximum allowed size "
            f"({app_config.COMPOSE_YAML_MAX_SIZE_BYTES} bytes). "
            "Kubernetes annotations have a 256KB limit."
        )
        logger.error(error_msg)
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            error_msg,
        )
        raise ValueError(error_msg)

    if compose_yaml_size > app_config.COMPOSE_YAML_MAX_SIZE_BYTES * 0.8:
        logger.warning(
            f"Compose YAML size ({compose_yaml_size} bytes) is approaching the limit "
            f"({app_config.COMPOSE_YAML_MAX_SIZE_BYTES} bytes). "
            "Consider reducing the size to avoid potential issues."
        )

    # get the helm values with deployment_id to load secrets
    secrets = await db.secrets.get_secrets(deployment_id)
    helm_generator = HelmValuesGenerator(deployment, secrets)
    helm_values, _ = helm_generator.generate_values(compose_file)
    helm_values.compose_yaml = compose_yaml
    deployment.helm_values = helm_values

    # Update status to deploying
    await _update_deployment_state(
        deployment_id,
        DeploymentStates.DEPLOYING,
        "Starting deployment",
    )

    # Extract deployment info from helm values
    name = create_release_name(deployment.workspace_id, deployment.name)
    namespace = deployment.namespace

    # Initialize Helm manager
    helm_manager = HelmManager()

    try:
        # Deploy namespace resources (NetworkPolicy, ResourceQuota, etc.)
        logger.info(f"Deploying namespace resources for {namespace}")

        # Get workspace owner and their subscription features for dynamic quota
        owner_user = await db.workspaces.get_owner_user(deployment.workspace_id)
        if not owner_user:
            raise ValueError(
                f"Workspace {deployment.workspace_id} has no owner. Cannot determine resource quota."
            )

        subscription_service = get_subscription_service()
        features = await subscription_service.get_user_features(owner_user.clerk_id)

        # Calculate object limits from features
        max_replicas = features.deployment.max_replicas_per_service
        pods_limit = (
            features.workspace.deployment_limit
            * features.deployment.service_limit
            * max_replicas
        )
        services_limit = (
            features.workspace.deployment_limit * features.deployment.service_limit
        )
        pvcs_limit = (
            features.workspace.deployment_limit * features.deployment.volume_limit
        )
        deployments_limit = features.workspace.deployment_limit
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

        namespace_values = HelmNamespaceValues(
            namespace=NamespaceConfig(
                name=namespace,
                labels={
                    "lazycloud.io/managed": "true",
                    "lazycloud.io/workspace-id": str(deployment.workspace_id),
                },
            ),
            resourceQuota={
                "enabled": True,
                "objects": quota_objects,
            },
        )
        namespace_config = HelmDeploymentConfig(
            release_name=namespace,
            namespace="default",
            chart_path=str(charts.namespace),
            values=namespace_values,
            timeout="2m",
            wait=True,
        )

        namespace_result = helm_manager.deploy(namespace_config)
        if not namespace_result.success:
            if (
                namespace_result.error
                and "already exists" not in namespace_result.error
            ):
                raise Exception(f"Failed to deploy namespace: {namespace_result.error}")

        # Step 2: Deploy application
        logger.info(f"Deploying application {name} in namespace {namespace}")

        target_job_services = [
            service
            for service in helm_values.services
            if service.enabled and service.workloadType == WorkloadType.JOB
        ]

        current_job_services = []
        if deployment.helm_values and deployment.helm_values.services:
            current_job_services = [
                service
                for service in deployment.helm_values.services
                if service.enabled and service.workloadType == WorkloadType.JOB
            ]

        all_jobs = {}
        for service in target_job_services:
            all_jobs[service.resourceName] = service
        for service in current_job_services:
            all_jobs[service.resourceName] = service

        if all_jobs:
            logger.info(
                f"Deployment will affect {len(all_jobs)} Job(s) (from current state and target revision), "
                "deleting existing Jobs before upgrade"
            )
            for service in all_jobs.values():
                try:
                    await _delete_job_with_timeout(
                        service.resourceName,
                        namespace,
                        app_config.ROLLBACK_JOB_DELETION_TIMEOUT_SECONDS,
                    )
                    logger.info(f"Deleted existing Job: {service.name}")
                except TimeoutError as e:
                    logger.error(
                        f"Failed to delete Job {service.name} within timeout: {e}. "
                        "Deployment may proceed but Job may need manual cleanup."
                    )
                    raise ValueError(
                        f"Job deletion timeout for {service.name}: {e}"
                    ) from e
                except ApiException as e:
                    if e.status != 404:
                        logger.warning(f"Could not delete Job {service.name}: {e}")
                        raise ValueError(
                            f"Failed to delete Job {service.name}: {e}"
                        ) from e
                except Exception as e:
                    logger.error(f"Unexpected error deleting Job {service.name}: {e}")
                    raise ValueError(f"Failed to delete Job {service.name}: {e}") from e

        helm_app_config = HelmDeploymentConfig(
            release_name=name,
            namespace=namespace,
            chart_path=str(charts.compose),
            values=deployment.helm_values,
            timeout="5m",
            strategy=DeploymentStrategy.ROLLING_UPDATE,
        )

        app_result = helm_manager.deploy(helm_app_config)
        if not app_result.success:
            if app_result.error and (
                "another operation" in app_result.error or "pending" in app_result.error
            ):
                logger.warning("Detected stuck deployment, attempting force update")
                helm_app_config.strategy = DeploymentStrategy.FORCE_UPDATE
                app_result = helm_manager.deploy(helm_app_config)

            if not app_result.success:
                raise Exception(f"Failed to deploy application: {app_result.error}")

        try:
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
                deployment.status_message = f"Deployment initiated successfully (revision: {app_result.revision})"
                deployment.deployed_at = datetime.now(UTC)
                if app_result.revision:
                    deployment.current_helm_revision = app_result.revision

                await db.compose_deployments.update(deployment, session=session)

            if secrets:
                async with db.secrets.transaction() as session:
                    for secret in secrets:
                        secret.state = SecretState.DEPLOYED
                        await db.secrets.update(secret, session=session)
        except Exception as db_error:
            logger.error(
                f"Helm deployment succeeded but DB update failed for deployment {deployment_id}. "
                f"Helm is at revision {app_result.revision} but DB update failed: {db_error}. "
                "Manual intervention may be required to sync state."
            )
            await _update_deployment_state(
                deployment_id,
                DeploymentStates.FAILED,
                f"Deployment partially completed: Helm deployed successfully (revision: {app_result.revision}) but database update failed. "
                f"Error: {str(db_error)[:200]}",
            )
            raise ValueError(
                f"Helm deployment succeeded but database update failed: {db_error}"
            ) from db_error

    except yaml.YAMLError as e:
        logger.error(f"YAML parsing error for deployment {deployment_id}: {e}")
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Invalid compose YAML: {str(e)}",
        )
        raise ValueError(f"Invalid YAML in deployment: {e}") from e
    except ValueError as e:
        error_type = type(e).__name__
        logger.error(
            f"Deployment {deployment_id} failed with {error_type}: {str(e)}",
            exc_info=True,
        )
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Deployment failed: {error_type} - {str(e)[:200]}",
        )
        if secrets:
            async with db.secrets.transaction() as session:
                for secret in secrets:
                    secret.state = SecretState.AWAITING_DEPLOYMENT
                    await db.secrets.update(secret, session=session)
        raise
    except Exception as e:
        error_type = type(e).__name__
        logger.error(
            f"Deployment {deployment_id} failed with {error_type}: {str(e)}",
            exc_info=True,
        )
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Deployment failed: {error_type} - {str(e)[:200]}",
        )

        if secrets:
            async with db.secrets.transaction() as session:
                for secret in secrets:
                    secret.state = SecretState.AWAITING_DEPLOYMENT
                    await db.secrets.update(secret, session=session)

        # Attempt cleanup on failure only if Helm deployment never succeeded
        try:
            if name and namespace and helm_manager:
                exists, _ = helm_manager._check_release_status(name, namespace)
                if exists:
                    logger.warning(
                        f"Helm release {name} exists after deployment failure. "
                        "Skipping cleanup to preserve partially deployed resources. "
                        "Manual cleanup may be required."
                    )
                else:
                    logger.info(
                        f"Helm release {name} does not exist, no cleanup needed"
                    )
        except Exception as cleanup_check_error:
            logger.warning(
                f"Could not check Helm release status for cleanup decision: {cleanup_check_error}"
            )

        raise


@task
async def destroy_compose_task(deployment_id: str) -> None:
    """Destroy a Docker Compose deployment from Kubernetes."""
    logger.info(f"Starting destruction of deployment {deployment_id}")
    ecr_auth_service = get_ecr_auth_service()

    # get the deployment
    deployment = await db.compose_deployments.get_by_id(deployment_id)
    if not deployment:
        raise Exception(f"Deployment {deployment_id} not found")

    # Update status to deleting
    await _update_deployment_state(
        deployment_id, DeploymentStates.DELETING, "Starting deletion"
    )

    # Initialize Helm manager
    helm_manager = HelmManager()
    name = create_release_name(deployment.workspace_id, deployment.name)
    namespace = deployment.namespace

    if not name:
        raise Exception("Deployment name is required")

    try:
        # Step 1: Destroy application
        logger.info(f"Destroying application {name} in namespace {namespace}")
        app_result = helm_manager.destroy(name, namespace)
        if not app_result.success:
            logger.warning(f"Failed to destroy application: {app_result.error}")

        # Step 2: Destroy namespace resources (only if not default namespace)
        logger.info(f"Destroying namespace resources for {namespace}")
        namespace_result = helm_manager.destroy(namespace, "default")
        if not namespace_result.success:
            logger.warning(
                f"Failed to destroy namespace resources: {namespace_result.error}"
            )

        # Step 3: Clean up ECR repositories
        logger.info(f"Cleaning up ECR repositories for deployment {deployment.name}")
        try:
            deleted_repos = await ecr_auth_service.delete_deployment_repositories(
                str(deployment.workspace_id), deployment.name
            )
            if deleted_repos:
                logger.info(
                    f"Deleted {len(deleted_repos)} ECR repositories: {deleted_repos}"
                )
            else:
                logger.info("No ECR repositories found to delete")
        except Exception as ecr_error:
            # ECR cleanup is non-fatal - log warning but continue
            logger.warning(f"ECR cleanup failed (continuing): {ecr_error}")

        # Step 4: Soft delete deployment from database
        deployment = await db.compose_deployments.get_by_id(deployment_id)
        if deployment:
            deployment.deleted_at = datetime.now(UTC)
            deployment.state = DeploymentStates.DELETED
            deployment.status_message = "Deployment deleted"
            await db.compose_deployments.update(deployment)

        logger.info(f"Successfully destroyed deployment {deployment_id}")

    except Exception as e:
        error_type = type(e).__name__
        logger.error(
            f"Destruction of deployment {deployment_id} failed with {error_type}: {str(e)}",
            exc_info=True,
        )
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Deletion failed: {error_type} - {str(e)[:200]}",
        )
        raise


@task
async def rollback_compose_task(deployment_id: str, revision: int) -> None:
    """Rollback a Docker Compose deployment to a previous Helm revision."""
    logger.info(
        f"Starting rollback of deployment {deployment_id} to revision {revision}"
    )

    helm_manager = HelmManager()

    async with db.compose_deployments.transaction() as session:
        deployment = await db.compose_deployments.get_by_id(
            deployment_id, with_lock=True, session=session
        )
        if deployment is None:
            raise ValueError(f"Deployment {deployment_id} not found")

        # Allow rollback from DEPLOYED, FAILED, or DEPLOYING states
        if deployment.state not in (
            DeploymentStates.DEPLOYED,
            DeploymentStates.FAILED,
            DeploymentStates.DEPLOYING,
        ):
            raise ValueError(
                f"Deployment {deployment_id} must be deployed, failed, or deploying to rollback (current state: {deployment.state})"
            )

        name = create_release_name(deployment.workspace_id, deployment.name)
        namespace = deployment.namespace

        if not name:
            raise ValueError("Deployment name is required")

        history = helm_manager.get_history(name, namespace)
        revision_numbers = [
            item.get("revision") for item in history if item.get("revision")
        ]
        if revision not in revision_numbers:
            raise ValueError(
                f"Revision {revision} does not exist in Helm history. "
                f"Available revisions: {sorted(revision_numbers)}"
            )

        current_revision = deployment.current_helm_revision
        if current_revision and revision >= current_revision:
            raise ValueError(
                f"Cannot rollback to revision {revision} which is not older than current revision {current_revision}"
            )

        helm_current_revision = helm_manager._get_latest_revision(name, namespace)
        if helm_current_revision == revision:
            logger.info(
                f"Helm is already at target revision {revision}. Checking DB state for idempotency."
            )
            if deployment.current_helm_revision != revision:
                logger.info(
                    f"DB revision ({deployment.current_helm_revision}) differs from Helm ({revision}). Syncing DB to match Helm."
                )
                deployment.current_helm_revision = revision
                deployment.state = DeploymentStates.DEPLOYED
                deployment.status_message = f"Rollback already completed (idempotent sync to revision {revision})"
                await db.compose_deployments.update(deployment, session=session)
                logger.info(
                    f"Successfully synced deployment {deployment_id} DB state to match Helm revision {revision}"
                )
                return
            else:
                logger.info(
                    f"Deployment {deployment_id} is already at revision {revision} in both Helm and DB. Skipping rollback."
                )
                return

        async with db.compose_deployments.transaction() as session:
            deployment = await db.compose_deployments.get_by_id(
                deployment_id, with_lock=True, session=session
            )
            if deployment is None:
                raise ValueError(f"Deployment {deployment_id} not found")

            if deployment.state in (
                DeploymentStates.DELETING,
                DeploymentStates.DELETED,
            ):
                raise ValueError(
                    f"Deployment {deployment_id} is being deleted (state: {deployment.state}). "
                    "Cannot start rollback."
                )

            deployment.state = DeploymentStates.DEPLOYING
            deployment.status_message = f"Rolling back to revision {revision}"
            await db.compose_deployments.update(deployment, session=session)

    try:
        compose_yaml = helm_manager.get_compose_yaml_from_release(
            name, namespace, revision
        )
        if not compose_yaml:
            history = helm_manager.get_history(name, namespace)
            available_revisions = [
                item.get("revision")
                for item in history
                if item.get("revision") and item.get("revision") != revision
            ]
            error_msg = (
                f"Could not retrieve compose_yaml for revision {revision}. "
                "The revision may not exist or may not have compose_yaml stored. "
                "Only revisions deployed after this feature was added will have compose_yaml available."
            )
            if available_revisions:
                error_msg += f" Available revisions: {sorted(available_revisions)}"
            logger.error(f"Rollback failed for deployment {deployment_id}: {error_msg}")
            raise ValueError(error_msg)

        compose_yaml_size = len(compose_yaml.encode("utf-8"))
        if compose_yaml_size > app_config.COMPOSE_YAML_MAX_SIZE_BYTES:
            error_msg = (
                f"Compose YAML size ({compose_yaml_size} bytes) in revision {revision} "
                f"exceeds maximum allowed size ({app_config.COMPOSE_YAML_MAX_SIZE_BYTES} bytes). "
                "Cannot proceed with rollback."
            )
            logger.error(f"Rollback failed for deployment {deployment_id}: {error_msg}")
            raise ValueError(error_msg)

        try:
            compose_data = yaml.safe_load(compose_yaml)
        except yaml.YAMLError as e:
            logger.error(
                f"YAML parsing error for rollback deployment {deployment_id}, revision {revision}: {e}",
                exc_info=True,
            )
            raise ValueError(f"Invalid compose YAML in revision {revision}: {e}") from e

        compose_file = ComposeParser.parse_dict(compose_data)

        secrets = await db.secrets.get_secrets(deployment_id)
        helm_generator = HelmValuesGenerator(deployment, secrets)
        helm_values, _ = helm_generator.generate_values(compose_file)
        helm_values.compose_yaml = compose_yaml

        target_job_services = [
            service
            for service in helm_values.services
            if service.enabled and service.workloadType == WorkloadType.JOB
        ]

        current_deployment = await db.compose_deployments.get_by_id(deployment_id)
        current_job_services = []
        if (
            current_deployment
            and current_deployment.helm_values
            and current_deployment.helm_values.services
        ):
            current_job_services = [
                service
                for service in current_deployment.helm_values.services
                if service.enabled and service.workloadType == WorkloadType.JOB
            ]

        all_jobs = {}
        for service in target_job_services:
            all_jobs[service.resourceName] = service
        for service in current_job_services:
            all_jobs[service.resourceName] = service

        if all_jobs:
            logger.info(
                f"Rollback will affect {len(all_jobs)} Job(s) (from current state and target revision), "
                "deleting existing Jobs before rollback"
            )
            for service in all_jobs.values():
                try:
                    await _delete_job_with_timeout(
                        service.resourceName,
                        namespace,
                        app_config.ROLLBACK_JOB_DELETION_TIMEOUT_SECONDS,
                    )
                    logger.info(f"Deleted existing Job: {service.name}")
                except TimeoutError as e:
                    logger.error(
                        f"Failed to delete Job {service.name} within timeout: {e}. "
                        "Rollback may proceed but Job may need manual cleanup."
                    )
                    raise ValueError(
                        f"Job deletion timeout for {service.name}: {e}"
                    ) from e
                except ApiException as e:
                    if e.status != 404:
                        logger.warning(f"Could not delete Job {service.name}: {e}")
                        raise ValueError(
                            f"Failed to delete Job {service.name}: {e}"
                        ) from e
                except Exception as e:
                    logger.error(f"Unexpected error deleting Job {service.name}: {e}")
                    raise ValueError(f"Failed to delete Job {service.name}: {e}") from e

        rollback_result = helm_manager.rollback(name, namespace, revision)
        if not rollback_result.success:
            raise Exception(f"Helm rollback failed: {rollback_result.error}")

        if not rollback_result.revision:
            raise ValueError("Helm rollback did not return a revision number")

        new_revision = rollback_result.revision
        if new_revision <= revision:
            raise ValueError(
                f"Invalid rollback result: new revision {new_revision} is not greater than target revision {revision}"
            )

        actual_helm_revision = helm_manager._get_latest_revision(name, namespace)
        if actual_helm_revision != new_revision:
            history = helm_manager.get_history(name, namespace)
            history_revisions = [
                item.get("revision") for item in history if item.get("revision")
            ]

            logger.warning(
                f"Helm revision mismatch detected for deployment {deployment_id}: "
                f"rollback reported revision {new_revision}, but actual Helm revision is {actual_helm_revision}. "
                f"Available revisions in history: {sorted(history_revisions)}. "
                "Helm may have been modified externally between rollback and verification."
            )

            if actual_helm_revision and actual_helm_revision in history_revisions:
                if actual_helm_revision > new_revision:
                    logger.info(
                        f"Actual Helm revision {actual_helm_revision} is newer than reported {new_revision}. "
                        f"Using actual revision {actual_helm_revision}."
                    )
                    new_revision = actual_helm_revision
                else:
                    raise ValueError(
                        f"Helm revision mismatch: rollback reported revision {new_revision}, "
                        f"but actual Helm revision is {actual_helm_revision} (older). "
                        "This indicates an unexpected state change. Manual intervention may be required."
                    )
            else:
                raise ValueError(
                    f"Helm revision mismatch: rollback reported revision {new_revision}, "
                    f"but actual Helm revision is {actual_helm_revision} (not in history). "
                    "Helm may have been modified externally. Manual intervention required."
                )

        try:
            async with db.compose_deployments.transaction() as session:
                deployment = await db.compose_deployments.get_by_id(
                    deployment_id, with_lock=True, session=session
                )
                if deployment is None:
                    raise ValueError(
                        f"Deployment {deployment_id} not found during rollback update"
                    )

                if deployment.state in (
                    DeploymentStates.DELETING,
                    DeploymentStates.DELETED,
                ):
                    raise ValueError(
                        f"Deployment {deployment_id} is being deleted (state: {deployment.state}). "
                        "Cannot complete rollback."
                    )

                if deployment.state != DeploymentStates.DEPLOYING:
                    raise ValueError(
                        f"Deployment {deployment_id} state changed during rollback. "
                        f"Expected DEPLOYING, got {deployment.state}"
                    )

                deployment.compose_yaml = compose_yaml
                deployment.helm_values = helm_values
                deployment.current_helm_revision = new_revision
                deployment.state = DeploymentStates.DEPLOYED
                deployment.status_message = f"Successfully rolled back to revision {revision} (new revision: {new_revision})"

                await db.compose_deployments.update(deployment, session=session)

            logger.info(
                f"Successfully rolled back deployment {deployment_id} to revision {revision}"
            )
        except Exception as db_error:
            logger.error(
                f"Helm rollback succeeded but DB update failed for deployment {deployment_id}. "
                f"Helm is at revision {new_revision} but DB update failed: {db_error}. "
                "Manual intervention may be required to sync state."
            )
            await _update_deployment_state(
                deployment_id,
                DeploymentStates.FAILED,
                f"Rollback partially completed: Helm rolled back to revision {revision} but database update failed. "
                f"Helm revision: {new_revision}. Error: {str(db_error)[:200]}",
            )
            raise ValueError(
                f"Helm rollback succeeded but database update failed: {db_error}"
            ) from db_error

    except ValueError as e:
        error_type = type(e).__name__
        error_msg_full = str(e)
        # Escape braces in error message to prevent KeyError when loguru tries to format
        error_msg_safe = error_msg_full.replace("{", "{{").replace("}", "}}")
        logger.error(
            "Rollback {} failed with {}: {}",
            deployment_id,
            error_type,
            error_msg_safe,
            exc_info=True,
        )
        # Truncate and escape for state message
        error_msg_truncated = error_msg_full[:200].replace("{", "{{").replace("}", "}}")
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Rollback failed: {error_type} - {error_msg_truncated}",
        )
        raise

    except Exception as e:
        error_type = type(e).__name__
        error_msg_full = str(e)
        # Escape braces in error message to prevent KeyError when loguru tries to format
        error_msg_safe = error_msg_full.replace("{", "{{").replace("}", "}}")
        logger.error(
            "Rollback {} failed with {}: {}",
            deployment_id,
            error_type,
            error_msg_safe,
            exc_info=True,
        )
        # Truncate and escape for state message
        error_msg_truncated = error_msg_full[:200].replace("{", "{{").replace("}", "}}")
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Rollback failed: {error_type} - {error_msg_truncated}",
        )
        raise


@flow(log_prints=True)
async def reconcile_rollback_states(
    minutes_old: int | None = None,
) -> dict:
    """Reconcile Helm and database states for stuck deployments."""
    if minutes_old is None:
        minutes_old = app_config.ROLLBACK_RECONCILIATION_INTERVAL_MINUTES
    logger.info(
        f"Starting rollback state reconciliation (checking deployments > {minutes_old} minutes old)"
    )

    stuck_deployments = await db.compose_deployments.find_stuck_deploying(minutes_old)
    logger.info(
        f"Found {len(stuck_deployments)} deployments in DEPLOYING state older than {minutes_old} minutes"
    )

    reconciled = 0
    errors = 0
    skipped = 0

    helm_manager = HelmManager()

    for deployment in stuck_deployments:
        try:
            name = create_release_name(deployment.workspace_id, deployment.name)
            namespace = deployment.namespace

            if not name:
                logger.warning(f"Skipping deployment {deployment.id}: no release name")
                skipped += 1
                continue

            helm_revision = helm_manager._get_latest_revision(name, namespace)
            db_revision = deployment.current_helm_revision

            if helm_revision is None:
                logger.warning(
                    f"Deployment {deployment.id} ({name}): Helm release not found. "
                    "May have been deleted externally."
                )
                skipped += 1
                continue

            history = helm_manager.get_history(name, namespace)
            history_revisions = [
                item.get("revision") for item in history if item.get("revision")
            ]

            if helm_revision == db_revision and db_revision in history_revisions:
                logger.debug(
                    f"Deployment {deployment.id} ({name}): Helm ({helm_revision}) and DB ({db_revision}) match. No action needed."
                )
                skipped += 1
                continue

            if (
                db_revision not in history_revisions
                and helm_revision in history_revisions
            ):
                logger.info(
                    f"Deployment {deployment.id} ({name}): DB revision {db_revision} not in history "
                    f"(only last {len(history_revisions)} revisions kept). Syncing to Helm revision {helm_revision}."
                )
            elif helm_revision != db_revision:
                logger.info(
                    f"Deployment {deployment.id} ({name}): Mismatch detected - Helm: {helm_revision}, DB: {db_revision}. Syncing DB to match Helm."
                )
            else:
                skipped += 1
                continue

            async with db.compose_deployments.transaction() as session:
                deployment_locked = await db.compose_deployments.get_by_id(
                    deployment.id, with_lock=True, session=session
                )
                if deployment_locked is None:
                    logger.warning(
                        f"Deployment {deployment.id} not found during reconciliation"
                    )
                    skipped += 1
                    continue

                if deployment_locked.state != DeploymentStates.DEPLOYING:
                    logger.info(
                        f"Deployment {deployment.id} state changed from DEPLOYING to {deployment_locked.state}. Skipping reconciliation."
                    )
                    skipped += 1
                    continue

                deployment_locked.current_helm_revision = helm_revision
                deployment_locked.state = DeploymentStates.DEPLOYED
                deployment_locked.status_message = (
                    f"Reconciled: DB synced to Helm revision {helm_revision} "
                    f"(was stuck in DEPLOYING state)"
                )
                await db.compose_deployments.update(deployment_locked, session=session)

            logger.info(
                f"Successfully reconciled deployment {deployment.id}: synced DB revision to {helm_revision}"
            )
            reconciled += 1

        except Exception as e:
            error_type = type(e).__name__
            logger.error(
                f"Error reconciling deployment {deployment.id}: {error_type} - {str(e)}",
                exc_info=True,
            )
            errors += 1

    result = {
        "checked": len(stuck_deployments),
        "reconciled": reconciled,
        "skipped": skipped,
        "errors": errors,
    }

    logger.info(
        f"Reconciliation complete: checked {result['checked']}, "
        f"reconciled {result['reconciled']}, skipped {result['skipped']}, errors {result['errors']}"
    )

    return result


reconcile_rollback_states_deployment = reconcile_rollback_states.to_deployment(
    name="reconcile-rollback-states",
    cron=f"*/{app_config.ROLLBACK_RECONCILIATION_INTERVAL_MINUTES} * * * *",
)
