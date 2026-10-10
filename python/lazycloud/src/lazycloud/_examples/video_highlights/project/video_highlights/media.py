"""Check a source video and run ffmpeg on it."""

import hashlib
import subprocess
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

VIDEO_SUFFIXES = frozenset({".m4v", ".mkv", ".mov", ".mp4", ".webm"})
MAX_KEY_LENGTH = 1024
MAX_VIDEO_BYTES = 20 * 1024**3
MAX_VIDEO_SECONDS = 2 * 3600
CLIP_MAX_HEIGHT = 1080
THUMBNAIL_WIDTH = 640


class VideoRejected(ValueError):
    """The object is not a video this pipeline accepts."""


class MediaError(RuntimeError):
    """ffmpeg or ffprobe failed on a video that passed the checks."""


class SourceVideo(BaseModel):
    key: str
    size_bytes: int
    duration_seconds: float
    # Names this exact version of the object, so a replaced video starts fresh.
    job_id: str


class ProbeStream(BaseModel):
    codec_type: str


class ProbeFormat(BaseModel):
    duration: float | None = None


class Probe(BaseModel):
    streams: list[ProbeStream]
    format: ProbeFormat


def video_path(root: Path, key: str) -> Path:
    """The mounted path of a bucket key, refusing keys that leave the bucket."""
    relative = PurePosixPath(key)
    if not key or len(key) > MAX_KEY_LENGTH or relative.is_absolute() or ".." in relative.parts:
        raise VideoRejected(f"invalid video key: {key!r}")
    if relative.suffix.lower() not in VIDEO_SUFFIXES:
        raise VideoRejected(f"{key} is not one of {', '.join(sorted(VIDEO_SUFFIXES))}")
    return root.joinpath(*relative.parts)


def inspect_video(root: Path, key: str) -> SourceVideo:
    path = video_path(root, key)
    try:
        stat = path.stat()
    except FileNotFoundError:
        raise VideoRejected(f"no object at {key}") from None
    if stat.st_size > MAX_VIDEO_BYTES:
        raise VideoRejected(f"{key} is larger than {MAX_VIDEO_BYTES} bytes")
    probe = run(["ffprobe", "-v", "error", "-of", "json", "-show_format", "-show_streams", path])
    version = f"{key}\n{stat.st_size}\n{stat.st_mtime_ns}".encode()
    return SourceVideo(
        key=key,
        size_bytes=stat.st_size,
        duration_seconds=read_duration(probe),
        job_id=hashlib.sha256(version).hexdigest()[:16],
    )


def read_duration(probe_json: str) -> float:
    """The duration ffprobe reports for a video with a soundtrack to transcribe."""
    probe = Probe.model_validate_json(probe_json)
    kinds = {stream.codec_type for stream in probe.streams}
    if "video" not in kinds:
        raise VideoRejected("the file has no video stream")
    if "audio" not in kinds:
        raise VideoRejected("the video has no audio to transcribe")
    duration = probe.format.duration
    if duration is None or duration <= 0:
        raise VideoRejected("the video has no readable duration")
    if duration > MAX_VIDEO_SECONDS:
        raise VideoRejected(f"the video is longer than {MAX_VIDEO_SECONDS} seconds")
    return duration


def write_audio(video: Path, target: Path) -> None:
    """Decode the soundtrack to 16 kHz mono FLAC, the input Whisper expects."""
    ffmpeg(["-i", video, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", "-f", "flac", target])


def write_clip(video: Path, start: float, end: float, target: Path) -> None:
    """Re-encode so the clip starts on the exact frame, not the previous keyframe."""
    window = ["-ss", f"{start:.3f}", "-i", video, "-t", f"{end - start:.3f}"]
    streams = ["-map", "0:v:0", "-map", "0:a:0"]
    video_codec = ["-vf", f"scale=-2:min(ih\\,{CLIP_MAX_HEIGHT})", "-c:v", "libx264"]
    encoding = ["-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k"]
    ffmpeg([*window, *streams, *video_codec, *encoding, "-movflags", "+faststart", target])


def write_thumbnail(video: Path, at: float, target: Path) -> None:
    frame = ["-ss", f"{at:.3f}", "-i", video, "-frames:v", "1"]
    ffmpeg([*frame, "-vf", f"scale={THUMBNAIL_WIDTH}:-2", "-q:v", "3", target])


def ffmpeg(args: list[str | Path]) -> None:
    run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args])


def run(command: list[str | Path]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MediaError(f"{command[0]} exited {result.returncode}: {result.stderr[-2000:]}")
    return result.stdout
