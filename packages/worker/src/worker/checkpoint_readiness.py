from __future__ import annotations

import http.client

from pydantic import Field
from shared.contracts import ContractModel


class CheckpointReadinessProbe(ContractModel):
    path: str = Field(pattern=r"^/")
    port: int = Field(ge=1, le=65535)
    timeout_seconds: float = Field(gt=0)

    def assert_ready(self, container_ip: str) -> None:
        if not container_ip:
            raise RuntimeError("checkpoint readiness requires the container's network address")
        connection = http.client.HTTPConnection(
            container_ip, self.port, timeout=self.timeout_seconds
        )
        try:
            connection.request("GET", self.path)
            status = connection.getresponse().status
        except OSError as exc:
            raise RuntimeError(f"checkpoint readiness probe {self.path} failed: {exc}") from exc
        finally:
            connection.close()
        if not 200 <= status < 400:
            raise RuntimeError(f"checkpoint readiness probe {self.path} returned HTTP {status}")
