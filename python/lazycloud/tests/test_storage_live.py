"""Volumes, disks, artifacts, queues and maps against a running platform.

Set LAZYCLOUD_ENDPOINT, LAZYCLOUD_WORKSPACE and LAZYCLOUD_TOKEN, for example
to the stack `deploy/local/run.sh start` prints. The suite isolates the
environment per test, so the settings are read once at import.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import httpx
import lazycloud.config
import pytest
from lazycloud.abstractions.artifact import ArtifactNotFoundError, Stat
from lazycloud.abstractions.disk import DiskOperationError
from lazycloud.cli.main import build_public_cli
from lazycloud.clients.api import ApiError
from lazycloud.contracts.api import ErrorCode, WorkloadKind
from lazycloud.control import api_client, resolve_control_client_config
from typer.testing import CliRunner, Result

from lazycloud import Artifact, Disk, Map, Queue, Volume

_SETTINGS = {
    name: os.environ.get(name, "")
    for name in ("LAZYCLOUD_ENDPOINT", "LAZYCLOUD_WORKSPACE", "LAZYCLOUD_TOKEN")
}
pytestmark = [
    pytest.mark.skipif(
        not _SETTINGS["LAZYCLOUD_ENDPOINT"],
        reason="LAZYCLOUD_ENDPOINT is unset; these tests need a running platform",
    ),
    pytest.mark.usefixtures("live", "isolated_imports"),
]
_APP = "storagelive"
T = TypeVar("T")


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in _SETTINGS.items():
        monkeypatch.setenv(name, value)
    lazycloud.config.reset_settings_cache()
    yield
    lazycloud.config.reset_settings_cache()


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _cli(*args: str) -> Result:
    return CliRunner().invoke(build_public_cli(), list(args))


def _json(*args: str) -> Any:
    result = _cli("--json", *args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _until_free(action: Callable[[], T], *, seconds: float = 60.0) -> T:
    """Retry while a stopping container still holds the resource."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return action()
        except ApiError as exc:
            if exc.code is not ErrorCode.conflict or time.monotonic() > deadline:
                raise
        time.sleep(0.5)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _large_file(path: Path, size: int) -> Path:
    block = os.urandom(1024 * 1024)
    with path.open("wb") as target:
        for _ in range(size // len(block)):
            target.write(block)
        target.write(block[: size % len(block)])
    return path


def _deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> Any:
    tmp_path.mkdir(parents=True, exist_ok=True)
    module = f"storage_live_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module}.py").write_text(source, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    loaded = importlib.import_module(module)
    loaded.app.deploy(prune=True, source_root=tmp_path)
    return loaded


@pytest.fixture
def volume() -> Iterator[Volume]:
    created = Volume(_unique("live"))
    created.create()
    yield created
    _until_free(created.delete)


def test_volume_files_round_trip(volume: Volume, tmp_path: Path) -> None:
    assert volume.get_or_create() is True
    assert volume.ready is True
    assert volume.volume_id is not None
    assert volume.write_text("notes/a.txt", "alpha") == f"{volume.name}/notes/a.txt"
    assert volume.write_bytes("notes/b.bin", b"\x00\x01") == f"{volume.name}/notes/b.bin"

    assert volume.read_text("notes/a.txt") == "alpha"
    assert volume.read_bytes("notes/b.bin") == b"\x00\x01"
    assert volume.list() == ["notes"]
    assert volume.list("notes") == ["notes/a.txt", "notes/b.bin"]
    directory = volume.stat("notes")
    assert (directory.is_dir, directory.modified_at) == (True, None)
    stat = volume.stat("notes/a.txt")
    assert (stat.path, stat.size, stat.is_dir) == ("notes/a.txt", 5, False)
    assert stat.modified_at is not None

    assert volume.move("notes/a.txt", "moved/a.txt") == f"{volume.name}/moved/a.txt"
    assert volume.list("moved") == ["moved/a.txt"]
    with pytest.raises(ApiError) as missing:
        volume.stat("notes/a.txt")
    assert missing.value.code is ErrorCode.not_found

    downloaded = volume.get("moved/a.txt", tmp_path / "out" / "a.txt")
    assert downloaded.read_text(encoding="utf-8") == "alpha"

    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "one.txt").write_text("1", encoding="utf-8")
    (tree / "sub" / "two.txt").write_text("2", encoding="utf-8")
    assert volume.put(tree, "uploaded") == f"{volume.name}/uploaded"
    assert volume.list("uploaded/sub") == ["uploaded/sub/two.txt"]

    assert sorted(volume.remove("uploaded")) == ["uploaded/one.txt", "uploaded/sub/two.txt"]
    assert volume.list("uploaded") == []
    with pytest.raises(ValueError, match="unsafe"):
        volume.read_text("../other/secret")

    assert volume.delete() is True
    assert volume.ready is False
    # The name is free at once, and the new volume starts empty.
    volume.create()
    assert volume.list() == []


def test_volume_put_sends_large_files_in_parts(volume: Volume, tmp_path: Path) -> None:
    source = _large_file(tmp_path / "large.bin", 70 * 1024 * 1024 + 123)

    started = time.perf_counter()
    assert volume.put(source, "big/large.bin") == f"{volume.name}/big/large.bin"
    upload_seconds = time.perf_counter() - started

    assert volume.stat("big/large.bin").size == source.stat().st_size
    copy = volume.get("big/large.bin", tmp_path / "copy.bin")
    assert _sha256(copy) == _sha256(source)
    print(f"70 MiB multipart put: {upload_seconds:.2f} s")


def test_volume_cli_commands(tmp_path: Path) -> None:
    name = _unique("cli")
    created = _json("volume", "create", name)
    assert created["name"] == name
    try:
        assert name in [item["name"] for item in _json("volume", "list")]
        human = _cli("volume", "list")
        assert human.exit_code == 0, human.output
        assert "Volumes" in human.stdout or name in human.stdout

        for file_name in ("a.txt", "b.txt", "skip.md"):
            (tmp_path / file_name).write_text(file_name, encoding="utf-8")
        uploaded = _json("cp", str(tmp_path / "*.txt"), f"{name}/docs")
        assert uploaded["copied"] == [f"{name}/docs/a.txt", f"{name}/docs/b.txt"]

        listing = _json("ls", f"lazycloud://{name}/docs")
        assert [(item["path"], item["size"]) for item in listing] == [
            ("docs/a.txt", 5),
            ("docs/b.txt", 5),
        ]
        table = _cli("ls", f"{name}/docs")
        assert table.exit_code == 0, table.output
        assert f"{name}/docs, 2 items" in table.stdout

        target = tmp_path / "download"
        target.mkdir()
        downloaded = _json("cp", f"lazycloud://{name}/docs/a.txt", f"{target}/")
        assert downloaded["destination"] == str((target / "a.txt").resolve())
        assert (target / "a.txt").read_text(encoding="utf-8") == "a.txt"

        assert _json("mv", f"{name}/docs/a.txt", f"{name}/docs/c.txt") == {
            "new_path": f"{name}/docs/c.txt"
        }
        assert _json("rm", f"{name}/docs") == {"deleted": ["docs/b.txt", "docs/c.txt"]}
    finally:
        assert _json("volume", "delete", name, "-y") == {"name": name, "deleted": True}
    assert name not in [item["name"] for item in _json("volume", "list")]


def test_disks_list_and_refuse_deleting_a_missing_disk() -> None:
    missing = _unique("absent")
    disks = Disk.list()
    assert missing not in [disk.name for disk in disks]
    assert _json("disk", "list") == [disk.model_dump(mode="json") for disk in disks]

    with pytest.raises(DiskOperationError, match="not found"):
        Disk(missing).delete()
    result = _cli("--json", "disk", "delete", missing, "-y")
    assert result.exit_code != 0


def test_queue_is_first_in_first_out() -> None:
    queue = Queue(f"jobs/{_unique('live')} with spaces")
    try:
        assert queue.pop() is None
        assert queue.peek() is None
        assert queue.empty() is True
        assert len(queue) == 0

        first: dict[str, Any] = {"kind": "train", "shape": (1, 2, 3)}
        assert queue.put(first) is True
        assert queue.put(["deploy", 7]) is True
        assert queue.put(None) is True
        assert len(queue) == 3
        assert queue.empty() is False
        assert queue.peek() == first
        assert len(queue) == 3
        assert queue.pop() == first
        assert queue.pop() == ["deploy", 7]
        assert queue.pop() is None
        assert queue.empty() is True

        queue.put("left over")
    finally:
        queue.delete()
    assert len(queue) == 0


def test_map_behaves_like_a_mutable_mapping() -> None:
    mapping = Map(f"cache/{_unique('live')}")
    try:
        assert mapping.set("a/b", {"value": (1, 2)}) is True
        mapping["plain"] = "text"
        mapping.set("forever", 1, ttl=0)
        mapping.set("brief", 2, ttl=1)

        assert mapping["a/b"] == {"value": (1, 2)}
        assert mapping.get("plain") == "text"
        assert mapping.get("missing") is None
        assert mapping.get("missing", "fallback") == "fallback"
        assert "plain" in mapping
        assert "missing" not in mapping
        with pytest.raises(KeyError):
            mapping["missing"]
        with pytest.raises(KeyError):
            del mapping["missing"]

        time.sleep(1.5)
        assert "brief" not in mapping
        assert sorted(mapping) == ["a/b", "forever", "plain"]
        assert len(mapping) == 3
        del mapping["a/b"]
        assert dict(mapping.items()) == {"forever": 1, "plain": "text"}
    finally:
        mapping.delete()
    assert len(mapping) == 0
    assert list(mapping) == []


ARTIFACT_APP = f"""\
import lazycloud

app = lazycloud.App("{_APP}")


@app.function(keep_warm=0)
def ping() -> str:
    return "pong"
"""


def test_artifacts_save_for_a_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _deploy(tmp_path / "project", monkeypatch, ARTIFACT_APP)
    call = module.ping.spawn()
    assert call.get() == "pong"
    task_id = call.task_id

    report = tmp_path / "report.txt"
    report.write_text("quarterly", encoding="utf-8")
    artifact = Artifact.file(report)
    saved = artifact.save(task_id=task_id)
    assert (saved.remote, saved.task_id, saved.filename) == (True, task_id, "report.txt")
    assert saved.expires_at is not None
    stat = artifact.stat()
    assert isinstance(stat, Stat)
    assert (stat.mode, stat.size) == ("0644", len("quarterly"))
    assert artifact.exists() is True
    assert httpx.get(artifact.public_url(expires=60), follow_redirects=True).text == "quarterly"

    folder = tmp_path / "plots"
    folder.mkdir()
    (folder / "a.csv").write_text("x,y\n", encoding="utf-8")
    zipped = Artifact(path=folder).save(task_id=task_id)
    assert (zipped.filename, zipped.stat.packaged) == ("plots.zip", True)

    large = Artifact(path=_large_file(tmp_path / "weights.bin", 65 * 1024 * 1024 + 7))
    large_saved = large.save(task_id=task_id)
    assert large.stat().size == large_saved.stat.size

    listed = _json("artifact", "list", "--task-id", task_id)
    assert {item["id"] for item in listed["artifacts"]} == {
        saved.artifact_id,
        zipped.artifact_id,
        large_saved.artifact_id,
    }
    searched = _json("artifact", "list", "--task-id", task_id, "--search", "PLOTS")
    assert [item["filename"] for item in searched["artifacts"]] == ["plots.zip"]
    usage = _json("artifact", "usage")
    assert usage["count"] >= 3
    assert set(usage) == {
        "count",
        "size_bytes",
        "estimated_monthly_nanos",
        "accrued_nanos",
        "accrued_since",
        "retention_seconds",
    }

    assert _json("artifact", "delete", zipped.artifact_id, "-y") == {"deleted": zipped.artifact_id}
    artifact.delete()
    large.delete()
    assert artifact.exists() is False
    with pytest.raises(ArtifactNotFoundError):
        artifact.delete()
    assert _json("artifact", "list", "--task-id", task_id)["artifacts"] == []


def test_function_writes_into_a_mounted_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _unique("mounted")
    source = f"""\
from pathlib import Path

import lazycloud

app = lazycloud.App("{_APP}")


@app.function(volumes=[lazycloud.Volume("{name}")], disk="2Gi", keep_warm=0)
def record(text: str) -> str:
    target = Path("/volumes/{name}/from-task.txt")
    target.write_text(text)
    return target.read_text()
"""
    module = _deploy(tmp_path / "project", monkeypatch, source)
    volume = Volume(name)
    try:
        config = resolve_control_client_config()
        with api_client(config) as client:
            workload = client.get_workload(config.workspace, _APP, WorkloadKind.function, "record")
            spec = workload.release.spec
        assert spec.resources.disk_mib == 2048
        assert [(item.name, item.mount_path, item.read_only) for item in spec.volumes or []] == [
            (name, f"/volumes/{name}", False)
        ]
        assert module.record.remote("written inside") == "written inside"
        assert volume.read_text("from-task.txt") == "written inside"
    finally:
        _until_free(volume.delete)


def test_tasks_use_queues_maps_and_artifacts_through_the_container_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, mapping = _unique("inside-q"), _unique("inside-m")
    source = f"""\
from pathlib import Path

import lazycloud

app = lazycloud.App("storageinside")


@app.function(keep_warm=0)
def work(n: int) -> str:
    import os

    assert "LAZYCLOUD_TOKEN" not in os.environ
    lazycloud.Queue("{queue}").put({{"n": n}})
    lazycloud.Map("{mapping}")["seen"] = n
    report = Path("/tmp/report.txt")
    report.write_text(f"task saw {{n}}")
    saved = lazycloud.Artifact.file(report).save()
    return saved.artifact_id
"""
    module = _deploy(tmp_path / "inside", monkeypatch, source)
    try:
        artifact_id = module.work.remote(7)
        assert Queue(queue).pop() == {"n": 7}
        assert Map(mapping)["seen"] == 7
        listing = _json("artifact", "list")["artifacts"]
        (saved,) = [item for item in listing if item["id"] == artifact_id]
        assert saved["filename"] == "report.txt" and saved["app"] == "storageinside"
        assert saved["task_id"] is not None
    finally:
        Queue(queue).delete()
        Map(mapping).delete()
