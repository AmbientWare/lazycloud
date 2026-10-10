"""Generate images with FLUX.2 [klein] 4B on a GPU and save them to the gallery."""

import secrets
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING

from lazycloud import GpuType, Image, current_task_id

from image_studio.gallery import GalleryEntry, image_path, job_directory, write_entry
from image_studio.generation import ASPECT_SIZES, STEPS_PER_IMAGE, GenerateRequest
from image_studio.progress import JobProgress, JobStage
from image_studio.resources import GALLERY_DIR, app, gallery, job_progress, models
from image_studio.weights import COMPLETE_MARKER, WEIGHTS_DIR, WeightsMissingError

if TYPE_CHECKING:
    from diffusers import Flux2KleinPipeline

gpu_image = Image.from_uv(".", groups=["gpu"])
PROGRESS_TTL_SECONDS = 24 * 60 * 60


@cache
def pipeline() -> "Flux2KleinPipeline":
    import torch
    from diffusers import Flux2KleinPipeline

    if not COMPLETE_MARKER.exists():
        raise WeightsMissingError(
            f"no weights in {WEIGHTS_DIR}; run `uv run python -m image_studio.configure` first"
        )
    return Flux2KleinPipeline.from_pretrained(
        WEIGHTS_DIR, torch_dtype=torch.bfloat16, device_map="cuda"
    )


def load_pipeline(_context: object) -> None:
    pipeline()


@app.function(
    image=gpu_image,
    gpu=[GpuType.L4, GpuType.A10G],
    cpu=2,
    memory="12Gi",
    volumes=[models, gallery],
    on_start=load_pipeline,
    keep_warm=300,
    timeout_seconds=300,
    retries=0,
    max_pending_tasks=16,
)
def generate_images(request: GenerateRequest) -> GalleryEntry:
    import torch

    job_id = current_task_id()
    seed = request.seed if request.seed is not None else secrets.randbelow(2**32)
    width, height = ASPECT_SIZES[request.aspect]
    total_steps = request.count * STEPS_PER_IMAGE
    completed_steps = 0

    def report(stage: JobStage, error: str | None = None) -> None:
        progress = JobProgress(
            stage=stage, completed_steps=completed_steps, total_steps=total_steps, error=error
        )
        job_progress.set(job_id, progress.model_dump(mode="json"), ttl=PROGRESS_TTL_SECONDS)

    def on_step_end(_pipe: object, _step: int, _timestep: object, tensors: dict) -> dict:
        nonlocal completed_steps
        completed_steps += 1
        report(JobStage.GENERATING)
        return tensors

    report(JobStage.GENERATING)
    try:
        job_directory(GALLERY_DIR, job_id).mkdir(exist_ok=True)
        for index in range(request.count):
            image = pipeline()(
                prompt=request.full_prompt(),
                width=width,
                height=height,
                num_inference_steps=STEPS_PER_IMAGE,
                guidance_scale=1.0,
                generator=torch.Generator("cuda").manual_seed(seed + index),
                callback_on_step_end=on_step_end,
            ).images[0]
            image.save(image_path(GALLERY_DIR, job_id, index), format="WEBP", quality=92)
        entry = GalleryEntry(
            job_id=job_id,
            prompt=request.prompt,
            style=request.style,
            aspect=request.aspect,
            seed=seed,
            image_count=request.count,
            created_at=datetime.now(UTC),
        )
        write_entry(GALLERY_DIR, entry)
    except Exception as error:
        report(JobStage.FAILED, error=f"{type(error).__name__}: {error}"[:300])
        raise
    report(JobStage.DONE)
    return entry
