from __future__ import annotations

import hashlib
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from foundation.process import run_process

from worker.execution import container_veth_names


@dataclass(slots=True)
class PreparedNetworkPool:
    worker_id: str
    bridge_name: str
    host_netns_path: str
    ip_binary: str = "ip"
    _executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="network-prepare"
        ),
        init=False,
        repr=False,
    )
    _slots: deque[tuple[str, Future[None]]] = field(default_factory=deque, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker identity is required for prepared networks")

    def initialize(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("prepared network pool is closed")
            if self._slots:
                return
            prefix = hashlib.sha256(self.worker_id.encode()).hexdigest()[:12]
            for index in range(2):
                name = f"lc-ready-{prefix}-{index}"
                self._slots.append((name, self._executor.submit(self._prepare, name)))
            for _, future in self._slots:
                future.result()

    def assign(self, container_id: str) -> None:
        with self._lock:
            if self._closed or not self._slots:
                raise RuntimeError("prepared network pool is unavailable")
            name, ready = self._slots.popleft()
            try:
                ready.result()
                self._transfer(name, container_id)
            except Exception as transfer_error:
                try:
                    self._remove(name)
                except Exception as cleanup_error:
                    raise ExceptionGroup(
                        "prepared network assignment and cleanup failed",
                        [transfer_error, cleanup_error],
                    ) from cleanup_error
                raise
            finally:
                self._slots.append((name, self._executor.submit(self._prepare, name)))

    def close(self) -> None:
        with self._lock:
            if self._closed and not self._slots:
                return
            self._closed = True
            self._executor.shutdown(wait=True)
            errors: list[Exception] = []
            remaining: deque[tuple[str, Future[None]]] = deque()
            for name, future in self._slots:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(exc)
                try:
                    self._remove(name)
                except Exception as exc:
                    errors.append(exc)
                    remaining.append((name, future))
            self._slots = remaining
            if errors:
                raise ExceptionGroup("prepared network pool cleanup failed", errors)

    def _prepare(self, name: str) -> None:
        self._remove(name)
        host, peer = container_veth_names(name)
        try:
            for argv in (
                [self.ip_binary, "netns", "add", name],
                [self.ip_binary, "link", "add", host, "type", "veth", "peer", "name", peer],
                [self.ip_binary, "link", "set", host, "master", self.bridge_name],
                [self.ip_binary, "link", "set", peer, "netns", name],
            ):
                self._run(argv)
        except Exception as prepare_error:
            try:
                self._remove(name)
            except Exception as cleanup_error:
                raise ExceptionGroup(
                    "network preparation and cleanup failed", [prepare_error, cleanup_error]
                ) from cleanup_error
            raise

    def _transfer(self, name: str, container_id: str) -> None:
        host, peer = container_veth_names(name)
        assigned_host, assigned_peer = container_veth_names(container_id)
        source = Path(self.host_netns_path) / name
        destination = Path(self.host_netns_path) / container_id
        destination.touch(exist_ok=False)
        # netns lives on a shared mount, where MS_MOVE is forbidden. Bind the
        # existing namespace to its durable container name before releasing ours.
        self._run(["mount", "--bind", str(source), str(destination)])
        self._run([self.ip_binary, "netns", "delete", name])
        self._run([self.ip_binary, "link", "set", host, "name", assigned_host])
        self._run(
            [
                self.ip_binary,
                "netns",
                "exec",
                container_id,
                self.ip_binary,
                "link",
                "set",
                peer,
                "name",
                assigned_peer,
            ]
        )

    def _remove(self, name: str) -> None:
        host, _ = container_veth_names(name)
        # Missing owned resources are already clean. Any other deletion failure
        # must remain visible so a replacement cannot hide an incomplete cleanup.
        link = run_process([self.ip_binary, "link", "show", host])
        if link.ok:
            self._run([self.ip_binary, "link", "delete", host])
        elif "does not exist" not in link.stderr and "Cannot find device" not in link.stderr:
            raise RuntimeError(link.stderr or "could not inspect prepared network interface")
        if (Path(self.host_netns_path) / name).exists():
            self._run([self.ip_binary, "netns", "delete", name])

    @staticmethod
    def _run(argv: list[str]) -> None:
        result = run_process(argv)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "prepared network command failed")
