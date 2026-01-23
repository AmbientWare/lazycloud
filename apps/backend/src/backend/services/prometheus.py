import asyncio
from asyncio import Semaphore
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger
from models.metrics import (
    NamespaceBreakdown,
    NamespaceSummary,
    PodMetrics,
    PodUsage,
    UsagePeriod,
    UsageTotals,
)
from tenacity import retry, stop_after_attempt, wait_exponential

# Maximum number of concurrent Prometheus queries to prevent overwhelming the server
MAX_CONCURRENT_PROMETHEUS_QUERIES = 10


class PrometheusMetricsService:
    def __init__(self, prometheus_url: str):
        self.base_url = prometheus_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v1"
        self._semaphore = Semaphore(MAX_CONCURRENT_PROMETHEUS_QUERIES)

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        reraise=True,
    )
    async def _query(self, query: str) -> dict[str, Any]:
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.api_url}/query", params={"query": query}
                )
                response.raise_for_status()
                data = response.json()

                if data.get("status") != "success":
                    logger.error(f"Prometheus query failed: {data}")
                    raise Exception(
                        f"Prometheus query failed with status: {data.get('status')}"
                    )

                return data.get("data", {})

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        reraise=True,
    )
    async def _query_range(
        self, query: str, start_time: datetime, end_time: datetime, step: str = "60s"
    ) -> dict[str, Any]:
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.api_url}/query_range",
                    params={
                        "query": query,
                        "start": start_time.timestamp(),
                        "end": end_time.timestamp(),
                        "step": step,
                    },
                )
                response.raise_for_status()
                data = response.json()

                if data.get("status") != "success":
                    logger.error(f"Prometheus range query failed: {data}")
                    raise Exception(
                        f"Prometheus range query failed with status: {data.get('status')}"
                    )

                return data.get("data", {})

    async def get_cpu_usage(
        self, namespace: str, start_time: datetime, end_time: datetime
    ) -> float:
        """Get total CPU usage in core-seconds for namespace"""
        # Calculate time range in seconds
        duration_seconds = (end_time - start_time).total_seconds()

        # Query: sum of CPU rate over the time range
        # This gives us cores used, multiply by duration to get core-seconds
        query = f'''
            sum(
                rate(
                    container_cpu_usage_seconds_total{{
                        namespace="{namespace}",
                        name!="",
                        name!="POD"
                    }}[5m]
                )
            )
        '''

        result = await self._query_range(query, start_time, end_time, step="60s")

        if not result or "result" not in result:
            logger.warning(f"No CPU usage data for namespace {namespace}")
            return 0.0

        # Calculate average CPU cores and multiply by duration
        values = result["result"]
        if not values or len(values) == 0:
            return 0.0

        # Sum all the values and calculate average
        total = 0.0
        count = 0
        for series in values:
            for _, value in series.get("values", []):
                try:
                    total += float(value)
                    count += 1
                except (ValueError, TypeError):
                    continue

        if count == 0:
            return 0.0

        avg_cores = total / count
        core_seconds = avg_cores * duration_seconds

        logger.debug(f"CPU usage for {namespace}: {core_seconds:.2f} core-seconds")
        return core_seconds

    async def get_memory_usage(
        self, namespace: str, start_time: datetime, end_time: datetime
    ) -> float:
        """Get average memory usage in GB-seconds for namespace"""
        duration_seconds = (end_time - start_time).total_seconds()

        # Query: sum of memory working set (actual memory used)
        query = f'''
            sum(
                container_memory_working_set_bytes{{
                    namespace="{namespace}",
                    name!="",
                    name!="POD"
                }}
            )
        '''

        result = await self._query_range(query, start_time, end_time, step="60s")

        if not result or "result" not in result:
            logger.warning(f"No memory usage data for namespace {namespace}")
            return 0.0

        # Calculate average memory in bytes and convert to GB-seconds
        values = result["result"]
        if not values or len(values) == 0:
            return 0.0

        total_bytes = 0.0
        count = 0
        for series in values:
            for _, value in series.get("values", []):
                try:
                    total_bytes += float(value)
                    count += 1
                except (ValueError, TypeError):
                    continue

        if count == 0:
            return 0.0

        avg_bytes = total_bytes / count
        # Convert bytes to GB
        avg_gb = avg_bytes / (1024**3)
        gb_seconds = avg_gb * duration_seconds

        logger.debug(f"Memory usage for {namespace}: {gb_seconds:.2f} GB-seconds")
        return gb_seconds

    async def get_cpu_requests(
        self, namespace: str, start_time: datetime, end_time: datetime
    ) -> float:
        """Get total CPU requests (reserved) in core-seconds for namespace.

        Uses kube_pod_container_resource_requests from kube-state-metrics.
        """
        duration_seconds = (end_time - start_time).total_seconds()

        # Query: sum of CPU requests for running pods
        query = f'''
            sum(
                kube_pod_container_resource_requests{{
                    namespace="{namespace}",
                    resource="cpu",
                    container!=""
                }}
                * on(pod, namespace) group_left()
                (kube_pod_status_phase{{phase="Running"}} == 1)
            )
        '''

        result = await self._query_range(query, start_time, end_time, step="60s")

        if not result or "result" not in result:
            logger.warning(f"No CPU requests data for namespace {namespace}")
            return 0.0

        values = result["result"]
        if not values or len(values) == 0:
            return 0.0

        total = 0.0
        count = 0
        for series in values:
            for _, value in series.get("values", []):
                try:
                    total += float(value)
                    count += 1
                except (ValueError, TypeError):
                    continue

        if count == 0:
            return 0.0

        avg_cores = total / count
        core_seconds = avg_cores * duration_seconds

        logger.debug(f"CPU requests for {namespace}: {core_seconds:.2f} core-seconds")
        return core_seconds

    async def get_memory_requests(
        self, namespace: str, start_time: datetime, end_time: datetime
    ) -> float:
        """Get total memory requests (reserved) in GB-seconds for namespace.

        Uses kube_pod_container_resource_requests from kube-state-metrics.
        """
        duration_seconds = (end_time - start_time).total_seconds()

        # Query: sum of memory requests for running pods (in bytes)
        query = f'''
            sum(
                kube_pod_container_resource_requests{{
                    namespace="{namespace}",
                    resource="memory",
                    container!=""
                }}
                * on(pod, namespace) group_left()
                (kube_pod_status_phase{{phase="Running"}} == 1)
            )
        '''

        result = await self._query_range(query, start_time, end_time, step="60s")

        if not result or "result" not in result:
            logger.warning(f"No memory requests data for namespace {namespace}")
            return 0.0

        values = result["result"]
        if not values or len(values) == 0:
            return 0.0

        total_bytes = 0.0
        count = 0
        for series in values:
            for _, value in series.get("values", []):
                try:
                    total_bytes += float(value)
                    count += 1
                except (ValueError, TypeError):
                    continue

        if count == 0:
            return 0.0

        avg_bytes = total_bytes / count
        avg_gb = avg_bytes / (1024**3)
        gb_seconds = avg_gb * duration_seconds

        logger.debug(f"Memory requests for {namespace}: {gb_seconds:.2f} GB-seconds")
        return gb_seconds

    async def get_pod_metrics(self, namespace: str, pod_name: str) -> PodMetrics:
        cpu_query = f'''
            sum(
                rate(
                    container_cpu_usage_seconds_total{{
                        namespace="{namespace}",
                        pod="{pod_name}",
                        name!="",
                        name!="POD"
                    }}[5m]
                )
            )
        '''

        memory_query = f'''
            sum(
                container_memory_working_set_bytes{{
                    namespace="{namespace}",
                    pod="{pod_name}",
                    name!="",
                    name!="POD"
                }}
            )
        '''

        cpu_result = await self._query(cpu_query)
        memory_result = await self._query(memory_query)

        cpu_cores = 0.0
        memory_gb = 0.0

        # Parse CPU result
        if cpu_result and "result" in cpu_result:
            results = cpu_result["result"]
            if results and len(results) > 0:
                try:
                    cpu_cores = float(results[0]["value"][1])
                except (KeyError, ValueError, TypeError, IndexError):
                    pass

        # Parse memory result
        if memory_result and "result" in memory_result:
            results = memory_result["result"]
            if results and len(results) > 0:
                try:
                    memory_bytes = float(results[0]["value"][1])
                    memory_gb = memory_bytes / (1024**3)
                except (KeyError, ValueError, TypeError, IndexError):
                    pass

        return PodMetrics(cpu_cores=cpu_cores, memory_gb=memory_gb)

    async def get_namespace_summary(self, namespace: str) -> NamespaceSummary:
        now = datetime.now(timezone.utc)

        cpu_query = f'''
            sum(
                rate(
                    container_cpu_usage_seconds_total{{
                        namespace="{namespace}",
                        name!="",
                        name!="POD"
                    }}[5m]
                )
            )
        '''

        memory_query = f'''
            sum(
                container_memory_working_set_bytes{{
                    namespace="{namespace}",
                    name!="",
                    name!="POD"
                }}
            )
        '''

        storage_query = f'''
            sum(
                kubelet_volume_stats_used_bytes{{
                    namespace="{namespace}"
                }}
            )
        '''

        cpu_result = await self._query(cpu_query)
        memory_result = await self._query(memory_query)
        storage_result = await self._query(storage_query)

        cpu_cores = 0.0
        memory_gb = 0.0
        storage_gb = 0.0

        # Parse results
        if cpu_result and "result" in cpu_result:
            results = cpu_result["result"]
            if results and len(results) > 0:
                try:
                    cpu_cores = float(results[0]["value"][1])
                except (KeyError, ValueError, TypeError, IndexError):
                    pass

        if memory_result and "result" in memory_result:
            results = memory_result["result"]
            if results and len(results) > 0:
                try:
                    memory_bytes = float(results[0]["value"][1])
                    memory_gb = memory_bytes / (1024**3)
                except (KeyError, ValueError, TypeError, IndexError):
                    pass

        if storage_result and "result" in storage_result:
            results = storage_result["result"]
            if results and len(results) > 0:
                try:
                    storage_bytes = float(results[0]["value"][1])
                    storage_gb = storage_bytes / (1024**3)
                except (KeyError, ValueError, TypeError, IndexError):
                    pass

        return NamespaceSummary(
            namespace=namespace,
            timestamp=now,
            cpu_cores=cpu_cores,
            memory_gb=memory_gb,
            storage_gb=storage_gb,
        )

    async def get_namespace_breakdown(
        self, namespace: str, start_time: datetime, end_time: datetime
    ) -> NamespaceBreakdown:
        """Get CPU and memory breakdowns.

        Totals are derived from sum of per-pod max(usage, requests) to ensure
        consistency between workspace totals and deployment breakdowns.
        """
        duration_seconds = (end_time - start_time).total_seconds()

        # Get per-pod breakdown with max(usage, requests) for each pod
        by_pod = await self._get_usage_by_pod(
            namespace, start_time, end_time, duration_seconds
        )

        # Derive totals from per-pod values to ensure consistency
        # Total = sum of max(usage, requests) for each pod
        cpu_total = sum(pod.cpu_core_seconds for pod in by_pod)
        memory_total = sum(pod.memory_gb_seconds for pod in by_pod)

        return NamespaceBreakdown(
            namespace=namespace,
            period=UsagePeriod(
                start=start_time,
                end=end_time,
                duration_seconds=duration_seconds,
            ),
            totals=UsageTotals(
                cpu_core_seconds=cpu_total,
                memory_gb_seconds=memory_total,
            ),
            by_pod=by_pod,
        )

    async def _get_usage_by_pod(
        self,
        namespace: str,
        start_time: datetime,
        end_time: datetime,
        duration_seconds: float,
    ) -> list[PodUsage]:
        """Get resource usage for individual pods.

        Bills for max(actual_usage, requests) per pod to match workspace totals.
        """
        # Actual usage queries
        cpu_usage_query = f'''
            sum by (pod) (
                rate(
                    container_cpu_usage_seconds_total{{
                        namespace="{namespace}",
                        name!="",
                        name!="POD"
                    }}[5m]
                )
            )
        '''

        memory_usage_query = f'''
            sum by (pod) (
                container_memory_working_set_bytes{{
                    namespace="{namespace}",
                    name!="",
                    name!="POD"
                }}
            )
        '''

        # Resource requests queries (reserved resources)
        cpu_requests_query = f'''
            sum by (pod) (
                kube_pod_container_resource_requests{{
                    namespace="{namespace}",
                    resource="cpu",
                    container!=""
                }}
                * on(pod, namespace) group_left()
                (kube_pod_status_phase{{phase="Running"}} == 1)
            )
        '''

        memory_requests_query = f'''
            sum by (pod) (
                kube_pod_container_resource_requests{{
                    namespace="{namespace}",
                    resource="memory",
                    container!=""
                }}
                * on(pod, namespace) group_left()
                (kube_pod_status_phase{{phase="Running"}} == 1)
            )
        '''

        (
            cpu_usage_result,
            memory_usage_result,
            cpu_requests_result,
            memory_requests_result,
        ) = await asyncio.gather(
            self._query_range(cpu_usage_query, start_time, end_time, step="60s"),
            self._query_range(memory_usage_query, start_time, end_time, step="60s"),
            self._query_range(cpu_requests_query, start_time, end_time, step="60s"),
            self._query_range(memory_requests_query, start_time, end_time, step="60s"),
        )

        pod_labels_map: dict[str, dict[str, str]] = {}
        labels_query = f'kube_pod_labels{{namespace="{namespace}"}}'
        labels_result = await self._query(labels_query)
        if labels_result and "result" in labels_result:
            for series in labels_result.get("result", []):
                pod_name = series.get("metric", {}).get("pod", "")
                if pod_name:
                    pod_labels_map[pod_name] = series.get("metric", {})

        pods: dict[str, PodUsage] = {}
        # Track actual usage and requests separately, then take max
        pod_cpu_usage: dict[str, float] = {}
        pod_cpu_requests: dict[str, float] = {}
        pod_memory_usage: dict[str, float] = {}
        pod_memory_requests: dict[str, float] = {}
        warned_pods: set[str] = set()

        def get_pod_info(pod_name: str) -> PodUsage | None:
            """Get or create PodUsage for a pod, returning None if pod should be skipped."""
            if pod_name == "unknown":
                return None

            # Skip pods not in pod_labels_map - they're terminated pods with stale metrics
            if pod_name not in pod_labels_map:
                if pod_name not in warned_pods:
                    warned_pods.add(pod_name)
                    logger.debug(
                        f"Skipping pod {pod_name} in namespace {namespace} - "
                        "not found in kube_pod_labels (likely terminated)"
                    )
                return None

            if pod_name in pods:
                return pods[pod_name]

            pod_labels = pod_labels_map[pod_name]
            service_name = pod_labels.get("label_lazycloud_dev_service", "unknown")
            release_name = pod_labels.get("label_app_kubernetes_io_instance")

            # Only warn once per pod to avoid duplicate warnings
            if service_name == "unknown" and pod_name not in warned_pods:
                warned_pods.add(pod_name)
                logger.warning(
                    f"Pod {pod_name} in namespace {namespace} missing lazycloud.dev/service label. "
                    "Ensure kube-state-metrics has metricLabelsAllowlist configured for this label."
                )

            pods[pod_name] = PodUsage(
                pod=pod_name, service=service_name, release_name=release_name
            )
            return pods[pod_name]

        def calculate_avg_from_series(series: dict) -> float | None:
            """Calculate average value from a time series."""
            values = series.get("values", [])
            if not values:
                return None

            total = 0.0
            count = 0
            for _, value in values:
                try:
                    total += float(value)
                    count += 1
                except (ValueError, TypeError):
                    continue

            return total / count if count > 0 else None

        def process_series(
            result: dict | None,
            storage: dict[str, float],
        ) -> None:
            """Process query result and store average values per pod."""
            if not result or "result" not in result:
                return
            for series in result["result"]:
                pod_name = series.get("metric", {}).get("pod", "unknown")
                if get_pod_info(pod_name) is None:
                    continue
                avg = calculate_avg_from_series(series)
                if avg is not None:
                    storage[pod_name] = avg

        # Process all metrics
        process_series(cpu_usage_result, pod_cpu_usage)
        process_series(cpu_requests_result, pod_cpu_requests)
        process_series(memory_usage_result, pod_memory_usage)
        process_series(memory_requests_result, pod_memory_requests)

        # Calculate final values using max(usage, requests) for each pod
        for pod_name, pod_usage in pods.items():
            cpu_usage = pod_cpu_usage.get(pod_name, 0.0)
            cpu_requests = pod_cpu_requests.get(pod_name, 0.0)
            memory_usage = pod_memory_usage.get(pod_name, 0.0)
            memory_requests = pod_memory_requests.get(pod_name, 0.0)

            # Bill for max(actual, requests) - same logic as workspace totals
            pod_usage.cpu_core_seconds = max(cpu_usage, cpu_requests) * duration_seconds
            pod_usage.memory_gb_seconds = (
                max(memory_usage, memory_requests) / (1024**3)
            ) * duration_seconds

        return list(pods.values())

    async def health_check(self) -> bool:
        """Check if Prometheus is accessible"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/-/healthy")
                return response.status_code == 200
        except Exception as e:
            logger.error(f"Prometheus health check failed: {e}")
            return False
