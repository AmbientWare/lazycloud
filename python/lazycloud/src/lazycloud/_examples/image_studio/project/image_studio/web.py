"""The studio's one CPU deployment: the static Next.js UI and the API it calls."""

import asyncio
import os
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, WebSocket, status
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from lazycloud import Image, Task
from lazycloud.exceptions import SdkError
from pydantic import BaseModel

from image_studio.auth import bearer_matches, sign_path, signature_valid
from image_studio.gallery import GalleryEntry, image_path, newest_entries
from image_studio.generate import generate_images
from image_studio.generation import (
    ASPECT_SIZES,
    MAX_IMAGES_PER_JOB,
    MAX_PROMPT_CHARS,
    Aspect,
    GenerateRequest,
    Style,
)
from image_studio.progress import FINISHED_STAGES, JobEvent, JobProgress, describe_job
from image_studio.resources import GALLERY_DIR, access_key, app, gallery, job_progress
from image_studio.share import ShareLink, share_image

STATIC_DIR = Path("/opt/image-studio/web")
SIGNED_URL_SECONDS = 60 * 60
GALLERY_LIMIT = 48
EVENTS_POLL_SECONDS = 1.0
EVENTS_MAX_POLLS = 600

web_image = (
    Image.from_uv(".").add_local_path("web").add_commands([f"python web/build.py {STATIC_DIR}"])
)

ImageIndex = Annotated[int, PathParam(ge=0, lt=MAX_IMAGES_PER_JOB)]


class Choice(BaseModel):
    id: str
    label: str


class Options(BaseModel):
    styles: list[Choice]
    aspects: list[Choice]
    max_prompt_chars: int
    max_images: int


class JobCreated(BaseModel):
    job_id: str
    events_url: str


class GalleryItem(GalleryEntry):
    image_urls: list[str]


def studio_key() -> str:
    return os.environ[access_key.name]


def require_key(authorization: Annotated[str | None, Header()] = None) -> None:
    if not bearer_matches(authorization, studio_key()):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "a valid studio key is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def signed(path: str) -> str:
    return sign_path(studio_key(), path, now=time.time(), ttl_seconds=SIGNED_URL_SECONDS)


def valid_signature(path: str, expires: int, signature: str) -> bool:
    return signature_valid(
        studio_key(), path, expires=expires, signature=signature, now=time.time()
    )


def gallery_item(entry: GalleryEntry) -> GalleryItem:
    urls = [signed(f"/api/jobs/{entry.job_id}/images/{n}") for n in range(entry.image_count)]
    return GalleryItem(**entry.model_dump(), image_urls=urls)


def existing_image(job_id: str, index: int) -> Path:
    try:
        path = image_path(GALLERY_DIR, job_id, index)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such image") from None
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such image")
    return path


# Routes that spend compute or reveal the gallery take the studio key as a
# bearer token. The browser stores it after the user types it once.
keyed = APIRouter(prefix="/api", dependencies=[Depends(require_key)])


@keyed.get("/options")
def options() -> Options:
    return Options(
        styles=[Choice(id=s, label=s.replace("-", " ").capitalize()) for s in Style],
        aspects=[
            Choice(id=a, label=f"{a.capitalize()} {ASPECT_SIZES[a][0]}x{ASPECT_SIZES[a][1]}")
            for a in Aspect
        ],
        max_prompt_chars=MAX_PROMPT_CHARS,
        max_images=MAX_IMAGES_PER_JOB,
    )


@keyed.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_job(request: GenerateRequest) -> JobCreated:
    try:
        call = generate_images.spawn(request)
    except SdkError as error:
        # Raised when the GPU queue is past max_pending_tasks, among other refusals.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
    return JobCreated(job_id=call.task_id, events_url=signed(f"/api/jobs/{call.task_id}/events"))


@keyed.get("/gallery")
def list_gallery() -> list[GalleryItem]:
    return [gallery_item(entry) for entry in newest_entries(GALLERY_DIR, GALLERY_LIMIT)]


@keyed.post("/jobs/{job_id}/images/{index}/share")
def share(job_id: str, index: ImageIndex) -> ShareLink:
    existing_image(job_id, index)
    try:
        return share_image.spawn(job_id, index).get(timeout_seconds=60)
    except (SdkError, TimeoutError) as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error


# An <img> tag or a WebSocket can't send a header, so these routes take a
# signature the keyed routes issued instead.
public = APIRouter(prefix="/api")


@public.get("/jobs/{job_id}/images/{index}")
def image(job_id: str, index: ImageIndex, expires: int, signature: str) -> FileResponse:
    if not valid_signature(f"/api/jobs/{job_id}/images/{index}", expires, signature):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the image link is invalid or expired")
    return FileResponse(
        existing_image(job_id, index),
        media_type="image/webp",
        headers={"Cache-Control": f"private, max-age={SIGNED_URL_SECONDS}, immutable"},
    )


@public.websocket("/jobs/{job_id}/events")
async def job_events(websocket: WebSocket, job_id: str, expires: int, signature: str) -> None:
    if not valid_signature(f"/api/jobs/{job_id}/events", expires, signature):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    sending = asyncio.create_task(send_events(websocket, Task.from_id(job_id)))
    # The browser sends nothing, so a receive ends only when it leaves.
    client_left = asyncio.create_task(websocket.receive())
    done, pending = await asyncio.wait({sending, client_left}, return_when=asyncio.FIRST_COMPLETED)
    for unfinished in pending:
        unfinished.cancel()
    if pending:
        await asyncio.wait(pending)
    if sending in done:
        sending.result()
        await websocket.close()


async def send_events(websocket: WebSocket, task: Task) -> None:
    last: JobEvent | None = None
    for _ in range(EVENTS_MAX_POLLS):
        event = await asyncio.to_thread(read_event, task)
        if event != last:
            await websocket.send_json(event.model_dump(mode="json"))
            last = event
        if event.stage in FINISHED_STAGES:
            return
        await asyncio.sleep(EVENTS_POLL_SECONDS)


def read_event(task: Task) -> JobEvent:
    raw = job_progress.get(task.task_id)
    progress = JobProgress.model_validate(raw) if raw is not None else None
    view = task.view()
    return describe_job(
        progress,
        task_status=view.status.value,
        pending_message=view.pending.message if view.pending else None,
    )


api = FastAPI(title="Image studio")
api.include_router(keyed)
api.include_router(public)
# The Next.js export, built into the image; mounted last so /api routes match first.
api.mount("/", StaticFiles(directory=STATIC_DIR, html=True, check_dir=False), name="ui")

studio = app.asgi(
    name="studio",
    image=web_image,
    cpu=0.25,
    memory="512Mi",
    concurrent_requests=32,
    keep_warm_seconds=120,
    # A browser can't send a platform token, so the studio key guards the API instead.
    authorized=False,
    secrets=[access_key.name],
    volumes=[gallery],
)(api)
