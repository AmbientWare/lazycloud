import asyncio
import queue
import time
from typing import AsyncGenerator

from kubernetes.client.exceptions import ApiException
from loguru import logger

from lazycloud_api.services.k8s.client import get_core_v1_api


class LogStreamer:
    """Streams logs from Kubernetes pods."""

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
        self._follow_response = None

    async def _check_pod_exists(self) -> tuple[bool, str | None]:
        if not self.pod_name:
            return True, None

        try:
            core_v1 = get_core_v1_api()

            def _get_pod_details():
                return core_v1.read_namespaced_pod(
                    name=self.pod_name,
                    namespace=self.namespace,
                    _request_timeout=5.0,
                )

            # Add timeout to prevent indefinite blocking
            pod = await asyncio.wait_for(
                asyncio.to_thread(_get_pod_details), timeout=5.0
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

                    if reason in [
                        "ImagePullBackOff",
                        "ErrImagePull",
                        "ErrImageNeverPull",
                        "InvalidImageName",
                    ]:
                        error_msg = f"{reason}"
                        if message:
                            short_msg = (
                                message[:100] + "..." if len(message) > 100 else message
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
                                message[:100] + "..." if len(message) > 100 else message
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
        if self._running:
            return

        self._running = True

        if self.pod_name:
            # Quick check for pod readiness (reduced retries for faster startup)
            max_retries = 3
            retry_delay = 1

            for attempt in range(max_retries):
                exists, reason = await self._check_pod_exists()

                if not exists:
                    yield f"INFO: Waiting for instance to be created... (attempt {attempt + 1}/{max_retries})"
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        yield "ERROR: Instance not found after waiting. It may have been deleted or failed to start."
                        self._running = False
                        return

                elif reason:
                    # Fail fast on unrecoverable image pull errors
                    if any(
                        err in reason
                        for err in [
                            "ImagePullBackOff",
                            "ErrImagePull",
                            "ErrImageNeverPull",
                            "InvalidImageName",
                        ]
                    ):
                        yield f"ERROR: {reason}"
                        yield "Logs are not available because the container cannot start."
                        yield "Please check the instance status and resolve the issue before viewing logs."
                        self._running = False
                        return
                    else:
                        yield f"INFO: {reason}, waiting... (attempt {attempt + 1}/{max_retries})"
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                            continue
                        else:
                            break
                else:
                    break

        try:
            core_v1 = get_core_v1_api()

            pod_info = (
                f"pod {self.pod_name}"
                if self.pod_name
                else f"service {self.service_name}"
            )
            logger.info(f"Started log streaming for {pod_info} in {self.namespace}")

            target_pod_name = self.pod_name
            if not target_pod_name:
                # Resolve pod name from service label selector
                def _find_pod():
                    pods = core_v1.list_namespaced_pod(
                        namespace=self.namespace,
                        label_selector=f"app.kubernetes.io/name={self.service_name}",
                        _request_timeout=5.0,
                    )
                    if pods.items:
                        return pods.items[0].metadata.name
                    return None

                # Add timeout to prevent indefinite blocking
                try:
                    target_pod_name = await asyncio.wait_for(
                        asyncio.to_thread(_find_pod), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    yield "ERROR: Timeout finding pod for service"
                    self._running = False
                    return

                if not target_pod_name:
                    yield "ERROR: No pods found for service"
                    self._running = False
                    return

            # Bounded queue prevents unbounded memory growth under high log volume
            log_queue: queue.Queue[str | None] = queue.Queue(maxsize=1000)
            MAX_BUFFER_SIZE = 1024 * 1024

            def _enqueue_line(line: str) -> None:
                """Enqueue a log line, dropping oldest if queue is full."""
                try:
                    log_queue.put_nowait(line)
                except queue.Full:
                    try:
                        log_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        log_queue.put_nowait(line)
                    except queue.Full:
                        pass

            def _process_buffer(buffer: str) -> str:
                """Process buffer, enqueue complete lines, return remaining buffer."""
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    _enqueue_line(line)
                return buffer

            def _stream_worker():
                try:
                    # Read initial logs without follow to get tail_lines immediately
                    if self.tail_lines > 0:
                        try:
                            initial_response = core_v1.read_namespaced_pod_log(
                                name=target_pod_name,
                                namespace=self.namespace,
                                tail_lines=self.tail_lines,
                                follow=False,
                                _preload_content=False,
                                _request_timeout=5.0,
                            )
                            buffer = ""
                            # Read with timeout protection - if connection hangs, break after reasonable time
                            read_start_time = time.time()
                            max_read_time = 10.0  # Max 10 seconds for initial read

                            while True:
                                # Check if we've been running too long (timeout protection)
                                elapsed = time.time() - read_start_time
                                if elapsed > max_read_time:
                                    logger.warning(
                                        f"Initial log read timeout after {elapsed:.1f}s"
                                    )
                                    break

                                try:
                                    chunk = initial_response.read(4096)
                                    if not chunk:
                                        break
                                    buffer += chunk.decode("utf-8", errors="replace")
                                    if len(buffer) > MAX_BUFFER_SIZE:
                                        buffer = (
                                            _process_buffer(buffer[:MAX_BUFFER_SIZE])
                                            + buffer[MAX_BUFFER_SIZE:]
                                        )
                                    else:
                                        buffer = _process_buffer(buffer)
                                except Exception as read_err:
                                    logger.debug(
                                        f"Error reading initial log chunk: {read_err}"
                                    )
                                    break

                            if buffer:
                                _enqueue_line(buffer)
                        except Exception as init_err:
                            logger.error(f"Error reading initial logs: {init_err}")
                            _enqueue_line(
                                f"ERROR: Failed to read initial logs: {str(init_err)}"
                            )

                    # Start following for new logs after initial batch
                    if self.follow:
                        try:
                            follow_response = core_v1.read_namespaced_pod_log(
                                name=target_pod_name,
                                namespace=self.namespace,
                                tail_lines=0,
                                follow=True,
                                _preload_content=False,
                                _request_timeout=5.0,
                            )
                            self._follow_response = follow_response
                            buffer = ""
                            last_read_time = time.time()
                            max_idle_time = 30.0  # Max 30 seconds without data before checking connection

                            while self._running:
                                try:
                                    chunk = follow_response.read(4096)
                                    if not chunk:
                                        # Empty chunk means connection closed
                                        break

                                    # Reset idle timer on successful read
                                    last_read_time = time.time()

                                    buffer += chunk.decode("utf-8", errors="replace")
                                except Exception as read_error:
                                    logger.debug(
                                        f"Follow stream read error: {read_error}"
                                    )
                                    break

                                # Check for idle timeout (connection might be dead)
                                idle_time = time.time() - last_read_time
                                if idle_time > max_idle_time:
                                    logger.warning(
                                        f"Follow stream idle timeout after {idle_time:.1f}s"
                                    )
                                    break

                                if len(buffer) > MAX_BUFFER_SIZE:
                                    buffer = (
                                        _process_buffer(buffer[:MAX_BUFFER_SIZE])
                                        + buffer[MAX_BUFFER_SIZE:]
                                    )
                                else:
                                    buffer = _process_buffer(buffer)

                            if buffer:
                                _enqueue_line(buffer)
                        except Exception as follow_err:
                            logger.error(f"Error starting follow stream: {follow_err}")
                            _enqueue_line(
                                f"ERROR: Failed to follow logs: {str(follow_err)}"
                            )

                except ApiException as e:
                    logger.error(
                        f"Kubernetes API error reading logs: {e}", exc_info=True
                    )
                    error_msg = None

                    if e.status == 404:
                        error_msg = "INFO: Pod was deleted or is no longer available"

                    elif e.status == 400:
                        api_error_msg = e.reason or str(e)
                        if (
                            "container" in api_error_msg.lower()
                            and "not running" in api_error_msg.lower()
                        ):
                            error_msg = "INFO: Container is not running yet. Logs will be available once the container starts."
                        else:
                            error_msg = f"ERROR: {api_error_msg}"

                    else:
                        error_msg = f"ERROR: {e.reason or str(e)}"

                    if error_msg:
                        _enqueue_line(error_msg)

                except Exception as e:
                    logger.error(
                        f"Unexpected error in log worker thread: {e}", exc_info=True
                    )
                    _enqueue_line(f"ERROR: {str(e)}")

                finally:
                    # Clear the response reference
                    self._follow_response = None
                    try:
                        log_queue.put_nowait(None)
                    except queue.Full:
                        pass

            # Worker thread reads logs synchronously, async generator consumes from queue
            stream_task = asyncio.create_task(asyncio.to_thread(_stream_worker))
            await asyncio.sleep(0.1)

            while self._running:
                try:
                    line = await asyncio.wait_for(
                        asyncio.to_thread(log_queue.get), timeout=0.5
                    )
                    if line is None:
                        break
                    yield line
                except asyncio.TimeoutError:
                    if stream_task.done():
                        # Drain remaining items from queue before exiting
                        try:
                            while True:
                                line = await asyncio.to_thread(log_queue.get_nowait)
                                if line is None:
                                    break
                                yield line
                        except queue.Empty:
                            pass
                        break
                    continue

            try:
                await stream_task

            except Exception:
                pass

        except asyncio.CancelledError:
            logger.info(f"Log streaming cancelled for {self.service_name}")

        except Exception as e:
            logger.error(f"Error streaming logs: {e}")
            if self._running:
                yield f"ERROR: Failed to stream logs: {str(e)}"

        finally:
            self._running = False

    async def stop(self):
        """Stop streaming logs."""
        self._running = False

        # Close the follow response to interrupt blocking read() call
        if self._follow_response:
            try:
                self._follow_response.close()
            except Exception as e:
                logger.debug(f"Error closing follow response: {e}")
            finally:
                self._follow_response = None

        pod_info = (
            f"pod {self.pod_name}" if self.pod_name else f"service {self.service_name}"
        )
        logger.info(f"Stopped log streaming for {pod_info}")
