from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from control.service import ControlPlaneService
from database.context import ServiceContext
from identity.auth import AuthService, BootstrapAdminToken, IdentityDatabaseContext
from identity.credential_files import CredentialFileError, CredentialFilePublication
from lazycloud.cli.components.output import print_payload
from shared.errors import ConflictError
from shared.identity import WorkspaceStorageConfig
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings

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
    github_user_id: Annotated[
        int | None,
        typer.Option(
            "--github-user-id",
            help=(
                "Numeric GitHub user id allowed to sign in as the first administrator. "
                "Resolve it with curl -s https://api.github.com/users/<login>. Without "
                "it the account is reachable only by its token."
            ),
        ),
    ] = None,
    github_login: Annotated[str, typer.Option("--github-login")] = "",
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
    """Create the first administrator: the account, its workspace, and a credential.

    The credential never depends on an identity provider, which is the point of it:
    it has to work when the provider is what is broken. Naming a GitHub id is
    additive and says who may also reach the account through the dashboard, so a
    rebuilt stack lands its operator straight back in without a second account.
    """
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
    storage_client = S3ObjectStoreClient.from_settings(S3ObjectStoreSettings())
    try:
        service = AuthService(IdentityDatabaseContext(database))
        if configured_token is not None:
            result = service.bootstrap_administrator(
                request_id=request_id,
                name=name,
                workspace=workspace,
                github_user_id=github_user_id,
                github_login=github_login,
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
                create=lambda staged, stage: service.bootstrap_administrator(
                    request_id=request_id,
                    name=name,
                    workspace=workspace,
                    github_user_id=github_user_id,
                    github_login=github_login,
                    staged_token=staged,
                    stage_token=stage,
                ),
            )
        service.mark_admin_token_published(request_id=request_id, recovery=False)
        # After the credential, because that is the call that creates the workspace.
        # Named rather than taken from the token: an administrator credential belongs
        # to a person now, so it carries no workspace of its own.
        storage = _provision_workspace_storage(database, storage_client, workspace)
    finally:
        storage_client.close()
        database.dispose()
    print_payload(
        ctx,
        {
            "status": "already_published" if result.replayed else "created",
            "request_id": request_id,
            "workspace": workspace,
            "user_id": result.user_id,
            "token_id": result.record.id,
            "credential_source": "configured_file" if configured_token is not None else "generated",
            "output": str(publication.resolved_output) if publication is not None else None,
            "mode": "0600" if publication is not None else None,
            "workspace_storage_bucket": storage.bucket,
            "workspace_storage_backend": storage.backend,
        },
    )


def _provision_workspace_storage(
    database: DatabaseClient,
    storage_client: S3ObjectStoreClient,
    workspace: str,
) -> WorkspaceStorageConfig:
    """Give the bootstrap workspace its storage before anyone can use it.

    This is the one workspace not created through `create_workspace`, so it is
    the one that would otherwise exist without a bucket. Doing it here, in the
    one-shot job the control plane already waits on, keeps the API's own start
    free of object-store I/O: a process that cannot reach storage should fail
    at the operation that needs it, not refuse to serve the routes that do not.
    """
    context = ServiceContext.create(database, create_schema=False)
    service = ControlPlaneService(context, workspace_storage_client=storage_client)
    record = service.ensure_workspace_storage(workspace)
    return record.storage


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
    """Re-establish administrator access for the bootstrapped account.

    It takes no selector for which account to restore. The account is the one the
    bootstrap credential was minted against, so recovery cannot name the wrong one —
    which matters when there is no second credential left to undo a mistake with.
    """
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
            "workspace": workspace,
            "user_id": result.user_id,
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


def read_private_file(path: Path, *, description: str) -> str:
    """Read a secret from a file the caller alone can read.

    Refuses a symlink, a non-regular file, group/world-readable modes, and anything
    unexpectedly large, so pointing this at the wrong path fails instead of quietly
    loading whatever was there.
    """
    resolved = Path(os.path.abspath(os.fspath(path.expanduser())))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags)
    except FileNotFoundError as exc:
        raise CredentialFileError(f"{description} does not exist: {resolved}") from exc
    except OSError as exc:
        raise CredentialFileError(f"{description} cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CredentialFileError(f"{description} is not a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CredentialFileError(f"{description} must not be group/world accessible")
        if metadata.st_size > 4096:
            raise CredentialFileError(f"{description} is unexpectedly large")
        payload = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if len(payload) > 4096:
        raise CredentialFileError(f"{description} is unexpectedly large")
    try:
        return payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise CredentialFileError(f"{description} is not UTF-8 text") from exc


def _read_configured_token(path: Path) -> str | None:
    value = read_private_file(path, description="configured credential file")
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
