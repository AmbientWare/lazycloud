"""
Kubernetes workload operations for managing deployments, statefulsets, etc.
"""

import subprocess

from loguru import logger
from pydantic import BaseModel

from shared.models.helm import HelmValues, ServiceValues, WorkloadType


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


class WorkloadOperations:
    """Handles Kubernetes workload operations like restart, scale, etc."""

    def restart_workload(
        self, resource_type: str, resource_name: str, namespace: str
    ) -> RestartResult:
        """Restart a specific Kubernetes workload using rollout restart."""
        cmd = [
            "kubectl",
            "rollout",
            "restart",
            resource_type,
            resource_name,
            "-n",
            namespace,
        ]

        logger.info(
            f"Restarting {resource_type}/{resource_name} in namespace {namespace}"
        )

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            return RestartResult(
                success=True,
                message=f"Successfully triggered restart of {resource_name}",
                resource_type=resource_type,
                resource_name=resource_name,
                output=result.stdout,
            )

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr or e.stdout or str(e)
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

    def restart_service(self, service: ServiceValues, namespace: str) -> RestartResult:
        """Restart a service based on its configuration."""
        # Determine resource type
        resource_type = self._determine_workload_type(service)

        # Get resource name
        resource_name = service.resourceName

        return self.restart_workload(resource_type, resource_name, namespace)

    def restart_all_services(
        self, helm_values: HelmValues, namespace: str
    ) -> RestartAllResult:
        """Restart all services in a deployment."""
        restart_results = []
        failed_count = 0

        for service in helm_values.services:
            result = self.restart_service(service, namespace)

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

    def get_rollout_status(
        self, resource_type: str, resource_name: str, namespace: str
    ) -> tuple[bool, str]:
        """Check the rollout status of a workload."""
        cmd = [
            "kubectl",
            "rollout",
            "status",
            resource_type,
            resource_name,
            "-n",
            namespace,
            "--timeout=5s",
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)

            if result.returncode == 0:
                return True, result.stdout.strip()
            else:
                # Non-zero return code might mean still rolling out
                return False, result.stdout.strip() or "Rollout in progress"

        except Exception as e:
            logger.error(f"Failed to get rollout status: {e}")
            return False, f"Error checking status: {str(e)}"

    def scale_workload(
        self, resource_type: str, resource_name: str, namespace: str, replicas: int
    ) -> tuple[bool, str]:
        """Scale a workload to specified number of replicas."""
        cmd = [
            "kubectl",
            "scale",
            resource_type,
            resource_name,
            "-n",
            namespace,
            f"--replicas={replicas}",
        ]

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True, f"Successfully scaled {resource_name} to {replicas} replicas"

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr or e.stdout or str(e)
            return False, f"Failed to scale: {error_msg}"

    def _determine_workload_type(self, service: ServiceValues) -> str:
        """Determine the Kubernetes workload type from service configuration."""
        # Explicit workload type
        workload_type = service.workloadType
        if workload_type in ["statefulset", "deployment", "daemonset"]:
            return workload_type

        # Check if service has volumes (indicates statefulset)
        volumes = service.volumes
        if volumes:
            return WorkloadType.STATEFULSET

        # Default to deployment
        return WorkloadType.DEPLOYMENT
