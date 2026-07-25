"""Transfer one object through presigned and multipart Volume APIs.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario creates and publicly deletes one uniquely named Volume.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence

import httpx
from lazycloud.abstractions.volume import CompletedPart
from lazycloud.cli.control import volume_client
from shared.http.volumes import PresignedUrlMethod
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

from lazycloud import Volume


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Volume transfer")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    workspace = profile.workspace
    name = f"volume-transfer-{secrets.token_hex(6)}"
    control = volume_client(workspace=workspace, timeout_seconds=60)
    volume = Volume(name, workspace=workspace)
    try:
        created = control.create(name)
        if created.volume is None or created.volume.name != name:
            raise RuntimeError("Volume transfer scenario did not create its resource")
        payload = f"presigned-{secrets.token_hex(12)}".encode()
        put = volume.presigned_url(
            "presigned/value.txt",
            method=PresignedUrlMethod.PutObject,
            content_length=len(payload),
            content_type="text/plain",
        )
        response = httpx.put(
            put.url,
            content=payload,
            headers={"Content-Type": "text/plain"},
            timeout=60,
        )
        response.raise_for_status()
        if volume.read_bytes("presigned/value.txt") != payload:
            raise RuntimeError("presigned Volume transfer returned the wrong bytes")

        multipart_payload = (b"multipart-volume-transfer-" * 300_000)[: 6 * 1024 * 1024]
        plan = volume.create_multipart_upload(
            "multipart/value.bin",
            file_size=len(multipart_payload),
            chunk_size=5 * 1024 * 1024,
        )
        completed: list[CompletedPart] = []
        for part in plan.parts:
            uploaded = httpx.put(
                part.url,
                content=multipart_payload[part.start : part.end],
                timeout=60,
            )
            uploaded.raise_for_status()
            etag = uploaded.headers.get("ETag", "").strip('"')
            if not etag:
                raise RuntimeError("multipart Volume upload returned no ETag")
            completed.append(CompletedPart(number=part.number, etag=etag))
        volume.complete_multipart_upload(plan.upload_id, plan.volume_path, completed)
        if volume.read_bytes(plan.volume_path) != multipart_payload:
            raise RuntimeError("multipart Volume transfer returned the wrong bytes")
        print(json.dumps({"capability": "storage.volume-transfer", "volume": name}))
    finally:
        if any(item.name == name for item in control.list_volumes().volumes):
            control.delete(name)
        if any(item.name == name for item in control.list_volumes().volumes):
            raise RuntimeError("Volume transfer cleanup left its unique resource")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
