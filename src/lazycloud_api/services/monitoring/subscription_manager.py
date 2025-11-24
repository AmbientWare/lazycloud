import asyncio
import hashlib
from typing import Any, Callable
from uuid import uuid4

from loguru import logger

from lazycloud_api.services.monitoring.base import BaseMonitor
from lazycloud_api.services.monitoring.deployment_monitor import DeploymentMonitor
from lazycloud_api.services.monitoring.log_monitor import LogMonitor
from lazycloud_api.services.monitoring.monitor_config import (
    DeploymentMonitorConfig,
    LogMonitorConfig,
    MonitorConfig,
    ServiceMonitorConfig,
    TaskMonitorConfig,
)
from lazycloud_api.services.monitoring.service_monitor import ServiceMonitor
from lazycloud_api.services.monitoring.task_monitor import TaskMonitor
from shared.models.monitoring import MonitorStats, SubscriptionManagerStats


class SubscriptionManager:
    """
    Manages shared monitor instances and subscriptions.

    Ensures only one monitor exists per unique resource, with multiple
    clients subscribing to the same monitor instance.
    """

    def __init__(self):
        """Initialize the subscription manager."""
        self._monitors: dict[str, BaseMonitor] = {}
        self._subscriptions: dict[
            str, dict[str, Callable]
        ] = {}  # monitor_key -> {sub_id -> callback}
        self._lock = asyncio.Lock()  # Global lock for monitor creation/removal
        self._monitor_locks: dict[
            str, asyncio.Lock
        ] = {}  # Per-monitor locks for operations

    def _generate_monitor_key(self, config: MonitorConfig) -> str:
        """Generate a unique key for a monitor based on its configuration"""
        key_string = config.get_key()
        # Use hash for cleaner keys
        return hashlib.md5(key_string.encode()).hexdigest()

    def is_monitor_running(self, monitor_key: str) -> bool:
        """Check if a monitor is still running"""
        monitor = self._monitors.get(monitor_key)
        return monitor._running if monitor else False

    async def subscribe(
        self,
        config: MonitorConfig,
        callback: Callable[[Any], None],
    ) -> tuple[str, str]:
        """Subscribe to a monitor, creating it if necessary"""
        monitor_key = self._generate_monitor_key(config)
        subscription_id = str(uuid4())

        # Phase 1: Check/create monitor using global lock
        async with self._lock:
            # Get or create monitor
            if monitor_key not in self._monitors:
                monitor = self._create_monitor(config)
                self._monitors[monitor_key] = monitor
                self._subscriptions[monitor_key] = {}
                self._monitor_locks[monitor_key] = asyncio.Lock()

                # Start the monitor
                await monitor.start()
                logger.info(
                    f"Created and started new {config.get_monitor_type()} monitor: {monitor_key}"
                )
            else:
                monitor = self._monitors[monitor_key]
                logger.debug(f"Reusing existing monitor: {monitor_key}")

        # Phase 2: Add callback and subscription using per-monitor lock
        # This allows concurrent subscriptions to different monitors
        async with self._monitor_locks[monitor_key]:
            # Add callback to monitor
            await monitor.add_callback(callback)

            # Track subscription
            self._subscriptions[monitor_key][subscription_id] = callback

            logger.info(
                f"New subscription {subscription_id} to {config.get_monitor_type()} monitor {monitor_key}. "
                f"Total subscribers: {len(self._subscriptions[monitor_key])}"
            )

        return monitor_key, subscription_id

    def _create_monitor(self, config: MonitorConfig) -> BaseMonitor:
        """Create a new monitor instance based on configuration."""
        if isinstance(config, DeploymentMonitorConfig):
            return DeploymentMonitor(
                deployment_id=config.deployment_id,
                deployment_name=config.deployment_name,
                namespace=config.namespace,
                helm_values=config.helm_values,
                deployed_at=config.deployed_at,
                callback=None,  # Callbacks will be added via add_callback
            )
        elif isinstance(config, ServiceMonitorConfig):
            return ServiceMonitor(
                deployment_id=config.deployment_id,
                deployment_name=config.deployment_name,
                service_name=config.service_name,
                namespace=config.namespace,
                helm_values=config.helm_values,
                callback=None,  # Callbacks will be added via add_callback
            )
        elif isinstance(config, TaskMonitorConfig):
            return TaskMonitor(
                task_id=config.task_id,
                callback=None,  # Callbacks will be added via add_callback
            )
        elif isinstance(config, LogMonitorConfig):
            return LogMonitor(
                deployment_id=config.deployment_id,
                namespace=config.namespace,
                service_name=config.service_name,
                pod_name=config.pod_name,
                tail_lines=config.tail_lines,
                callback=None,  # Callbacks will be added via add_callback
            )
        else:
            raise TypeError(f"Unsupported monitor config type: {type(config).__name__}")

    async def unsubscribe(self, monitor_key: str, subscription_id: str) -> None:
        """Unsubscribe from a monitor"""
        # Check if monitor exists (quick read check)
        if monitor_key not in self._subscriptions:
            logger.warning(
                f"Attempted to unsubscribe from non-existent monitor: {monitor_key}"
            )
            return

        # Use per-monitor lock for callback/subscription removal
        async with self._monitor_locks[monitor_key]:
            if subscription_id not in self._subscriptions[monitor_key]:
                logger.warning(
                    f"Attempted to remove non-existent subscription: {subscription_id}"
                )
                return

            # Remove callback from monitor
            callback = self._subscriptions[monitor_key][subscription_id]
            monitor = self._monitors.get(monitor_key)
            if monitor:
                await monitor.remove_callback(callback)

            # Remove subscription tracking
            del self._subscriptions[monitor_key][subscription_id]

            remaining = len(self._subscriptions[monitor_key])
            logger.info(
                f"Removed subscription {subscription_id} from monitor {monitor_key}. "
                f"Remaining subscribers: {remaining}"
            )

            # Cleanup immediately if no more subscribers (use global lock)
            monitor_to_stop = None
            if remaining == 0:
                async with self._lock:
                    # Double-check still no subscribers after acquiring lock
                    if len(self._subscriptions[monitor_key]) == 0:
                        monitor_to_stop = self._monitors.pop(monitor_key, None)
                        self._subscriptions.pop(monitor_key, None)
                        self._monitor_locks.pop(monitor_key, None)

            # Stop monitor outside the lock to avoid blocking other operations
            if monitor_to_stop:
                asyncio.create_task(
                    self._stop_monitor_background(monitor_to_stop, monitor_key)
                )

    async def _stop_monitor_background(
        self, monitor: BaseMonitor, monitor_key: str
    ) -> None:
        """Stop a monitor in the background without blocking."""
        try:
            await asyncio.wait_for(monitor.stop(), timeout=2.0)
            logger.info(f"Stopped and removed monitor: {monitor_key}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout stopping monitor {monitor_key}, forcing stop")
        except Exception as e:
            logger.error(
                f"Error stopping monitor {monitor_key}: {e}",
                exc_info=True,
            )

    async def shutdown(self) -> None:
        """Stop all monitors and clean up resources."""
        logger.info("Shutting down SubscriptionManager")

        # Stop all monitors
        for monitor_key, monitor in self._monitors.items():
            try:
                await monitor.stop()
                logger.info(f"Stopped monitor: {monitor_key}")

            except Exception as e:
                logger.error(f"Error stopping monitor {monitor_key}: {e}")

        self._monitors.clear()
        self._subscriptions.clear()
        self._monitor_locks.clear()
        logger.info("SubscriptionManager shutdown complete")

    def get_stats(self) -> SubscriptionManagerStats:
        """Get statistics about active monitors and subscriptions."""
        return SubscriptionManagerStats(
            active_monitors=len(self._monitors),
            total_subscriptions=sum(len(subs) for subs in self._subscriptions.values()),
            pending_cleanups=0,
            monitors={
                key: MonitorStats(
                    subscribers=len(self._subscriptions.get(key, {})),
                    running=monitor._running,
                )
                for key, monitor in self._monitors.items()
            },
        )


# Global singleton instance
_subscription_manager: SubscriptionManager | None = None


def get_subscription_manager() -> SubscriptionManager:
    """Get the global SubscriptionManager instance."""
    global _subscription_manager
    if _subscription_manager is None:
        raise RuntimeError(
            "SubscriptionManager not initialized. Call initialize_subscription_manager() first."
        )

    return _subscription_manager


def initialize_subscription_manager() -> SubscriptionManager:
    """Initialize the global SubscriptionManager instance."""
    global _subscription_manager
    if _subscription_manager is not None:
        logger.warning("SubscriptionManager already initialized")
        return _subscription_manager

    _subscription_manager = SubscriptionManager()
    logger.info("SubscriptionManager initialized")
    return _subscription_manager


async def shutdown_subscription_manager() -> None:
    """Shutdown the global SubscriptionManager instance."""
    global _subscription_manager
    if _subscription_manager is not None:
        await _subscription_manager.shutdown()
        _subscription_manager = None
