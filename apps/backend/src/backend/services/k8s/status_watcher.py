import asyncio
from datetime import UTC, datetime

from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger
from models.compose import LazyCloudLabel
from models.helm import (
    CurrentUsage,
    HealthCheckValues,
    HelmValues,
    ServiceValues,
)
from models.k8s import (
    Deployment,
    PodList,
    WorkloadType,
)
from models.pod_states import ERROR_MESSAGES, PodFailureReasons
from models.statuses import (
    JOB_CONDITION_COMPLETE,
    JOB_CONDITION_FAILED,
    DeploymentStatus,
    NetworkStatusSummary,
    PodStatus,
    ServiceStatus,
    ServiceStatusSummary,
    StatusPhase,
    VolumeStatusSummary,
)
from models.storage import STORAGE_CLASS_SHARED, StorageType

from backend.config import app_config
from backend.services.k8s.client import (
    get_async_api_client,
    get_async_apps_v1_api,
    get_async_batch_v1_api,
    get_async_core_v1_api,
    get_kubeconfig_path,
)


class StatusWatcher:
    """Watches Kubernetes resources and provides real-time status updates."""

    def __init__(
        self,
        deployment_id: str,
        namespace: str,
        helm_values: HelmValues,
        cluster_id: str,
        deployment_name: str,
        deployed_at: datetime | None,
    ):
        """Initialize the status watcher."""
        self.deployment_id = deployment_id
        self.namespace = namespace
        self.helm_values = helm_values
        self.deployment_name = deployment_name
        self.deployed_at = deployed_at
        self.cluster_id = cluster_id

    def _is_custom_domain(self, hostname: str | None) -> bool:
        """Check if hostname is a custom domain (not a platform domain)."""
        if not hostname:
            return False
        return not hostname.endswith(f".{app_config.BASE_DOMAIN}")

    async def _get_domain_status(self, hostname: str) -> tuple[str | None, str | None]:
        """Get domain status from Cloudflare for a custom domain.

        Returns:
            Tuple of (domain_status, cname_target).
            domain_status is the SSL status from Cloudflare (e.g., "active", "pending_validation").
            cname_target is the CNAME target to show if domain is not active.
        """
        # Skip if Cloudflare is not configured
        if not app_config.CLOUDFLARE_API_KEY:
            return None, None

        try:
            from backend.services import get_cloudflare_service

            cloudflare = get_cloudflare_service()
            result = await asyncio.wait_for(
                cloudflare.get_domain_status(hostname),
                timeout=3.0,
            )
            ssl_status = result.get("ssl", {}).get("status")

            # Only show CNAME target if domain is not active
            cname_target = app_config.BASE_DOMAIN if ssl_status != "active" else None
            return ssl_status, cname_target

        except asyncio.TimeoutError:
            logger.debug(f"Timeout getting Cloudflare status for {hostname}")
            return None, None
        except Exception as e:
            logger.debug(f"Error getting Cloudflare status for {hostname}: {e}")
            return None, None

    async def get_service_statuses_for_deployment(
        self, skip_metrics: bool = False
    ) -> list[ServiceStatus]:
        """Get the status of all services in a deployment."""
        if not self.helm_values or not self.helm_values.services:
            return []

        tasks = [
            self._get_service_status(service_config, skip_metrics=skip_metrics)
            for service_config in self.helm_values.services
        ]
        # Overall timeout: max 3 seconds per service, but cap total at 10 seconds
        max_total_timeout = min(10.0, len(tasks) * 3.0)
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=max_total_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Timeout getting all service statuses after {max_total_timeout}s"
            )
            # Return error statuses for all services
            results = [
                TimeoutError(f"Timeout getting status for {s.name}")
                for s in self.helm_values.services
            ]

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
                        status=StatusPhase.ERROR,
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
                services_list.append(result)  # type: ignore[arg-type]

        return services_list

    async def get_service_status(self, service_name: str) -> ServiceStatus | None:
        """Get the status of a specific service with pod details."""
        if not self.helm_values or not self.helm_values.services:
            return None

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
                    status=service.get_deploy_phase(),
                    ready_replicas=service.ready_replicas,
                    total_replicas=service.replicas,
                    image=service.image,
                    ports=service.ports,
                    restarts=service.total_restarts,
                    endpoint=service.endpoint,
                    custom_domain=service.custom_domain,
                    domain_status=service.domain_status,
                    cname_target=service.cname_target,
                    resources=service.resources,
                    current_usage=service.current_usage,
                    healthcheck=service.healthcheck,
                    hpa=service.hpa,
                    pods=service.pods,
                )
            )

        # Determine overall status from service statuses
        overall_status = self._determine_deployment_status(services_list)

        # Get volumes summaries
        volumes_summary = None
        if self.helm_values and self.helm_values.volumes:
            volumes_summary = []
            for v in self.helm_values.volumes:
                # Determine storage type from storage class
                storage_type = StorageType.STANDARD
                if v.storageClass == STORAGE_CLASS_SHARED or (
                    v.labels and v.labels.get(LazyCloudLabel.VOLUME_SHARED) == "true"
                ):
                    storage_type = StorageType.SHARED

                volumes_summary.append(
                    VolumeStatusSummary(
                        name=v.name,
                        status="active",  # TODO: Get actual status from K8s
                        storage_type=storage_type,
                    )
                )

        # Get networks summaries
        networks_summary = None
        if self.helm_values and self.helm_values.networks:
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
            ready=overall_status == StatusPhase.RUNNING,
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

    async def _get_service_status(
        self, service: ServiceValues, skip_metrics: bool = False
    ) -> ServiceStatus:
        """Get status for a specific service."""
        replicas = service.replicas or 1
        updated_replicas: int | None = None
        k8s_healthcheck = None
        resources = None
        job_status = None
        resource_not_found = False

        is_job = service.workloadType == WorkloadType.JOB

        try:
            if is_job:
                batch_v1 = await get_async_batch_v1_api(self.cluster_id)
                api_client = await get_async_api_client(self.cluster_id)

                k8s_job = await asyncio.wait_for(
                    batch_v1.read_namespaced_job(  # type: ignore[misc]
                        name=service.resourceName,
                        namespace=self.namespace,
                    ),
                    timeout=2.0,
                )
                k8s_job_dict = api_client.sanitize_for_serialization(k8s_job)
                replicas = 1

                # Extract resources from job spec
                spec = k8s_job_dict.get("spec", {})  # type: ignore[union-attr]
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
                status_obj = k8s_job_dict.get("status", {})  # type: ignore[union-attr]
                conditions = status_obj.get("conditions", [])
                succeeded = status_obj.get("succeeded", 0) or 0
                failed = status_obj.get("failed", 0) or 0
                active = status_obj.get("active", 0) or 0

                for condition in conditions:
                    condition_type = condition.get("type")
                    condition_status = condition.get("status")
                    is_true = condition_status in ["True", "true", True]

                    if condition_type == JOB_CONDITION_COMPLETE and is_true:
                        job_status = StatusPhase.EXITED
                        break
                    elif condition_type == JOB_CONDITION_FAILED and is_true:
                        job_status = StatusPhase.ERROR
                        break

                if job_status is None:
                    if succeeded > 0:
                        job_status = StatusPhase.EXITED
                    elif failed > 0:
                        job_status = StatusPhase.ERROR
                    elif active > 0:
                        job_status = StatusPhase.RUNNING
                    else:
                        job_status = StatusPhase.PENDING

            else:
                apps_v1 = await get_async_apps_v1_api(self.cluster_id)
                api_client = await get_async_api_client(self.cluster_id)

                if service.workloadType == WorkloadType.DEPLOYMENT:
                    k8s_resource_raw = await asyncio.wait_for(
                        apps_v1.read_namespaced_deployment(  # type: ignore[misc]
                            name=service.resourceName,
                            namespace=self.namespace,
                        ),
                        timeout=2.0,
                    )
                    resource_dict = api_client.sanitize_for_serialization(
                        k8s_resource_raw
                    )
                    k8s_resource = Deployment(**resource_dict)  # type: ignore[arg-type]
                    if k8s_resource.status:
                        updated_replicas = k8s_resource.status.updated_replicas
                else:
                    raise ValueError(
                        f"Unsupported resource type: {service.workloadType}"
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
            if e.status == 404:
                resource_not_found = True
                if is_job:
                    # If Job not found and deployment is older than TTL (7 days), assume it completed and expired
                    if self.deployed_at:
                        age_seconds = (
                            datetime.now(UTC) - self.deployed_at
                        ).total_seconds()
                        if age_seconds > 604800:  # TTL is 7 days (604800 seconds)
                            job_status = StatusPhase.EXITED
                        else:
                            job_status = StatusPhase.PENDING
                    else:
                        job_status = StatusPhase.PENDING
                else:
                    # Deployment/resource not found - log once at debug level
                    logger.debug(
                        f"Kubernetes resource not found for {service.name} "
                        f"(may be pending or deleted)"
                    )
            else:
                logger.error(
                    f"Kubernetes API error getting status for {service.name}: {e}"
                )

        except Exception as e:
            logger.error(f"Error getting status for {service.name}: {e}")

        # Get pods and calculate average resource usage
        pods = await self._get_service_pods(service, skip_metrics=skip_metrics)
        if pods is None:
            pods = []
        current_usage = (
            self._calculate_average_usage(pods) if pods and not skip_metrics else None
        )

        # Determine status: use job status for Jobs, otherwise use pod status
        if is_job and job_status is not None:
            status_enum = job_status
            ready_replicas = 1 if job_status == StatusPhase.EXITED else 0
        elif resource_not_found and not pods:
            # Resource doesn't exist in K8s and no pods - likely deleted or never created
            status_enum = StatusPhase.UNKNOWN
            ready_replicas = 0
        else:
            ready_replicas = (
                len([p for p in pods if p.phase == StatusPhase.RUNNING]) if pods else 0
            )
            status_enum = self._determine_status_from_pods(pods, replicas)

        # Format ports and volumes
        formatted_ports = [
            f"{p.port}:{p.targetPort or p.port}/{getattr(p, 'protocol', 'TCP').upper()}"
            for p in (service.ports or [])
        ]

        formatted_volumes = None
        if service.volumes:
            formatted_volumes = [
                f"{v.name}:{v.mount_path}" + (":ro" if v.read_only else "")
                for v in service.volumes
            ]

        # Extract endpoint from ingress config if available
        endpoint = None
        custom_domain = None
        domain_status = None
        cname_target = None

        if service.ingress and service.ingress.enabled:
            endpoint = service.ingress.hostname

            # Check if this is a custom domain and fetch its status
            if endpoint and self._is_custom_domain(endpoint):
                custom_domain = endpoint
                domain_status, cname_target = await self._get_domain_status(endpoint)

        return ServiceStatus(
            name=service.name,
            image=f"{service.image.repository}:{service.image.tag}",
            workload_type=service.workloadType,
            status=status_enum,
            replicas=replicas,
            ready_replicas=ready_replicas,
            updated_replicas=updated_replicas,
            pods=pods,
            resources=resources,
            current_usage=current_usage,
            ports=formatted_ports or None,
            volumes=formatted_volumes,
            hpa=service.hpa,
            healthcheck=k8s_healthcheck,
            total_restarts=sum(pod.restart_count for pod in pods) if pods else 0,
            last_checked=datetime.now(UTC),
            endpoint=endpoint,
            custom_domain=custom_domain,
            domain_status=domain_status,
            cname_target=cname_target,
        )

    async def _get_service_pods(
        self, service_config: ServiceValues, skip_metrics: bool = False
    ) -> list[PodStatus]:
        """Get pod details for a specific service."""

        try:
            core_v1 = await get_async_core_v1_api(self.cluster_id)
            api_client = await get_async_api_client(self.cluster_id)

            v1_pods = await asyncio.wait_for(
                core_v1.list_namespaced_pod(
                    namespace=self.namespace,
                    label_selector=f"app.kubernetes.io/name={service_config.resourceName}",
                ),
                timeout=2.0,
            )
            # Convert to our model
            pods_dict = api_client.sanitize_for_serialization(v1_pods)
            pod_list = PodList(**pods_dict)  # type: ignore[arg-type]

            if pod_list is None:
                logger.debug(
                    f"No pod list returned for {service_config.name} (may be starting up)"
                )
                return []

            if pod_list.items is None:
                logger.debug(
                    f"No pods found for {service_config.name} (may be starting up)"
                )
                return []

            # Start all metrics tasks in parallel (skip if not needed)
            metrics_tasks: dict[str, asyncio.Task[dict[str, str] | None]] = {}
            if not skip_metrics:
                metrics_tasks = {
                    pod.metadata.name: asyncio.create_task(
                        self._get_pod_metrics(pod.metadata.name)
                    )
                    for pod in pod_list.items
                    if pod.metadata.name
                }

            pods = []
            for pod in pod_list.items:
                # Step 1: Extract container statuses and initialize tracking variables
                container_statuses = (
                    (pod.status.container_statuses or []) if pod.status else []
                )
                ready_containers = 0
                total_containers = len(container_statuses)
                restart_count = 0
                container_reason = None
                container_message = None
                has_container_error = False
                is_creating = False  # Track if container is being created/image pulled

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
                            # Use unified error constants
                            if container_reason in PodFailureReasons.IMAGE_ERRORS:
                                has_container_error = True
                                container_message = ERROR_MESSAGES.get(
                                    container_reason, container_message
                                )
                            elif container_reason in PodFailureReasons.RESTART_ERRORS:
                                has_container_error = True
                                container_message = ERROR_MESSAGES.get(
                                    container_reason, "Service keeps crashing"
                                )
                            elif container_reason in PodFailureReasons.CONTAINER_ERRORS:
                                has_container_error = True
                                container_message = ERROR_MESSAGES.get(
                                    container_reason, container_message
                                )
                            elif container_reason in (
                                "ContainerCreating",
                                "PodInitializing",
                            ):
                                # Container is being created or init containers running
                                is_creating = True
                        else:
                            terminated = container_status.get("state", {}).get(
                                "terminated"
                            )
                            if terminated:
                                exit_code = terminated.get("exitCode", 0)
                                if exit_code != 0:
                                    container_reason = (
                                        terminated.get("reason") or "ContainerError"
                                    )
                                    container_message = terminated.get("message")
                                    has_container_error = True

                                elif restart_count > 0:
                                    # Container exited with code 0 but has restarts
                                    # This usually means it's not a long-running process
                                    container_reason = "ContainerExited"
                                    container_message = ERROR_MESSAGES.get(
                                        "ContainerExited",
                                        "Service exited - use restart: no for one-time tasks",
                                    )
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
                phase = StatusPhase.RUNNING
                pod_reason = None
                pod_message = None

                if pod.status:
                    if pod.status.phase:
                        phase_map = {
                            "Running": StatusPhase.RUNNING,
                            "Pending": StatusPhase.PENDING,
                            "Failed": StatusPhase.ERROR,
                            "Succeeded": StatusPhase.EXITED,
                            "Unknown": StatusPhase.ERROR,
                        }
                        phase = phase_map.get(pod.status.phase, StatusPhase.ERROR)

                    pod_reason = pod.status.reason
                    pod_message = pod.status.message

                # Step 6: Set reason from container state
                if container_reason:
                    pod_reason = container_reason
                if container_message:
                    pod_message = container_message

                # Override phase to ERROR if container errors detected
                if has_container_error:
                    phase = StatusPhase.ERROR

                # Step 7: Check if pod is not ready - differentiate CREATING vs HEALTH_CHECK
                if total_containers > 0 and ready_containers < total_containers:
                    if is_creating:
                        # Container is being created or image is being pulled
                        phase = StatusPhase.CREATING
                    elif phase == StatusPhase.RUNNING:
                        # Container is running but readiness probe hasn't passed
                        phase = StatusPhase.HEALTH_CHECK

                # Step 8: Check if pod is being terminated
                if pod.metadata.deletion_timestamp:
                    phase = StatusPhase.STOPPING

                # Step 8: Get metrics result (already started in parallel)
                pod_metrics = None
                if not skip_metrics and pod.metadata.name in metrics_tasks:
                    pod_metrics = await metrics_tasks[pod.metadata.name]

                pod_info = PodStatus(
                    name=pod.metadata.name or "unknown",
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
            logger.debug(
                f"Timeout getting pods for {service_config.name} (may be starting up)"
            )
            return []

        except ApiException as e:
            if e.status == 404:
                logger.debug(
                    f"No pods found for {service_config.name} (may be starting up)"
                )
            else:
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

    def _determine_deployment_status(
        self, services: list[ServiceStatus]
    ) -> StatusPhase:
        """Determine overall deployment status from service statuses."""
        if not services:
            return StatusPhase.PENDING

        # Count services by their deploy phase (unified status)
        phases = [s.get_deploy_phase() for s in services]

        error_count = sum(
            1 for p in phases if p in (StatusPhase.ERROR, StatusPhase.RESTARTING)
        )
        updating_count = sum(1 for p in phases if p == StatusPhase.UPDATING)
        creating_count = sum(1 for p in phases if p == StatusPhase.CREATING)
        health_check_count = sum(1 for p in phases if p == StatusPhase.HEALTH_CHECK)
        running_count = sum(1 for p in phases if p == StatusPhase.RUNNING)
        stopping_count = sum(1 for p in phases if p == StatusPhase.STOPPING)
        pending_count = sum(1 for p in phases if p == StatusPhase.PENDING)
        exited_count = sum(1 for p in phases if p == StatusPhase.EXITED)

        # Priority order for deployment status:

        # 1. Any service in error = deployment error
        if error_count > 0:
            return StatusPhase.ERROR

        # 2. All services stopping = deployment stopping
        if stopping_count == len(services):
            return StatusPhase.STOPPING

        # 3. Any service updating = deployment updating
        if updating_count > 0:
            return StatusPhase.UPDATING

        # 4. All services running = deployment running
        if running_count == len(services):
            return StatusPhase.RUNNING

        # 5. All services exited (jobs completed) = exited
        if exited_count == len(services):
            return StatusPhase.EXITED

        # 6. Health check pending (container running, waiting for readiness)
        if health_check_count > 0:
            return StatusPhase.HEALTH_CHECK

        # 7. Creating (pulling image, creating container)
        if creating_count > 0:
            return StatusPhase.CREATING

        # 8. Mix of states with some running = health check (progress being made)
        if running_count > 0:
            return StatusPhase.HEALTH_CHECK

        # 9. All pending = pending
        if pending_count == len(services):
            return StatusPhase.PENDING

        return StatusPhase.PENDING

    def _determine_status_from_pods(
        self, pods: list[PodStatus] | None, replicas: int
    ) -> StatusPhase:
        """Determine service status based on actual pod phases."""
        if replicas == 0:
            return StatusPhase.EXITED

        if not pods:
            return StatusPhase.PENDING

        # Count pods by phase
        running_count = sum(1 for p in pods if p.phase == StatusPhase.RUNNING)
        creating_count = sum(1 for p in pods if p.phase == StatusPhase.CREATING)
        health_check_count = sum(1 for p in pods if p.phase == StatusPhase.HEALTH_CHECK)
        pending_count = sum(1 for p in pods if p.phase == StatusPhase.PENDING)
        stopping_count = sum(1 for p in pods if p.phase == StatusPhase.STOPPING)
        error_count = sum(
            1 for p in pods if p.phase in [StatusPhase.ERROR, StatusPhase.RESTARTING]
        )

        # If all pods are stopping, service is stopping
        if stopping_count == len(pods):
            return StatusPhase.STOPPING

        # If any pods are in error state, service is in error
        if error_count > 0:
            return StatusPhase.ERROR

        # More pods than expected = rolling update in progress
        if len(pods) > replicas:
            return StatusPhase.UPDATING

        # Check for rolling update: stopping pods while others run = updating
        if stopping_count > 0 and (
            running_count > 0 or creating_count > 0 or health_check_count > 0
        ):
            return StatusPhase.UPDATING

        # All pods running and count matches replicas = fully healthy
        if running_count == replicas:
            return StatusPhase.RUNNING

        # Health check pending (container running but readiness probe not passed)
        if health_check_count > 0:
            return StatusPhase.HEALTH_CHECK

        # Creating (pulling image, creating container)
        if creating_count > 0:
            return StatusPhase.CREATING

        # Pending pods (waiting to be scheduled)
        if pending_count > 0:
            return StatusPhase.PENDING

        # Some pods running but not all expected
        if running_count > 0:
            return StatusPhase.HEALTH_CHECK

        # Shouldn't reach here, but default to pending
        return StatusPhase.PENDING

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

            kubeconfig_path = get_kubeconfig_path(self.cluster_id)
            cmd = [
                "kubectl",
                "--kubeconfig",
                kubeconfig_path,
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
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

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
