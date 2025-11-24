import asyncio
from datetime import UTC, datetime

from kubernetes.client.exceptions import ApiException
from loguru import logger

from lazycloud_api.services.k8s.client import (
    get_apps_v1_api,
    get_batch_v1_api,
    get_core_v1_api,
)
from shared.models.helm import (
    CurrentUsage,
    HealthCheckValues,
    HelmValues,
    ServiceValues,
)
from shared.models.k8s import (
    Deployment,
    PodList,
    WorkloadType,
)
from shared.models.statuses import (
    JOB_CONDITION_COMPLETE,
    JOB_CONDITION_FAILED,
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
        tasks = [
            self._get_service_status(service_config)
            for service_config in self.helm_values.services
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        services_list: list[ServiceStatus] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    f"Error getting status for service {self.helm_values.services[i].name}: {result}"
                )
                # Return a minimal error status so the deployment status can still be computed
                service_config = self.helm_values.services[i]
                services_list.append(
                    ServiceStatus(
                        name=service_config.name,
                        image=f"{service_config.image.repository}:{service_config.image.tag}",
                        workload_type=service_config.workloadType,
                        status=KubernetesPhase.ERROR,
                        replicas=service_config.replicas or 1,
                        ready_replicas=0,
                        pods=[],
                        resources=None,
                        current_usage=None,
                        ports=None,
                        volumes=None,
                        hpa=service_config.hpa,
                        healthcheck=None,
                        total_restarts=0,
                        last_checked=datetime.now(UTC),
                    )
                )
            else:
                services_list.append(result)

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
        job_status = None

        is_job = service.workloadType == WorkloadType.JOB

        try:
            if is_job:
                batch_v1 = get_batch_v1_api()

                def _get_job():
                    k8s_job = batch_v1.read_namespaced_job(
                        name=service.resourceName,
                        namespace=self.namespace,
                        _request_timeout=5.0,
                    )
                    return batch_v1.api_client.sanitize_for_serialization(k8s_job)

                k8s_job_dict = await asyncio.wait_for(
                    asyncio.to_thread(_get_job), timeout=5.0
                )
                replicas = 1

                # Extract resources from job spec
                spec = k8s_job_dict.get("spec", {})
                if spec:
                    template = spec.get("template", {})
                    if template:
                        pod_spec = template.get("spec", {})
                        if pod_spec:
                            containers = pod_spec.get("containers", [])
                            for container in containers:
                                if container.get("name") == service.name:
                                    resources = container.get("resources")
                                    break

                # Determine job status from conditions or counts
                status_obj = k8s_job_dict.get("status", {})
                conditions = status_obj.get("conditions", [])
                succeeded = status_obj.get("succeeded", 0) or 0
                failed = status_obj.get("failed", 0) or 0
                active = status_obj.get("active", 0) or 0

                for condition in conditions:
                    condition_type = condition.get("type")
                    condition_status = condition.get("status")
                    is_true = condition_status in ["True", "true", True]

                    if condition_type == JOB_CONDITION_COMPLETE and is_true:
                        job_status = KubernetesPhase.STOPPED
                        break
                    elif condition_type == JOB_CONDITION_FAILED and is_true:
                        job_status = KubernetesPhase.ERROR
                        break

                if job_status is None:
                    if succeeded > 0:
                        job_status = KubernetesPhase.STOPPED
                    elif failed > 0:
                        job_status = KubernetesPhase.ERROR
                    elif active > 0:
                        job_status = KubernetesPhase.RUNNING
                    else:
                        job_status = KubernetesPhase.PENDING

            else:
                apps_v1 = get_apps_v1_api()

                def _get_resource():
                    if service.workloadType == WorkloadType.DEPLOYMENT:
                        k8s_resource = apps_v1.read_namespaced_deployment(
                            name=service.resourceName,
                            namespace=self.namespace,
                            _request_timeout=5.0,
                        )
                        resource_dict = apps_v1.api_client.sanitize_for_serialization(
                            k8s_resource
                        )
                        return Deployment(**resource_dict)
                    else:
                        raise ValueError(
                            f"Unsupported resource type: {service.workloadType}"
                        )

                k8s_resource = await asyncio.wait_for(
                    asyncio.to_thread(_get_resource), timeout=5.0
                )
                replicas = k8s_resource.spec.replicas or 1

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

        except asyncio.TimeoutError:
            logger.warning(
                f"Timeout getting Kubernetes resource status for {service.name}"
            )
        except ApiException as e:
            if e.status == 404 and is_job:
                # If Job not found and deployment is older than TTL (300s), assume it completed
                if self.deployed_at:
                    age_seconds = (datetime.now(UTC) - self.deployed_at).total_seconds()
                    if age_seconds > 300:  # TTL is 300 seconds
                        job_status = KubernetesPhase.STOPPED
                    else:
                        job_status = KubernetesPhase.PENDING
                else:
                    job_status = KubernetesPhase.PENDING
            else:
                logger.error(
                    f"Kubernetes API error getting status for {service.name}: {e}"
                )

        except Exception as e:
            logger.error(f"Error getting status for {service.name}: {e}")

        # Get pods and calculate average resource usage
        pods = await self._get_service_pods(service)
        current_usage = self._calculate_average_usage(pods) if pods else None

        # Determine status: use job status for Jobs, otherwise use pod status
        if is_job and job_status is not None:
            status_enum = job_status
            ready_replicas = 1 if job_status == KubernetesPhase.STOPPED else 0
        else:
            ready_replicas = (
                len([p for p in pods if p.phase == KubernetesPhase.RUNNING])
                if pods
                else 0
            )
            status_enum = self._determine_status_from_pods(pods, replicas)

        # Format ports and volumes
        formatted_ports = [
            f"{p.port}:{p.target_port or p.port}/{getattr(p, 'protocol', 'TCP').upper()}"
            for p in (service.ports or [])
        ]

        formatted_volumes = None
        if service.volumes:
            formatted_volumes = [
                f"{v.name}:{v.mount_path}" + (":ro" if v.read_only else "")
                for v in service.volumes
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
            volumes=formatted_volumes,
            hpa=service.hpa,
            healthcheck=k8s_healthcheck,
            total_restarts=sum(pod.restart_count for pod in pods) if pods else 0,
            last_checked=datetime.now(UTC),
        )

    async def _get_service_pods(self, service_config: ServiceValues) -> list[PodStatus]:
        """Get pod details for a specific service."""

        try:
            core_v1 = get_core_v1_api()

            def _list_pods():
                v1_pods = core_v1.list_namespaced_pod(
                    namespace=self.namespace,
                    label_selector=f"app.kubernetes.io/name={service_config.resourceName}",
                    _request_timeout=5.0,
                )
                # Convert to our model
                pods_dict = core_v1.api_client.sanitize_for_serialization(v1_pods)
                return PodList(**pods_dict)

            pod_list = await asyncio.wait_for(
                asyncio.to_thread(_list_pods), timeout=5.0
            )

            # Start all metrics tasks in parallel
            metrics_tasks = {
                pod.metadata.name: asyncio.create_task(
                    self._get_pod_metrics(pod.metadata.name)
                )
                for pod in pod_list.items
            }

            pods = []
            for pod in pod_list.items:
                # Step 1: Extract container statuses and initialize tracking variables
                container_statuses = pod.status.container_statuses if pod.status else []
                ready_containers = 0
                total_containers = len(container_statuses)
                restart_count = 0
                container_reason = None
                container_message = None
                has_container_error = False

                # Step 2: Process each container to calculate metrics and detect errors
                for container_status in container_statuses:
                    if container_status.get("ready", False):
                        ready_containers += 1

                    restart_count += container_status.get("restartCount", 0)

                    # Step 3: Check for container errors (waiting or terminated states)
                    if not has_container_error:
                        waiting = container_status.get("state", {}).get("waiting")
                        if waiting:
                            container_reason = waiting.get("reason")
                            container_message = waiting.get("message")
                            if container_reason in [
                                "ImagePullBackOff",
                                "ErrImagePull",
                                "ErrImageNeverPull",
                                "InvalidImageName",
                            ]:
                                has_container_error = True
                        else:
                            terminated = container_status.get("state", {}).get(
                                "terminated"
                            )
                            if terminated and terminated.get("exitCode", 0) != 0:
                                container_reason = (
                                    terminated.get("reason") or "ContainerError"
                                )
                                container_message = terminated.get("message")
                                has_container_error = True

                # Step 4: Calculate pod age from creation timestamp
                if pod.metadata.creation_timestamp:
                    created = datetime.fromisoformat(
                        pod.metadata.creation_timestamp.replace("Z", "+00:00")
                    )
                    age = datetime.now(UTC) - created
                    age_str = self._format_age(age)
                else:
                    age_str = "Unknown"

                # Step 5: Determine pod phase from status, defaulting to RUNNING
                phase = KubernetesPhase.RUNNING
                pod_reason = None
                pod_message = None

                if pod.status:
                    if pod.status.phase:
                        phase_map = {
                            "Running": KubernetesPhase.RUNNING,
                            "Pending": KubernetesPhase.PENDING,
                            "Failed": KubernetesPhase.ERROR,
                            "Succeeded": KubernetesPhase.STOPPED,
                            "Unknown": KubernetesPhase.ERROR,
                        }
                        phase = phase_map.get(pod.status.phase, KubernetesPhase.ERROR)

                    pod_reason = pod.status.reason
                    pod_message = pod.status.message

                # Step 6: Override phase to ERROR if container errors detected
                if has_container_error:
                    phase = KubernetesPhase.ERROR
                    if container_reason:
                        pod_reason = container_reason
                    if container_message:
                        pod_message = container_message

                # Step 7: Check if pod is being terminated
                if pod.metadata.deletion_timestamp:
                    phase = KubernetesPhase.TERMINATING

                # Step 8: Get metrics result (already started in parallel)
                pod_metrics = await metrics_tasks[pod.metadata.name]

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
                    memory_usage=pod_metrics.get("memory") if pod_metrics else "N/A",
                    reason=pod_reason,
                    message=pod_message,
                )

                pods.append(pod_info)

            return pods

        except asyncio.TimeoutError:
            logger.warning(f"Timeout getting pods for {service_config.name}")
            return []

        except ApiException as e:
            logger.error(
                f"Kubernetes API error getting pods for {service_config.name}: {e}"
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
            # Note: kubectl top uses the metrics.k8s.io API which may not be available
            # We use kubectl top instead of CustomObjectsApi because:
            # 1. Metrics API may not be installed in all clusters
            # 2. kubectl top handles API availability gracefully
            # 3. Simpler than using CustomObjectsApi for metrics.k8s.io/v1beta1

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
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

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

        except asyncio.TimeoutError:
            logger.debug(f"Timeout getting metrics for pod {pod_name}")
            return None
        except Exception as e:
            logger.debug(f"Error getting metrics for pod {pod_name}: {e}")
            return None
