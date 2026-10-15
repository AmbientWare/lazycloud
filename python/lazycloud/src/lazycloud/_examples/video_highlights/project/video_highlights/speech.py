"""Transcribe audio with faster-whisper on a GPU."""

from functools import cache
from pathlib import Path

from faster_whisper import BatchedInferencePipeline, WhisperModel

from video_highlights.transcript import Segment, Transcript


@cache
def load_model(model_dir: str) -> BatchedInferencePipeline:
    """Load the weights once per container; later calls reuse them."""
    return BatchedInferencePipeline(WhisperModel(model_dir, device="cuda", compute_type="float16"))


def transcribe_file(model_dir: str, audio: Path) -> Transcript:
    segments, info = load_model(model_dir).transcribe(str(audio), batch_size=16)
    return Transcript(
        language=info.language,
        duration_seconds=info.duration,
        segments=[
            Segment(start=segment.start, end=segment.end, text=text)
            for segment in segments
            if (text := segment.text.strip())
        ],
    )
