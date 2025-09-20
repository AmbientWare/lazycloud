import json
import os
import subprocess
from typing import Optional

from loguru import logger
from pydantic import BaseModel

from shared.models.k8s import Pod


class PodOperationResult(BaseModel):
    """Result of a pod operation."""

    success: bool
    message: str
    error: Optional[str] = None
    pod: Optional[Pod] = None


class KubernetesPodManager:
    """Manages individual Kubernetes pod operations."""

    def __init__(self):
        pass

    def get_pod(self, pod_name: str, namespace: str) -> PodOperationResult:
        """Get detailed information about a pod."""
        logger.info(f"Getting pod {pod_name} in namespace {namespace}")

        cmd = [
            "kubectl",
            "get",
            "pod",
            pod_name,
            "-n",
            namespace,
            "-o",
            "json",
        ]

        result = self._run_kubectl_command(cmd)

        if result.returncode != 0:
            if "NotFound" in result.stderr:
                return PodOperationResult(
                    success=False,
                    message=f"Pod {pod_name} not found",
                    error="Pod does not exist",
                )
            return PodOperationResult(
                success=False,
                message=f"Failed to get pod {pod_name}",
                error=result.stderr,
            )

        try:
            pod_data = json.loads(result.stdout)
            pod = Pod(**pod_data)
            return PodOperationResult(
                success=True,
                message=f"Successfully retrieved pod {pod_name}",
                pod=pod,
            )
        except (json.JSONDecodeError, KeyError) as e:
            return PodOperationResult(
                success=False,
                message="Failed to parse pod information",
                error=str(e),
            )

    def delete_pod(
        self,
        pod_name: str,
        namespace: str,
        grace_period: Optional[int] = None,
        force: bool = False,
    ) -> PodOperationResult:
        """Delete a pod with options for grace period and force deletion."""
        logger.info(f"Deleting pod {pod_name} in namespace {namespace}")

        cmd = [
            "kubectl",
            "delete",
            "pod",
            pod_name,
            "-n",
            namespace,
            "--ignore-not-found=true",
        ]

        if grace_period is not None:
            cmd.extend(["--grace-period", str(grace_period)])

        if force:
            cmd.append("--force")
        else:
            # Don't wait for deletion to complete for non-force deletions
            cmd.append("--wait=false")

        result = self._run_kubectl_command(cmd)

        if result.returncode == 0:
            # Check if the pod was actually deleted or didn't exist
            if "not found" in result.stdout.lower():
                return PodOperationResult(
                    success=True,
                    message=f"Pod {pod_name} does not exist",
                )
            else:
                return PodOperationResult(
                    success=True,
                    message=f"Successfully initiated deletion of pod {pod_name}",
                )

        return PodOperationResult(
            success=False,
            message=f"Failed to delete pod {pod_name}",
            error=result.stderr,
        )

    def verify_pod_ownership(
        self,
        pod_name: str,
        namespace: str,
        service_name: str,
        resource_name: str,
    ) -> PodOperationResult:
        """Verify that a pod belongs to a specific service."""
        logger.info(
            f"Verifying pod {pod_name} belongs to service {service_name} in namespace {namespace}"
        )

        # Get pod information
        pod_result = self.get_pod(pod_name, namespace)
        if not pod_result.success:
            return pod_result

        pod = pod_result.pod
        if not pod:
            return PodOperationResult(
                success=False,
                message="Pod information not available",
                error="Failed to retrieve pod details",
            )

        # Check labels to verify ownership
        pod_labels = pod.metadata.labels or {}
        app_name = pod_labels.get("app.kubernetes.io/name")

        if app_name != resource_name:
            return PodOperationResult(
                success=False,
                message=f"Pod {pod_name} does not belong to service {service_name}",
                error=f"Expected resource name {resource_name}, got {app_name}",
            )

        return PodOperationResult(
            success=True,
            message=f"Pod {pod_name} verified as belonging to service {service_name}",
            pod=pod,
        )

    def _run_kubectl_command(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """Run a kubectl command with proper error handling."""
        logger.debug(f"Running command: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ,
        )

        if result.returncode != 0:
            logger.debug(f"Command failed with error: {result.stderr}")

        return result
