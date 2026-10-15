"""Download FLUX.2 [klein] 4B into the models volume once, on a small CPU container."""

from lazycloud import Image

from image_studio.resources import MODELS_DIR, app, models

MODEL_REPO = "black-forest-labs/FLUX.2-klein-4B"
MODEL_REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"
WEIGHTS_DIR = MODELS_DIR / "flux2-klein-4b" / MODEL_REVISION
# Written after the last file lands, so no GPU container loads a partial download.
COMPLETE_MARKER = WEIGHTS_DIR / ".complete"
# The diffusers layout only; the repository also holds a single-file copy of the transformer.
WEIGHT_FILES = [
    "model_index.json",
    "scheduler/*",
    "text_encoder/*",
    "tokenizer/*",
    "transformer/*",
    "vae/*",
]

weights_image = Image.from_uv(".", groups=["weights"])


class WeightsMissingError(RuntimeError):
    pass


@app.function(
    image=weights_image,
    cpu=2,
    memory="4Gi",
    volumes=[models],
    timeout_seconds=3600,
    retries=2,
)
def download_weights() -> str:
    from huggingface_hub import snapshot_download

    if COMPLETE_MARKER.exists():
        return f"{MODEL_REPO} is already in {WEIGHTS_DIR}"
    snapshot_download(
        MODEL_REPO,
        revision=MODEL_REVISION,
        local_dir=WEIGHTS_DIR,
        allow_patterns=WEIGHT_FILES,
    )
    COMPLETE_MARKER.write_text(MODEL_REVISION)
    return f"Downloaded {MODEL_REPO} to {WEIGHTS_DIR}"
