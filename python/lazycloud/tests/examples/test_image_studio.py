from __future__ import annotations

import importlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

PROJECT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "lazycloud"
    / "_examples"
    / "image_studio"
    / "project"
)
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
KEY = "a-studio-key-for-tests"


@pytest.fixture(scope="module")
def studio() -> dict[str, ModuleType]:
    """The example's modules that hold its logic; none of them touches the platform."""
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(PROJECT))
        names = ("auth", "gallery", "generation", "progress")
        return {name: importlib.import_module(f"image_studio.{name}") for name in names}


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
def test_requests_outside_the_limits_are_refused(
    studio: dict[str, ModuleType], fields: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        studio["generation"].GenerateRequest(**fields)


def test_a_style_extends_the_prompt(studio: dict[str, ModuleType]) -> None:
    generation = studio["generation"]
    plain = generation.GenerateRequest(prompt="  a fox in snow ")
    styled = generation.GenerateRequest(prompt="a fox in snow", style="watercolor")

    assert plain.full_prompt() == "a fox in snow"
    assert styled.full_prompt().startswith("a fox in snow, ")
    assert "watercolor" in styled.full_prompt()


def test_api_calls_need_the_exact_key_as_a_bearer_token(studio: dict[str, ModuleType]) -> None:
    auth = studio["auth"]

    assert auth.bearer_matches(f"Bearer {KEY}", KEY)
    assert auth.bearer_matches(f"bearer {KEY}", KEY)
    assert not auth.bearer_matches(None, KEY)
    assert not auth.bearer_matches(KEY, KEY)
    assert not auth.bearer_matches(f"Basic {KEY}", KEY)
    assert not auth.bearer_matches(f"Bearer {KEY}x", KEY)


def test_a_signed_url_opens_only_its_own_path_until_it_expires(
    studio: dict[str, ModuleType],
) -> None:
    auth = studio["auth"]
    path = f"/api/jobs/{uuid.uuid4()}/images/0"
    url = auth.sign_path(KEY, path, now=1000, ttl_seconds=60)
    query = dict(part.split("=") for part in url.split("?", 1)[1].split("&"))
    expires, signature = int(query["expires"]), query["signature"]

    def valid(path: str, now: float, key: str = KEY) -> bool:
        return auth.signature_valid(key, path, expires=expires, signature=signature, now=now)

    assert valid(path, now=1059)
    assert not valid(path, now=1060)
    assert not valid(path.replace("images/0", "images/1"), now=1001)
    assert not valid(path, now=1001, key="another-studio-key")


def test_short_keys_are_refused(studio: dict[str, ModuleType]) -> None:
    auth = studio["auth"]

    with pytest.raises(auth.WeakKeyError):
        auth.check_key_strength("x" * (auth.MIN_KEY_LENGTH - 1))
    assert auth.check_key_strength(KEY) == KEY


@pytest.mark.parametrize("job_id", ["../models", "..", "not-a-task", "/etc/passwd"])
def test_gallery_paths_accept_only_task_ids(
    studio: dict[str, ModuleType], tmp_path: Path, job_id: str
) -> None:
    with pytest.raises(ValueError):
        studio["gallery"].image_path(tmp_path, job_id, 0)


def _write_job(gallery: ModuleType, root: Path, created_at: datetime) -> str:
    job_id = str(uuid.uuid4())
    gallery.job_directory(root, job_id).mkdir()
    gallery.write_entry(
        root,
        gallery.GalleryEntry(
            job_id=job_id,
            prompt="a fox",
            style="none",
            aspect="square",
            seed=7,
            image_count=1,
            created_at=created_at,
        ),
    )
    return job_id


def test_the_gallery_lists_the_newest_jobs_first(
    studio: dict[str, ModuleType], tmp_path: Path
) -> None:
    gallery = studio["gallery"]
    oldest, newest, middle = (
        _write_job(gallery, tmp_path, NOW - timedelta(hours=hours)) for hours in (3, 1, 2)
    )

    listed = [entry.job_id for entry in gallery.newest_entries(tmp_path, limit=2)]

    assert listed == [newest, middle]
    assert oldest not in listed


def test_cleanup_selects_jobs_past_retention_including_unfinished_ones(
    studio: dict[str, ModuleType], tmp_path: Path
) -> None:
    gallery = studio["gallery"]
    keep_for = timedelta(days=7)
    expired = _write_job(gallery, tmp_path, NOW - timedelta(days=8))
    kept = _write_job(gallery, tmp_path, NOW - timedelta(days=6))
    abandoned = gallery.job_directory(tmp_path, str(uuid.uuid4()))
    abandoned.mkdir()
    stamp = (NOW - timedelta(days=9)).timestamp()
    os.utime(abandoned, (stamp, stamp))

    selected = gallery.expired_directories(tmp_path, now=NOW, keep_for=keep_for)

    assert sorted(selected) == sorted([gallery.job_directory(tmp_path, expired), abandoned])
    assert gallery.job_directory(tmp_path, kept) not in selected


def test_progress_reports_queue_steps_and_outcome(studio: dict[str, ModuleType]) -> None:
    progress = studio["progress"]
    stage = progress.JobStage

    def recorded(name: str, done: int, error: str | None = None) -> object:
        return progress.JobProgress(
            stage=stage(name), completed_steps=done, total_steps=8, error=error
        )

    def describe(record: object, status: str, pending: str | None = None) -> object:
        return progress.describe_job(record, task_status=status, pending_message=pending)

    waiting = describe(None, "queued", "Waiting for an L4 GPU")
    assert (waiting.stage, waiting.message) == (stage.QUEUED, "Waiting for an L4 GPU")

    running = describe(recorded("generating", 3), "running")
    assert (running.stage, running.completed_steps, running.total_steps) == (
        stage.GENERATING,
        3,
        8,
    )

    assert describe(recorded("done", 8), "running").stage is stage.DONE
    failed = describe(recorded("failed", 2, "OutOfMemoryError: CUDA"), "failed")
    assert (failed.stage, failed.message) == (stage.FAILED, "OutOfMemoryError: CUDA")


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_a_task_that_stops_without_reporting_fails_the_job(
    studio: dict[str, ModuleType], status: str
) -> None:
    progress = studio["progress"]
    stalled = progress.JobProgress(
        stage=progress.JobStage.GENERATING, completed_steps=2, total_steps=4
    )

    for record in (None, stalled):
        event = progress.describe_job(record, task_status=status, pending_message=None)
        assert event.stage is progress.JobStage.FAILED
        assert status in event.message
