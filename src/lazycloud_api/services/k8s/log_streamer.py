import asyncio
import queue
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

    async def _check_pod_exists(self) -> tuple[bool, str | None]:
        """Check if pod exists and get its status with detailed error information"""
        if not self.pod_name:
            return True, None

        try:
            core_v1 = get_core_v1_api()

            def _get_pod_details():
                return core_v1.read_namespaced_pod(
                    name=self.pod_name, namespace=self.namespace
                )

            # Step 1: Fetch pod details from Kubernetes API
            pod = await asyncio.to_thread(_get_pod_details)

            if not pod.status:
                return True, "Instance status not available"

            phase = pod.status.phase

            # Step 2: Check container states for errors that prevent log access
            container_statuses = pod.status.container_statuses or []
            for container_status in container_statuses:
                if not container_status.state:
                    continue

                # Step 3: Detect waiting states (image pull errors, etc.)
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

                # Step 4: Detect terminated containers with errors
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

            # Step 5: Determine if logs are available based on pod phase
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

        # Step 1: Wait for pod to be ready (with retries for starting pods)
        if self.pod_name:
            max_retries = 15
            retry_delay = 2

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
                    # Step 2: Check for unrecoverable errors that prevent log access
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

            # Step 3: Resolve target pod name (use provided name or find first pod for service)
            target_pod_name = self.pod_name
            if not target_pod_name:

                def _find_pod():
                    pods = core_v1.list_namespaced_pod(
                        namespace=self.namespace,
                        label_selector=f"app.kubernetes.io/name={self.service_name}",
                    )
                    if pods.items:
                        return pods.items[0].metadata.name
                    return None

                target_pod_name = await asyncio.to_thread(_find_pod)
                if not target_pod_name:
                    yield "ERROR: No pods found for service"
                    self._running = False
                    return

            # Step 4: Set up worker thread to read logs synchronously and queue them
            log_queue: queue.Queue[str | None] = queue.Queue()

            def _stream_worker():
                """Worker function to stream logs and put them in queue."""
                try:
                    response = core_v1.read_namespaced_pod_log(
                        name=target_pod_name,
                        namespace=self.namespace,
                        tail_lines=self.tail_lines,
                        follow=self.follow,
                        _preload_content=False,
                    )

                    # Step 5: Read log stream in chunks and buffer until complete lines
                    buffer = ""
                    while self._running:
                        chunk = response.read(4096)
                        if not chunk:
                            break

                        buffer += chunk.decode("utf-8", errors="replace")
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            if line.strip():
                                log_queue.put(line.rstrip())

                    if buffer.strip():
                        log_queue.put(buffer.rstrip())

                except ApiException as e:
                    if e.status == 404:
                        log_queue.put("INFO: Pod was deleted or is no longer available")
                    elif e.status == 400:
                        error_msg = e.reason or str(e)
                        if (
                            "container" in error_msg.lower()
                            and "not running" in error_msg.lower()
                        ):
                            log_queue.put(
                                "INFO: Container is not running yet. Logs will be available once the container starts."
                            )
                        else:
                            log_queue.put(f"ERROR: {error_msg}")
                    else:
                        error_msg = e.reason or str(e)
                        log_queue.put(f"ERROR: {error_msg}")
                except Exception as e:
                    log_queue.put(f"ERROR: {str(e)}")
                finally:
                    log_queue.put(None)

            # Step 6: Start worker thread and consume from queue asynchronously
            stream_task = asyncio.create_task(asyncio.to_thread(_stream_worker))

            while self._running:
                try:
                    line = await asyncio.to_thread(log_queue.get_nowait)
                    if line is None:
                        break
                    yield line
                except queue.Empty:
                    if stream_task.done():
                        try:
                            while True:
                                line = await asyncio.to_thread(log_queue.get_nowait)
                                if line is None:
                                    break
                                yield line
                        except queue.Empty:
                            pass
                        break
                    await asyncio.sleep(0.1)
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

        pod_info = (
            f"pod {self.pod_name}" if self.pod_name else f"service {self.service_name}"
        )
        logger.info(f"Stopped log streaming for {pod_info}")
