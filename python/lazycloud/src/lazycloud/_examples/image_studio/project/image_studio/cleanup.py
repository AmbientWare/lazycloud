"""Delete gallery jobs older than the retention window, every night."""

import shutil
from datetime import UTC, datetime, timedelta

from image_studio.gallery import expired_directories
from image_studio.resources import GALLERY_DIR, app, app_image, gallery

KEEP_IMAGES_FOR = timedelta(days=7)


@app.function(
    image=app_image,
    cron="0 4 * * *",
    cpu=0.25,
    memory="256Mi",
    volumes=[gallery],
    timeout_seconds=900,
    retries=2,
)
def delete_expired_images() -> int:
    expired = expired_directories(GALLERY_DIR, now=datetime.now(UTC), keep_for=KEEP_IMAGES_FOR)
    for directory in expired:
        shutil.rmtree(directory)
    print(f"Deleted {len(expired)} jobs older than {KEEP_IMAGES_FOR.days} days", flush=True)
    return len(expired)
