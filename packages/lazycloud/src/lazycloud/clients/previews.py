from __future__ import annotations

from dataclasses import dataclass

from shared.http.previews import CreatePreviewRequest, PreviewSessionResponse
from shared.http_transport import HttpChannel

from lazycloud.control import ControlClientConfig, workspace_path


@dataclass(slots=True)
class PreviewControlClient:
    channel: HttpChannel
    workspace: str

    @classmethod
    def from_config(cls, config: ControlClientConfig) -> PreviewControlClient:
        return cls(
            channel=HttpChannel(
                endpoint=config.endpoint,
                token=config.token,
                timeout_seconds=config.timeout_seconds,
            ),
            workspace=config.workspace,
        )

    def create(self, stub_id: str, *, timeout: int = 0) -> PreviewSessionResponse:
        return PreviewSessionResponse.model_validate(
            self.channel.post(
                workspace_path("/api/v1/previews", self.workspace),
                CreatePreviewRequest(stub_id=stub_id, timeout=timeout).model_dump(mode="json"),
            )
        )

    def get_preview(self, preview_id: str) -> PreviewSessionResponse:
        return PreviewSessionResponse.model_validate(
            self.channel.get(workspace_path(f"/api/v1/previews/{preview_id}", self.workspace))
        )

    def renew(self, preview_id: str) -> PreviewSessionResponse:
        return PreviewSessionResponse.model_validate(
            self.channel.post(
                workspace_path(f"/api/v1/previews/{preview_id}/heartbeat", self.workspace), {}
            )
        )

    def stop(self, preview_id: str) -> None:
        self.channel.delete(workspace_path(f"/api/v1/previews/{preview_id}", self.workspace))
