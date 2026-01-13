import asyncio
from typing import AsyncGenerator

from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger
from models.pod_states import PodFailureReasons

from backend.services.k8s.client import get_async_core_v1_api

# Constants for pod readiness checks
POD_READINESS_MAX_RETRIES = 3
POD_READINESS_RETRY_DELAY = 1
POD_STATUS_TIMEOUT = 2.0
POD_LIST_TIMEOUT = 2.0
INITIAL_LOG_READ_TIMEOUT = 10.0
STREAM_CHUNK_SIZE = 4096
MESSAGE_TRUNCATE_LENGTH = 100


class LogStreamer:
    """Streams logs from Kubernetes pods using async kubernetes client."""

    def __init__(
        self,
        deployment_id: str,
        namespace: str,
        service_name: str,
        follow: bool = True,
        tail_lines: int = 100,
        pod_name: str | None = None,
    ):
        """Initialize the log streamer."""
        self.deployment_id = deployment_id
        self.namespace = namespace
        self.service_name = service_name
        self.follow = follow
        self.tail_lines = tail_lines
        self.pod_name = pod_name
        self._running = False

    async def _check_pod_exists(self) -> tuple[bool, str | None]:
        """Check if pod exists and return its status."""
        if not self.pod_name:
            return True, None

        try:
            core_v1 = await get_async_core_v1_api()

            pod = await asyncio.wait_for(
                core_v1.read_namespaced_pod(
                    name=self.pod_name,
                    namespace=self.namespace,
                ),
                timeout=POD_STATUS_TIMEOUT,
            )

            if not pod.status:
                return True, "Instance status not available"

            phase = pod.status.phase

            container_statuses = pod.status.container_statuses or []
            for container_status in container_statuses:
                if not container_status.state:
                    continue

                if container_status.state.waiting:
                    waiting = container_status.state.waiting
                    reason = waiting.reason or ""
                    message = waiting.message or ""

                    if reason in PodFailureReasons.IMAGE_ERRORS:
                        error_msg = f"{reason}"
                        if message:
                            short_msg = (
                                message[:MESSAGE_TRUNCATE_LENGTH] + "..."
                                if len(message) > MESSAGE_TRUNCATE_LENGTH
                                else message
                            )
                            error_msg += f": {short_msg}"
                        return True, error_msg

                    if reason:
                        return True, f"Container waiting: {reason}"

                elif container_status.state.terminated:
                    terminated = container_status.state.terminated
                    if terminated.exit_code and terminated.exit_code != 0:
                        reason = terminated.reason or "ContainerError"
                        message = terminated.message or ""
                        error_msg = f"Container {reason}"
                        if message:
                            short_msg = (
                                message[:MESSAGE_TRUNCATE_LENGTH] + "..."
                                if len(message) > MESSAGE_TRUNCATE_LENGTH
                                else message
                            )
                            error_msg += f": {short_msg}"
                        return True, error_msg

            if phase in ["Running", "Succeeded", "Failed"]:
                return True, None

            elif phase == "Pending":
                if pod.status.reason:
                    return True, f"Instance pending: {pod.status.reason}"
                return True, "Instance is still starting up"

            else:
                reason_msg = f"Instance is in {phase} state"
                if pod.status.reason:
                    reason_msg += f": {pod.status.reason}"
                return True, reason_msg

        except asyncio.CancelledError:
            raise

        except asyncio.TimeoutError:
            logger.warning(f"Timeout checking pod status for {self.pod_name}")
            return True, "Timeout checking pod status"

        except ApiException as e:
            if e.status == 404:
                return False, "Instance not found"
            logger.error(f"Error checking pod status: {e}")
            return True, f"Error checking pod status: {e.reason or str(e)}"

        except Exception as e:
            logger.error(f"Error checking pod status: {e}")
            return True, f"Error checking pod status: {str(e)}"

    async def stream(self) -> AsyncGenerator[str, None]:
        """Stream logs from a pod or service."""
        self._running = True

        if self.pod_name:
            # Quick check for pod readiness
            for attempt in range(POD_READINESS_MAX_RETRIES):
                exists, reason = await self._check_pod_exists()

                if not exists:
                    yield f"INFO: Waiting for pod to be created... (attempt {attempt + 1}/{POD_READINESS_MAX_RETRIES})"
                    if attempt < POD_READINESS_MAX_RETRIES - 1:
                        await asyncio.sleep(POD_READINESS_RETRY_DELAY)
                        continue

                    else:
                        yield "ERROR: Pod not found after waiting. It may have been deleted or failed to start."
                        self._running = False
                        return

                elif reason:
                    # Fail fast on unrecoverable image pull errors
                    if any(err in reason for err in PodFailureReasons.IMAGE_ERRORS):
                        yield f"ERROR: {reason}"
                        yield "Logs are not available because the container cannot start."
                        yield "Please check the pod status and resolve the issue before viewing logs."
                        self._running = False
                        return

                    else:
                        yield f"INFO: {reason}, waiting... (attempt {attempt + 1}/{POD_READINESS_MAX_RETRIES})"
                        if attempt < POD_READINESS_MAX_RETRIES - 1:
                            await asyncio.sleep(POD_READINESS_RETRY_DELAY)
                            continue

                        else:
                            break

                else:
                    break

        try:
            core_v1 = await get_async_core_v1_api()

            pod_info = (
                f"pod {self.pod_name}"
                if self.pod_name
                else f"service {self.service_name}"
            )
            logger.info(f"Started log streaming for {pod_info} in {self.namespace}")

            target_pod_name = self.pod_name
            if not target_pod_name:
                # Resolve pod name from service label selector
                try:
                    pods = await asyncio.wait_for(
                        core_v1.list_namespaced_pod(
                            namespace=self.namespace,
                            label_selector=f"app.kubernetes.io/name={self.service_name}",
                        ),
                        timeout=POD_LIST_TIMEOUT,
                    )
                    if pods.items:
                        target_pod_name = pods.items[0].metadata.name

                    else:
                        yield "ERROR: No pods found for service"
                        self._running = False
                        return

                except asyncio.TimeoutError:
                    yield "ERROR: Timeout finding pod for service"
                    self._running = False
                    return

            # Stream initial logs (tail_lines)
            if self.tail_lines > 0:
                try:
                    initial_logs = await asyncio.wait_for(
                        core_v1.read_namespaced_pod_log(
                            name=target_pod_name,
                            namespace=self.namespace,
                            tail_lines=self.tail_lines,
                            follow=False,
                            _preload_content=True,
                        ),
                        timeout=INITIAL_LOG_READ_TIMEOUT,
                    )

                    # Yield initial logs line by line
                    if initial_logs:
                        for line in initial_logs.splitlines():
                            if not self._running:
                                return
                            yield line

                except asyncio.CancelledError:
                    raise

                except asyncio.TimeoutError:
                    logger.warning(
                        f"Timeout reading initial logs for {target_pod_name}"
                    )
                    yield "WARNING: Timeout reading initial logs"

                except ApiException as e:
                    if e.status == 400:
                        api_error_msg = e.reason or str(e)
                        if (
                            "container" in api_error_msg.lower()
                            and "not running" in api_error_msg.lower()
                        ):
                            yield "INFO: Container is not running yet. Logs will be available once the container starts."
                        else:
                            yield f"ERROR: {api_error_msg}"

                    else:
                        logger.error(f"Error reading initial logs: {e}")
                        yield f"ERROR: Failed to read initial logs: {e.reason or str(e)}"

                except Exception as e:
                    logger.error(f"Error reading initial logs: {e}")
                    yield f"ERROR: Failed to read initial logs: {str(e)}"

            # Stream follow logs if requested
            if self.follow and self._running:
                resp = None
                try:
                    # Use follow=True with streaming
                    resp = await core_v1.read_namespaced_pod_log(
                        name=target_pod_name,
                        namespace=self.namespace,
                        follow=True,
                        tail_lines=0,  # We already read initial logs
                        _preload_content=False,
                    )

                    # Stream logs line by line
                    buffer = ""
                    async for chunk in resp.content.iter_chunked(STREAM_CHUNK_SIZE):
                        if not self._running:
                            break

                        try:
                            buffer += chunk.decode("utf-8", errors="replace")

                            # Process complete lines
                            while "\n" in buffer:
                                line, buffer = buffer.split("\n", 1)
                                yield line

                        except Exception as decode_err:
                            logger.debug(f"Error decoding log chunk: {decode_err}")
                            continue

                    # Yield any remaining buffer
                    if buffer and self._running:
                        yield buffer

                except asyncio.CancelledError:
                    logger.info(f"Log streaming cancelled for {target_pod_name}")
                    raise
                except ApiException as e:
                    if e.status == 404:
                        yield "INFO: Pod was deleted or is no longer available"

                    elif e.status == 400:
                        api_error_msg = e.reason or str(e)
                        if (
                            "container" in api_error_msg.lower()
                            and "not running" in api_error_msg.lower()
                        ):
                            yield "INFO: Container is not running. Logs streaming stopped."
                        else:
                            yield f"ERROR: {api_error_msg}"
                    else:
                        logger.error(f"Kubernetes API error in follow stream: {e}")
                        yield f"ERROR: {e.reason or str(e)}"

                except Exception as e:
                    logger.error(f"Error in follow stream: {e}")
                    yield f"ERROR: Failed to follow logs: {str(e)}"
                finally:
                    # Ensure streaming response is closed
                    if resp is not None:
                        try:
                            await resp.release()
                        except Exception as e:
                            logger.debug(f"Error releasing streaming response: {e}")

        except asyncio.CancelledError:
            logger.info(f"Log streaming cancelled for {self.service_name}")
            raise

        except Exception as e:
            logger.error(f"Error streaming logs: {e}")
            if self._running:
                yield f"ERROR: Failed to stream logs: {str(e)}"

        finally:
            self._running = False

    async def stop(self):
        """Stop streaming logs."""
        self._running = False
        pod_info = (
            f"pod {self.pod_name}" if self.pod_name else f"service {self.service_name}"
        )
        logger.info(f"Stopped log streaming for {pod_info}")
