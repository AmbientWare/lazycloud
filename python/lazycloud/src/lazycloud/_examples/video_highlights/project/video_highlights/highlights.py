"""The chapters and highlights a model proposes, and the rules that make them usable."""

from pydantic import BaseModel

from video_highlights.transcript import Segment, Transcript

MAX_HIGHLIGHTS = 10
MIN_CLIP_SECONDS = 10.0
MAX_CLIP_SECONDS = 90.0
# Video sites that show chapters want the first at 0:00 and none under 10 seconds.
MIN_CHAPTER_SECONDS = 10.0


class Chapter(BaseModel):
    start_seconds: float
    title: str


class Highlight(BaseModel):
    start_seconds: float
    end_seconds: float
    title: str
    reason: str


class HighlightPlan(BaseModel):
    summary: str
    chapters: list[Chapter]
    highlights: list[Highlight]


def normalize_plan(
    plan: HighlightPlan, transcript: Transcript, max_highlights: int
) -> HighlightPlan:
    """Keep the picks that fit the video, cut on whole segments, best first."""
    highlights: list[Highlight] = []
    for pick in plan.highlights:
        snapped = snap_to_segments(pick, transcript.segments)
        if snapped is None or any(overlaps(snapped, kept) for kept in highlights):
            continue
        highlights.append(snapped)
        if len(highlights) == max_highlights:
            break
    return HighlightPlan(
        summary=plan.summary.strip(),
        chapters=clean_chapters(plan.chapters, transcript.duration_seconds),
        highlights=highlights,
    )


def snap_to_segments(pick: Highlight, segments: list[Segment]) -> Highlight | None:
    """Widen a pick to the segments it touches, so no clip starts or ends mid-sentence.

    A pick longer than MAX_CLIP_SECONDS ends at the last segment that fits; one
    shorter than MIN_CLIP_SECONDS, or touching no speech, is dropped.
    """
    touched = [s for s in segments if s.end > pick.start_seconds and s.start < pick.end_seconds]
    if not touched:
        return None
    start = touched[0].start
    end = touched[-1].end
    if end - start > MAX_CLIP_SECONDS:
        end = max(
            (s.end for s in touched if s.end - start <= MAX_CLIP_SECONDS),
            default=start + MAX_CLIP_SECONDS,
        )
    if end - start < MIN_CLIP_SECONDS:
        return None
    return pick.model_copy(update={"start_seconds": start, "end_seconds": end})


def overlaps(a: Highlight, b: Highlight) -> bool:
    return a.start_seconds < b.end_seconds and b.start_seconds < a.end_seconds


def clean_chapters(chapters: list[Chapter], duration_seconds: float) -> list[Chapter]:
    kept: list[Chapter] = []
    for chapter in sorted(chapters, key=lambda c: c.start_seconds):
        title = chapter.title.strip()
        if not title or not 0 <= chapter.start_seconds <= duration_seconds - MIN_CHAPTER_SECONDS:
            continue
        if kept and chapter.start_seconds - kept[-1].start_seconds < MIN_CHAPTER_SECONDS:
            continue
        kept.append(Chapter(start_seconds=chapter.start_seconds, title=title))
    if kept:
        kept[0] = kept[0].model_copy(update={"start_seconds": 0.0})
    return kept
