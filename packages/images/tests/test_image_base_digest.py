from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread

from images.building import (
    BaseImageDigestCache,
    BaseImageDigestRequest,
    BaseImageDigestResolution,
    BaseImageDigestResolutionStatus,
    resolve_base_image_digest,
)


def test_base_image_digest_credentials_never_enter_shared_cache() -> None:
    cache = BaseImageDigestCache(ttl_seconds=300, max_entries=4)
    digests = iter(["sha256:first", "sha256:second"])
    calls = 0

    def inspector(_source_image: str, credentials: str) -> str:
        nonlocal calls
        calls += 1
        assert credentials == "private-registry-credentials"
        return next(digests)

    request = BaseImageDigestRequest(
        registry="registry.example",
        name="team/private-app",
        tag="latest",
        credentials="private-registry-credentials",
        cacheable=True,
    )

    first = resolve_base_image_digest(request, inspector=inspector, cache=cache)
    second = resolve_base_image_digest(request, inspector=inspector, cache=cache)

    assert first.digest == "sha256:first"
    assert second.digest == "sha256:second"
    assert not first.cacheable
    assert not second.cacheable
    assert calls == 2
    assert cache.entry_count() == 0


def test_base_image_digest_cache_hits_and_refreshes_expired_entries() -> None:
    now = datetime(2026, 6, 18, tzinfo=UTC)
    cache = BaseImageDigestCache(ttl_seconds=300)
    digests = iter(["sha256:first", "sha256:second"])
    calls = 0

    def inspector(_source_image: str, _credentials: str) -> str:
        nonlocal calls
        calls += 1
        return next(digests)

    request = BaseImageDigestRequest(
        registry="registry.example",
        name="team/app",
        tag="latest",
    )

    first = resolve_base_image_digest(request, inspector=inspector, cache=cache, now=now)
    cached = resolve_base_image_digest(
        request,
        inspector=inspector,
        cache=cache,
        now=now + timedelta(seconds=60),
    )
    refreshed = resolve_base_image_digest(
        request,
        inspector=inspector,
        cache=cache,
        now=now + timedelta(seconds=301),
    )

    assert first.status is BaseImageDigestResolutionStatus.Resolved
    assert cached.status is BaseImageDigestResolutionStatus.CacheHit
    assert cached.digest == "sha256:first"
    assert refreshed.status is BaseImageDigestResolutionStatus.Resolved
    assert refreshed.digest == "sha256:second"
    assert calls == 2


def test_base_image_digest_cache_evicts_the_least_recently_used_tag() -> None:
    now = datetime(2026, 6, 18, tzinfo=UTC)
    cache = BaseImageDigestCache(ttl_seconds=300, max_entries=2)
    outcomes: list[BaseImageDigestResolutionStatus] = []

    for tag in ("a", "b", "a", "c", "a", "b"):
        result = resolve_base_image_digest(
            BaseImageDigestRequest(registry="registry.example", name="team/app", tag=tag),
            inspector=lambda source, _credentials: f"sha256:{source.rsplit(':', 1)[-1]}",
            cache=cache,
            now=now,
        )
        outcomes.append(result.status)
        assert cache.entry_count(now=now) <= 2

    assert outcomes == [
        BaseImageDigestResolutionStatus.Resolved,
        BaseImageDigestResolutionStatus.Resolved,
        BaseImageDigestResolutionStatus.CacheHit,
        BaseImageDigestResolutionStatus.Resolved,
        BaseImageDigestResolutionStatus.CacheHit,
        BaseImageDigestResolutionStatus.Resolved,
    ]
    assert cache.inflight_count() == 0


def test_base_image_digest_resolution_reports_missing_and_failed_inspect() -> None:
    request = BaseImageDigestRequest(
        registry="registry.example",
        name="team/app",
        tag="latest",
    )

    missing = resolve_base_image_digest(request, inspector=lambda _source, _creds: "")

    def failed(_source_image: str, _credentials: str) -> str:
        msg = "registry unavailable"
        raise RuntimeError(msg)

    failure = resolve_base_image_digest(request, inspector=failed)

    assert missing.status is BaseImageDigestResolutionStatus.MissingDigest
    assert "missing" in missing.reason
    assert failure.status is BaseImageDigestResolutionStatus.InspectFailed
    assert failure.reason == "registry unavailable"


def test_base_image_digest_cache_shares_concurrent_lookup() -> None:
    cache = BaseImageDigestCache(ttl_seconds=300)
    request = BaseImageDigestRequest(
        registry="registry.example",
        name="team/app",
        tag="latest",
    )
    entered = Event()
    release = Event()
    calls_lock = Lock()
    calls = 0
    results: list[BaseImageDigestResolution] = []

    def inspector(_source_image: str, _credentials: str) -> str:
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        release.wait(timeout=2)
        return "sha256:shared"

    def resolve() -> None:
        results.append(resolve_base_image_digest(request, inspector=inspector, cache=cache))

    first = Thread(target=resolve)
    second = Thread(target=resolve)
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert calls == 1
    assert [result.digest for result in results] == ["sha256:shared", "sha256:shared"]
    assert {result.status for result in results} == {
        BaseImageDigestResolutionStatus.Resolved,
    }
    assert any(result.shared_lookup for result in results)
    assert cache.inflight_count() == 0


def test_base_image_digest_cache_shares_failed_concurrent_lookup() -> None:
    cache = BaseImageDigestCache(ttl_seconds=300)
    request = BaseImageDigestRequest(
        registry="registry.example",
        name="team/missing",
        tag="latest",
    )
    entered = Event()
    release = Event()
    calls_lock = Lock()
    calls = 0
    results: list[BaseImageDigestResolution] = []

    def inspector(_source_image: str, _credentials: str) -> str:
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        release.wait(timeout=2)
        return ""

    def resolve() -> None:
        results.append(resolve_base_image_digest(request, inspector=inspector, cache=cache))

    first = Thread(target=resolve)
    second = Thread(target=resolve)
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert calls == 1
    assert len(results) == 2
    assert all(result.status is BaseImageDigestResolutionStatus.MissingDigest for result in results)
    assert any(result.shared_lookup for result in results)
    assert cache.entry_count() == 0
    assert cache.inflight_count() == 0
