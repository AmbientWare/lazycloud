from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

from pydantic import JsonValue
from shared.http.ssh import SshCertificateRequest, SshCertificateResponse, SshHostKeyResponse
from shared.http_transport import HttpChannel
from shared.urls import url_path_segment

from lazycloud.control import workspace_query


class SshControlChannel(Protocol):
    def get(self, path: str) -> JsonValue: ...

    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue: ...


@dataclass(slots=True)
class SshControlClient:
    channel: SshControlChannel
    endpoint: str
    workspace: str = ""

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "",
    ) -> SshControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            endpoint=endpoint,
            workspace=workspace,
        )

    def create_certificate(self, public_key: str) -> SshCertificateResponse:
        request = SshCertificateRequest(public_key=public_key)
        return SshCertificateResponse.model_validate(
            self.channel.post(
                self._path("/api/v1/ssh/certificates"),
                request.model_dump(mode="json"),
            )
        )

    def host_key(self, pod: str, *, app: str) -> SshHostKeyResponse:
        return SshHostKeyResponse.model_validate(
            self.channel.get(self._pod_path(pod, "/ssh/host-key", app=app))
        )

    def tunnel_url(self, pod: str, *, app: str) -> str:
        parsed = urlsplit(self.endpoint.rstrip("/") + self._pod_path(pod, "/ssh", app=app))
        schemes = {"http": "ws", "https": "wss"}
        if parsed.scheme not in schemes:
            msg = f"unsupported control endpoint scheme: {parsed.scheme or '<missing>'}"
            raise ValueError(msg)
        return urlunsplit((schemes[parsed.scheme], parsed.netloc, parsed.path, parsed.query, ""))

    def _pod_path(self, pod: str, suffix: str, *, app: str) -> str:
        query = urlencode({"app": app, **workspace_query(self.workspace)})
        return f"/api/v1/pods/{url_path_segment(pod)}{suffix}?{query}"

    def _path(self, path: str) -> str:
        query = workspace_query(self.workspace)
        return f"{path}?{urlencode(query)}" if query else path


__all__ = ["SshControlChannel", "SshControlClient"]
