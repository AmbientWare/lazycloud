from __future__ import annotations

import stat
from pathlib import Path

from worker.checkpoint_filesystem import copy_checkpoint_filesystem


def test_checkpoint_filesystem_copy_preserves_links_and_metadata_without_following_symlinks(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    state = source / "state"
    state.write_bytes(b"container state")
    state.chmod(0o6750)
    (source / "hardlink").hardlink_to(state)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host-file").write_bytes(b"host data")
    (source / "absolute").symlink_to(outside, target_is_directory=True)
    (source / "relative").symlink_to("missing")
    original_host_metadata = outside.stat()
    destination = tmp_path / "copy"

    copy_checkpoint_filesystem(source, destination)

    copied = destination / "state"
    assert copied.read_bytes() == b"container state"
    assert copied.stat().st_ino == (destination / "hardlink").stat().st_ino
    assert stat.S_IMODE(copied.stat().st_mode) == 0o6750
    assert copied.stat().st_uid == state.stat().st_uid
    assert copied.stat().st_gid == state.stat().st_gid
    assert copied.stat().st_mtime_ns == state.stat().st_mtime_ns
    assert (destination / "absolute").readlink() == outside
    assert (destination / "relative").readlink() == Path("missing")
    assert outside.stat() == original_host_metadata
    assert (outside / "host-file").read_bytes() == b"host data"
