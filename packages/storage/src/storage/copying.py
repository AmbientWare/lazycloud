from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from shared.objects import ObjectRecord

from storage.service import ObjectStorage


@dataclass(slots=True)
class ObjectCopier:
    storage: ObjectStorage

    @contextmanager
    def copy(
        self, sources: Sequence[ObjectRecord], *, source_workspace_id: str, target_workspace_id: str
    ) -> Iterator[dict[str, ObjectRecord]]:
        copied: dict[str, ObjectRecord] = {}
        try:
            with TemporaryDirectory(prefix="lazycloud-clone-") as directory:
                for record in sources:
                    # Existing filesystem records remain readable during storage migration.
                    source = Path(record.path.removeprefix("file://"))
                    if "://" in record.path and not record.path.startswith("file://"):
                        source = Path(directory) / record.id
                        self.storage.download_by_id_for_workspace(
                            record.id, source, workspace_id=source_workspace_id
                        )
                    copied[record.id] = self.storage.put_file_for_workspace(
                        workspace_id=target_workspace_id,
                        bucket=record.bucket,
                        key=f"clones/{uuid4()}/{Path(record.key).name or record.id}",
                        source=source,
                        content_type=record.content_type,
                        metadata={"workspace_id": target_workspace_id},
                        overwrite=False,
                    )
            yield copied
        except Exception as failure:
            failures: list[Exception] = [failure]
            for record in copied.values():
                try:
                    self.storage.delete_for_workspace(
                        workspace_id=target_workspace_id, bucket=record.bucket, key=record.key
                    )
                except Exception as cleanup_failure:
                    failures.append(cleanup_failure)
            if len(failures) > 1:
                raise ExceptionGroup(
                    "clone failed; object cleanup requires retry", failures
                ) from None
            raise
