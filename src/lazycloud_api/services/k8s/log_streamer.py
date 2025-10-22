import asyncio
from typing import AsyncGenerator

from loguru import logger


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
        self._process: asyncio.subprocess.Process | None = None
        self._running = False

    async def _check_pod_exists(self) -> tuple[bool, str | None]:
        """Check if pod exists and get its status.

        Returns:
            Tuple of (exists, status_reason) where status_reason is None if ready
        """
        if not self.pod_name:
            return True, None  # Skip check for label selector

        try:
            cmd = [
                "kubectl",
                "get",
                "pod",
                self.pod_name,
                "-n",
                self.namespace,
                "-o",
                "jsonpath={.status.phase}",
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                # Pod doesn't exist
                return False, "Instance not found"

            phase = stdout.decode("utf-8").strip()

            # Check if pod is in a state where logs might be available
            if phase in ["Running", "Succeeded", "Failed"]:
                return True, None
            elif phase == "Pending":
                return True, "Instance is still starting up"
            else:
                return True, f"Instance is in {phase} state"

        except Exception as e:
            logger.error(f"Error checking pod status: {e}")
            return True, None  # Proceed anyway

    async def stream(self) -> AsyncGenerator[str, None]:
        """Stream logs from kubectl"""
        if self._running:
            return

        self._running = True

        # Check if pod exists and is ready (with retry for starting pods)
        if self.pod_name:
            max_retries = 15  # 30 seconds total
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
                    yield f"INFO: {reason}, waiting... (attempt {attempt + 1}/{max_retries})"
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        # Proceed anyway after max retries
                        break
                else:
                    # Pod is ready
                    break

        # Build kubectl logs command
        cmd = [
            "kubectl",
            "logs",
            "-n",
            self.namespace,
        ]

        # Use pod name if specified, otherwise use label selector
        if self.pod_name:
            cmd.append(self.pod_name)
        else:
            cmd.extend(["-l", f"app.kubernetes.io/name={self.service_name}"])

        cmd.extend(["--tail", str(self.tail_lines)])

        if self.follow:
            cmd.append("-f")

        try:
            # Start the kubectl process
            self._process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            pod_info = (
                f"pod {self.pod_name}"
                if self.pod_name
                else f"service {self.service_name}"
            )
            logger.info(f"Started log streaming for {pod_info} in {self.namespace}")

            # Read stdout line by line
            if self._process.stdout:
                while self._running:
                    line = await self._process.stdout.readline()
                    if not line:
                        break

                    # Decode and strip line
                    log_line = line.decode("utf-8").rstrip()
                    if log_line:
                        yield log_line

            # Check for any errors
            if self._process.stderr:
                stderr = await self._process.stderr.read()
                if stderr:
                    error_msg = stderr.decode("utf-8").strip()
                    # Don't log as error if it's a known transient issue
                    if "not found" in error_msg.lower():
                        logger.warning(f"kubectl logs warning: {error_msg}")
                        yield "INFO: Pod was deleted or is no longer available"
                    else:
                        logger.error(f"kubectl logs error: {error_msg}")
                        yield f"ERROR: {error_msg}"

        except asyncio.CancelledError:
            logger.info(f"Log streaming cancelled for {self.service_name}")

        except Exception as e:
            logger.error(f"Error streaming logs: {e}")
            # Only send error if we're still running (connection not closed)
            if self._running:
                yield f"ERROR: Failed to stream logs: {str(e)}"

        finally:
            self._running = False

    async def stop(self):
        """Stop streaming logs."""
        self._running = False
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)

            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

            except Exception as e:
                logger.error(f"Error stopping log stream: {e}")

        pod_info = (
            f"pod {self.pod_name}" if self.pod_name else f"service {self.service_name}"
        )
        logger.info(f"Stopped log streaming for {pod_info}")
