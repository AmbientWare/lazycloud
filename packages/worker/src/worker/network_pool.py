from __future__ import annotations

import hashlib
import secrets
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from foundation.process import run_process

from worker.execution import container_veth_names


def _random_mac_address() -> str:
    address = bytearray(secrets.token_bytes(6))
    address[0] = (address[0] & 0xFC) | 0x02
    return address.hex(":")


@dataclass(slots=True)
class PreparedNetworkPool:
    worker_id: str
    bridge_name: str
    host_netns_path: str
    capacity: int
    ip_binary: str = "ip"
    _executor: ThreadPoolExecutor = field(init=False, repr=False)
    _slots: deque[tuple[str, Future[None]]] = field(default_factory=deque, init=False)
    _condition: threading.Condition = field(default_factory=threading.Condition, init=False)
    _closed: bool = field(default=False, init=False)
    _closing: bool = field(default=False, init=False)
    _assignments: int = field(default=0, init=False)
    _initialization: Future[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker identity is required for prepared networks")
        if self.capacity < 1:
            raise ValueError("prepared network capacity must be positive")
        self._executor = ThreadPoolExecutor(
            max_workers=self.capacity, thread_name_prefix="network-prepare"
        )

    def initialize(self) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError("prepared network pool is closed")
            initialization = self._initialization
            first = initialization is None
            if initialization is None:
                initialization = self._initialization = Future()
                prefix = hashlib.sha256(self.worker_id.encode()).hexdigest()[:12]
                for index in range(self.capacity):
                    name = f"lc-ready-{prefix}-{index}"
                    self._slots.append((name, self._executor.submit(self._prepare, name)))
                self._condition.notify_all()
            preparing = tuple(future for _, future in self._slots)
        if first:
            try:
                for future in preparing:
                    future.result()
            except BaseException as exc:
                initialization.set_exception(exc)
            else:
                initialization.set_result(None)
        initialization.result()

    def assign(self, container_id: str) -> None:
        with self._condition:
            if self._initialization is None:
                raise RuntimeError("prepared network pool is unavailable")
            while not self._slots and not self._closed:
                self._condition.wait()
            if self._closed:
                raise RuntimeError("prepared network pool is closed")
            name, ready = self._slots.popleft()
            self._assignments += 1
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
            with self._condition:
                if not self._closed:
                    ready = self._executor.submit(self._prepare, name)
                self._slots.append((name, ready))
                self._assignments -= 1
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            while self._closing:
                self._condition.wait()
            if self._closed and not self._slots:
                return
            self._closed = True
            self._closing = True
            self._condition.notify_all()
            while self._assignments:
                self._condition.wait()
            slots, self._slots = self._slots, deque()
        errors: list[Exception] = []
        remaining: deque[tuple[str, Future[None]]] = deque()
        try:
            self._executor.shutdown(wait=True)
            for name, future in slots:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(exc)
                try:
                    self._remove(name)
                except Exception as exc:
                    errors.append(exc)
                    remaining.append((name, future))
        finally:
            with self._condition:
                self._slots = remaining
                self._closing = False
                self._condition.notify_all()
        if errors:
            raise ExceptionGroup("prepared network pool cleanup failed", errors)

    def _prepare(self, name: str) -> None:
        self._remove(name)
        host, peer = container_veth_names(name)
        try:
            for argv in (
                [self.ip_binary, "netns", "add", name],
                # Set both addresses at creation so udev cannot derive reused
                # MACs from slot names while earlier containers still use them.
                [
                    self.ip_binary,
                    "link",
                    "add",
                    host,
                    "address",
                    _random_mac_address(),
                    "type",
                    "veth",
                    "peer",
                    "name",
                    peer,
                    "address",
                    _random_mac_address(),
                ],
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
