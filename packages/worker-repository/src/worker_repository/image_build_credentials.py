"""Server-side image-build credential lease storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from coordination.redis_client import RedisClient, redis_text
from pydantic import Field, field_validator, model_validator
from shared.contracts import ContractModel
from shared.image_building.credentials import normalize_registry_host
from worker.repository_payloads import ImageBuildPrivateInputs

DEFAULT_IMAGE_BUILD_CREDENTIAL_TTL_SECONDS = 5 * 60
DEFAULT_IMAGE_BUILD_UPLOAD_CAPABILITY_TTL_SECONDS = 5 * 60
IMAGE_BUILD_CREDENTIAL_BINDING_MISMATCH = "image-build-credential-binding-mismatch"

_CONSUME_SCRIPT = """
local value = redis.call("GET", KEYS[1])
if not value then
    return nil
end
local lease = cjson.decode(value)
if lease.workspace_id ~= ARGV[1]
    or lease.build_id ~= ARGV[2]
    or lease.container_id ~= ARGV[3]
    or lease.registry ~= ARGV[4] then
    return ARGV[5]
end
redis.call("DEL", KEYS[1])
return value
"""


class ImageBuildCredentialStore(Protocol):
    def put(self, cache_key: str, lease: ImageBuildCredentialLease) -> None: ...

    def delete(self, cache_key: str) -> None: ...


class ImageBuildCredentialLease(ContractModel):
    workspace_id: str
    build_id: str
    container_id: str
    registry: str
    private_inputs: ImageBuildPrivateInputs = Field(repr=False)

    @field_validator("registry")
    @classmethod
    def normalize_bound_registry(cls, value: str) -> str:
        return normalize_registry_host(value)

    @model_validator(mode="after")
    def require_matching_registry_auth(self) -> ImageBuildCredentialLease:
        registry_auth = self.private_inputs.registry_auth
        if registry_auth is not None and registry_auth.registry != self.registry:
            raise ValueError("image build registry auth does not match lease registry")
        return self


@dataclass(slots=True)
class RedisImageBuildCredentialCache:
    redis: RedisClient
    ttl_seconds: int = DEFAULT_IMAGE_BUILD_CREDENTIAL_TTL_SECONDS

    def put(self, cache_key: str, lease: ImageBuildCredentialLease) -> None:
        if not cache_key:
            raise ValueError("image build credential cache key is required")
        if lease.private_inputs.empty:
            raise ValueError("image build private inputs are required")
        created = self.redis.set(
            self._key(cache_key),
            lease.model_dump_json(),
            ex=max(self.ttl_seconds, 1),
            nx=True,
        )
        if not created:
            raise RuntimeError("image build credential capability already exists")

    def consume(
        self,
        cache_key: str,
        *,
        workspace_id: str,
        build_id: str,
        container_id: str,
        registry: str,
    ) -> ImageBuildPrivateInputs | None:
        if not cache_key:
            return None
        value = self.redis.eval_scalar(
            _CONSUME_SCRIPT,
            1,
            self._key(cache_key),
            workspace_id,
            build_id,
            container_id,
            normalize_registry_host(registry),
            IMAGE_BUILD_CREDENTIAL_BINDING_MISMATCH,
        )
        if value is None:
            return None
        serialized = redis_text(value)
        if serialized == IMAGE_BUILD_CREDENTIAL_BINDING_MISMATCH:
            raise PermissionError("image build credential lease binding does not match request")
        return ImageBuildCredentialLease.model_validate_json(serialized).private_inputs

    def delete(self, cache_key: str) -> None:
        if cache_key:
            self.redis.delete(self._key(cache_key))

    def _key(self, cache_key: str) -> str:
        return self.redis.key("image-build-credentials", cache_key)


@dataclass(slots=True)
class RedisImageBuildUploadCapabilityGuard:
    redis: RedisClient
    ttl_seconds: int = DEFAULT_IMAGE_BUILD_UPLOAD_CAPABILITY_TTL_SECONDS

    def consume(self, capability: str) -> bool:
        if not capability:
            return False
        return self.redis.set(
            self.redis.key("image-build-upload-capabilities", capability),
            "consumed",
            ex=max(self.ttl_seconds, 1),
            nx=True,
        )


__all__ = [
    "DEFAULT_IMAGE_BUILD_CREDENTIAL_TTL_SECONDS",
    "DEFAULT_IMAGE_BUILD_UPLOAD_CAPABILITY_TTL_SECONDS",
    "IMAGE_BUILD_CREDENTIAL_BINDING_MISMATCH",
    "ImageBuildCredentialLease",
    "ImageBuildCredentialStore",
    "RedisImageBuildCredentialCache",
    "RedisImageBuildUploadCapabilityGuard",
]
