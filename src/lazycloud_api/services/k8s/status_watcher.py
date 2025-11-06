import asyncio
import json
from datetime import UTC, datetime

from loguru import logger

from shared.models.helm import (
    CurrentUsage,
    HealthCheckValues,
    HelmValues,
    ServiceValues,
)
from shared.models.k8s import (
    Deployment,
    PodList,
    StatefulSet,
)
from shared.models.statuses import (
    DeploymentStatus,
    KubernetesPhase,
    NetworkStatusSummary,
    PodStatus,
    ServiceStatus,
    ServiceStatusSummary,
    StorageType,
    VolumeStatusSummary,
)


class StatusWatcher:
    """Watches Kubernetes resources and provides real-time status updates."""

    def __init__(
        self,
        deployment_id: str,
        namespace: str,
        helm_values: HelmValues,
        deployment_name: str | None = None,
        deployed_at: datetime | None = None,
    ):
        """Initialize the status watcher."""
        self.deployment_id = deployment_id
        self.namespace = namespace
        self.helm_values = helm_values
        self.deployment_name = deployment_name
        self.deployed_at = deployed_at

    async def get_service_statuses_for_deployment(self) -> list[ServiceStatus]:
        """Get the status of all services in a deployment."""
        services_list: list[ServiceStatus] = []
        for service_config in self.helm_values.services:
            status = await self._get_service_status(service_config)
            services_list.append(status)

        return services_list

    async def get_service_status(self, service_name: str) -> ServiceStatus | None:
        """Get the status of a specific service with pod details."""
        for s in self.helm_values.services:
            if s.name == service_name:
                service = s
                break
        else:
            return None

        return await self._get_service_status(service)

    async def get_deployment_status(self) -> DeploymentStatus:
        """Get the current deployment status with all services."""
        services_list = await self.get_service_statuses_for_deployment()

        # Create service summaries
        services_summary = []
        total_replicas = 0
        ready_replicas = 0
        ready_services = 0

        for service in services_list:
            total_replicas += service.replicas
            ready_replicas += service.ready_replicas
            if service.ready_replicas == service.replicas:
                ready_services += 1

            services_summary.append(
                ServiceStatusSummary(
                    name=service.name,
                    status=service.status,
                    ready_replicas=service.ready_replicas,
                    total_replicas=service.replicas,
                    image=service.image,
                    ports=service.ports,
                    restarts=service.total_restarts,
                )
            )

        # Determine overall status
        all_ready = all(s.ready_replicas == s.replicas for s in services_list)
        any_running = any(s.ready_replicas > 0 for s in services_list)

        if all_ready:
            overall_status = KubernetesPhase.RUNNING
        elif any_running:
            overall_status = KubernetesPhase.PARTIALLY_RUNNING
        else:
            overall_status = KubernetesPhase.STOPPED

        # Get volumes summaries
        volumes_summary = None
        if self.helm_values.volumes:
            volumes_summary = []
            for v in self.helm_values.volumes:
                # Determine storage type from labels
                storage_type = StorageType.STANDARD
                if v.labels and v.labels.get("lazycloud.storage.hp") == "true":
                    storage_type = StorageType.PREMIUM

                volumes_summary.append(
                    VolumeStatusSummary(
                        name=v.name,
                        status="active",  # TODO: Get actual status from K8s
                        storage_type=storage_type,
                    )
                )

        # Get networks summaries
        networks_summary = None
        if self.helm_values.networks:
            networks_summary = [
                NetworkStatusSummary(
                    name=n.name,
                    status="active",  # TODO: Get actual status from K8s
                )
                for n in self.helm_values.networks
            ]

        return DeploymentStatus(
            deployment_id=self.deployment_id,
            deployment_name=self.deployment_name,
            namespace=self.namespace,
            status=overall_status,
            ready=all_ready,
            last_checked=datetime.now(UTC),
            deployed_at=self.deployed_at,
            total_services=len(services_list),
            ready_services=ready_services,
            total_replicas=total_replicas,
            ready_replicas=ready_replicas,
            services=services_summary,
            volumes=volumes_summary,
            networks=networks_summary,
        )

    async def _get_service_status(self, service: ServiceValues) -> ServiceStatus:
        """Get status for a specific service."""
        replicas = service.replicas or 1
        k8s_healthcheck = None
        resources = None

        try:
            cmd = [
                "kubectl",
                "get",
                service.workloadType,
                service.resourceName,
                "-n",
                self.namespace,
                "-o",
                "json",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            k8s_raw_json = json.loads(stdout.decode())

            kind = k8s_raw_json.get("kind")
            if kind == "Deployment":
                k8s_resource = Deployment(**k8s_raw_json)
            elif kind == "StatefulSet":
                k8s_resource = StatefulSet(**k8s_raw_json)
            else:
                raise ValueError(f"Unsupported resource type: {kind}")

            replicas = k8s_resource.spec.replicas or 1

            k8s_healthcheck = None
            for container in k8s_resource.spec.template.spec.containers:
                if container.name == service.name:
                    if container.liveness_probe or container.readiness_probe:
                        k8s_healthcheck = HealthCheckValues(
                            enabled=True,
                            livenessProbe=container.liveness_probe,
                            readinessProbe=container.readiness_probe,
                        )
                    resources = container.resources
                    break

        except Exception as e:
            logger.error(f"Error getting status for {service.name}: {e}")

        # Get pods and calculate average resource usage
        pods = await self._get_service_pods(service)
        current_usage = self._calculate_average_usage(pods) if pods else None

        # Determine status based on actual pod phases
        ready_replicas = (
            len([p for p in pods if p.phase == KubernetesPhase.RUNNING]) if pods else 0
        )
        status_enum = self._determine_status_from_pods(pods, replicas)

        # Format ports and volumes
        formatted_ports = [
            f"{p.port}:{p.target_port or p.port}/{getattr(p, 'protocol', 'TCP').upper()}"
            for p in (service.ports or [])
        ]

        return ServiceStatus(
            name=service.name,
            image=f"{service.image.repository}:{service.image.tag}",
            workload_type=service.workloadType,
            status=status_enum,
            replicas=replicas,
            ready_replicas=ready_replicas,
            pods=pods,
            resources=resources,
            current_usage=current_usage,
            ports=formatted_ports or None,
            volumes=service.volumes or None,
            hpa=service.hpa,
            healthcheck=k8s_healthcheck,
            total_restarts=sum(pod.restart_count for pod in pods) if pods else 0,
            last_checked=datetime.now(UTC),
        )

    async def _get_service_pods(self, service_config: ServiceValues) -> list[PodStatus]:
        """Get pod details for a specific service."""

        try:
            cmd = [
                "kubectl",
                "get",
                "pods",
                "-n",
                self.namespace,
                "-l",
                f"app.kubernetes.io/name={service_config.resourceName}",
                "-o",
                "json",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                pods_json = json.loads(stdout.decode())

                pod_list = PodList(**pods_json)
                pods = []

                for pod in pod_list.items:
                    # Calculate container readiness
                    container_statuses = (
                        pod.status.container_statuses if pod.status else []
                    )
                    ready_containers = sum(
                        1 for c in container_statuses if c.get("ready", False)
                    )
                    total_containers = len(container_statuses)

                    # Calculate restarts
                    restart_count = sum(
                        c.get("restartCount", 0) for c in container_statuses
                    )

                    # Calculate age
                    if pod.metadata.creation_timestamp:
                        created = datetime.fromisoformat(
                            pod.metadata.creation_timestamp.replace("Z", "+00:00")
                        )
                        age = datetime.now(UTC) - created
                        age_str = self._format_age(age)
                    else:
                        age_str = "Unknown"

                    # Get metrics for this pod
                    pod_metrics = await self._get_pod_metrics(pod.metadata.name)

                    phase = KubernetesPhase.RUNNING
                    if pod.status and pod.status.phase:
                        phase_map = {
                            "Running": KubernetesPhase.RUNNING,
                            "Pending": KubernetesPhase.PENDING,
                            "Failed": KubernetesPhase.ERROR,
                            "Succeeded": KubernetesPhase.STOPPED,
                            "Unknown": KubernetesPhase.ERROR,
                        }
                        phase = phase_map.get(pod.status.phase, KubernetesPhase.ERROR)

                        # Check if pod is terminating (has deletionTimestamp)
                        if pod.metadata.deletion_timestamp:
                            phase = KubernetesPhase.TERMINATING

                    # Create PodInfo object
                    pod_info = PodStatus(
                        name=pod.metadata.name,
                        phase=phase,
                        ready_containers=ready_containers,
                        total_containers=total_containers,
                        restart_count=restart_count,
                        age=age_str,
                        node=pod.spec.scheduler_name or "",
                        ip=pod.status.pod_ip if pod.status else "",
                        cpu_usage=pod_metrics.get("cpu") if pod_metrics else "N/A",
                        memory_usage=pod_metrics.get("memory")
                        if pod_metrics
                        else "N/A",
                    )

                    pods.append(pod_info)

                return pods
            else:
                logger.error(
                    f"Failed to get pods for {service_config.name}: {stderr.decode()}"
                )
                return []

        except Exception as e:
            logger.error(f"Error getting pods for {service_config.name}: {e}")
            return []

    def _format_age(self, delta) -> str:
        """Format a timedelta into a human-readable age string."""
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60

        if days > 0:
            return f"{days}d{hours}h"
        elif hours > 0:
            return f"{hours}h{minutes}m"
        else:
            return f"{minutes}m"

    def _determine_status_from_pods(
        self, pods: list[PodStatus], replicas: int
    ) -> KubernetesPhase:
        """Determine service status based on actual pod phases."""
        if replicas == 0:
            return KubernetesPhase.STOPPED

        if not pods:
            # Expected pods but none exist yet - likely just deployed
            return KubernetesPhase.PENDING

        # Count pods by phase
        running_count = sum(1 for p in pods if p.phase == KubernetesPhase.RUNNING)
        pending_count = sum(1 for p in pods if p.phase == KubernetesPhase.PENDING)
        terminating_count = sum(
            1 for p in pods if p.phase == KubernetesPhase.TERMINATING
        )
        error_count = sum(
            1
            for p in pods
            if p.phase in [KubernetesPhase.ERROR, KubernetesPhase.FAILED]
        )

        # If all pods are terminating, service is terminating
        if terminating_count == len(pods):
            return KubernetesPhase.TERMINATING

        # If any pods are in error/failed state, service is in error
        if error_count > 0:
            return KubernetesPhase.ERROR

        # All pods running
        if running_count == replicas:
            return KubernetesPhase.RUNNING

        # Some pods running, some pending - partially running
        if running_count > 0 and pending_count > 0:
            return KubernetesPhase.PARTIALLY_RUNNING

        # Some pods running but not all expected
        if running_count > 0:
            return KubernetesPhase.PARTIALLY_RUNNING

        # All pods pending (starting up)
        if pending_count > 0:
            return KubernetesPhase.PENDING

        # Shouldn't reach here, but default to unknown
        return KubernetesPhase.UNKNOWN

    def _calculate_average_usage(self, pods: list[PodStatus]) -> CurrentUsage | None:
        """Calculate average CPU and memory usage from pods."""
        cpu_values = [
            float(pod.cpu_usage.rstrip("m"))
            for pod in pods
            if pod.cpu_usage and pod.cpu_usage != "N/A"
        ]
        memory_values = [
            float(pod.memory_usage.rstrip("Mi"))
            for pod in pods
            if pod.memory_usage and pod.memory_usage != "N/A"
        ]

        if not cpu_values and not memory_values:
            return None

        return CurrentUsage(
            cpu=f"{sum(cpu_values) / len(cpu_values):.0f}m" if cpu_values else None,
            memory=f"{sum(memory_values) / len(memory_values):.0f}Mi"
            if memory_values
            else None,
        )

    async def _get_pod_metrics(self, pod_name: str) -> dict[str, str] | None:
        """Get resource metrics for a pod."""
        try:
            cmd = [
                "kubectl",
                "top",
                "pod",
                pod_name,
                "-n",
                self.namespace,
                "--no-headers",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                output = stdout.decode().strip()
                if output:
                    parts = output.split()
                    if len(parts) >= 3:
                        return {
                            "cpu": parts[1],
                            "memory": parts[2],
                        }
            return None

        except Exception as e:
            logger.error(f"Error getting metrics for pod {pod_name}: {e}")
            return None
