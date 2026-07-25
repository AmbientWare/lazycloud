"""Typed access to the prepared Tailscale node status."""

from __future__ import annotations

import subprocess

from pydantic import BaseModel, ConfigDict, Field


class TailnetSelf(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str = Field(alias="ID")
    online: bool = Field(alias="Online", default=False)
    addresses: tuple[str, ...] | None = Field(alias="TailscaleIPs", default=None)


class TailnetStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    self_node: TailnetSelf = Field(alias="Self")


def tailscale_status(socket: str) -> TailnetStatus:
    completed = subprocess.run(
        ["tailscale", f"--socket={socket}", "status", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "prepared Tailnet node is unavailable")
    try:
        return TailnetStatus.model_validate_json(completed.stdout)
    except ValueError as exc:
        raise RuntimeError("tailscale status returned an invalid object") from exc


__all__ = ["TailnetSelf", "TailnetStatus", "tailscale_status"]
