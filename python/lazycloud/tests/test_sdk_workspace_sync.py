from __future__ import annotations

import threading
from pathlib import Path

from lazycloud.abstractions.workspace_sync import ContainerWorkspaceSyncer
from lazycloud.clients.api import ApiClient
from lazycloud.clients.workloads import WorkloadsClient
from lazycloud.terminal import Terminal

from tests.api_server import TOKEN, ApiRequest, FakeApi, Reply

CONTAINER = "0192f0a0-0000-7000-8000-0000000000c1"
FILES = f"/v1/workspaces/team/containers/{CONTAINER}/files"


def test_workspace_syncer_writes_the_filtered_tree_then_follows_changes(
    tmp_path: Path, fake_api: FakeApi
) -> None:
    rewritten, deleted = threading.Event(), threading.Event()

    @fake_api.route("PUT", f"{FILES}/content")
    def write(request: ApiRequest) -> Reply:
        if request.body == b"print('changed')\n":
            rewritten.set()
        return 204, {}, b""

    @fake_api.route("DELETE", FILES)
    def delete(_: ApiRequest) -> Reply:
        deleted.set()
        return 204, {}, b""

    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "__pycache__").mkdir()
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "app.py").chmod(0o644)
    (source / "pkg" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "pkg" / "worker.py").chmod(0o750)
    (source / "ignored.py").write_text("raise RuntimeError\n", encoding="utf-8")
    (source / "__pycache__" / "skip.py").write_text("skip\n", encoding="utf-8")
    (source / ".lazycloudignore").write_text(
        ".lazycloudignore\nignored.py\n__pycache__\n", encoding="utf-8"
    )
    syncer = ContainerWorkspaceSyncer(
        container_id=CONTAINER,
        local_dir=str(source),
        client=WorkloadsClient(ApiClient(endpoint=fake_api.url, token=TOKEN), "team"),
        terminal=Terminal(quiet=True),
        debounce_seconds=0.05,
    )

    syncer.sync_once()

    writes = {
        request.query["path"][0]: (request.query["mode"][0], request.body)
        for request in fake_api.calls("PUT", f"{FILES}/content")
    }
    assert writes == {
        "app.py": ("420", b"print('ok')\n"),
        "pkg/worker.py": ("488", b"VALUE = 1\n"),
    }

    syncer.start()
    try:
        (source / "app.py").write_text("print('changed')\n", encoding="utf-8")
        (source / "pkg" / "worker.py").unlink()
        assert rewritten.wait(10) and deleted.wait(10)
    finally:
        # A stop that does not wake the follow loop never returns.
        stopping = threading.Thread(target=syncer.stop, daemon=True)
        stopping.start()
        stopping.join(10)
    assert not stopping.is_alive(), "stop() did not wake the follow loop"
    syncer.raise_if_failed()

    assert [r.query["path"] for r in fake_api.calls("DELETE", FILES)] == [["pkg/worker.py"]]
    assert fake_api.calls("PUT", f"{FILES}/content")[-1].body == b"print('changed')\n"
