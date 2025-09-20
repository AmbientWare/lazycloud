import asyncio
import time
from typing import Any, Dict

import nest_asyncio
import yaml
from loguru import logger
from prefect import State, Task, task
from prefect.client.schemas.objects import TaskRun

from lazycloud_api.database import db
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.k8s import (
    create_release_name,
    get_chart_paths,
)
from lazycloud_api.services.k8s.helm_manager import (
    DeploymentStrategy,
    HelmDeploymentConfig,
    HelmManager,
)
from lazycloud_api.services.k8s.pod_manager import KubernetesPodManager
from lazycloud_api.services.k8s.helm_values_generator import HelmValuesGenerator
from shared.models.deployments import DeploymentStates
from shared.models.helm import HelmNamespaceValues, NamespaceConfig

# Apply nest_asyncio to handle event loops in Celery
nest_asyncio.apply()

charts = get_chart_paths()


async def _print_output(task: Task, task_run: TaskRun, state: State[Any]):
    result = await state.result()
    print(f"result type: {type(result)}")
    print(f"result: {result!r}")


async def _update_deployment_state(
    deployment_id: str,
    user_id: str,
    state: DeploymentStates,
    message: str | None = None,
) -> None:
    """Update deployment status in database."""
    try:
        deployment = await db.compose_deployments.aget_by_id(deployment_id)
        if deployment and deployment.user_id == user_id:
            deployment.state = state
            if message:
                deployment.status_message = message[:500]  # Truncate to fit
            await db.compose_deployments.aupdate(deployment)

    except Exception as e:
        logger.error(f"Failed to update deployment status: {e}")


async def _wait_for_secrets(deployment_id: str, timeout: int = 10) -> None:
    """Wait for secrets to be stored for a deployment."""
    # attempt to get the secrets from the database
    start_time = time.time()
    while time.time() - start_time < timeout:
        await asyncio.sleep(1)
        secrets = await db.secrets.aget_secret(deployment_id)
        if secrets and secrets.secrets:  # type: ignore
            break

    if not secrets or not secrets.secrets:
        raise Exception(f"Secrets not found for deployment {deployment_id}")


@task(on_completion=[_print_output])
async def deploy_compose_task(
    deployment_id: str, wait_for_secrets: bool = False
) -> Dict[str, Any]:
    """Deploy a Docker Compose file to Kubernetes using improved Helm management."""
    logger.info(f"Starting deployment {deployment_id}")

    if wait_for_secrets:
        # TODO: need some better way to wait for secrets
        await _wait_for_secrets(deployment_id)

    # get deployment
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment:
        raise Exception(f"Deployment {deployment_id} not found")

    # Parse the compose YAML
    compose_data = yaml.safe_load(deployment.compose_yaml)
    compose_file = ComposeParser.parse_dict(compose_data)

    # get the helm values with deployment_id to load secrets
    helm_generator = HelmValuesGenerator(deployment)
    helm_values, _ = helm_generator.generate_values(compose_file)
    deployment.helm_values = helm_values

    # Update status to deploying
    await _update_deployment_state(
        deployment_id,
        deployment.user_id,
        DeploymentStates.DEPLOYING,
        "Starting deployment",
    )

    # Extract deployment info from helm values
    name = create_release_name(deployment_id, deployment.user_id)
    namespace = deployment.namespace

    # Initialize Helm manager
    helm_manager = HelmManager()

    try:
        # Deploy namespace resources (NetworkPolicy, ResourceQuota, etc.)
        logger.info(f"Deploying namespace resources for {namespace}")
        namespace_values = HelmNamespaceValues(
            namespace=NamespaceConfig(
                name=namespace,
                labels={
                    "lazycloud.io/managed": "true",
                    "lazycloud.io/user-id": deployment.user_id,
                },
            )
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

        # Step 2: Deploy application without waiting
        logger.info(f"Deploying application {name} in namespace {namespace}")
        app_config = HelmDeploymentConfig(
            release_name=name,
            namespace=namespace,
            chart_path=str(charts.compose),
            values=deployment.helm_values,
            timeout="5m",
            strategy=DeploymentStrategy.ROLLING_UPDATE,
        )

        app_result = helm_manager.deploy(app_config)
        if not app_result.success:
            # Check if it's a stuck deployment issue
            if app_result.error and (
                "another operation" in app_result.error or "pending" in app_result.error
            ):
                logger.warning("Detected stuck deployment, attempting force update")
                app_config.strategy = DeploymentStrategy.FORCE_UPDATE
                app_result = helm_manager.deploy(app_config)

            if not app_result.success:
                raise Exception(f"Failed to deploy application: {app_result.error}")

        # we have finished and can udpate the deployment helm values
        await db.compose_deployments.aupdate(deployment)

        # Update status to deployed (deployment is initiated, not necessarily ready)
        await _update_deployment_state(
            deployment_id,
            deployment.user_id,
            DeploymentStates.DEPLOYED,
            f"Deployment initiated successfully (revision: {app_result.revision})",
        )

        return {
            "success": True,
            "deployment_id": deployment_id,
            "namespace": namespace,
            "release_name": name,
            "revision": app_result.revision,
            "message": "Deployment initiated successfully",
        }

    except Exception as e:
        logger.error(f"Deployment {deployment_id} failed: {str(e)}")
        await _update_deployment_state(
            deployment_id,
            deployment.user_id,
            DeploymentStates.FAILED,
            "Deployment failed",
        )

        # Attempt cleanup on failure
        try:
            logger.info("Attempting to cleanup failed deployment")
            helm_manager.destroy(name, "default")
        except Exception as cleanup_error:
            logger.error(f"Cleanup failed: {cleanup_error}")

        raise


@task(on_completion=[_print_output])
async def destroy_compose_task(deployment_id: str, user_id: str) -> Dict[str, Any]:
    """Destroy a Docker Compose deployment from Kubernetes."""
    logger.info(f"Starting destruction of deployment {deployment_id}")

    # get the deployment
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment:
        raise Exception(f"Deployment {deployment_id} not found")

    # Update status to deleting
    await _update_deployment_state(
        deployment_id, user_id, DeploymentStates.DELETING, "Starting deletion"
    )

    # Initialize Helm manager
    helm_manager = HelmManager()
    name = create_release_name(deployment_id, user_id)
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

        # Step 3: Delete deployment from database
        await db.compose_deployments.adelete(deployment_id)

        logger.info(f"Successfully destroyed deployment {deployment_id}")

        return {
            "success": True,
            "deployment_id": deployment_id,
            "message": "Deployment destroyed successfully",
        }

    except Exception as e:
        logger.error(f"Destruction of deployment {deployment_id} failed: {str(e)}")
        await _update_deployment_state(
            deployment_id, user_id, DeploymentStates.FAILED, "Deletion failed", str(e)
        )
        raise


@task(on_completion=[_print_output])
async def delete_instance_task(
    deployment_id: str, service_name: str, pod_name: str, user_id: str
) -> Dict[str, Any]:
    """Delete a specific instance (pod) in a deployment."""
    logger.info(
        f"Starting deletion of instance {pod_name} in deployment {deployment_id} for service {service_name}"
    )

    # Get the deployment
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment:
        raise Exception(f"Deployment {deployment_id} not found")

    if deployment.user_id != user_id:
        raise Exception(f"User {user_id} not authorized for deployment {deployment_id}")

    # Validate service exists in deployment
    helm_values = deployment.helm_values
    if not helm_values or not helm_values.services:
        raise Exception("Deployment does not have service configuration")

    service = next((s for s in helm_values.services if s.name == service_name), None)
    if not service:
        raise Exception(f"Service '{service_name}' not found in deployment")

    namespace = deployment.namespace
    resource_name = service.resourceName

    # Initialize pod manager
    pod_manager = KubernetesPodManager()

    # Verify pod ownership before deletion
    logger.info(f"Verifying pod {pod_name} belongs to service {service_name}")
    verification_result = pod_manager.verify_pod_ownership(
        pod_name=pod_name,
        namespace=namespace,
        service_name=service_name,
        resource_name=resource_name,
    )

    if not verification_result.success:
        raise Exception(verification_result.error or "Pod ownership verification failed")

    # Delete the pod
    logger.info(f"Deleting pod {pod_name} in namespace {namespace}")
    delete_result = pod_manager.delete_pod(
        pod_name=pod_name,
        namespace=namespace,
        grace_period=30,  # Give 30 seconds for graceful shutdown
    )

    if not delete_result.success:
        raise Exception(delete_result.error or "Failed to delete pod")

    logger.info(f"Successfully deleted instance {pod_name}")

    return {
        "deployment_id": deployment_id,
        "service_name": service_name,
        "pod_name": pod_name,
        "message": f"Successfully deleted instance {pod_name}",
    }
