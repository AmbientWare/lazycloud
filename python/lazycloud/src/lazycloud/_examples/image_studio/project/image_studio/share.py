"""Turn a gallery image into a public link that expires, saved as an artifact."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from lazycloud import Artifact
from pydantic import BaseModel

from image_studio.gallery import image_path
from image_studio.resources import GALLERY_DIR, app, app_image, gallery

SHARE_LINK_SECONDS = 7 * 24 * 60 * 60


class ShareLink(BaseModel):
    url: str
    expires_at: datetime


@app.function(
    image=app_image,
    cpu=0.25,
    memory="256Mi",
    volumes=[gallery],
    timeout_seconds=60,
    retries=2,
)
def share_image(job_id: str, index: int) -> ShareLink:
    with TemporaryDirectory() as scratch:
        # The artifact keeps the file's name, which becomes the download's name.
        named = Path(scratch) / f"image-studio-{job_id[:8]}-{index + 1}.webp"
        shutil.copyfile(image_path(GALLERY_DIR, job_id, index), named)
        artifact = Artifact.file(named, content_type="image/webp")
        saved = artifact.save()
    url = artifact.public_url(expires=SHARE_LINK_SECONDS)
    # The platform caps a link at the artifact's retention, which depends on the plan.
    expires_at = datetime.now(UTC) + timedelta(seconds=SHARE_LINK_SECONDS)
    if saved.expires_at is not None:
        expires_at = min(expires_at, saved.expires_at)
    return ShareLink(url=url, expires_at=expires_at)
