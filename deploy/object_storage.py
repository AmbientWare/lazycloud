"""Publish and download deployment objects using the caller's storage identity."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
from storage_client.s3 import S3Credentials, S3ObjectStoreClient


def put_object(
    source: Path,
    *,
    bucket: str,
    key: str,
    content_type: str,
    cache_control: str,
    immutable: bool,
) -> None:
    store = S3ObjectStoreClient.from_settings(
        S3Credentials().transport_settings(bucket=bucket)
    )
    payload = source.read_bytes()
    try:
        try:
            store.put_bytes(
                key,
                payload,
                content_type=content_type,
                cache_control=cache_control,
                if_absent=immutable,
            )
        except ClientError as exc:
            if not immutable or exc.response.get("Error", {}).get("Code") != "PreconditionFailed":
                raise
        actual = store.read_bytes(key)
        if hashlib.sha256(actual).digest() != hashlib.sha256(payload).digest():
            raise RuntimeError("object differs from the intended publication")
    finally:
        store.close()


class _Arguments(argparse.Namespace):
    command: str
    uri: str
    file: Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("publish", "download"))
    parser.add_argument("--uri", required=True, help="s3://bucket/object-key")
    parser.add_argument("--file", required=True, type=Path)
    args = _Arguments()
    parser.parse_args(namespace=args)
    location = urlsplit(args.uri)
    if (
        location.scheme != "s3"
        or not location.netloc
        or not location.path.strip("/")
        or location.query
        or location.fragment
        or location.username
        or location.password
    ):
        parser.error("--uri must name one object-store bucket and object key")
    if args.command == "publish":
        from deploy.chart_values import Infrastructure

        Infrastructure.model_validate_json(args.file.read_bytes())
        put_object(
            args.file,
            bucket=location.netloc,
            key=location.path.lstrip("/"),
            content_type="application/json",
            cache_control="no-store",
            immutable=False,
        )
    else:
        store = S3ObjectStoreClient.from_settings(
            S3Credentials().transport_settings(bucket=location.netloc)
        )
        try:
            store.download_file(location.path.lstrip("/"), args.file)
        finally:
            store.close()


if __name__ == "__main__":
    main()
