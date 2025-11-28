import yaml
from kubernetes.client.exceptions import ApiException
from loguru import logger
from models.deployments import DeploymentStates
from models.k8s import WorkloadType
from prefect import task

from backend.config import app_config
from backend.database import get_db_context
from backend.prefect_app.deployment.utils import (
    delete_job_with_timeout,
    update_deployment_state,
)
from backend.services.compose.parser import ComposeParser
from backend.services.k8s import create_release_name
from backend.services.k8s.helm_manager import HelmManager
from backend.services.k8s.helm_values_generator import HelmValuesGenerator


@task(log_prints=True)
async def rollback_compose_task(deployment_id: str, revision: int) -> None:
    """Rollback a Docker Compose deployment to a previous Helm revision."""
    logger.info(
        f"Starting rollback of deployment {deployment_id} to revision {revision}"
    )

    helm_manager = HelmManager()

    # Step 1: Quick DB read to validate state and extract needed data
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(deployment_id)

    if deployment is None:
        raise ValueError(f"Deployment {deployment_id} not found")

    if deployment.state not in (
        DeploymentStates.DEPLOYED,
        DeploymentStates.FAILED,
        DeploymentStates.DEPLOYING,
    ):
        raise ValueError(
            f"Deployment {deployment_id} must be deployed, failed, or deploying to rollback "
            f"(current state: {deployment.state})"
        )

    workspace_id = deployment.workspace_id
    deployment_name = deployment.name
    namespace = deployment.namespace
    current_db_revision = deployment.current_helm_revision

    name = create_release_name(workspace_id, deployment_name)
    if not name:
        raise ValueError("Deployment name is required")

    # Step 2: Helm validation (external calls - NO DB transaction)
    history = helm_manager.get_history(name, namespace)
    revision_numbers = [
        item.get("revision") for item in history if item.get("revision")
    ]
    if revision not in revision_numbers:
        raise ValueError(
            f"Revision {revision} does not exist in Helm history. "
            f"Available revisions: {sorted(revision_numbers)}"
        )

    if current_db_revision and revision >= current_db_revision:
        raise ValueError(
            f"Cannot rollback to revision {revision} which is not older than "
            f"current revision {current_db_revision}"
        )

    # Step 3: Check idempotency - if Helm is already at target revision
    helm_current_revision = helm_manager._get_latest_revision(name, namespace)
    if helm_current_revision == revision:
        logger.info(
            f"Helm is already at target revision {revision}. Checking DB state for idempotency."
        )
        if current_db_revision != revision:
            # Sync DB to match Helm state
            async with get_db_context() as db:
                deployment = await db.compose_deployments.get_by_id(
                    deployment_id, with_lock=True
                )
                if deployment is None:
                    raise ValueError(f"Deployment {deployment_id} not found")

                deployment.current_helm_revision = revision
                deployment.state = DeploymentStates.DEPLOYED
                deployment.status_message = f"Rollback already completed (idempotent sync to revision {revision})"
                deployment.current_task_run_id = None
                await db.compose_deployments.update(deployment)

            logger.info(
                f"Successfully synced deployment {deployment_id} DB state to match Helm revision {revision}"
            )

        else:
            logger.info(
                f"Deployment {deployment_id} is already at revision {revision} in both Helm and DB. "
                "Skipping rollback."
            )
        return

    # Step 4: Mark deployment as DEPLOYING (quick DB write)
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(
            deployment_id, with_lock=True
        )
        if deployment is None:
            raise ValueError(f"Deployment {deployment_id} not found")

        if deployment.state in (DeploymentStates.DELETING, DeploymentStates.DELETED):
            raise ValueError(
                f"Deployment {deployment_id} is being deleted (state: {deployment.state}). "
                "Cannot start rollback."
            )

        deployment.state = DeploymentStates.DEPLOYING
        deployment.status_message = f"Rolling back to revision {revision}"
        await db.compose_deployments.update(deployment)

    # Step 5: Perform the actual rollback (external operations - NO DB transaction)
    try:
        # Get compose YAML from target revision
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

        # Quick DB reads for helm values generation
        async with get_db_context() as db:
            secrets = await db.secrets.get_secrets(deployment_id)
            current_deployment = await db.compose_deployments.get_by_id(deployment_id)

        if current_deployment is None:
            raise ValueError(f"Deployment {deployment_id} not found during rollback")

        helm_generator = HelmValuesGenerator(current_deployment, secrets)
        helm_values, _ = helm_generator.generate_values(compose_file)
        helm_values.compose_yaml = compose_yaml

        # Identify jobs that need cleanup
        target_job_services = [
            service
            for service in helm_values.services
            if service.enabled and service.workloadType == WorkloadType.JOB
        ]

        current_job_services = []
        if current_deployment.helm_values and current_deployment.helm_values.services:
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

        # Delete jobs before rollback (K8s operations - external)
        if all_jobs:
            logger.info(
                f"Rollback will affect {len(all_jobs)} Job(s), deleting existing Jobs before rollback"
            )
            for service in all_jobs.values():
                try:
                    await delete_job_with_timeout(
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

        # Perform Helm rollback (external)
        rollback_result = helm_manager.rollback(name, namespace, revision)
        if not rollback_result.success:
            raise Exception(f"Helm rollback failed: {rollback_result.error}")

        if not rollback_result.revision:
            raise ValueError("Helm rollback did not return a revision number")

        new_revision = rollback_result.revision
        if new_revision <= revision:
            raise ValueError(
                f"Invalid rollback result: new revision {new_revision} is not greater "
                f"than target revision {revision}"
            )

        # Verify Helm state
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

        # Step 6: Update deployment with successful rollback state (quick DB write)
        try:
            async with get_db_context() as db:
                deployment = await db.compose_deployments.get_by_id(
                    deployment_id, with_lock=True
                )
                if deployment is None:
                    raise ValueError(
                        f"Deployment {deployment_id} not found during rollback update"
                    )

                # Optimistic concurrency check - verify state hasn't changed unexpectedly
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
                deployment.current_task_run_id = None
                await db.compose_deployments.update(deployment)

            logger.info(
                f"Successfully rolled back deployment {deployment_id} to revision {revision}"
            )

        except Exception as db_error:
            logger.error(
                f"Helm rollback succeeded but DB update failed for deployment {deployment_id}. "
                f"Helm is at revision {new_revision} but DB update failed: {db_error}. "
                "Manual intervention may be required to sync state."
            )

            await update_deployment_state(
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
        error_msg_safe = error_msg_full.replace("{", "{{").replace("}", "}}")
        logger.error(
            "Rollback {} failed with {}: {}",
            deployment_id,
            error_type,
            error_msg_safe,
            exc_info=True,
        )
        error_msg_truncated = error_msg_full[:200].replace("{", "{{").replace("}", "}}")
        await update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Rollback failed: {error_type} - {error_msg_truncated}",
        )
        raise

    except Exception as e:
        error_type = type(e).__name__
        error_msg_full = str(e)
        error_msg_safe = error_msg_full.replace("{", "{{").replace("}", "}}")
        logger.error(
            "Rollback {} failed with {}: {}",
            deployment_id,
            error_type,
            error_msg_safe,
            exc_info=True,
        )
        error_msg_truncated = error_msg_full[:200].replace("{", "{{").replace("}", "}}")

        await update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Rollback failed: {error_type} - {error_msg_truncated}",
        )
        raise
