from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger


async def safe_send_json(websocket: WebSocket, data: dict) -> bool:
    """Safely send JSON data through WebSocket, handling closed connections"""
    try:
        await websocket.send_json(data)
        return True
    except (WebSocketDisconnect, RuntimeError) as e:
        # Connection is closed or closing
        if "Cannot call" in str(e) or "close" in str(e).lower():
            logger.debug("WebSocket already closed, cannot send data")
        else:
            logger.debug(f"WebSocket disconnected: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending WebSocket data: {e}")
        return False


async def safe_receive_json(websocket: WebSocket) -> dict | None:
    """Safely receive JSON data from WebSocket, handling closed connections"""
    try:
        return await websocket.receive_json()
    except WebSocketDisconnect:
        logger.debug("WebSocket disconnected while receiving")
        return None
    except RuntimeError as e:
        # Connection is closed or not accepted
        if "not connected" in str(e).lower() or "accept" in str(e).lower():
            logger.debug("WebSocket not connected, cannot receive data")
        else:
            logger.debug(f"WebSocket runtime error while receiving: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error receiving WebSocket data: {e}")
        return None
