"""Where finished jobs live in the gallery volume, and how the studio reads them.

Each job is a directory named by its task ID, holding its images and a
`job.json` written after the last image.
"""

from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from image_studio.generation import Aspect, Style

ENTRY_FILE = "job.json"


class GalleryEntry(BaseModel):
    job_id: str
    prompt: str
    style: Style
    aspect: Aspect
    seed: int
    image_count: int
    created_at: datetime


def job_directory(root: Path, job_id: str) -> Path:
    """The job's directory; anything but a task ID raises ValueError, so no path escapes."""
    return root / str(UUID(job_id))


def image_path(root: Path, job_id: str, index: int) -> Path:
    return job_directory(root, job_id) / f"{index}.webp"


def write_entry(root: Path, entry: GalleryEntry) -> None:
    (job_directory(root, entry.job_id) / ENTRY_FILE).write_text(entry.model_dump_json())


def newest_entries(root: Path, limit: int) -> list[GalleryEntry]:
    entries = [
        GalleryEntry.model_validate_json(path.read_text()) for path in root.glob(f"*/{ENTRY_FILE}")
    ]
    entries.sort(key=lambda entry: entry.created_at, reverse=True)
    return entries[:limit]


def expired_directories(root: Path, *, now: datetime, keep_for: timedelta) -> list[Path]:
    """Job directories older than `keep_for`.

    A job that failed before writing its entry is aged by the directory itself.
    """
    expired: list[Path] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        entry_file = directory / ENTRY_FILE
        if entry_file.exists():
            created = GalleryEntry.model_validate_json(entry_file.read_text()).created_at
        else:
            created = datetime.fromtimestamp(directory.stat().st_mtime, now.tzinfo)
        if now - created > keep_for:
            expired.append(directory)
    return expired
