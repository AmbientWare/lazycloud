from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from worker.network_pool import PreparedNetworkPool


def test_network_assignments_overlap_and_close_preserves_transferred_resources(
    tmp_path: Path,
) -> None:
    slots, assigned = tmp_path / "slots", tmp_path / "assigned"
    slots.mkdir()
    assigned.mkdir()
    unrelated = slots / "unrelated"
    unrelated.mkdir()
    signals = {name: threading.Event() for name in ("first", "second", "third")}
    releases = {name: os.pipe() for name in signals}

    class DirectoryPool(PreparedNetworkPool):
        def _prepare(self, name: str) -> None:
            self._remove(name)
            (slots / name).mkdir()

        def _remove(self, name: str) -> None:
            shutil.rmtree(slots / name, ignore_errors=True)

        def _transfer(self, name: str, container_id: str) -> None:
            read, _ = releases[container_id]
            signals[container_id].set()
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; os.read(int(sys.argv[1]), 1); "
                    "os.rename(sys.argv[2], sys.argv[3])",
                    str(read),
                    str(slots / name),
                    str(assigned / container_id),
                ],
                pass_fds=(read,),
                check=True,
                timeout=5,
            )

    pool = DirectoryPool(worker_id="worker", bridge_name="bridge", host_netns_path=str(slots))
    try:
        pool.initialize()
        with ThreadPoolExecutor(max_workers=5) as executor:
            try:
                first = executor.submit(pool.assign, "first")
                second = executor.submit(pool.assign, "second")
                assert signals["first"].wait(2)
                assert signals["second"].wait(2)
                third = executor.submit(pool.assign, "third")
                os.write(releases["first"][1], b"1")
                first.result(timeout=2)
                assert signals["third"].wait(2)
                waiting = executor.submit(pool.assign, "waiting")
                closing = executor.submit(pool.close)
                with pytest.raises(RuntimeError, match="closed"):
                    waiting.result(timeout=2)
                assert not closing.done()
                for name in ("second", "third"):
                    os.write(releases[name][1], b"1")
                second.result(timeout=2)
                third.result(timeout=2)
                closing.result(timeout=2)
            finally:
                for _, write in releases.values():
                    os.write(write, b"1")
        pool.close()
        assert sorted(path.name for path in assigned.iterdir()) == ["first", "second", "third"]
        assert list(slots.iterdir()) == [unrelated]
    finally:
        try:
            pool.close()
        finally:
            for read, write in releases.values():
                os.close(read)
                os.close(write)
