import json
import os
import subprocess
import tempfile
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel

from shared.models.helm import HelmNamespaceValues, HelmValues


class DeploymentStrategy(StrEnum):
    """Deployment strategies for Helm operations."""

    RECREATE = "recreate"
    ROLLING_UPDATE = "rolling_update"
    FORCE_UPDATE = "force_update"


class HelmDeploymentConfig(BaseModel):
    """Configuration for Helm deployments."""

    release_name: str
    namespace: str
    chart_path: str
    values: HelmValues | HelmNamespaceValues
    timeout: str = "5m"
    wait: bool = False
    atomic: bool = False
    force: bool = False
    recreate_pods: bool = False
    cleanup_on_fail: bool = True
    create_namespace: bool = False
    strategy: DeploymentStrategy = DeploymentStrategy.ROLLING_UPDATE


class DeploymentResult(BaseModel):
    """Result of a Helm deployment operation."""

    success: bool
    message: str
    error: str | None = None
    resources: dict[str, Any] | None = None
    revision: int | None = None


class HelmManager:
    """Manages Helm deployments with robust error handling and recovery."""

    def __init__(self):
        pass

    def deploy(self, config: HelmDeploymentConfig) -> DeploymentResult:
        """Deploy or update a Helm release with intelligent handling."""
        logger.info(f"Deploying {config.release_name} in namespace {config.namespace}")

        # Check if release exists
        exists, _ = self._check_release_status(config.release_name, config.namespace)

        if exists and config.strategy == DeploymentStrategy.RECREATE:
            logger.info("Using RECREATE strategy, deleting existing release first")
            delete_result = self.destroy(config.release_name, config.namespace)
            if not delete_result.success:
                return delete_result
            exists = False

        # Prepare values file
        values_file = self._prepare_values_file(config.values)

        try:
            if not exists:
                # Fresh install
                result = self._helm_install(config, values_file)
            else:
                # Update existing release
                result = self._helm_upgrade(config, values_file)

            if result.success and config.wait:
                # Optionally wait for resources to be ready
                ready_result = self._wait_for_resources(config)
                if not ready_result.success:
                    # NOTE: we don't fail the deployment, just warn
                    logger.warning(f"Resources not ready: {ready_result.message}")

            return result

        finally:
            # Cleanup temporary values file if it exists
            if values_file and values_file.exists():
                values_file.unlink()

    def destroy(
        self, release_name: str, namespace: str, purge: bool = True
    ) -> DeploymentResult:
        """Destroy a Helm release."""
        logger.info(f"Destroying release {release_name} in namespace {namespace}")

        cmd = ["helm", "uninstall", release_name, "-n", namespace, "--wait"]
        if not purge:
            cmd.append("--keep-history")

        result = self._run_helm_command(cmd)

        if result.returncode == 0:
            logger.info(f"Helm uninstall completed with stdout: {result.stdout}")

            # Verify the release is actually gone
            check_cmd = ["helm", "status", release_name, "-n", namespace]
            check_result = self._run_helm_command(check_cmd)

            if check_result.returncode != 0 and "not found" in check_result.stderr:
                return DeploymentResult(
                    success=True,
                    message=f"Successfully destroyed release {release_name}",
                )
            else:
                logger.error(f"Release {release_name} still exists after uninstall!")
                return DeploymentResult(
                    success=False,
                    message=f"Release {release_name} still exists after uninstall",
                    error="Helm uninstall did not remove the release",
                )

        if "release: not found" in result.stderr:
            return DeploymentResult(
                success=True, message=f"Release {release_name} does not exist"
            )

        return DeploymentResult(
            success=False,
            message=f"Failed to destroy release {release_name}",
            error=result.stderr,
        )

    def get_status(self, release_name: str, namespace: str) -> DeploymentResult:
        """Get detailed status of a Helm release."""
        exists, is_deployed = self._check_release_status(release_name, namespace)

        if not exists:
            return DeploymentResult(
                success=False, message=f"Release {release_name} not found"
            )

        # Get release values
        cmd = ["helm", "get", "values", release_name, "-n", namespace, "-o", "json"]
        result = self._run_helm_command(cmd)

        if result.returncode != 0:
            return DeploymentResult(
                success=False,
                message="Failed to get release values",
                error=result.stderr,
            )

        # Get resource statuses
        resources = self._get_resource_status(release_name, namespace)

        return DeploymentResult(
            success=True,
            message=f"Release {release_name} is {'deployed' if is_deployed else 'not fully deployed'}",
            resources=resources,
        )

    def rollback(
        self, release_name: str, namespace: str, revision: int | None = None
    ) -> DeploymentResult:
        """Rollback a Helm release to a previous revision."""
        logger.info(f"Rolling back {release_name} to revision {revision or 'previous'}")

        cmd = ["helm", "rollback", release_name, "-n", namespace]
        if revision:
            cmd.append(str(revision))

        result = self._run_helm_command(cmd)

        if result.returncode == 0:
            return DeploymentResult(
                success=True, message=f"Successfully rolled back {release_name}"
            )

        return DeploymentResult(
            success=False,
            message=f"Failed to rollback {release_name}",
            error=result.stderr,
        )

    def _helm_install(
        self, config: HelmDeploymentConfig, values_file: Path
    ) -> DeploymentResult:
        """Perform Helm install."""
        # get the namespace
        cmd = [
            "helm",
            "upgrade",
            "--install",
            config.release_name,
            config.chart_path,
            "-n",
            config.namespace,
            "-f",
            str(values_file),
            "--timeout",
            config.timeout,
        ]

        if config.create_namespace:
            cmd.append("--create-namespace")

        if config.wait:
            cmd.append("--wait")

        if config.atomic:
            cmd.extend(["--atomic", "--cleanup-on-fail"])

        result = self._run_helm_command(cmd)

        if result.returncode == 0:
            return DeploymentResult(
                success=True,
                message=f"Successfully deployed {config.release_name}",
                revision=self._get_latest_revision(
                    config.release_name, config.namespace
                ),
            )

        return DeploymentResult(
            success=False,
            message=f"Failed to deploy {config.release_name}",
            error=result.stderr,
        )

    def _helm_upgrade(
        self, config: HelmDeploymentConfig, values_file: Path
    ) -> DeploymentResult:
        """Perform Helm upgrade with intelligent handling."""
        cmd = [
            "helm",
            "upgrade",
            config.release_name,
            config.chart_path,
            "-n",
            config.namespace,
            "-f",
            str(values_file),
            "--timeout",
            config.timeout,
        ]

        if config.wait:
            cmd.append("--wait")

        if config.force or config.strategy == DeploymentStrategy.FORCE_UPDATE:
            cmd.append("--force")

        if config.recreate_pods:
            cmd.append("--recreate-pods")

        if config.atomic:
            cmd.extend(["--atomic", "--cleanup-on-fail"])

        # First attempt
        result = self._run_helm_command(cmd)

        if result.returncode == 0:
            return DeploymentResult(
                success=True,
                message=f"Successfully upgraded {config.release_name}",
                revision=self._get_latest_revision(
                    config.release_name, config.namespace
                ),
            )

        # If failed due to pending operations, try force update
        if (
            "another operation" in result.stderr.lower()
            or "pending" in result.stderr.lower()
        ):
            logger.warning("Detected stuck operation, attempting force update")

            # Add force flag if not already present
            if "--force" not in cmd:
                cmd.append("--force")

            # Retry with force
            result = self._run_helm_command(cmd)

            if result.returncode == 0:
                return DeploymentResult(
                    success=True,
                    message=f"Successfully force-upgraded {config.release_name}",
                    revision=self._get_latest_revision(
                        config.release_name, config.namespace
                    ),
                )

        return DeploymentResult(
            success=False,
            message=f"Failed to upgrade {config.release_name}",
            error=result.stderr,
        )

    def _check_release_status(
        self, release_name: str, namespace: str
    ) -> tuple[bool, bool]:
        """Check if a release exists and its deployment status."""
        cmd = ["helm", "status", release_name, "-n", namespace, "-o", "json"]
        result = self._run_helm_command(cmd)

        if result.returncode != 0:
            return False, False

        try:
            status = json.loads(result.stdout)
            info = status.get("info", {})
            return True, info.get("status", "").lower() == "deployed"

        except (json.JSONDecodeError, KeyError):
            return True, False

    def _wait_for_resources(
        self, config: HelmDeploymentConfig, timeout: int = 300
    ) -> DeploymentResult:
        """Wait for resources to be ready (custom implementation)."""
        logger.info(f"Waiting for resources in {config.release_name} to be ready")

        start_time = time.time()
        while time.time() - start_time < timeout:
            resources = self._get_resource_status(config.release_name, config.namespace)

            all_ready = True
            not_ready_resources = []

            for resource_type, items in resources.items():
                for item in items:
                    if not item.get("ready", False):
                        all_ready = False
                        not_ready_resources.append(f"{resource_type}/{item['name']}")

            if all_ready:
                return DeploymentResult(
                    success=True, message="All resources are ready", resources=resources
                )

            logger.debug(f"Waiting for resources: {', '.join(not_ready_resources)}")
            time.sleep(5)

        return DeploymentResult(
            success=False,
            message=f"Timeout waiting for resources after {timeout}s",
            resources=resources,
        )

    def _get_resource_status(
        self, release_name: str, namespace: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Get status of all resources in a release."""
        resources = {}

        # Get all resources with the release label
        resource_types = ["deployment", "statefulset", "daemonset", "pod", "service"]

        for resource_type in resource_types:
            cmd = [
                "kubectl",
                "get",
                resource_type,
                "-n",
                namespace,
                "-l",
                f"app.kubernetes.io/instance={release_name}",
                "-o",
                "json",
            ]

            logger.debug(f"Running command: {' '.join(cmd)}")

            result = subprocess.run(cmd, capture_output=True, text=True, env=os.environ)

            if result.returncode == 0 and result.stdout:
                try:
                    data = json.loads(result.stdout)
                    items = []

                    for item in data.get("items", []):
                        metadata = item.get("metadata", {})
                        status = item.get("status", {})

                        # Determine readiness based on resource type
                        ready = self._is_resource_ready(resource_type, status)

                        items.append(
                            {
                                "name": metadata.get("name"),
                                "ready": ready,
                                "status": self._get_resource_status_summary(
                                    resource_type, status
                                ),
                            }
                        )

                    if items:
                        resources[resource_type] = items

                except json.JSONDecodeError:
                    logger.error(f"Failed to parse {resource_type} status")

        return resources

    def _is_resource_ready(self, resource_type: str, status: dict[str, Any]) -> bool:
        """Determine if a resource is ready based on its type and status."""
        if resource_type in ["deployment", "statefulset", "daemonset"]:
            replicas = status.get("replicas", 0)
            ready_replicas = status.get("readyReplicas", 0)
            return replicas > 0 and replicas == ready_replicas

        elif resource_type == "pod":
            conditions = status.get("conditions", [])
            for condition in conditions:
                if (
                    condition.get("type") == "Ready"
                    and condition.get("status") == "True"
                ):
                    return True
            return False

        elif resource_type == "service":
            # Services are generally ready immediately
            return True

        return False

    def _get_resource_status_summary(
        self, resource_type: str, status: dict[str, Any]
    ) -> str:
        """Get a human-readable status summary for a resource."""
        if resource_type in ["deployment", "statefulset", "daemonset"]:
            replicas = status.get("replicas", 0)
            ready = status.get("readyReplicas", 0)
            return f"{ready}/{replicas} ready"

        elif resource_type == "pod":
            phase = status.get("phase", "Unknown")
            return phase

        return "Active"

    def _get_latest_revision(self, release_name: str, namespace: str) -> int | None:
        """Get the latest revision number for a release."""
        cmd = ["helm", "history", release_name, "-n", namespace, "-o", "json"]
        result = self._run_helm_command(cmd)

        if result.returncode == 0 and result.stdout:
            try:
                history = json.loads(result.stdout)
                if history:
                    return history[-1].get("revision")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        return None

    def _prepare_values_file(self, values: HelmValues | HelmNamespaceValues) -> Path:
        """Prepare a temporary values file for Helm."""
        fd, path = tempfile.mkstemp(suffix=".yaml", prefix="helm-values-")
        values_file = Path(path)

        with open(fd, "w") as f:
            yaml.dump(
                values.model_dump(exclude_none=True, by_alias=True, mode="json"),
                f,
                default_flow_style=False,
            )

        return values_file

    def _run_helm_command(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """Run a Helm command with proper error handling."""
        logger.debug(f"Running command: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ,
        )

        if result.returncode != 0:
            logger.warning(f"Command failed: {result.stderr}")

        return result
