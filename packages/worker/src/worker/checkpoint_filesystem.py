from __future__ import annotations

import os
import shutil
from pathlib import Path


def copy_checkpoint_filesystem(
    source: Path,
    destination: Path,
    *,
    excluded_root_entries: tuple[str, ...] = (),
) -> None:
    hardlinks: dict[tuple[int, int], str] = {}

    def copy_file(source_file: str, destination_file: str) -> str:
        metadata = os.stat(source_file, follow_symlinks=False)
        identity = (metadata.st_dev, metadata.st_ino)
        existing = hardlinks.get(identity)
        if existing is None:
            shutil.copyfile(source_file, destination_file, follow_symlinks=False)
            if metadata.st_nlink > 1:
                hardlinks[identity] = destination_file
        else:
            os.link(existing, destination_file, follow_symlinks=False)
        return destination_file

    def ignore(directory: str, _names: list[str]) -> tuple[str, ...]:
        return excluded_root_entries if Path(directory) == source else ()

    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=ignore,
        copy_function=copy_file,
    )
    for directory, directories, files in os.walk(
        destination, topdown=False, followlinks=False, onerror=_raise_copy_error
    ):
        copied_directory = Path(directory)
        for name in (*directories, *files):
            copied = copied_directory / name
            _preserve_metadata(source / copied.relative_to(destination), copied)
    _preserve_metadata(source, destination)


def _preserve_metadata(source: Path, destination: Path) -> None:
    metadata = source.stat(follow_symlinks=False)
    # chown can clear set-id bits and capabilities, so copy those attributes afterward.
    os.chown(destination, metadata.st_uid, metadata.st_gid, follow_symlinks=False)
    shutil.copystat(source, destination, follow_symlinks=False)


def _raise_copy_error(error: OSError) -> None:
    raise error
