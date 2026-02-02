from datetime import datetime, timezone

from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger
from models.helm import HelmValues, ServiceValues
from models.k8s import WorkloadType
from pydantic import BaseModel

from backend.services.k8s.client import get_async_apps_v1_api


class RestartResult(BaseModel):
    """Result of a restart operation."""

    success: bool
    message: str
    resource_type: str
    resource_name: str
    output: str | None = None
    error: str | None = None


class RestartAllResult(BaseModel):
    """Result of restarting all services."""

    total_services: int
    successful: int
    failed: int
    results: list[RestartResult]


class WorkloadManager:
    """Handles Kubernetes workload operations like restart, scale, etc."""

    def __init__(self, cluster_id: str):
        self.cluster_id = cluster_id

    async def restart_workload(
        self, resource_type: str, resource_name: str, namespace: str
    ) -> RestartResult:
        """Restart a specific Kubernetes workload using rollout restart."""
        logger.info(
            f"Restarting {resource_type}/{resource_name} in namespace {namespace}"
        )

        try:
            apps_v1 = await get_async_apps_v1_api(self.cluster_id)

            # Trigger restart by updating the restartedAt annotation
            # This is the same mechanism kubectl rollout restart uses
            restarted_at = datetime.now(timezone.utc).isoformat()
            patch_body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": restarted_at
                            }
                        }
                    }
                }
            }

            if resource_type.lower() == "deployment":
                await apps_v1.patch_namespaced_deployment(  # type: ignore[arg-type]
                    name=resource_name,
                    namespace=namespace,
                    body=patch_body,
                )
            else:
                return RestartResult(
                    success=False,
                    message=f"Unsupported resource type: {resource_type}",
                    resource_type=resource_type,
                    resource_name=resource_name,
                    error=f"Resource type {resource_type} not supported for restart",
                )

            return RestartResult(
                success=True,
                message=f"Successfully triggered restart of {resource_name}",
                resource_type=resource_type,
                resource_name=resource_name,
                output=f"Restarted at {restarted_at}",
            )

        except ApiException as e:
            error_msg = e.reason or str(e)
            logger.error(
                f"Failed to restart {resource_type}/{resource_name}: {error_msg}"
            )

            return RestartResult(
                success=False,
                message=f"Failed to restart {resource_name}",
                resource_type=resource_type,
                resource_name=resource_name,
                error=error_msg,
            )
        except Exception as e:
            error_msg = str(e)
            logger.error(
                f"Failed to restart {resource_type}/{resource_name}: {error_msg}"
            )

            return RestartResult(
                success=False,
                message=f"Failed to restart {resource_name}",
                resource_type=resource_type,
                resource_name=resource_name,
                error=error_msg,
            )

    async def restart_service(
        self, service: ServiceValues, namespace: str
    ) -> RestartResult:
        """Restart a service based on its configuration."""

        return await self.restart_workload(
            service.workloadType.value.lower(), service.resourceName, namespace
        )

    async def restart_all_services(
        self, helm_values: HelmValues, namespace: str
    ) -> RestartAllResult:
        """Restart all services in a deployment."""
        restart_results = []
        failed_count = 0

        for service in helm_values.services:
            result = await self.restart_service(service, namespace)

            restart_results.append(
                RestartResult(
                    success=result.success,
                    message=result.message,
                    resource_type=result.resource_type,
                    resource_name=result.resource_name,
                    output=result.output,
                    error=result.error,
                )
            )

            if not result.success:
                failed_count += 1

        total_services = len(helm_values.services)

        return RestartAllResult(
            total_services=total_services,
            successful=total_services - failed_count,
            failed=failed_count,
            results=restart_results,
        )

    async def get_rollout_status(
        self, resource_type: str, resource_name: str, namespace: str
    ) -> tuple[bool, str]:
        """Check the rollout status of a workload."""
        try:
            apps_v1 = await get_async_apps_v1_api(self.cluster_id)

            if resource_type.lower() == "deployment":
                deployment = await apps_v1.read_namespaced_deployment(  # type: ignore[arg-type]
                    name=resource_name, namespace=namespace
                )
                if deployment.status:  # type: ignore[union-attr]
                    # Check if rollout is complete
                    conditions = deployment.status.conditions or []  # type: ignore[union-attr]
                    progressing_condition = next(
                        (c for c in conditions if c.type == "Progressing"),
                        None,
                    )

                    if (
                        progressing_condition
                        and progressing_condition.status == "True"
                        and progressing_condition.reason == "NewReplicaSetAvailable"
                    ):
                        return True, "Deployment rollout complete"
                    else:
                        return False, "Rollout in progress"

            return False, "Unknown status"

        except ApiException as e:
            logger.error(f"Failed to get rollout status: {e}")
            return False, f"Error checking status: {e.reason or str(e)}"
        except Exception as e:
            logger.error(f"Failed to get rollout status: {e}")
            return False, f"Error checking status: {str(e)}"

    async def scale_workload(
        self, resource_type: str, resource_name: str, namespace: str, replicas: int
    ) -> tuple[bool, str]:
        """Scale a workload to specified number of replicas."""
        try:
            apps_v1 = await get_async_apps_v1_api(self.cluster_id)

            patch_body = {"spec": {"replicas": replicas}}

            if resource_type.lower() == "deployment":
                await apps_v1.patch_namespaced_deployment(  # type: ignore[arg-type]
                    name=resource_name,
                    namespace=namespace,
                    body=patch_body,
                )
            else:
                return False, f"Unsupported resource type: {resource_type}"

            return True, f"Successfully scaled {resource_name} to {replicas} replicas"

        except ApiException as e:
            error_msg = e.reason or str(e)
            return False, f"Failed to scale: {error_msg}"

        except Exception as e:
            error_msg = str(e)
            return False, f"Failed to scale: {error_msg}"

    def _determine_workload_type(self, service: ServiceValues) -> str:
        """Determine the Kubernetes workload type from service configuration."""
        # Explicit workload type
        logger.info(
            f"Determining workload type for service {service.name} for workload type {service.workloadType}"
        )
        if service.workloadType == WorkloadType.DEPLOYMENT:
            return service.workloadType.value

        # Default to deployment
        return WorkloadType.DEPLOYMENT.value
