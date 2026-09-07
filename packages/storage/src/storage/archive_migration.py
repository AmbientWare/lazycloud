from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

from database.repositories.images import ImageArchiveRepository
from pydantic import ConfigDict
from shared.contracts import ContractModel
from shared.errors import ConflictError, InvalidInputError
from shared.image_building.records import ImageArchiveRecord

from database import DatabaseClient
from storage.image_archive import ImageArchiveBackendSettings, ResolvedImageArchiveSettings
from storage.service import ObjectByteClient


class ArchiveMigrationOperation(StrEnum):
    Copy = "copy"
    Verify = "verify"
    Cutover = "cutover"


class ArchiveMigrationConfiguration(ContractModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    source: ImageArchiveBackendSettings
    target: ImageArchiveBackendSettings
    prefix: str = ""


@dataclass(frozen=True, slots=True)
class ArchiveMigrationProgress:
    image_id: str
    verified: int
    copied: int
    bytes_verified: int


@dataclass(frozen=True, slots=True)
class ArchiveMigrationResult:
    verified: int
    copied: int
    bytes_verified: int
    records_moved: int


@dataclass(slots=True)
class ImageArchiveMigration:
    database: DatabaseClient
    source: ObjectByteClient
    target: ObjectByteClient
    source_settings: ResolvedImageArchiveSettings
    target_settings: ResolvedImageArchiveSettings

    def run(
        self,
        operation: ArchiveMigrationOperation,
        *,
        writers_stopped: bool = False,
        progress: Callable[[ArchiveMigrationProgress], None] | None = None,
    ) -> ArchiveMigrationResult:
        source_bucket = self.source_settings.bucket
        target_bucket = self.target_settings.bucket
        if source_bucket == target_bucket:
            raise InvalidInputError("archive migration requires distinct bucket names")
        if self.source_settings.prefix != self.target_settings.prefix:
            raise InvalidInputError("archive migration must preserve the archive key prefix")
        cutover = operation == ArchiveMigrationOperation.Cutover
        if cutover and not writers_stopped:
            raise InvalidInputError(
                "cutover requires stopped archive writers and retention, and expired upload URLs"
            )
        copied = verified = bytes_verified = moved = 0
        with (
            self.database.session() as session,
            TemporaryDirectory(prefix="lazycloud-archive-migration-") as directory,
        ):
            archives = ImageArchiveRepository(session)
            archives.claim_store_migration()
            if cutover:
                # Fence row mutations for the entire verification and coordinate commit.
                # Already issued object-store capabilities must be quiesced by the operator.
                archives.lock_for_store_migration()
            after = ""
            while batch := archives.list_for_store_migration(after=after, limit=100):
                for archive in batch:
                    if archive.bucket not in {source_bucket, target_bucket}:
                        raise ConflictError(
                            f"archive {archive.image_id} belongs to an unexpected bucket"
                        )
                    if archive.cleanup_claimed_at is not None:
                        raise ConflictError(f"archive {archive.image_id} is being reclaimed")
                    key = self.source_settings.physical_key(archive.object_key)
                    source_path = Path(directory) / "source"
                    target_path = Path(directory) / "target"
                    if archive.bucket == source_bucket:
                        self._download_verified(
                            self.source, source_bucket, key, archive, source_path
                        )
                    if not self.target.exists(key, bucket=target_bucket):
                        if operation != ArchiveMigrationOperation.Copy:
                            raise ConflictError(f"archive {archive.image_id} is missing at target")
                        if archive.bucket != source_bucket:
                            raise ConflictError(
                                f"archive {archive.image_id} has moved but its target is missing"
                            )
                        metadata = self.source.head(key, bucket=source_bucket).metadata
                        self.target.put_file(
                            key, source_path, bucket=target_bucket, metadata=metadata
                        )
                        copied += 1
                    self._download_verified(self.target, target_bucket, key, archive, target_path)
                    if archive.bucket == source_bucket:
                        source_metadata = self.source.head(key, bucket=source_bucket).metadata
                        target_metadata = self.target.head(key, bucket=target_bucket).metadata
                        if source_metadata != target_metadata:
                            raise ConflictError(f"archive {archive.image_id} metadata differs")
                    if cutover and archive.bucket == source_bucket:
                        archives.move_store(archive, target_bucket=target_bucket)
                        moved += 1
                    verified += 1
                    bytes_verified += archive.size_bytes
                    if progress is not None:
                        progress(
                            ArchiveMigrationProgress(
                                archive.image_id, verified, copied, bytes_verified
                            )
                        )
                    source_path.unlink(missing_ok=True)
                    target_path.unlink(missing_ok=True)
                after = batch[-1].image_id
        return ArchiveMigrationResult(verified, copied, bytes_verified, moved)

    @staticmethod
    def _download_verified(
        client: ObjectByteClient,
        bucket: str,
        key: str,
        archive: ImageArchiveRecord,
        path: Path,
    ) -> None:
        head = client.head(key, bucket=bucket)
        if (
            head.size != archive.size_bytes
            or head.metadata.get("artifact-sha256") != archive.sha256
        ):
            raise ConflictError(f"archive {archive.image_id} size or checksum metadata differs")
        client.download_file(key, path, bucket=bucket)
        with path.open("rb") as contents:
            digest = hashlib.file_digest(contents, "sha256").hexdigest()
        if path.stat().st_size != archive.size_bytes or digest != archive.sha256:
            raise ConflictError(f"archive {archive.image_id} bytes fail checksum verification")
