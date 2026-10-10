"""The app, the image its CPU workloads share, and the storage its workloads name."""

from pathlib import Path

from lazycloud import App, Image, Map, Secret, Volume

app = App("image_studio")

# The project's own dependencies, without the GPU group.
app_image = Image.from_uv(".")

MODELS_DIR = Path("/models")
GALLERY_DIR = Path("/gallery")

models = Volume("image-studio-models", str(MODELS_DIR))
gallery = Volume("image-studio-gallery", str(GALLERY_DIR))
job_progress = Map("image-studio-progress")
access_key = Secret("IMAGE_STUDIO_KEY")
