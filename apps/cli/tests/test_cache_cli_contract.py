from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cli.main import build_admin_cli
from shared.http.storage import CacheContentResponse, CacheEntryResponse
from typer.testing import CliRunner

from cli import storage

cli = build_admin_cli()


@dataclass(slots=True)
class _AdminApi:
    response: CacheContentResponse

    def read_cache_entry(self, namespace: str, key: str) -> CacheContentResponse:
        assert (namespace, key) == ("acceptance", "canonical")
        return self.response


def test_cache_get_downloads_remote_binary_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    payload = b"\x00\xffremote-cache"
    response = CacheContentResponse.from_content(
        entry=CacheEntryResponse(
            key="a" * 64,
            size=len(payload),
            sha256="b" * 64,
            created_at=now,
            updated_at=now,
        ),
        data=payload,
    )
    monkeypatch.setattr(storage, "admin_api_client", lambda: _AdminApi(response))
    destination = tmp_path / "nested" / "artifact.bin"

    result = CliRunner().invoke(
        cli,
        ["cache", "get", "acceptance", "canonical", str(destination)],
    )

    assert result.exit_code == 0, result.output
    assert destination.read_bytes() == payload
    assert str(destination) in result.output
