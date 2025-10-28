from asyncio import Semaphore
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from shared.models.billing import STORAGE_CLASS_EFS, STORAGE_CLASS_S3
from shared.models.metrics import (
    NamespaceBreakdown,
    NamespaceSummary,
    PodMetrics,
    PodUsage,
    StorageUsage,
    UsagePeriod,
    UsageTotals,
)

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

    async def get_storage_usage(
        self, namespace: str, start_time: datetime, end_time: datetime
    ) -> float:
        """Get storage usage in GB-hours for namespace PVCs"""
        duration_hours = (end_time - start_time).total_seconds() / 3600

        # Query: sum of actual PVC usage (requires CSI drivers)
        query = f'''
            sum(
                kubelet_volume_stats_used_bytes{{
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

    def _process_storage_result(
        self, result: dict[str, Any], duration_hours: float
    ) -> float:
        """Helper to process storage query result and return GB-hours."""
        if not result or "result" not in result or not result["result"]:
            return 0.0

        total_bytes = 0.0
        count = 0
        for series in result["result"]:
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
        return avg_gb * duration_hours

    async def get_storage_usage_by_class(
        self, namespace: str, start_time: datetime, end_time: datetime
    ) -> dict[str, float]:
        """Get storage usage split by storage class (s3-sc vs efs-sc) in GB-hours"""
        duration_hours = (end_time - start_time).total_seconds() / 3600

        # Query for S3 storage (actual usage, requires CSI drivers)
        s3_query = f'''
            sum(
                kubelet_volume_stats_used_bytes{{
                    namespace="{namespace}"
                }}
                * on(persistentvolumeclaim, namespace) group_left(storageclass)
                kube_persistentvolumeclaim_info{{
                    storageclass="{STORAGE_CLASS_S3}",
                    namespace="{namespace}"
                }}
            )
        '''

        # Query for EFS storage (actual usage, requires CSI drivers)
        efs_query = f'''
            sum(
                kubelet_volume_stats_used_bytes{{
                    namespace="{namespace}"
                }}
                * on(persistentvolumeclaim, namespace) group_left(storageclass)
                kube_persistentvolumeclaim_info{{
                    storageclass="{STORAGE_CLASS_EFS}",
                    namespace="{namespace}"
                }}
            )
        '''

        # Execute both queries concurrently
        s3_result = await self._query_range(s3_query, start_time, end_time, step="5m")
        efs_result = await self._query_range(efs_query, start_time, end_time, step="5m")

        # Process results using helper
        s3_gb_hours = self._process_storage_result(s3_result, duration_hours)
        efs_gb_hours = self._process_storage_result(efs_result, duration_hours)

        logger.debug(
            f"Storage usage for {namespace}: S3={s3_gb_hours:.2f} GB-hours, EFS={efs_gb_hours:.2f} GB-hours"
        )
        return {"s3": s3_gb_hours, "efs": efs_gb_hours}

    async def get_storage_usage_by_pvc(
        self, namespace: str, start_time: datetime, end_time: datetime
    ) -> list[StorageUsage]:
        """Get storage usage per PVC with storage class

        TODO: This requires CSI drivers that expose kubelet_volume_stats_used_bytes.
        Test with EKS and proper EFS/EBS CSI drivers for accurate usage tracking.
        Without CSI drivers (e.g., Minikube hostpath), this will return empty results.
        """
        duration_hours = (end_time - start_time).total_seconds() / 3600

        # Query actual disk usage (requires CSI drivers in production)
        query = f'''
            kubelet_volume_stats_used_bytes{{
                namespace="{namespace}"
            }}
            * on(persistentvolumeclaim, namespace) group_left(storageclass)
            kube_persistentvolumeclaim_info{{
                namespace="{namespace}"
            }}
        '''

        result = await self._query_range(query, start_time, end_time, step="5m")

        # Process results grouped by PVC and storage class
        pvc_usage: dict[tuple[str, str], list[float]] = {}

        if result and result.get("result"):
            for series in result["result"]:
                pvc_name = series["metric"].get("persistentvolumeclaim")
                storage_class = series["metric"].get("storageclass")

                if not pvc_name or not storage_class:
                    continue

                key = (pvc_name, storage_class)
                if key not in pvc_usage:
                    pvc_usage[key] = []

                # Collect all used bytes values
                for value in series["values"]:
                    try:
                        used_bytes = float(value[1])
                        pvc_usage[key].append(used_bytes)
                    except (ValueError, IndexError):
                        continue
        else:
            logger.warning(
                f"kubelet_volume_stats_used_bytes not available for {namespace}. "
                f"This metric requires CSI drivers (e.g., AWS EFS/EBS CSI drivers in EKS). "
                f"Storage usage data will be unavailable."
            )

        # Calculate gb_hours for each PVC
        storage_list = []
        for (pvc_name, storage_class), used_values in pvc_usage.items():
            if used_values:
                avg_bytes = sum(used_values) / len(used_values)
                avg_gb = avg_bytes / (1024**3)
                gb_hours = avg_gb * duration_hours

                storage_list.append(
                    StorageUsage(
                        pvc_name=pvc_name,
                        storage_class=storage_class,
                        gb_hours=gb_hours,
                    )
                )

        logger.debug(
            f"Storage breakdown for {namespace}: {len(storage_list)} PVCs tracked"
        )
        return storage_list

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
        duration_seconds = (end_time - start_time).total_seconds()

        # Get overall totals
        cpu_total = await self.get_cpu_usage(namespace, start_time, end_time)
        memory_total = await self.get_memory_usage(namespace, start_time, end_time)

        # Get breakdown by pod
        by_pod = await self._get_usage_by_pod(
            namespace, start_time, end_time, duration_seconds
        )

        # Get breakdown by PVC
        by_pvc = await self.get_storage_usage_by_pvc(namespace, start_time, end_time)

        # Calculate storage totals from PVC breakdown (eliminates 2 redundant queries)
        s3_total = sum(
            pvc.gb_hours for pvc in by_pvc if pvc.storage_class == STORAGE_CLASS_S3
        )
        efs_total = sum(
            pvc.gb_hours for pvc in by_pvc if pvc.storage_class == STORAGE_CLASS_EFS
        )
        storage_total = s3_total + efs_total

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
                s3_gb_hours=s3_total,
                efs_gb_hours=efs_total,
            ),
            by_pod=by_pod,
            by_pvc=by_pvc,
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
