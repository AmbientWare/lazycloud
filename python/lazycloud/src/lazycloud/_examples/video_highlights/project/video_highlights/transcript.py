"""The transcript every later step reads."""

from pydantic import BaseModel


class Segment(BaseModel):
    start: float
    end: float
    text: str


class Transcript(BaseModel):
    language: str
    duration_seconds: float
    segments: list[Segment]

    def timed_text(self) -> str:
        """One line per segment, prefixed with its start and end in seconds."""
        return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in self.segments)
