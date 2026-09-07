from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import typer
from lazycloud.cli.components.results import emit_result
from pydantic import JsonValue, ValidationError
from shared.errors import InvalidInputError
from storage.archive_migration import (
    ArchiveMigrationConfiguration,
    ArchiveMigrationOperation,
    ArchiveMigrationProgress,
    ImageArchiveMigration,
)
from storage.image_archive import ResolvedImageArchiveSettings
from storage_client.s3 import S3ObjectStoreClient

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

archive_migration_app = typer.Typer(
    help="Copy, verify and cut over the private image archive store."
)


@archive_migration_app.command("run")
def migrate_archives(
    ctx: typer.Context,
    configuration: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
    operation: ArchiveMigrationOperation = ArchiveMigrationOperation.Copy,
    writers_stopped: bool = typer.Option(
        False,
        help="Confirm archive writers/retention are stopped and all upload URLs have expired.",
    ),
) -> None:
    try:
        config = ArchiveMigrationConfiguration.model_validate_json(configuration.read_bytes())
    except ValidationError:
        raise InvalidInputError("archive migration configuration is invalid") from None
    settings = DatabaseSettings(
        application_name=DatabaseApplicationName.Admin,
        pool_size=1,
        max_overflow=0,
    ).direct()
    with ExitStack() as resources:
        database = DatabaseClient.from_settings(settings)
        resources.callback(database.dispose)
        source_settings = config.source.object_store_settings()
        target_settings = config.target.object_store_settings()
        source = S3ObjectStoreClient.from_settings(source_settings)
        resources.callback(source.close)
        target = S3ObjectStoreClient.from_settings(target_settings)
        resources.callback(target.close)
        source.validate_bucket_access()
        target.validate_bucket_access()
        result = ImageArchiveMigration(
            database,
            source,
            target,
            ResolvedImageArchiveSettings(source_settings, config.prefix, 900),
            ResolvedImageArchiveSettings(target_settings, config.prefix, 900),
        ).run(operation, writers_stopped=writers_stopped, progress=_report_progress)
    payload: dict[str, JsonValue] = {
        "verified": result.verified,
        "copied": result.copied,
        "bytes_verified": result.bytes_verified,
        "records_moved": result.records_moved,
    }
    emit_result(ctx, payload=payload, title="Image archive migration", fields=payload)


def _report_progress(progress: ArchiveMigrationProgress) -> None:
    typer.echo(
        f"Verified {progress.verified} archives, copied {progress.copied}, "
        f"verified {progress.bytes_verified} bytes; image {progress.image_id}",
        err=True,
    )
