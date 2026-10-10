from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import lazycloud

sys.path.insert(0, str(Path(lazycloud.__file__).parent / "_examples" / "image_studio" / "project"))

from image_studio.auth import (
    MIN_KEY_LENGTH,
    WeakKeyError,
    bearer_matches,
    check_key_strength,
    sign_path,
    signature_valid,
)
from image_studio.gallery import (
    GalleryEntry,
    expired_directories,
    image_path,
    job_directory,
    newest_entries,
    write_entry,
)
from image_studio.generation import Aspect, GenerateRequest, Style
from image_studio.progress import JobEvent, JobProgress, JobStage, describe_job

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
KEY = "a-studio-key-for-tests"


@pytest.mark.parametrize(
    "fields",
    [
        {"prompt": "   "},
        {"prompt": "x" * 501},
        {"prompt": "a fox", "count": 0},
        {"prompt": "a fox", "count": 5},
        {"prompt": "a fox", "seed": -1},
        {"prompt": "a fox", "seed": 2**32},
        {"prompt": "a fox", "style": "oil"},
        {"prompt": "a fox", "steps": 50},
    ],
)
def test_requests_outside_the_limits_are_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(fields)


def test_a_style_extends_the_prompt() -> None:
    plain = GenerateRequest(prompt="  a fox in snow ")
    styled = GenerateRequest(prompt="a fox in snow", style=Style.WATERCOLOR)

    assert plain.full_prompt() == "a fox in snow"
    assert styled.full_prompt().startswith("a fox in snow, ")
    assert "watercolor" in styled.full_prompt()


def test_api_calls_need_the_exact_key_as_a_bearer_token() -> None:
    assert bearer_matches(f"Bearer {KEY}", KEY)
    assert bearer_matches(f"bearer {KEY}", KEY)
    assert not bearer_matches(None, KEY)
    assert not bearer_matches(KEY, KEY)
    assert not bearer_matches(f"Basic {KEY}", KEY)
    assert not bearer_matches(f"Bearer {KEY}x", KEY)


def test_a_signed_url_opens_only_its_own_path_until_it_expires() -> None:
    path = f"/api/jobs/{uuid.uuid4()}/images/0"
    url = sign_path(KEY, path, now=1000, ttl_seconds=60)
    query = dict(part.split("=") for part in url.split("?", 1)[1].split("&"))
    expires, signature = int(query["expires"]), query["signature"]

    def valid(path: str, now: float, key: str = KEY) -> bool:
        return signature_valid(key, path, expires=expires, signature=signature, now=now)

    assert sign_path(KEY, path, now=1019, ttl_seconds=60) == url
    assert valid(path, now=1060)
    assert not valid(path, now=1120)
    assert not valid(path.replace("images/0", "images/1"), now=1001)
    assert not valid(path, now=1001, key="another-studio-key")
    assert not signature_valid(KEY, path, expires=expires, signature="é", now=1001)


def test_short_keys_are_refused() -> None:
    with pytest.raises(WeakKeyError):
        check_key_strength("x" * (MIN_KEY_LENGTH - 1))
    assert check_key_strength(KEY) == KEY


@pytest.mark.parametrize("job_id", ["../models", "..", "not-a-task", "/etc/passwd"])
def test_gallery_paths_accept_only_task_ids(tmp_path: Path, job_id: str) -> None:
    with pytest.raises(ValueError):
        image_path(tmp_path, job_id, 0)


def _write_job(root: Path, created_at: datetime) -> str:
    job_id = str(uuid.uuid4())
    job_directory(root, job_id).mkdir()
    entry = GalleryEntry(
        job_id=job_id,
        prompt="a fox",
        style=Style.NONE,
        aspect=Aspect.SQUARE,
        seed=7,
        image_count=1,
        created_at=created_at,
    )
    write_entry(root, entry)
    return job_id


def test_the_gallery_lists_the_newest_jobs_first(tmp_path: Path) -> None:
    oldest, newest, middle = (
        _write_job(tmp_path, NOW - timedelta(hours=hours)) for hours in (3, 1, 2)
    )

    listed = [entry.job_id for entry in newest_entries(tmp_path, limit=2)]

    assert listed == [newest, middle]
    assert oldest not in listed


def test_cleanup_selects_jobs_past_retention_including_unfinished_ones(tmp_path: Path) -> None:
    expired = _write_job(tmp_path, NOW - timedelta(days=8))
    kept = _write_job(tmp_path, NOW - timedelta(days=6))
    abandoned = job_directory(tmp_path, str(uuid.uuid4()))
    abandoned.mkdir()
    stamp = (NOW - timedelta(days=9)).timestamp()
    os.utime(abandoned, (stamp, stamp))

    selected = expired_directories(tmp_path, now=NOW, keep_for=timedelta(days=7))

    assert sorted(selected) == sorted([job_directory(tmp_path, expired), abandoned])
    assert job_directory(tmp_path, kept) not in selected


def _recorded(stage: JobStage, done: int, error: str | None = None) -> JobProgress:
    return JobProgress(stage=stage, completed_steps=done, total_steps=8, error=error)


def _describe(record: JobProgress | None, status: str, pending: str | None = None) -> JobEvent:
    return describe_job(record, task_status=status, pending_message=pending)


def test_progress_reports_queue_steps_and_outcome() -> None:
    waiting = _describe(None, "queued", "Waiting for an L4 GPU")
    assert (waiting.stage, waiting.message) == (JobStage.QUEUED, "Waiting for an L4 GPU")

    running = _describe(_recorded(JobStage.GENERATING, 3), "running")
    assert (running.stage, running.completed_steps, running.total_steps) == (
        JobStage.GENERATING,
        3,
        8,
    )

    assert _describe(_recorded(JobStage.DONE, 8), "running").stage is JobStage.DONE
    failed = _describe(_recorded(JobStage.FAILED, 2, "OutOfMemoryError: CUDA"), "failed")
    assert (failed.stage, failed.message) == (JobStage.FAILED, "OutOfMemoryError: CUDA")


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_a_task_that_stops_without_reporting_fails_the_job(status: str) -> None:
    for record in (None, _recorded(JobStage.GENERATING, 2)):
        event = _describe(record, status)
        assert event.stage is JobStage.FAILED
        assert status in event.message
