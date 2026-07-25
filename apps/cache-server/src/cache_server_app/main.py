from __future__ import annotations

import argparse
from pathlib import Path
from typing import Self

from cache.server import FileCacheServer, WorkerCacheHttpService
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import CACHE_SERVER_PROCESS_NAME
from shared.paths import state_home


class CacheServerSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    service_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="LAZYCLOUD_CACHE_SERVICE_TOKEN"
    )
    service_token_file: Path | None = Field(
        default=None, validation_alias="LAZYCLOUD_CACHE_SERVICE_TOKEN_FILE"
    )
    previous_service_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="LAZYCLOUD_CACHE_SERVICE_PREVIOUS_TOKEN",
    )
    max_content_bytes: int = Field(
        default=20 * 1024 * 1024 * 1024,
        gt=0,
        validation_alias="LAZYCLOUD_CACHE_MAX_CONTENT_BYTES",
    )
    max_object_bytes: int = Field(
        default=8 * 1024 * 1024 * 1024,
        gt=0,
        validation_alias="LAZYCLOUD_CACHE_MAX_OBJECT_BYTES",
    )
    max_metadata_entries: int = Field(
        default=100_000,
        gt=0,
        validation_alias="LAZYCLOUD_CACHE_MAX_METADATA_ENTRIES",
    )
    max_cache_path_bytes: int = Field(
        default=4096,
        gt=0,
        validation_alias="LAZYCLOUD_CACHE_MAX_PATH_BYTES",
    )
    disk_max_usage_pct: float = Field(
        default=0.90,
        gt=0,
        le=1,
        validation_alias="LAZYCLOUD_CACHE_DISK_MAX_USAGE_PCT",
    )
    disk_evict_watermark_pct: float = Field(
        default=0.85,
        gt=0,
        le=1,
        validation_alias="LAZYCLOUD_CACHE_DISK_EVICT_WATERMARK_PCT",
    )

    @model_validator(mode="after")
    def validate_disk_watermarks(self) -> Self:
        if self.disk_evict_watermark_pct > self.disk_max_usage_pct:
            msg = "cache disk watermarks must satisfy 0 < evict <= max <= 1"
            raise ValueError(msg)
        return self

    def resolved_service_token(self) -> str:
        configured = self.service_token.get_secret_value()
        if configured:
            return configured
        if self.service_token_file is not None:
            token = self.service_token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        msg = "LAZYCLOUD_CACHE_SERVICE_TOKEN or LAZYCLOUD_CACHE_SERVICE_TOKEN_FILE is required"
        raise ValueError(msg)


class CacheServerArguments(argparse.Namespace):
    host: str
    port: int
    root: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=CACHE_SERVER_PROCESS_NAME)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--root", type=Path, default=state_home() / "cache")
    return parser


def serve_cache_directory(
    host: str,
    port: int,
    root: Path,
    *,
    service_token: str,
    previous_service_token: str = "",
    max_content_bytes: int = 20 * 1024 * 1024 * 1024,
    max_object_bytes: int = 8 * 1024 * 1024 * 1024,
    max_metadata_entries: int = 100_000,
    max_cache_path_bytes: int = 4096,
    disk_max_usage_pct: float = 0.90,
    disk_evict_watermark_pct: float = 0.85,
) -> None:
    WorkerCacheHttpService(
        FileCacheServer(
            root,
            max_content_bytes=max_content_bytes,
            max_object_bytes=max_object_bytes,
            max_metadata_entries=max_metadata_entries,
            max_cache_path_bytes=max_cache_path_bytes,
            disk_max_usage_pct=disk_max_usage_pct,
            disk_evict_watermark_pct=disk_evict_watermark_pct,
        ),
        service_token=service_token,
        previous_service_token=previous_service_token,
        host=host,
        port=port,
    ).serve_forever()


def main(argv: list[str] | None = None) -> None:
    args = CacheServerArguments()
    build_parser().parse_args(argv, namespace=args)
    settings = CacheServerSettings()
    serve_cache_directory(
        args.host,
        args.port,
        args.root,
        service_token=settings.resolved_service_token(),
        previous_service_token=settings.previous_service_token.get_secret_value(),
        max_content_bytes=settings.max_content_bytes,
        max_object_bytes=settings.max_object_bytes,
        max_metadata_entries=settings.max_metadata_entries,
        max_cache_path_bytes=settings.max_cache_path_bytes,
        disk_max_usage_pct=settings.disk_max_usage_pct,
        disk_evict_watermark_pct=settings.disk_evict_watermark_pct,
    )


if __name__ == "__main__":
    main()
