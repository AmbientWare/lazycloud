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

    async def stream(self) -> AsyncGenerator[str, None]:
        """Stream logs from kubectl"""
        if self._running:
            return

        self._running = True

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
