from kubernetes.client.exceptions import ApiException
from loguru import logger
from pydantic import BaseModel

from lazycloud_api.services.k8s.client import get_core_v1_api
from shared.models.k8s import Pod


class PodOperationResult(BaseModel):
    """Result of a pod operation."""

    success: bool
    message: str
    error: str | None = None
    pod: Pod | None = None


class KubernetesPodManager:
    """Manages individual Kubernetes pod operations."""

    def __init__(self):
        pass

    def get_pod(self, pod_name: str, namespace: str) -> PodOperationResult:
        """Get detailed information about a pod."""
        logger.info(f"Getting pod {pod_name} in namespace {namespace}")

        try:
            core_v1 = get_core_v1_api()
            v1_pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)

            # Convert Kubernetes client object to dict, then to our Pod model
            pod_dict = core_v1.api_client.sanitize_for_serialization(v1_pod)
            pod = Pod(**pod_dict)

            return PodOperationResult(
                success=True,
                message=f"Successfully retrieved pod {pod_name}",
                pod=pod,
            )

        except ApiException as e:
            if e.status == 404:
                return PodOperationResult(
                    success=False,
                    message=f"Pod {pod_name} not found",
                    error="Pod does not exist",
                )

            logger.error(f"Kubernetes API error getting pod {pod_name}: {e}")
            return PodOperationResult(
                success=False,
                message=f"Failed to get pod {pod_name}",
                error=e.reason or str(e),
            )

        except Exception as e:
            logger.error(f"Error getting pod {pod_name}: {e}")
            return PodOperationResult(
                success=False,
                message="Failed to parse pod information",
                error=str(e),
            )

    def delete_pod(
        self,
        pod_name: str,
        namespace: str,
        grace_period: int | None = None,
        force: bool = False,
    ) -> PodOperationResult:
        """Delete a pod with options for grace period and force deletion."""
        logger.info(f"Deleting pod {pod_name} in namespace {namespace}")

        try:
            core_v1 = get_core_v1_api()

            # Build delete options
            delete_options = {}
            if grace_period is not None:
                delete_options["grace_period_seconds"] = grace_period
            if force:
                delete_options["propagation_policy"] = "Background"

            # Delete the pod
            core_v1.delete_namespaced_pod(
                name=pod_name,
                namespace=namespace,
                **delete_options,
            )

            return PodOperationResult(
                success=True,
                message=f"Successfully initiated deletion of pod {pod_name}",
            )

        except ApiException as e:
            if e.status == 404:
                # Pod doesn't exist - treat as success (ignore-not-found behavior)
                return PodOperationResult(
                    success=True,
                    message=f"Pod {pod_name} does not exist",
                )

            logger.error(f"Kubernetes API error deleting pod {pod_name}: {e}")
            return PodOperationResult(
                success=False,
                message=f"Failed to delete pod {pod_name}",
                error=e.reason or str(e),
            )

        except Exception as e:
            logger.error(f"Error deleting pod {pod_name}: {e}")
            return PodOperationResult(
                success=False,
                message=f"Failed to delete pod {pod_name}",
                error=str(e),
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
