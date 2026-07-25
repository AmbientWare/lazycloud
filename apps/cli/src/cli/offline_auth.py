from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from identity.auth import AuthService, BootstrapAdminToken, IdentityDatabaseContext
from identity.credential_files import CredentialFileError, CredentialFilePublication
from lazycloud.cli.components.output import print_payload
from shared.errors import ConflictError

from database import (
    ControlPlaneRecoveryFence,
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
)

auth_app = typer.Typer(help="Bootstrap or recover control-plane administrator access offline.")


@auth_app.command("bootstrap")
def bootstrap_admin(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            dir_okay=False,
            resolve_path=True,
            help="Mode-0600 destination required when generating the administrator token.",
        ),
    ] = None,
    name: Annotated[str, typer.Option("--name")] = "first-admin",
    workspace: Annotated[str, typer.Option("--workspace")] = "default",
    token_file: Annotated[
        Path | None,
        typer.Option(
            "--token-file",
            dir_okay=False,
            resolve_path=True,
            help="Optional private file containing a stable administrator credential.",
        ),
    ] = None,
) -> None:
    configured_token = _read_configured_token(token_file) if token_file is not None else None
    if output is None and configured_token is None:
        raise CredentialFileError("--output is required without a configured credential file")
    request_id = (
        _bootstrap_request_id(output)
        if output is not None
        else "bootstrap:configured-administrator"
    )
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin)
    )
    try:
        service = AuthService(IdentityDatabaseContext(database))
        if configured_token is not None:
            result = service.bootstrap_admin_token(
                request_id=request_id,
                name=name,
                workspace=workspace,
                configured_token=configured_token,
            )
            publication = None
        else:
            if output is None:
                raise CredentialFileError(
                    "--output is required without a configured credential file"
                )
            publication = CredentialFilePublication(output, request_id)
            current_request = service.bootstrap_request_id()
            result = _publish_admin_credential(
                publication=publication,
                request_exists=current_request == request_id,
                create=lambda staged, stage: service.bootstrap_admin_token(
                    request_id=request_id,
                    name=name,
                    workspace=workspace,
                    staged_token=staged,
                    stage_token=stage,
                ),
            )
        service.mark_admin_token_published(request_id=request_id, recovery=False)
    finally:
        database.dispose()
    print_payload(
        ctx,
        {
            "status": "already_published" if result.replayed else "created",
            "request_id": request_id,
            "workspace_id": result.record.workspace_id,
            "token_id": result.record.id,
            "credential_source": "configured_file" if configured_token is not None else "generated",
            "output": str(publication.resolved_output) if publication is not None else None,
            "mode": "0600" if publication is not None else None,
        },
    )


@auth_app.command("recover")
def recover_admin(
    ctx: typer.Context,
    request_id: Annotated[
        str,
        typer.Option(
            "--request-id",
            help="Required incident or operator request ID retained in the audit trail.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            dir_okay=False,
            resolve_path=True,
            help="Required mode-0600 destination for the one-time recovery token.",
        ),
    ],
    name: Annotated[str, typer.Option("--name")] = "recovery-admin",
    workspace: Annotated[str, typer.Option("--workspace")] = "default",
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin)
    )
    try:
        fence = ControlPlaneRecoveryFence(database)
        if not fence.supported:
            raise ConflictError("offline administrator recovery requires PostgreSQL")
        with fence.offline_recovery() as acquired:
            if not acquired:
                raise ConflictError(
                    "the control plane is still serving; stop every API replica before recovery"
                )
            service = AuthService(IdentityDatabaseContext(database))
            publication = CredentialFilePublication(output, f"recovery:{request_id}")
            result = _publish_admin_credential(
                publication=publication,
                request_exists=service.recovery_request_exists(request_id),
                create=lambda staged, stage: service.recover_admin_token(
                    request_id=request_id,
                    name=name,
                    workspace=workspace,
                    staged_token=staged,
                    stage_token=stage,
                ),
            )
            service.mark_admin_token_published(request_id=request_id, recovery=True)
    finally:
        database.dispose()
    print_payload(
        ctx,
        {
            "status": "already_published" if result.replayed else "created",
            "request_id": request_id,
            "workspace_id": result.record.workspace_id,
            "token_id": result.record.id,
            "output": str(publication.resolved_output),
            "mode": "0600",
        },
    )


def _publish_admin_credential(
    *,
    publication: CredentialFilePublication,
    request_exists: bool,
    create: Callable[
        [str | None, Callable[[str], None] | None],
        BootstrapAdminToken,
    ],
) -> BootstrapAdminToken:
    published = publication.read_published()
    staged = publication.read_staged()
    if request_exists:
        candidate = published or staged
        if candidate is None:
            raise CredentialFileError(
                "the committed request has no staged credential; use a new offline recovery request"
            )
        result = create(candidate, None)
        if published is not None:
            publication.secure_published_mode()
            if staged is not None:
                publication.discard_staged()
        else:
            publication.publish(replace=False)
        return result
    if published is not None:
        raise CredentialFileError(
            f"refusing to overwrite existing credential output: {publication.resolved_output}"
        )
    if staged is not None:
        publication.discard_staged()
    result = create(None, publication.stage)
    publication.publish(replace=False)
    return result


def _read_configured_token(path: Path) -> str | None:
    resolved = Path(os.path.abspath(os.fspath(path.expanduser())))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags)
    except FileNotFoundError as exc:
        raise CredentialFileError(f"configured credential file does not exist: {resolved}") from exc
    except OSError as exc:
        raise CredentialFileError("configured credential path cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CredentialFileError("configured credential path is not a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CredentialFileError(
                "configured credential file must not be group/world accessible"
            )
        if metadata.st_size > 4096:
            raise CredentialFileError("configured credential file is unexpectedly large")
        payload = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if len(payload) > 4096:
        raise CredentialFileError("configured credential file is unexpectedly large")
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise CredentialFileError("configured credential file is not UTF-8 text") from exc
    if not value:
        return None
    if re.fullmatch(r"rt_[A-Za-z0-9_-]{43}", value) is None:
        raise CredentialFileError("configured credential file does not contain one valid token")
    return value


def _bootstrap_request_id(output: Path) -> str:
    resolved = str(output.expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:32]
    return f"bootstrap:{digest}"


__all__ = ["auth_app"]
