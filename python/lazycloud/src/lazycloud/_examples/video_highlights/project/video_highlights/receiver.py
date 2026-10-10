"""The webhook your app adds to hear when a highlight run finishes.

LazyCloud signs each callback with the workspace's callback signing key. This
route accepts only callbacks whose signature matches and whose timestamp is
recent, so the URL can stay public.
"""

import base64
import hashlib
import hmac
import os
import time
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, JsonValue

from video_highlights.manifest import HighlightRun

SIGNING_KEY = "LAZYCLOUD_CALLBACK_SIGNING_KEY"
MAX_BODY_BYTES = 512 * 1024
MAX_CLOCK_SKEW_SECONDS = 300


class TaskResult(BaseModel):
    encoding: str
    value: JsonValue = None


class TaskError(BaseModel):
    kind: str
    message: str


class TaskEvent(BaseModel):
    task_id: str
    status: Literal["retry", "succeeded", "failed", "cancelled"]
    attempt_number: int
    data: TaskResult | None = None
    error: TaskError | None = None


def signature_matches(key: str, body: bytes, timestamp: str, signature: str, now: float) -> bool:
    """Check X-Task-Signature: hex HMAC-SHA256 of the base64 body, a colon and the timestamp."""
    if not timestamp.isdigit() or abs(now - int(timestamp)) > MAX_CLOCK_SKEW_SECONDS:
        return False
    message = base64.b64encode(body) + b":" + timestamp.encode()
    expected = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def describe(event: TaskEvent) -> str:
    match event.status:
        case "succeeded" if event.data is not None and event.data.encoding == "json":
            run = HighlightRun.model_validate(event.data.value)
            manifest = run.manifest
            return f"{manifest.video_key}: {len(manifest.clips)} clips, {run.manifest_url}"
        case "succeeded":
            return f"task {event.task_id} succeeded; read its result with lazycloud task result"
        case "retry":
            return f"task {event.task_id} attempt {event.attempt_number} failed and will retry"
        case "failed" | "cancelled":
            reason = event.error.message if event.error else "no error recorded"
            return f"task {event.task_id} {event.status}: {reason}"


api = FastAPI(title="Highlight run notifications")


@api.post("/video-highlights", status_code=204)
async def run_finished(
    request: Request,
    x_task_signature: Annotated[str, Header()],
    x_task_timestamp: Annotated[str, Header()],
) -> None:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "callback body too large")
    key = os.environ[SIGNING_KEY]
    if not signature_matches(key, bytes(body), x_task_timestamp, x_task_signature, time.time()):
        raise HTTPException(401, "signature does not match")
    # Your app would store the manifest or notify someone; this prints it.
    print(describe(TaskEvent.model_validate_json(body)), flush=True)
