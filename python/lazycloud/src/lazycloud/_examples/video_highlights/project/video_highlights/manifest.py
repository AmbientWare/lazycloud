"""What a finished run hands back: the plan plus a share link per clip."""

from pydantic import BaseModel

from video_highlights.highlights import Chapter, Highlight


class Clip(BaseModel):
    number: int
    title: str
    reason: str
    start_seconds: float
    end_seconds: float
    video_url: str
    thumbnail_url: str


class HighlightManifest(BaseModel):
    video_key: str
    duration_seconds: float
    language: str
    model: str
    summary: str
    chapters: list[Chapter]
    clips: list[Clip]
    # Picks whose clip failed after its retries; the rest of the run still counts.
    failed_highlights: list[Highlight]


class HighlightRun(BaseModel):
    manifest_url: str
    manifest: HighlightManifest
