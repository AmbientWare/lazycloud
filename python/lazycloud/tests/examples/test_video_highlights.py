from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import lazycloud

sys.path.insert(
    0, str(Path(lazycloud.__file__).parent / "_examples" / "video_highlights" / "project")
)

from video_highlights.highlights import (
    MAX_CLIP_SECONDS,
    Chapter,
    Highlight,
    HighlightPlan,
    normalize_plan,
)
from video_highlights.manifest import HighlightManifest, HighlightRun
from video_highlights.media import VideoRejected, read_duration, video_path
from video_highlights.receiver import SIGNING_KEY, api
from video_highlights.transcript import Segment, Transcript

KEY = "whsec_test-signing-key"

# Ten-second segments with a one-second pause between them, then silence to 300 seconds.
TRANSCRIPT = Transcript(
    language="en",
    duration_seconds=300.0,
    segments=[
        Segment(start=float(start), end=float(start + 10), text=f"line {start}")
        for start in range(0, 290, 11)
    ],
)


def pick(start: float, end: float, title: str = "pick") -> Highlight:
    return Highlight(start_seconds=start, end_seconds=end, title=title, reason="why")


def plan(*highlights: Highlight, chapters: list[Chapter] | None = None) -> HighlightPlan:
    return HighlightPlan(summary=" A talk. ", chapters=chapters or [], highlights=list(highlights))


def probe(*kinds: str, duration: str | None = "120.5") -> str:
    fmt = {} if duration is None else {"duration": duration}
    return json.dumps({"streams": [{"codec_type": kind} for kind in kinds], "format": fmt})


def test_keys_stay_inside_the_bucket_and_name_videos() -> None:
    root = Path("/videos")
    assert video_path(root, "talks/2026/keynote.MP4") == root / "talks/2026/keynote.MP4"
    for key in ("", "/etc/passwd.mp4", "talks/../../secrets.mp4", "notes.txt", "x" * 1100 + ".mp4"):
        with pytest.raises(VideoRejected):
            video_path(root, key)


def test_probe_requires_video_audio_and_a_bounded_duration() -> None:
    assert read_duration(probe("video", "audio")) == 120.5
    rejected = (
        probe("audio"),
        probe("video"),
        probe("video", "audio", duration=None),
        probe("video", "audio", duration="7201"),
    )
    for output in rejected:
        with pytest.raises(VideoRejected):
            read_duration(output)


def test_highlights_widen_to_whole_segments() -> None:
    result = normalize_plan(plan(pick(25.0, 47.0)), TRANSCRIPT, max_highlights=5)
    assert [(h.start_seconds, h.end_seconds) for h in result.highlights] == [(22.0, 54.0)]
    assert result.summary == "A talk."


def test_long_highlights_end_at_the_last_segment_that_fits() -> None:
    [highlight] = normalize_plan(plan(pick(0.0, 200.0)), TRANSCRIPT, 5).highlights
    assert highlight.start_seconds == 0.0
    assert highlight.end_seconds == 87.0
    assert highlight.end_seconds - highlight.start_seconds <= MAX_CLIP_SECONDS


def test_short_silent_and_overlapping_picks_are_dropped_best_first() -> None:
    picks = plan(
        pick(110.0, 140.0, "best"),
        pick(130.0, 160.0, "overlaps best"),
        pick(305.0, 320.0, "past the end"),
        pick(10.2, 10.8, "between segments"),
        pick(200.0, 230.0, "second"),
        pick(0.0, 30.0, "over the limit"),
    )
    result = normalize_plan(picks, TRANSCRIPT, max_highlights=2)
    assert [h.title for h in result.highlights] == ["best", "second"]


def test_chapters_start_at_zero_and_stay_apart() -> None:
    chapters = [
        Chapter(start_seconds=150.0, title="Questions"),
        Chapter(start_seconds=4.0, title=" Intro "),
        Chapter(start_seconds=9.0, title="Too close"),
        Chapter(start_seconds=60.0, title="  "),
        Chapter(start_seconds=295.0, title="Too late"),
    ]
    result = normalize_plan(plan(chapters=chapters), TRANSCRIPT, 5)
    assert [(c.start_seconds, c.title) for c in result.chapters] == [
        (0.0, "Intro"),
        (150.0, "Questions"),
    ]


def signed_headers(body: bytes, key: str = KEY, sent_at: int | None = None) -> dict[str, str]:
    timestamp = str(int(time.time()) if sent_at is None else sent_at)
    message = base64.b64encode(body) + b":" + timestamp.encode()
    signature = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Task-Signature": signature,
        "X-Task-Timestamp": timestamp,
    }


def finished_run() -> bytes:
    run = HighlightRun(
        manifest_url="https://artifacts.example/manifest.json",
        manifest=HighlightManifest(
            video_key="talks/keynote.mp4",
            duration_seconds=300.0,
            language="en",
            model="gpt-5-mini",
            summary="A talk.",
            chapters=[],
            clips=[],
            failed_highlights=[],
        ),
    )
    event: dict[str, object] = {
        "task_id": "4b4d3a52-1f7e-4c55-9d38-0f0f3b4c1a10",
        "root_task_id": "4b4d3a52-1f7e-4c55-9d38-0f0f3b4c1a10",
        "status": "succeeded",
        "attempt_number": 1,
        "max_attempts": 1,
        "retry_scheduled": False,
        "data": {"encoding": "json", "value": run.model_dump(mode="json")},
        "error": None,
        "finished_at": "2026-10-09T12:00:00Z",
    }
    return json.dumps(event, separators=(",", ":"), sort_keys=True).encode()


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(SIGNING_KEY, KEY)
    return TestClient(api)


def test_receiver_accepts_a_signed_callback(
    receiver: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    body = finished_run()
    response = receiver.post("/video-highlights", content=body, headers=signed_headers(body))
    assert response.status_code == 204
    assert "talks/keynote.mp4: 0 clips, https://artifacts.example/manifest.json" in (
        capsys.readouterr().out
    )


def test_receiver_rejects_forged_altered_and_replayed_callbacks(receiver: TestClient) -> None:
    body = finished_run()
    attempts = [
        (body, signed_headers(body, key="whsec_someone-else")),
        (body.replace(b"keynote", b"keynoteX"), signed_headers(body)),
        (body, signed_headers(body, sent_at=int(time.time()) - 3600)),
    ]
    for content, headers in attempts:
        response = receiver.post("/video-highlights", content=content, headers=headers)
        assert response.status_code == 401
    assert receiver.post("/video-highlights", content=body).status_code == 422


def test_receiver_refuses_oversized_bodies(receiver: TestClient) -> None:
    body = b"x" * (600 * 1024)
    response = receiver.post("/video-highlights", content=body, headers=signed_headers(body))
    assert response.status_code == 413
