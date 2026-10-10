"""Turn a video in your S3 bucket into chapters, highlight clips and thumbnails.

Set your bucket in settings.py, store the secrets with
`uv run python -m video_highlights.configure`, then deploy with
`uv run lazycloud deploy video_highlights.pipeline:app`.
"""

import tempfile
from pathlib import Path

from lazycloud import (
    App,
    Artifact,
    Autoscaler,
    CloudBucket,
    CloudBucketConfig,
    GpuType,
    Image,
    Secret,
    Volume,
)

from video_highlights import settings
from video_highlights.highlights import MAX_HIGHLIGHTS, Highlight, HighlightPlan, normalize_plan
from video_highlights.manifest import Clip, HighlightManifest, HighlightRun
from video_highlights.media import (
    SourceVideo,
    inspect_video,
    write_audio,
    write_clip,
    write_thumbnail,
)
from video_highlights.transcript import Transcript

VIDEOS = Path("/videos")
WORK = Path("/work")
AUDIO = "audio.flac"
TRANSCRIPT = "transcript.json"
WHISPER_MODEL = "large-v3-turbo"
WHISPER_DIR = "/opt/whisper"
# The longest a share link lasts; a shorter artifact retention ends it sooner.
SHARE_SECONDS = 7 * 24 * 3600

app = App("video_highlights")

BUCKET_ACCESS_KEY = Secret("VIDEO_BUCKET_ACCESS_KEY")
BUCKET_SECRET_KEY = Secret("VIDEO_BUCKET_SECRET_KEY")
OPENAI_API_KEY = Secret("OPENAI_API_KEY")

source = CloudBucket(
    settings.BUCKET_NAME,
    str(VIDEOS),
    CloudBucketConfig(
        access_key=BUCKET_ACCESS_KEY.name,
        secret_key=BUCKET_SECRET_KEY.name,
        region=settings.BUCKET_REGION,
        read_only=True,
    ),
)
work = Volume("video-highlights-work", str(WORK))

ffmpeg_image = Image.from_uv(".").add_commands(
    [
        "apt-get update && apt-get install -y --no-install-recommends ffmpeg"
        " && rm -rf /var/lib/apt/lists/*"
    ]
)
whisper_image = Image.from_uv(
    ".", groups=["transcribe"], base_image="nvidia/cuda:12.8.1-runtime-ubuntu24.04"
).add_commands(
    [
        "python -c 'from faster_whisper import download_model; "
        f'download_model("{WHISPER_MODEL}", output_dir="{WHISPER_DIR}")\''
    ]
)
openai_image = Image.from_uv(".", groups=["llm"])


@app.function(
    image=ffmpeg_image,
    volumes=[source, work],
    cpu=0.25,
    memory="1Gi",
    concurrency=4,
    timeout_seconds=3 * 3600,
    retries=0,
    preemptible=False,
    callback_url=settings.CALLBACK_URL,
)
def highlight_video(
    video_key: str, model: str = settings.OPENAI_MODEL, max_highlights: int = 6
) -> HighlightRun:
    """Run every step for one video and return its manifest and share link."""
    if not 1 <= max_highlights <= MAX_HIGHLIGHTS:
        raise ValueError(f"max_highlights must be between 1 and {MAX_HIGHLIGHTS}")
    video = inspect_video(VIDEOS, video_key)
    transcript = saved_transcript(video)
    if transcript is None:
        extract_audio.remote(video)
        transcript = transcribe.remote(video)
    plan = pick_highlights.remote(transcript, model, max_highlights)
    picks = list(enumerate(plan.highlights, start=1))
    clips = list(cut_clip.map([(video, number, pick) for number, pick in picks]))
    manifest = HighlightManifest(
        video_key=video.key,
        duration_seconds=video.duration_seconds,
        language=transcript.language,
        model=model,
        summary=plan.summary,
        chapters=plan.chapters,
        clips=[clip for clip in clips if clip is not None],
        failed_highlights=[
            pick for (_, pick), clip in zip(picks, clips, strict=True) if clip is None
        ],
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "manifest.json"
        path.write_text(manifest.model_dump_json(indent=2))
        return HighlightRun(manifest_url=share(path, "application/json"), manifest=manifest)


@app.function(
    image=ffmpeg_image,
    volumes=[source, work],
    cpu=1,
    memory="1Gi",
    timeout_seconds=1800,
    retries=2,
    retry_delay_seconds=10,
    autoscaler=Autoscaler(max_containers=4),
)
def extract_audio(video: SourceVideo) -> None:
    directory = WORK / video.job_id
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / f"{AUDIO}.part"
    write_audio(VIDEOS / video.key, partial)
    partial.rename(directory / AUDIO)


@app.function(
    image=whisper_image,
    volumes=[work],
    gpu=[GpuType.L4, GpuType.A10G, GpuType.T4],
    cpu=4,
    memory="8Gi",
    timeout_seconds=3600,
    retries=2,
    retry_delay_seconds=10,
    keep_warm=60,
    autoscaler=Autoscaler(max_containers=2),
)
def transcribe(video: SourceVideo) -> Transcript:
    """Transcribe the extracted audio, keep the transcript and drop the audio."""
    from video_highlights.speech import transcribe_file

    directory = WORK / video.job_id
    transcript = transcribe_file(WHISPER_DIR, directory / AUDIO)
    partial = directory / f"{TRANSCRIPT}.part"
    partial.write_text(transcript.model_dump_json())
    partial.rename(directory / TRANSCRIPT)
    (directory / AUDIO).unlink()
    return transcript


@app.function(
    image=openai_image,
    secrets=[OPENAI_API_KEY.name],
    cpu=0.25,
    memory="512Mi",
    concurrency=4,
    timeout_seconds=600,
    retries=3,
    retry_delay_seconds=20,
)
def pick_highlights(transcript: Transcript, model: str, max_highlights: int) -> HighlightPlan:
    from video_highlights.llm import draft_plan

    plan = draft_plan(transcript, model, max_highlights)
    return normalize_plan(plan, transcript, max_highlights)


@app.function(
    image=ffmpeg_image,
    volumes=[source],
    cpu=2,
    memory="2Gi",
    timeout_seconds=900,
    retries=2,
    retry_delay_seconds=10,
    autoscaler=Autoscaler(max_containers=MAX_HIGHLIGHTS),
)
def cut_clip(video: SourceVideo, number: int, pick: Highlight) -> Clip:
    """Cut one highlight and its thumbnail, and share both."""
    source_path = VIDEOS / video.key
    middle = (pick.start_seconds + pick.end_seconds) / 2
    with tempfile.TemporaryDirectory() as directory:
        clip_path = Path(directory) / f"clip-{number:02d}.mp4"
        thumbnail_path = Path(directory) / f"clip-{number:02d}.jpg"
        write_clip(source_path, pick.start_seconds, pick.end_seconds, clip_path)
        write_thumbnail(source_path, middle, thumbnail_path)
        return Clip(
            number=number,
            title=pick.title,
            reason=pick.reason,
            start_seconds=pick.start_seconds,
            end_seconds=pick.end_seconds,
            video_url=share(clip_path, "video/mp4"),
            thumbnail_url=share(thumbnail_path, "image/jpeg"),
        )


def saved_transcript(video: SourceVideo) -> Transcript | None:
    """The transcript an earlier run of this exact video kept, if any."""
    path = WORK / video.job_id / TRANSCRIPT
    return Transcript.model_validate_json(path.read_text()) if path.exists() else None


def share(path: Path, content_type: str) -> str:
    """Save a file as an artifact of the current task and return a public link."""
    artifact = Artifact.file(path, content_type=content_type)
    artifact.save()
    return artifact.public_url(expires=SHARE_SECONDS)
