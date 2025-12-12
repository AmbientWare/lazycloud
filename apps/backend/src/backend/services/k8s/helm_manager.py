import asyncio
import json
import os
import subprocess
import tempfile
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from kubernetes.client.exceptions import ApiException
from loguru import logger
from models.helm import HelmNamespaceValues, HelmValues
from pydantic import BaseModel

from backend.config import app_config
from backend.services.k8s.client import get_apps_v1_api, get_core_v1_api


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

    def _namespace_exists(self, namespace: str) -> bool:
        """Check if a namespace exists."""
        try:
            core_v1 = get_core_v1_api()
            core_v1.read_namespace(name=namespace)
            return True

        except ApiException as e:
            if e.status == 404:
                return False

            # For other errors, assume namespace doesn't exist to be safe
            logger.warning(f"Error checking namespace {namespace}: {e}")
            return False

        except Exception as e:
            logger.warning(f"Unexpected error checking namespace {namespace}: {e}")
            return False

    def deploy(self, config: HelmDeploymentConfig) -> DeploymentResult:
        """Deploy or update a Helm release with intelligent handling."""
        logger.info(f"Deploying {config.release_name} in namespace {config.namespace}")

        # Check if release exists
        exists, _ = self.check_release_status(config.release_name, config.namespace)
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
        exists, is_deployed = self.check_release_status(release_name, namespace)

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

        cmd.extend(["--history-max", str(app_config.HELM_HISTORY_MAX_REVISIONS)])
        cmd.append("--cleanup-on-fail")

        result = self._run_helm_command(cmd)

        if result.returncode == 0:
            new_revision = self._get_latest_revision(release_name, namespace)
            return DeploymentResult(
                success=True,
                message=f"Successfully rolled back {release_name}",
                revision=new_revision,
            )

        return DeploymentResult(
            success=False,
            message=f"Failed to rollback {release_name}",
            error=result.stderr,
        )

    def get_history(self, release_name: str, namespace: str) -> list[dict[str, Any]]:
        """Get Helm release history."""
        cmd = ["helm", "history", release_name, "-n", namespace, "-o", "json"]
        result = self._run_helm_command(cmd)

        if result.returncode == 0 and result.stdout:
            try:
                history = json.loads(result.stdout)
                history.sort(key=lambda x: x.get("revision", 0))
                return history

            except (json.JSONDecodeError, KeyError):
                logger.error(f"Failed to parse Helm history: {result.stdout}")
                return []

        return []

    def get_compose_yaml_from_release(
        self, release_name: str, namespace: str, revision: int | None = None
    ) -> str | None:
        """Get compose_yaml from a Helm release revision."""
        cmd = ["helm", "get", "values", release_name, "-n", namespace, "-o", "json"]
        if revision:
            cmd.extend(["--revision", str(revision)])

        result = self._run_helm_command(cmd)

        if result.returncode == 0 and result.stdout:
            try:
                values = json.loads(result.stdout)
                compose_yaml = values.get("compose_yaml")
                if compose_yaml:
                    return compose_yaml

            except (json.JSONDecodeError, KeyError):
                logger.error(f"Failed to parse Helm values: {result.stdout}")

        return None

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

        # Only use --create-namespace if namespace doesn't exist
        if config.create_namespace and not self._namespace_exists(config.namespace):
            cmd.append("--create-namespace")

        if config.wait:
            cmd.append("--wait")

        if config.atomic:
            cmd.extend(["--atomic", "--cleanup-on-fail"])

        cmd.extend(["--history-max", str(app_config.HELM_HISTORY_MAX_REVISIONS)])

        result = self._run_helm_command(cmd)

        if result.returncode == 0:
            return DeploymentResult(
                success=True,
                message=f"Successfully deployed {config.release_name}",
                revision=self._get_latest_revision(
                    config.release_name, config.namespace
                ),
            )

        # If failed due to pending operations, clear locks and retry
        if (
            "another operation" in result.stderr.lower()
            or "pending" in result.stderr.lower()
        ):
            logger.warning(
                "Detected stuck operation during install, clearing Helm locks and retrying"
            )

            # Clear Helm lock secrets
            self._clear_helm_locks(config.release_name, config.namespace)

            # Add force flag if not already present
            if "--force" not in cmd:
                cmd.append("--force")

            # Retry with force after clearing locks
            result = self._run_helm_command(cmd)

            if result.returncode == 0:
                return DeploymentResult(
                    success=True,
                    message=f"Successfully deployed {config.release_name} after clearing locks",
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

        cmd.extend(["--history-max", str(app_config.HELM_HISTORY_MAX_REVISIONS)])

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

        # If failed due to pending operations, clear locks and retry
        if (
            "another operation" in result.stderr.lower()
            or "pending" in result.stderr.lower()
        ):
            logger.warning("Detected stuck operation, clearing Helm locks and retrying")

            # Clear Helm lock secrets
            self._clear_helm_locks(config.release_name, config.namespace)

            # Add force flag if not already present
            if "--force" not in cmd:
                cmd.append("--force")

            # Retry with force after clearing locks
            result = self._run_helm_command(cmd)

            if result.returncode == 0:
                return DeploymentResult(
                    success=True,
                    message=f"Successfully force-upgraded {config.release_name} after clearing locks",
                    revision=self._get_latest_revision(
                        config.release_name, config.namespace
                    ),
                )

        return DeploymentResult(
            success=False,
            message=f"Failed to upgrade {config.release_name}",
            error=result.stderr,
        )

    def check_release_status(
        self, release_name: str, namespace: str
    ) -> tuple[bool, bool]:
        """Check if a release exists and its deployment status."""
        cmd = ["helm", "status", release_name, "-n", namespace, "-o", "json"]
        result = self._run_helm_command(cmd, suppress_not_found_warning=True)

        if result.returncode != 0:
            return False, False

        try:
            status = json.loads(result.stdout)
            info = status.get("info", {})
            return True, info.get("status", "").lower() == "deployed"

        except (json.JSONDecodeError, KeyError):
            return True, False

    async def _run_helm_command_async(
        self, cmd: list[str], suppress_not_found_warning: bool = False
    ) -> subprocess.CompletedProcess:
        """Run a Helm command asynchronously with proper error handling."""
        logger.debug(f"Running command (async): {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ,
        )

        stdout, stderr = await process.communicate()

        result = subprocess.CompletedProcess(
            args=cmd,
            returncode=process.returncode,
            stdout=stdout.decode("utf-8") if stdout else "",
            stderr=stderr.decode("utf-8") if stderr else "",
        )

        if result.returncode != 0:
            if suppress_not_found_warning and "not found" in result.stderr.lower():
                logger.debug(f"Command returned not found (expected): {result.stderr}")
            else:
                logger.warning(f"Command failed: {result.stderr}")

        return result

    async def check_release_status_async(
        self, release_name: str, namespace: str
    ) -> tuple[bool, bool]:
        """Check if a release exists and its deployment status (async)."""
        cmd = ["helm", "status", release_name, "-n", namespace, "-o", "json"]
        result = await self._run_helm_command_async(
            cmd, suppress_not_found_warning=True
        )

        if result.returncode != 0:
            return False, False

        try:
            status = json.loads(result.stdout)
            info = status.get("info", {})
            return True, info.get("status", "").lower() == "deployed"

        except (json.JSONDecodeError, KeyError):
            return True, False

    def _clear_helm_locks(self, release_name: str, namespace: str) -> None:
        """Clear Helm lock secrets for a release to unstick operations."""
        core_v1 = get_core_v1_api()
        lock_prefix = f"sh.helm.release.v1.{release_name}."

        try:
            secrets = core_v1.list_namespaced_secret(namespace=namespace)
            deleted_count = 0

            for secret in secrets.items:
                secret_name = secret.metadata.name
                if secret_name.startswith(lock_prefix):
                    # Delete all lock secrets (pending-install, pending-upgrade, pending-rollback)
                    # but keep deployed releases (they end with just the revision number)
                    if any(
                        status in secret_name
                        for status in [
                            "pending-install",
                            "pending-upgrade",
                            "pending-rollback",
                        ]
                    ):
                        try:
                            core_v1.delete_namespaced_secret(
                                name=secret_name, namespace=namespace
                            )
                            logger.info(
                                f"Deleted Helm lock secret: {secret_name} in namespace {namespace}"
                            )
                            deleted_count += 1
                        except ApiException as e:
                            if e.status != 404:  # Ignore if already deleted
                                logger.warning(
                                    f"Failed to delete Helm lock secret {secret_name}: {e}"
                                )

            if deleted_count > 0:
                logger.info(
                    f"Cleared {deleted_count} Helm lock secret(s) for release {release_name}"
                )
                # Small delay to ensure Kubernetes processes the deletion
                time.sleep(1)
            else:
                logger.debug(f"No Helm lock secrets found for release {release_name}")

        except ApiException as e:
            logger.warning(f"Failed to list secrets when clearing Helm locks: {e}")

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
        apps_v1 = get_apps_v1_api()
        core_v1 = get_core_v1_api()
        label_selector = f"app.kubernetes.io/instance={release_name}"

        try:
            # Get Deployments
            try:
                deployments = apps_v1.list_namespaced_deployment(
                    namespace=namespace, label_selector=label_selector
                )
                items = []
                for deployment in deployments.items:
                    status = (
                        apps_v1.api_client.sanitize_for_serialization(deployment.status)
                        if deployment.status
                        else {}
                    )
                    ready = self._is_resource_ready("deployment", status)
                    items.append(
                        {
                            "name": deployment.metadata.name,
                            "ready": ready,
                            "status": self._get_resource_status_summary(
                                "deployment", status
                            ),
                        }
                    )
                if items:
                    resources["deployment"] = items
            except ApiException as e:
                logger.debug(f"Error getting deployments: {e}")

            # Get Pods
            try:
                pods = core_v1.list_namespaced_pod(
                    namespace=namespace, label_selector=label_selector
                )
                items = []
                for pod in pods.items:
                    status = (
                        core_v1.api_client.sanitize_for_serialization(pod.status)
                        if pod.status
                        else {}
                    )
                    ready = self._is_resource_ready("pod", status)
                    items.append(
                        {
                            "name": pod.metadata.name,
                            "ready": ready,
                            "status": self._get_resource_status_summary("pod", status),
                        }
                    )
                if items:
                    resources["pod"] = items
            except ApiException as e:
                logger.debug(f"Error getting pods: {e}")

            # Get Services
            try:
                services = core_v1.list_namespaced_service(
                    namespace=namespace, label_selector=label_selector
                )
                items = []
                for service in services.items:
                    status = (
                        core_v1.api_client.sanitize_for_serialization(service.status)
                        if service.status
                        else {}
                    )
                    ready = self._is_resource_ready("service", status)
                    items.append(
                        {
                            "name": service.metadata.name,
                            "ready": ready,
                            "status": self._get_resource_status_summary(
                                "service", status
                            ),
                        }
                    )
                if items:
                    resources["service"] = items
            except ApiException as e:
                logger.debug(f"Error getting services: {e}")

        except Exception as e:
            logger.error(f"Error getting release status: {e}")

        return resources

    def _is_resource_ready(self, resource_type: str, status: dict[str, Any]) -> bool:
        """Determine if a resource is ready based on its type and status."""
        if resource_type == "deployment":
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
        if resource_type == "deployment":
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
                    history.sort(key=lambda x: x.get("revision", 0))
                    return history[-1].get("revision")

            except (json.JSONDecodeError, KeyError, IndexError):
                logger.error(f"Failed to parse Helm history: {result.stdout}")

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

    def _run_helm_command(
        self, cmd: list[str], suppress_not_found_warning: bool = False
    ) -> subprocess.CompletedProcess:
        """Run a Helm command with proper error handling."""
        logger.debug(f"Running command: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ,
        )

        if result.returncode != 0:
            # Suppress warning for expected "not found" cases (e.g., checking if release exists)
            if suppress_not_found_warning and "not found" in result.stderr.lower():
                logger.debug(f"Command returned not found (expected): {result.stderr}")
            else:
                logger.warning(f"Command failed: {result.stderr}")

        return result
