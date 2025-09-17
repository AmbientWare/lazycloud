"""
WebSocket endpoints for real-time updates.
"""

import traceback
from datetime import UTC, datetime
from typing import Dict, Set

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger

from lazycloud_api.api.security import get_current_active_user_ws
from lazycloud_api.database import db
from lazycloud_api.services.k8s.log_streamer import K8sLogStreamer
from lazycloud_api.services.k8s.status_watcher import K8sStatusWatcher

ws_router = APIRouter(prefix="/ws", tags=["websocket"])

# Store active connections per deployment
connections: Dict[str, Set[WebSocket]] = {}
# Store active watchers per deployment
watchers: Dict[str, K8sStatusWatcher] = {}


async def send_status_update(deployment_id: str, status_data: dict):
    """Send status update to all connected clients for a deployment."""
    if deployment_id in connections:
        disconnected = set()
        for websocket in connections[deployment_id]:
            try:
                await websocket.send_json(
                    {
                        "type": "status_update",
                        "data": status_data,
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to send to websocket: {e}")
                disconnected.add(websocket)

        # Remove disconnected websockets
        connections[deployment_id] -= disconnected


@ws_router.websocket("/deployments/{deployment_id}/status")
async def websocket_deployment_status(
    websocket: WebSocket,
    deployment_id: str,
):
    """WebSocket endpoint for real-time deployment status updates."""
    await websocket.accept()

    try:
        # Authenticate the websocket connection
        current_user = await get_current_active_user_ws(websocket)
        if not current_user:
            await websocket.close(code=1008, reason="Authentication failed")
            return

        # Verify deployment ownership
        deployment = await db.compose_deployments.aget_by_id(deployment_id)
        if not deployment or deployment.user_id != current_user.user_id:
            await websocket.close(code=1008, reason="Deployment not found")
            return

        # Add connection to the set
        if deployment_id not in connections:
            connections[deployment_id] = set()
        connections[deployment_id].add(websocket)

        # Start or get existing watcher
        if deployment_id not in watchers:

            async def status_callback(status: dict):
                await send_status_update(deployment_id, status)

            watcher = K8sStatusWatcher(
                deployment_id=deployment_id,
                namespace=deployment.namespace,
                helm_values=deployment.helm_values,
                callback=status_callback,
            )
            watchers[deployment_id] = watcher
            await watcher.start()

        logger.info(f"WebSocket connected for deployment {deployment_id}")

        # Keep connection alive and handle messages
        while True:
            try:
                # Wait for messages from client (ping/pong or commands)
                message = await websocket.receive_json()

                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                elif message.get("type") == "get_status":
                    # Send immediate status update
                    if deployment_id in watchers:
                        # Check if service_name is specified
                        service_name = message.get("service")
                        if service_name:
                            # Get service-specific status
                            status = await watchers[deployment_id].get_service_status(
                                service_name
                            )
                            if status:
                                await websocket.send_json(
                                    {
                                        "type": "status_update",
                                        "data": status,
                                        "timestamp": datetime.now(UTC).isoformat(),
                                    }
                                )
                            else:
                                await websocket.send_json(
                                    {
                                        "type": "error",
                                        "error": f"Service '{service_name}' not found",
                                        "timestamp": datetime.now(UTC).isoformat(),
                                    }
                                )
                        else:
                            # Get full deployment status
                            status = await watchers[deployment_id].get_current_status()
                            await websocket.send_json(
                                {
                                    "type": "status_update",
                                    "data": status,
                                    "timestamp": datetime.now(UTC).isoformat(),
                                }
                            )

            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"WebSocket error: {e}\n{traceback.format_exc()}")
                break

    finally:
        # Remove connection
        if deployment_id in connections:
            connections[deployment_id].discard(websocket)

            # Stop watcher if no more connections
            if not connections[deployment_id]:
                del connections[deployment_id]
                if deployment_id in watchers:
                    await watchers[deployment_id].stop()
                    del watchers[deployment_id]

        logger.info(f"WebSocket disconnected for deployment {deployment_id}")


@ws_router.websocket("/deployments/{deployment_id}/logs/{service_name}")
async def websocket_service_logs(
    websocket: WebSocket,
    deployment_id: str,
    service_name: str,
    tail: int = Query(100),
):
    """WebSocket endpoint for streaming service logs."""
    await websocket.accept()
    log_streamer = None

    try:
        # Authenticate the websocket connection
        current_user = await get_current_active_user_ws(websocket)
        if not current_user:
            await websocket.close(code=1008, reason="Authentication failed")
            return

        # Verify deployment ownership
        deployment = await db.compose_deployments.aget_by_id(deployment_id)
        if not deployment or deployment.user_id != current_user.user_id:
            await websocket.close(code=1008, reason="Deployment not found")
            return

        # Create async callback for log lines
        async def log_callback(log_line: str):
            await websocket.send_json(
                {
                    "type": "log",
                    "data": {
                        "service": service_name,
                        "line": log_line,
                    },
                }
            )

        # Create log streamer
        log_streamer = K8sLogStreamer(
            deployment_id=deployment_id,
            namespace=deployment.namespace,
            service_name=service_name,
            callback=log_callback,
            tail_lines=tail,
        )

        logger.info(f"WebSocket connected for logs: {deployment_id}/{service_name}")

        # Start streaming logs
        await log_streamer.start()

        # Keep connection alive and handle messages
        while True:
            try:
                # Wait for messages from client (ping/pong or commands)
                message = await websocket.receive_json()

                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

                elif message.get("type") == "stop":
                    break

            except WebSocketDisconnect:
                break

            except Exception as e:
                logger.error(f"WebSocket error: {e}\n{traceback.format_exc()}")
                break

    finally:
        # Stop log streaming
        if log_streamer:
            await log_streamer.stop()

        logger.info(f"WebSocket disconnected for logs: {deployment_id}/{service_name}")
