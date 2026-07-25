from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from types import TracebackType


@dataclass(slots=True)
class CheckpointArtifactLeaseRegistry:
    _active: dict[str, int] = field(default_factory=dict, init=False)
    _materializations: dict[str, _CheckpointMaterializationState] = field(
        default_factory=dict,
        init=False,
    )
    _lock: Lock = field(default_factory=Lock, init=False)

    def acquire(self, checkpoint_id: str) -> CheckpointArtifactLease:
        if not checkpoint_id:
            raise ValueError("checkpoint artifact lease requires a checkpoint id")
        with self._lock:
            self._active[checkpoint_id] = self._active.get(checkpoint_id, 0) + 1
        return CheckpointArtifactLease(registry=self, checkpoint_id=checkpoint_id)

    def protected_checkpoint_ids(self) -> set[str]:
        with self._lock:
            return set(self._active)

    def acquire_materialization(self, checkpoint_id: str) -> CheckpointMaterializationLease:
        artifact_lease = self.acquire(checkpoint_id)
        with self._lock:
            state = self._materializations.get(checkpoint_id)
            if state is None:
                state = _CheckpointMaterializationState()
                self._materializations[checkpoint_id] = state
            state.references += 1
        try:
            state.token.acquire()
        except BaseException:
            self._release_materialization_reference(checkpoint_id, state)
            artifact_lease.close()
            raise
        return CheckpointMaterializationLease(
            registry=self,
            checkpoint_id=checkpoint_id,
            state=state,
            artifact_lease=artifact_lease,
        )

    @contextmanager
    def retention_guard(self, checkpoint_id: str) -> Iterator[bool]:
        if not checkpoint_id:
            raise ValueError("checkpoint retention guard requires a checkpoint id")
        with self._lock:
            yield checkpoint_id not in self._active

    def release(self, checkpoint_id: str) -> None:
        with self._lock:
            count = self._active.get(checkpoint_id)
            if count is None:
                raise RuntimeError(f"checkpoint artifact lease is not active: {checkpoint_id}")
            if count == 1:
                del self._active[checkpoint_id]
            else:
                self._active[checkpoint_id] = count - 1

    def release_materialization(
        self,
        checkpoint_id: str,
        state: _CheckpointMaterializationState,
    ) -> None:
        state.token.release()
        self._release_materialization_reference(checkpoint_id, state)

    def _release_materialization_reference(
        self,
        checkpoint_id: str,
        state: _CheckpointMaterializationState,
    ) -> None:
        with self._lock:
            state.references -= 1
            if state.references < 0:
                raise RuntimeError(
                    f"checkpoint materialization reference underflow: {checkpoint_id}"
                )
            if state.references == 0:
                current = self._materializations.get(checkpoint_id)
                if current is not state:
                    raise RuntimeError(
                        f"checkpoint materialization ownership changed: {checkpoint_id}"
                    )
                del self._materializations[checkpoint_id]


@dataclass(slots=True)
class _CheckpointMaterializationState:
    token: Lock = field(default_factory=Lock)
    references: int = 0


@dataclass(slots=True)
class CheckpointArtifactLease:
    registry: CheckpointArtifactLeaseRegistry
    checkpoint_id: str
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        if self._closed:
            return
        self.registry.release(self.checkpoint_id)
        self._closed = True

    def __enter__(self) -> CheckpointArtifactLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = (exc_type, exc, traceback)
        self.close()


@dataclass(slots=True)
class CheckpointMaterializationLease:
    registry: CheckpointArtifactLeaseRegistry
    checkpoint_id: str
    state: _CheckpointMaterializationState
    artifact_lease: CheckpointArtifactLease
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.registry.release_materialization(self.checkpoint_id, self.state)
        finally:
            self.artifact_lease.close()
            self._closed = True

    def __enter__(self) -> CheckpointMaterializationLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = (exc_type, exc, traceback)
        self.close()
