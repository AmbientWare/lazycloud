from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Event, RLock

from shared.timestamps import utc_now

from images.building.constants import (
    BASE_IMAGE_DIGEST_CACHE_MAX_ENTRIES,
    BASE_IMAGE_DIGEST_CACHE_TTL_SECONDS,
)
from images.building.models import (
    BaseImageDigestCacheEntry,
    BaseImageDigestInspector,
    BaseImageDigestRequest,
    BaseImageDigestResolution,
    BaseImageDigestResolutionStatus,
)


@dataclass(slots=True)
class _BaseImageDigestFlight:
    done: Event = field(default_factory=Event)
    participants: int = 0
    resolution: BaseImageDigestResolution | None = None


@dataclass(slots=True)
class BaseImageDigestCache:
    ttl_seconds: int = BASE_IMAGE_DIGEST_CACHE_TTL_SECONDS
    max_entries: int = BASE_IMAGE_DIGEST_CACHE_MAX_ENTRIES
    _entries: dict[str, BaseImageDigestCacheEntry] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock)
    _inflight: dict[str, _BaseImageDigestFlight] = field(default_factory=dict)

    def get(self, source_image: str, *, now: datetime | None = None) -> str:
        current = now or utc_now()
        with self._lock:
            self._sweep_expired(current)
            entry = self._entries.pop(source_image, None)
            if entry is None:
                return ""
            self._entries[source_image] = entry
            return entry.digest

    def set(self, source_image: str, digest: str, *, now: datetime | None = None) -> None:
        if not source_image or not digest or self.ttl_seconds <= 0 or self.max_entries <= 0:
            return
        current = now or utc_now()
        with self._lock:
            self._sweep_expired(current)
            self._entries.pop(source_image, None)
            while len(self._entries) >= self.max_entries:
                del self._entries[next(iter(self._entries))]
            self._entries[source_image] = BaseImageDigestCacheEntry(
                source_image=source_image,
                digest=digest,
                expires_at=current + timedelta(seconds=self.ttl_seconds),
            )

    def entry_count(self, *, now: datetime | None = None) -> int:
        current = now or utc_now()
        with self._lock:
            self._sweep_expired(current)
            return len(self._entries)

    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    def resolve(
        self,
        source_image: str,
        credentials: str,
        inspector: BaseImageDigestInspector,
        *,
        now: datetime | None = None,
    ) -> BaseImageDigestResolution:
        if credentials:
            return _inspect_base_image_digest(
                source_image,
                credentials,
                inspector,
                cacheable=False,
            )
        if digest := self.get(source_image, now=now):
            return BaseImageDigestResolution(
                status=BaseImageDigestResolutionStatus.CacheHit,
                source_image=source_image,
                digest=digest,
                shared_lookup=False,
            )

        flight, leader = self._join_flight(source_image)

        try:
            if not leader:
                flight.done.wait()
                resolution = flight.resolution
                if resolution is None:
                    raise RuntimeError("base image digest lookup completed without a result")
                return resolution.model_copy(update={"shared_lookup": True})

            if digest := self.get(source_image, now=now):
                resolution = BaseImageDigestResolution(
                    status=BaseImageDigestResolutionStatus.CacheHit,
                    source_image=source_image,
                    digest=digest,
                )
            else:
                resolution = _inspect_base_image_digest(
                    source_image,
                    credentials,
                    inspector,
                    cacheable=True,
                )
                if resolution.digest:
                    self.set(source_image, resolution.digest, now=now)
            flight.resolution = resolution
            flight.done.set()
            return resolution
        finally:
            self._leave_flight(source_image, flight)

    def _join_flight(self, source_image: str) -> tuple[_BaseImageDigestFlight, bool]:
        with self._lock:
            flight = self._inflight.get(source_image)
            leader = flight is None
            if flight is None:
                flight = _BaseImageDigestFlight()
                self._inflight[source_image] = flight
            flight.participants += 1
            return flight, leader

    def _leave_flight(self, source_image: str, flight: _BaseImageDigestFlight) -> None:
        with self._lock:
            flight.participants -= 1
            if flight.participants == 0 and self._inflight.get(source_image) is flight:
                del self._inflight[source_image]

    def _sweep_expired(self, current: datetime) -> None:
        expired = [
            source_image
            for source_image, entry in self._entries.items()
            if current >= entry.expires_at
        ]
        for source_image in expired:
            del self._entries[source_image]


def resolve_base_image_digest(
    request: BaseImageDigestRequest,
    *,
    inspector: BaseImageDigestInspector,
    cache: BaseImageDigestCache | None = None,
    now: datetime | None = None,
) -> BaseImageDigestResolution:
    source_image = request.source_image
    cacheable = request.cacheable and not request.credentials
    if request.current_digest:
        return BaseImageDigestResolution(
            status=BaseImageDigestResolutionStatus.Skipped,
            source_image=source_image,
            digest=request.current_digest,
            cacheable=cacheable,
            reason="base image digest already set",
        )
    if not source_image:
        return BaseImageDigestResolution(
            status=BaseImageDigestResolutionStatus.Skipped,
            cacheable=cacheable,
            reason="base image registry, name, and tag are required",
        )
    if cacheable and cache is not None:
        return cache.resolve(source_image, request.credentials, inspector, now=now)
    return _inspect_base_image_digest(
        source_image,
        request.credentials,
        inspector,
        cacheable=False,
    )


def _inspect_base_image_digest(
    source_image: str,
    credentials: str,
    inspector: BaseImageDigestInspector,
    *,
    cacheable: bool,
) -> BaseImageDigestResolution:
    try:
        digest = inspector(source_image, credentials)
    except Exception as exc:
        return BaseImageDigestResolution(
            status=BaseImageDigestResolutionStatus.InspectFailed,
            source_image=source_image,
            cacheable=cacheable,
            reason=str(exc),
        )
    if not digest:
        return BaseImageDigestResolution(
            status=BaseImageDigestResolutionStatus.MissingDigest,
            source_image=source_image,
            cacheable=cacheable,
            reason="base image digest missing from registry inspect",
        )
    return BaseImageDigestResolution(
        status=BaseImageDigestResolutionStatus.Resolved,
        source_image=source_image,
        digest=digest,
        cacheable=cacheable,
    )
