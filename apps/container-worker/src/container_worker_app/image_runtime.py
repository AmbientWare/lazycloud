from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from worker.image_runtime import ImageRuntimeClient


@dataclass(slots=True)
class ImageRuntimeProcess:
    image_root: Path
    mount_root: Path
    cache_root: Path
    build_root: Path
    binary: Path = Path("/usr/local/bin/lazycloud-image-runtime")
    socket_path: Path = Path("/run/lazycloud/image-runtime.sock")
    process: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _expected_stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def start(self) -> ImageRuntimeClient:
        for root in (self.image_root, self.mount_root, self.cache_root, self.build_root):
            root.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [
                str(self.binary),
                "--socket",
                str(self.socket_path),
                "--image-root",
                str(self.image_root),
                "--mount-root",
                str(self.mount_root),
                "--cache-root",
                str(self.cache_root),
                "--build-root",
                str(self.build_root),
            ],
            stdin=subprocess.DEVNULL,
        )
        client = ImageRuntimeClient(self.socket_path)
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"image runtime exited during startup with code {self.process.returncode}"
                )
            try:
                response = client.health()
            except (OSError, RuntimeError):
                time.sleep(0.01)
                continue
            if response.ok:
                threading.Thread(
                    target=self._terminate_worker_if_runtime_stops,
                    daemon=True,
                    name="image-runtime-liveness",
                ).start()
                return client
            raise RuntimeError(response.error or "image runtime readiness failed")
        self.stop()
        raise RuntimeError("image runtime did not become ready within one second")

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        self._expected_stop.set()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def _terminate_worker_if_runtime_stops(self) -> None:
        process = self.process
        if process is None:
            return
        process.wait()
        if not self._expected_stop.is_set():
            os.kill(os.getpid(), signal.SIGTERM)
