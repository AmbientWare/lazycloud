from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import worker.checkpoint_transfer as checkpoint_transfer
from networking.internal_http import InternalHttpClient


def test_checkpoint_transfer_errors_never_disclose_capability_query(tmp_path: Path) -> None:
    sentinel = "never-log-this-checkpoint-signature"

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.RequestError(f"failed request with {sentinel}", request=request)

    http = InternalHttpClient()
    capability = f"https://objects.example.test/archive?X-Amz-Signature={sentinel}"
    checkpoint = tmp_path / "checkpoint.tar"
    checkpoint.write_bytes(b"checkpoint")

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        http._client = client
        with pytest.raises(RuntimeError) as upload_error:
            checkpoint_transfer._put_presigned_checkpoint_archive(
                http,
                capability,
                checkpoint,
                content_length=checkpoint.stat().st_size,
            )
        with pytest.raises(RuntimeError) as download_error:
            checkpoint_transfer._download_presigned_url(
                http,
                capability,
                tmp_path / "download.tar",
                timeout_seconds=5,
                resource_name="checkpoint archive",
            )

    assert sentinel not in str(upload_error.value)
    assert sentinel not in str(download_error.value)
    assert upload_error.value.__cause__ is None
    assert download_error.value.__cause__ is None
