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
        """Get CPU and memory breakdowns"""
        duration_seconds = (end_time - start_time).total_seconds()

        # Get CPU and memory from Prometheus
        cpu_total = await self.get_cpu_usage(namespace, start_time, end_time)
        memory_total = await self.get_memory_usage(namespace, start_time, end_time)

        # Get breakdown by pod
        by_pod = await self._get_usage_by_pod(
            namespace, start_time, end_time, duration_seconds
        )

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
        """Get resource usage for individual pods."""
        cpu_query = f'''
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

        memory_query = f'''
            sum by (pod) (
                container_memory_working_set_bytes{{
                    namespace="{namespace}",
                    name!="",
                    name!="POD"
                }}
            )
        '''

        cpu_result = await self._query_range(
            cpu_query, start_time, end_time, step="60s"
        )
        memory_result = await self._query_range(
            memory_query, start_time, end_time, step="60s"
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

        def process_series(
            series: dict, metric_type: str, pods: dict[str, PodUsage]
        ) -> None:
            metric = series.get("metric", {})
            pod_name = metric.get("pod", "unknown")
            if pod_name == "unknown":
                return

            pod_labels = pod_labels_map.get(pod_name, {})
            service_name = pod_labels.get("label_lazycloud_io_service", "unknown")
            release_name = pod_labels.get("label_app_kubernetes_io_instance")

            if service_name == "unknown":
                logger.warning(
                    f"Pod {pod_name} in namespace {namespace} missing lazycloud.dev/service label. "
                    "Labels may not be configured or pod may be from system namespace."
                )

            if pod_name not in pods:
                pods[pod_name] = PodUsage(
                    pod=pod_name, service=service_name, release_name=release_name
                )
            elif pods[pod_name].release_name is None and release_name:
                pods[pod_name].release_name = release_name

            values = series.get("values", [])
            if not values:
                return

            # Calculate average across time series and convert to core-seconds or GB-seconds
            total = 0.0
            count = 0
            for _, value in values:
                try:
                    total += float(value)
                    count += 1
                except (ValueError, TypeError):
                    continue

            if count > 0:
                avg = total / count
                if metric_type == "CPU":
                    pods[pod_name].cpu_core_seconds = avg * duration_seconds
                else:
                    pods[pod_name].memory_gb_seconds = (
                        avg / (1024**3)
                    ) * duration_seconds

        if cpu_result and "result" in cpu_result:
            for series in cpu_result["result"]:
                process_series(series, "CPU", pods)

        if memory_result and "result" in memory_result:
            for series in memory_result["result"]:
                process_series(series, "Memory", pods)

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
