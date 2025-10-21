from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from shared.models.metrics import (
    NamespaceBreakdown,
    NamespaceSummary,
    PodMetrics,
    PodUsage,
    ServiceUsage,
    UsagePeriod,
    UsageTotals,
)


class PrometheusMetricsService:
    def __init__(self, prometheus_url: str):
        self.base_url = prometheus_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v1"

    async def _query(self, query: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.api_url}/query", params={"query": query}
                )
                response.raise_for_status()
                data = response.json()

                if data.get("status") != "success":
                    logger.error(f"Prometheus query failed: {data}")
                    return {}

                return data.get("data", {})
        except Exception as e:
            logger.error(f"Error querying Prometheus: {e}")
            return {}

    async def _query_range(
        self, query: str, start_time: datetime, end_time: datetime, step: str = "60s"
    ) -> dict[str, Any]:
        try:
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
                    return {}

                return data.get("data", {})
        except Exception as e:
            logger.error(f"Error querying Prometheus range: {e}")
            return {}

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

    async def get_storage_usage(
        self, namespace: str, start_time: datetime, end_time: datetime
    ) -> float:
        """Get storage usage in GB-hours for namespace PVCs"""
        duration_hours = (end_time - start_time).total_seconds() / 3600

        # Query: sum of PVC capacity
        query = f'''
            sum(
                kubelet_volume_stats_capacity_bytes{{
                    namespace="{namespace}"
                }}
            )
        '''

        result = await self._query_range(query, start_time, end_time, step="5m")

        if not result or "result" not in result:
            logger.warning(f"No storage usage data for namespace {namespace}")
            return 0.0

        values = result["result"]
        if not values or len(values) == 0:
            return 0.0

        # Calculate average storage
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
        gb_hours = avg_gb * duration_hours

        logger.debug(f"Storage usage for {namespace}: {gb_hours:.2f} GB-hours")
        return gb_hours

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
                kubelet_volume_stats_capacity_bytes{{
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
        duration_seconds = (end_time - start_time).total_seconds()

        # Get overall totals
        cpu_total = await self.get_cpu_usage(namespace, start_time, end_time)
        memory_total = await self.get_memory_usage(namespace, start_time, end_time)
        storage_total = await self.get_storage_usage(namespace, start_time, end_time)

        # Get breakdown by service (using lazycloud.io/service label)
        by_service = await self._get_usage_by_service(
            namespace, start_time, end_time, duration_seconds
        )

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
                storage_gb_hours=storage_total,
            ),
            by_service=by_service,
            by_pod=by_pod,
        )

    async def _get_usage_by_service(
        self,
        namespace: str,
        start_time: datetime,
        end_time: datetime,
        duration_seconds: float,
    ) -> dict[str, ServiceUsage]:
        """Get resource usage grouped by service label."""
        cpu_query = f'''
            sum by (label_lazycloud_io_service) (
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
            sum by (label_lazycloud_io_service) (
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

        services: dict[str, ServiceUsage] = {}

        # Process CPU results
        if cpu_result and "result" in cpu_result:
            for series in cpu_result["result"]:
                service_name = series.get("metric", {}).get(
                    "label_lazycloud_io_service", "unknown"
                )
                if service_name not in services:
                    services[service_name] = ServiceUsage()

                # Calculate average CPU and multiply by duration
                total_cpu = 0.0
                count = 0
                for _, value in series.get("values", []):
                    try:
                        total_cpu += float(value)
                        count += 1
                    except (ValueError, TypeError):
                        continue

                if count > 0:
                    avg_cpu = total_cpu / count
                    services[service_name].cpu_core_seconds = avg_cpu * duration_seconds

        # Process memory results
        if memory_result and "result" in memory_result:
            for series in memory_result["result"]:
                service_name = series.get("metric", {}).get(
                    "label_lazycloud_io_service", "unknown"
                )
                if service_name not in services:
                    services[service_name] = ServiceUsage()

                # Calculate average memory
                total_bytes = 0.0
                count = 0
                for _, value in series.get("values", []):
                    try:
                        total_bytes += float(value)
                        count += 1
                    except (ValueError, TypeError):
                        continue

                if count > 0:
                    avg_bytes = total_bytes / count
                    avg_gb = avg_bytes / (1024**3)
                    services[service_name].memory_gb_seconds = avg_gb * duration_seconds

        # Get pod counts per service
        pod_count_query = f'''
            count by (label_lazycloud_io_service) (
                kube_pod_info{{namespace="{namespace}"}}
            )
        '''
        pod_count_result = await self._query(pod_count_query)

        if pod_count_result and "result" in pod_count_result:
            for series in pod_count_result["result"]:
                service_name = series.get("metric", {}).get(
                    "label_lazycloud_io_service", "unknown"
                )
                if service_name in services:
                    try:
                        services[service_name].pod_count = int(
                            float(series["value"][1])
                        )
                    except (KeyError, ValueError, TypeError, IndexError):
                        pass

        return services

    async def _get_usage_by_pod(
        self,
        namespace: str,
        start_time: datetime,
        end_time: datetime,
        duration_seconds: float,
    ) -> list[PodUsage]:
        """Get resource usage for individual pods."""
        cpu_query = f'''
            sum by (pod, label_lazycloud_io_service) (
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
            sum by (pod, label_lazycloud_io_service) (
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

        pods: dict[str, PodUsage] = {}

        # Process CPU results
        if cpu_result and "result" in cpu_result:
            for series in cpu_result["result"]:
                pod_name = series.get("metric", {}).get("pod", "unknown")
                service_name = series.get("metric", {}).get(
                    "label_lazycloud_io_service", "unknown"
                )

                if pod_name not in pods:
                    pods[pod_name] = PodUsage(
                        pod=pod_name,
                        service=service_name,
                    )

                # Calculate average CPU
                total_cpu = 0.0
                count = 0
                for _, value in series.get("values", []):
                    try:
                        total_cpu += float(value)
                        count += 1
                    except (ValueError, TypeError):
                        continue

                if count > 0:
                    avg_cpu = total_cpu / count
                    pods[pod_name].cpu_core_seconds = avg_cpu * duration_seconds

        # Process memory results
        if memory_result and "result" in memory_result:
            for series in memory_result["result"]:
                pod_name = series.get("metric", {}).get("pod", "unknown")
                service_name = series.get("metric", {}).get(
                    "label_lazycloud_io_service", "unknown"
                )

                if pod_name not in pods:
                    pods[pod_name] = PodUsage(
                        pod=pod_name,
                        service=service_name,
                    )

                # Calculate average memory
                total_bytes = 0.0
                count = 0
                for _, value in series.get("values", []):
                    try:
                        total_bytes += float(value)
                        count += 1
                    except (ValueError, TypeError):
                        continue

                if count > 0:
                    avg_bytes = total_bytes / count
                    avg_gb = avg_bytes / (1024**3)
                    pods[pod_name].memory_gb_seconds = avg_gb * duration_seconds

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
