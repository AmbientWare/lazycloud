from datetime import UTC, datetime
from typing import Dict, Set

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger

from lazycloud_api.api.security import get_current_active_user_ws
from lazycloud_api.api.utils import safe_receive_json, safe_send_json
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.services.monitoring import (
    DeploymentMonitor,
    LogMonitor,
    ServiceMonitor,
)
from shared.models.statuses import DeploymentStatus, ServiceStatus

ws_router = APIRouter(prefix="/ws", tags=["websocket"])

# NOTE: Store active connections per key
connections: Dict[str, Set[WebSocket]] = {}
# NOTE: Store active watchers/monitors per key
watchers: Dict[str, DeploymentMonitor | ServiceMonitor | LogMonitor] = {}


async def _validate_and_track_ws(
    websocket: WebSocket, deployment_id: str, connections_key: str
) -> tuple[bool, ComposeDeploymentPydantic | None]:
    """Validate WebSocket connection and add to tracking."""
    # Authenticate the websocket connection
    current_user = await get_current_active_user_ws(websocket)
    if not current_user:
        await websocket.close(code=1008, reason="Authentication failed")
        return False, None

    # Verify deployment ownership
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment or deployment.user_id != current_user.user_id:
        await websocket.close(code=1008, reason="Deployment not found")
        return False, None

    # Add connection to tracking
    if connections_key not in connections:
        connections[connections_key] = set()
    connections[connections_key].add(websocket)

    return True, deployment


async def _send_status_update(connections_key: str, status_data: DeploymentStatus):
    """Send status update to all connected clients for a deployment"""
    if connections_key in connections:
        disconnected = set()
        for websocket in connections[connections_key]:
            success = await safe_send_json(
                websocket,
                {
                    "type": "status",
                    "data": status_data.model_dump(mode="json"),
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
            if not success:
                disconnected.add(websocket)

        connections[connections_key] -= disconnected


async def _send_log_update(
    connections_key: str, log_line: str, service_name: str
) -> bool:
    """Send log update to all connected clients for a service's logs"""
    if connections_key in connections:
        disconnected = set()
        any_success = False
        for websocket in connections[connections_key]:
            success = await safe_send_json(
                websocket,
                {
                    "type": "log",
                    "data": {
                        "service": service_name,
                        "line": log_line,
                    },
                },
            )
            if success:
                any_success = True
            else:
                disconnected.add(websocket)

        connections[connections_key] -= disconnected
        return any_success
    return False


async def cleanup_watcher(connections_key: str, websocket: WebSocket):
    # Remove connection
    if connections_key in connections:
        connections[connections_key].discard(websocket)

        # Stop watcher if no more connections
        if not connections[connections_key]:
            connections.pop(connections_key, None)
            if connections_key in watchers:
                await watchers[connections_key].stop()
                watchers.pop(connections_key, None)


@ws_router.websocket("/deployments/{deployment_id}/status")
async def websocket_deployment_status(
    websocket: WebSocket,
    deployment_id: str,
):
    """WebSocket endpoint for real-time deployment status updates"""
    await websocket.accept()

    connections_key = f"{deployment_id}"

    try:
        is_valid, deployment = await _validate_and_track_ws(
            websocket, deployment_id, connections_key
        )
        if not is_valid:
            return

        if connections_key not in watchers:

            async def status_callback(status: DeploymentStatus):
                await _send_status_update(connections_key, status)

            watcher = DeploymentMonitor(
                deployment_id=deployment_id,
                namespace=deployment.namespace,
                helm_values=deployment.helm_values,
                callback=status_callback,
            )
            watchers[connections_key] = watcher
            await watcher.start()

        logger.info(f"Deployment status websocket connected for {deployment_id}")

        # Keep connection alive and handle messages
        while True:
            message = await safe_receive_json(websocket)
            if message is None:
                # Connection closed
                break

            if message.get("type") == "ping":
                await safe_send_json(websocket, {"type": "pong"})

    finally:
        # Remove connection
        await cleanup_watcher(connections_key, websocket)

        logger.info(f"Deployment status websocket disconnected for {deployment_id}")


@ws_router.websocket("/services/{deployment_id}/{service_name}/status")
async def websocket_service_status(
    websocket: WebSocket,
    deployment_id: str,
    service_name: str,
):
    """WebSocket endpoint for real-time service status updates"""
    await websocket.accept()

    connections_key = f"{deployment_id}/{service_name}"

    try:
        is_valid, deployment = await _validate_and_track_ws(
            websocket, deployment_id, connections_key
        )
        if not is_valid:
            return

        if connections_key not in watchers:

            async def status_callback(status: ServiceStatus):
                await _send_status_update(connections_key, status)

            watcher = ServiceMonitor(
                deployment_id=deployment_id,
                service_name=service_name,
                namespace=deployment.namespace,
                helm_values=deployment.helm_values,
                callback=status_callback,
            )
            watchers[connections_key] = watcher
            await watcher.start()

        logger.info(
            f"Service status websocket connected for {deployment_id}/{service_name}"
        )

        # Keep connection alive and handle messages
        while True:
            message = await safe_receive_json(websocket)
            if message is None:
                # Connection closed
                break

            if message.get("type") == "ping":
                await safe_send_json(websocket, {"type": "pong"})

    finally:  # Remove connection
        await cleanup_watcher(connections_key, websocket)

        logger.info(
            f"Service status websocket disconnected for {deployment_id}/{service_name}"
        )


@ws_router.websocket("/deployments/{deployment_id}/logs/{service_name}/{pod_name}")
async def websocket_service_logs(
    websocket: WebSocket,
    deployment_id: str,
    service_name: str,
    pod_name: str,
    tail: int = Query(100),
):
    """WebSocket endpoint for streaming service logs"""
    await websocket.accept()

    connections_key = f"{deployment_id}/{service_name}/{pod_name}"

    try:
        is_valid, deployment = await _validate_and_track_ws(
            websocket, deployment_id, connections_key
        )
        if not is_valid:
            return

        # Create or reuse log monitor
        if connections_key not in watchers:
            # Create async callback for log lines
            async def log_callback(log_line: str):
                success = await _send_log_update(
                    connections_key, log_line, service_name
                )
                if not success:
                    # Stop monitoring if we can't send to any client
                    raise WebSocketDisconnect()

            log_monitor = LogMonitor(
                deployment_id=deployment_id,
                namespace=deployment.namespace,
                service_name=service_name,
                pod_name=pod_name,
                tail_lines=tail,
                callback=log_callback,
            )
            watchers[connections_key] = log_monitor
            await log_monitor.start()

        logger.info(
            f"Service logs websocket connected for {deployment_id}/{service_name}/{pod_name}"
        )

        while True:
            message = await safe_receive_json(websocket)
            if message is None:
                # Connection closed
                break

            if message.get("type") == "ping":
                await safe_send_json(websocket, {"type": "pong"})
            elif message.get("type") == "stop":
                break

    finally:
        # Remove connection
        await cleanup_watcher(connections_key, websocket)

        logger.info(
            f"Service logs websocket disconnected for {deployment_id}/{service_name}/{pod_name}"
        )
