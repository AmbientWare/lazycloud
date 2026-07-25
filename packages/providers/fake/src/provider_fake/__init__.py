"""Dev/test-only fake pooled compute provider adapter.

Never composed into production processes; used by dev loops and tests to drive
the real bootstrap/reclaim owners without AWS.
"""

from provider_fake.identity import (
    FakeReplayGuard,
    FakeStsHttpClient,
    FakeStsIdentityResponse,
    fake_presigned_proof_url,
)
from provider_fake.provider import FakeInstance, FakePooledCapacityProvider

__all__ = [
    "FakeInstance",
    "FakePooledCapacityProvider",
    "FakeReplayGuard",
    "FakeStsHttpClient",
    "FakeStsIdentityResponse",
    "fake_presigned_proof_url",
]
