from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from worker.repository_payloads import (
    GetImageBuildCredentialsRequest,
    GetImageBuildCredentialsResponse,
    ImageBuildPrivateInputs,
)


class ImageBuildCredentialRepositoryClient(Protocol):
    def get_image_build_credentials(
        self,
        request: GetImageBuildCredentialsRequest,
    ) -> GetImageBuildCredentialsResponse: ...


class ImageBuildCredentialLoader(Protocol):
    def load(
        self,
        *,
        workspace_id: str,
        build_id: str,
        container_id: str,
        registry: str,
        cache_key: str,
    ) -> ImageBuildPrivateInputs: ...


@dataclass(slots=True)
class RemoteImageBuildCredentialLoader:
    repository: ImageBuildCredentialRepositoryClient

    def load(
        self,
        *,
        workspace_id: str,
        build_id: str,
        container_id: str,
        registry: str,
        cache_key: str,
    ) -> ImageBuildPrivateInputs:
        request = GetImageBuildCredentialsRequest(
            workspace_id=workspace_id,
            build_id=build_id,
            container_id=container_id,
            registry=registry,
            cache_key=cache_key,
        )
        response = self.repository.get_image_build_credentials(request)
        return response.private_inputs or ImageBuildPrivateInputs()
