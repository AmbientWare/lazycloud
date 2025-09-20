import asyncio
import json
from datetime import UTC, datetime
from typing import Awaitable, Callable

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
    VolumeStatusSummary,
)


class K8sStatusWatcher:
    """Watches Kubernetes resources and provides real-time status updates."""

    def __init__(
        self,
        deployment_id: str,
        namespace: str,
        helm_values: HelmValues,
        callback: Callable[[DeploymentStatus], None]
        | Callable[[DeploymentStatus], Awaitable[None]]
        | None = None,
    ):
        """Initialize the status watcher."""
        self.deployment_id = deployment_id
        self.namespace = namespace
        self.helm_values = helm_values
        self.callback = callback
        self._watch_task: asyncio.Task | None = None
        self._running = False
        self._current_status: DeploymentStatus | None = None

    async def start(self):
        """Start watching Kubernetes resources."""
        if self._running:
            return

        self._running = True
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info(f"Started K8s watcher for deployment {self.deployment_id}")

    async def stop(self):
        """Stop watching Kubernetes resources."""
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task

            except asyncio.CancelledError:
                pass

        logger.info(f"Stopped K8s watcher for deployment {self.deployment_id}")

    async def get_service_statuses_for_deployment(self) -> list[ServiceStatus]:
        """Get the status of all services in a deployment."""
        services_list: list[ServiceStatus] = []
        for service_config in self.helm_values.services:
            status = await self._get_service_status(service_config)
            services_list.append(status)

        return services_list

    async def get_service_status(self, service_name: str) -> ServiceStatus | None:
        """Get the status of a specific service with pod details."""
        # Check if service exists
        for s in self.helm_values.services:
            if s.name == service_name:
                service = s
                break
        else:
            return None

        # Get service status (which includes pods)
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
            volumes_summary = [
                VolumeStatusSummary(
                    name=v.name,
                    status="active",  # TODO: Get actual status from K8s
                )
                for v in self.helm_values.volumes
            ]

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
            deployment_name=self.namespace.split("-", 1)[-1]
            if "-" in self.namespace
            else self.namespace,
            namespace=self.namespace,
            status=overall_status,
            ready=all_ready,
            last_updated=datetime.now(UTC),
            total_services=len(services_list),
            ready_services=ready_services,
            total_replicas=total_replicas,
            ready_replicas=ready_replicas,
            services=services_summary,
            volumes=volumes_summary,
            networks=networks_summary,
        )

    async def _watch_loop(self):
        """Main watch loop that monitors for changes."""
        poll_interval = 2  # seconds

        while self._running:
            try:
                # Get current status
                new_status = await self.get_deployment_status()

                # Check if status changed
                if new_status != self._current_status:
                    self._current_status = new_status
                    if self.callback:
                        if asyncio.iscoroutinefunction(self.callback):
                            await self.callback(new_status)
                        else:
                            self.callback(new_status)

                # Wait before next check
                await asyncio.sleep(poll_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in watch loop: {e}")
                await asyncio.sleep(poll_interval)

    async def _get_service_status(self, service: ServiceValues) -> ServiceStatus:
        """Get status for a specific service."""
        # Default values
        replicas = service.replicas or 1
        k8s_healthcheck = None
        resources = None

        # Get status from kubectl
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

            # Parse using appropriate Pydantic model
            kind = k8s_raw_json.get("kind")
            if kind == "Deployment":
                k8s_resource = Deployment(**k8s_raw_json)
            elif kind == "StatefulSet":
                k8s_resource = StatefulSet(**k8s_raw_json)
            else:
                raise ValueError(f"Unsupported resource type: {kind}")

            replicas = k8s_resource.spec.replicas or 1

            # Find matching container
            k8s_healthcheck = None
            for container in k8s_resource.spec.template.spec.containers:
                if container.name == service.name:
                    # Get health checks if present
                    if container.liveness_probe or container.readiness_probe:
                        k8s_healthcheck = HealthCheckValues(
                            enabled=True,
                            livenessProbe=container.liveness_probe,
                            readinessProbe=container.readiness_probe,
                        )
                    # Get resources if present
                    resources = container.resources
                    break

        except Exception as e:
            logger.error(f"Error getting status for {service.name}: {e}")

        # Get pods and calculate average resource usage
        pods = await self._get_service_pods(service)
        current_usage = self._calculate_average_usage(pods) if pods else None

        # Determine status based on pod count
        ready_replicas = (
            len([p for p in pods if p.phase == KubernetesPhase.RUNNING]) if pods else 0
        )
        status_enum = self._determine_status(ready_replicas, replicas)

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
        )

    async def _get_service_pods(self, service_config: ServiceValues) -> list[PodStatus]:
        """Get pod details for a specific service."""
        # Get resource name
        resource_name = service_config.resourceName

        try:
            # Get pods for this service
            cmd = [
                "kubectl",
                "get",
                "pods",
                "-n",
                self.namespace,
                "-l",
                f"app.kubernetes.io/name={resource_name}",
                "-o",
                "json",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                pods_json = json.loads(stdout.decode())

                # Parse using Pydantic model
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

    def _determine_status(self, ready_replicas: int, replicas: int) -> KubernetesPhase:
        """Determine service status based on replica counts."""
        if ready_replicas == replicas and replicas > 0:
            return KubernetesPhase.RUNNING
        elif ready_replicas > 0:
            return KubernetesPhase.PENDING  # partially running
        elif replicas == 0:
            return KubernetesPhase.STOPPED
        else:
            return KubernetesPhase.ERROR

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
