"""Prove one shared image archive outlives the deletion of a workspace that used it.

An image archive is global and content-addressed: one archive and one object key
per image id, reached by every workspace that holds an authorization row for that
id. Deleting one of two workspaces that share an image must therefore complete,
must leave the survivor able to mount the very same image, and must leave the
deleted workspace's credentials unable to resolve anything. Before archives became
global each workspace carried its own copy, so deleting one workspace destroyed
bytes a surviving workspace still referenced and then wedged the deletion itself on
a RESTRICT foreign key.

Prerequisites:

- the explicit `--live` opt-in, which authorizes mutation of the prepared target;
- an authenticated public lazycloud profile holding an administrator token and
  targeting the already-healthy root Compose stack, because creating and deleting
  a workspace is an administrator-only public operation;
- a stack that can build and run a Linux image, which `tests/e2e/README.md`
  describes for every local scenario that builds one.

Everything this scenario creates is uniquely named: two workspaces, one workspace
token in each, one image whose marker makes its content-addressed id unique to the
run, and three sandboxes. Sandboxes are terminated and both workspaces are deleted
through their public owner in cleanup. The shared archive is deliberately left to
the platform's own archive cleanup owner, which is the only owner of a global
archive no workspace references any more.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import quote

from lazycloud.abstractions.sandbox import Sandbox, SandboxInstance
from lazycloud.clients.image.control import ImageControlClient
from shared.http.errors import HttpApiError
from shared.http.images import VerifyImageBuildRequest
from shared.http.system import TokenCreateRequest, TokenCreateResponse
from shared.http.workspaces import (
    WorkspaceCreateRequest,
    WorkspaceListResponse,
    WorkspaceResponse,
)
from shared.http_transport import HttpChannel
from shared.identity import WorkspaceStatus
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

from lazycloud import App, Image

MARKER_DIRECTORY = "/opt/lazycloud-e2e"
MARKER_PATH = f"{MARKER_DIRECTORY}/image-archive-marker"
ADMIN_TIMEOUT_SECONDS = 60.0
IMAGE_BUILD_TIMEOUT_SECONDS = 900.0
SANDBOX_CREATE_TIMEOUT_SECONDS = 300.0
SANDBOX_COMMAND_TIMEOUT_SECONDS = 60.0
DELETION_TIMEOUT_SECONDS = 180.0
DELETION_POLL_SECONDS = 1.0
REJECTED_STATUS_CODES = frozenset({401, 403})
DELETED_WORKSPACE_STATUS_CODE = 404
"""A deleted workspace is not found; the account credential naming it stays valid."""


@dataclass(frozen=True, slots=True)
class _WorkspaceAccess:
    """One created workspace, and the account credential used to act inside it.

    The credential names the account, so both workspaces are reached with the same
    one and the workspace is named per request. What deletion has to revoke is
    therefore the workspace's own resources, not a credential bound to it.
    """

    id: str
    name: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _AdminApi:
    """The administrator-only public workspace routes this scenario needs."""

    channel: HttpChannel

    def create_workspace(self, name: str) -> WorkspaceResponse:
        return WorkspaceResponse.model_validate(
            self.channel.post(
                "/api/v1/workspaces",
                WorkspaceCreateRequest(name=name).model_dump(mode="json"),
            )
        )

    def create_account_token(self, *, name: str) -> str:
        response = TokenCreateResponse.model_validate(
            self.channel.post(
                "/api/v1/tokens",
                TokenCreateRequest(name=name).model_dump(mode="json"),
            )
        )
        return response.token

    def delete_workspace(self, workspace_id: str) -> None:
        self.channel.delete(f"/api/v1/workspaces/{quote(workspace_id, safe='')}")

    def workspace_record(self, workspace_id: str) -> WorkspaceResponse | None:
        listing = WorkspaceListResponse.model_validate(
            self.channel.get("/api/v1/workspaces?include_deleted=true")
        )
        return next((item for item in listing.workspaces if item.id == workspace_id), None)

    def is_listed_active(self, workspace_id: str) -> bool:
        listing = WorkspaceListResponse.model_validate(self.channel.get("/api/v1/workspaces"))
        return any(item.id == workspace_id for item in listing.workspaces)


def _shared_image(marker: str) -> Image:
    """The one image both workspaces resolve, marked so its id is unique to this run."""
    return Image(python_version="3.12").add_commands(
        [f"mkdir -p {MARKER_DIRECTORY} && printf '%s' {marker} > {MARKER_PATH}"]
    )


def _build_shared_image(marker: str, *, endpoint: str, access: _WorkspaceAccess) -> str:
    result = _shared_image(marker).build(
        ImageControlClient.from_endpoint(
            endpoint,
            token=access.token,
            workspace=access.name,
            timeout_seconds=IMAGE_BUILD_TIMEOUT_SECONDS,
        )
    )
    if not result.success or not result.image_id:
        detail = result.error or "the build reported no image id"
        raise RuntimeError(f"shared image build failed in {access.name}: {detail}")
    return result.image_id


def _bound_sandbox(
    app: App,
    image: Image,
    *,
    name: str,
    endpoint: str,
    access: _WorkspaceAccess,
) -> Sandbox:
    """A sandbox that acts with exactly one workspace's own credential."""
    sandbox = app.sandbox(
        name=name,
        image=image,
        cpu=0.25,
        memory="256Mi",
        keep_warm_seconds=120,
    )
    sandbox.endpoint = endpoint
    sandbox.token = access.token
    sandbox.workspace = access.name
    return sandbox


def _assert_marker(instance: SandboxInstance, marker: str, *, stage: str) -> None:
    result = instance.run(["cat", MARKER_PATH], timeout_seconds=SANDBOX_COMMAND_TIMEOUT_SECONDS)
    if result.exit_code != 0 or result.stdout.strip() != marker:
        raise RuntimeError(f"{stage}: the mounted image did not carry the shared archive marker")


def _terminate(instance: SandboxInstance, *, stage: str) -> None:
    if not instance.terminate():
        raise RuntimeError(f"{stage}: sandbox termination did not report success")


def _await_workspace_deleted(admin: _AdminApi, workspace_id: str) -> WorkspaceResponse:
    deadline = time.monotonic() + DELETION_TIMEOUT_SECONDS
    while True:
        record = admin.workspace_record(workspace_id)
        if record is not None and record.status is WorkspaceStatus.Deleted:
            if admin.is_listed_active(workspace_id):
                raise RuntimeError("the deleted workspace is still listed as active")
            return record
        if time.monotonic() >= deadline:
            observed = "absent" if record is None else record.status.value
            raise RuntimeError(f"workspace deletion did not reach its terminal state: {observed}")
        time.sleep(DELETION_POLL_SECONDS)


def _assert_deleted_workspace_resolves_nothing(
    image_id: str,
    *,
    endpoint: str,
    access: _WorkspaceAccess,
) -> int:
    """Naming the deleted workspace resolves nothing, whoever asks.

    The credential names the account and outlives the workspace, so what has to stop
    working is the workspace: the request is refused because there is no such
    workspace, not because the caller stopped being who they are.
    """
    client = ImageControlClient.from_endpoint(
        endpoint,
        token=access.token,
        workspace=access.name,
        timeout_seconds=ADMIN_TIMEOUT_SECONDS,
    )
    try:
        client.verify_image_build(VerifyImageBuildRequest(image_id=image_id))
    except HttpApiError as exc:
        if exc.status_code != DELETED_WORKSPACE_STATUS_CODE:
            raise RuntimeError(
                f"naming the deleted workspace failed for the wrong reason (HTTP {exc.status_code})"
            ) from exc
        return exc.status_code
    raise RuntimeError("the deleted workspace still resolved the shared image")


def _cleanup(
    admin: _AdminApi,
    instances: Sequence[SandboxInstance],
    workspace_ids: Sequence[str],
) -> None:
    failures: list[str] = []
    for instance in instances:
        if instance.terminated:
            continue
        try:
            _terminate(instance, stage="cleanup")
        except Exception as exc:
            failures.append(f"sandbox {instance.container_id}: {exc}")
    for workspace_id in workspace_ids:
        try:
            admin.delete_workspace(workspace_id)
            _await_workspace_deleted(admin, workspace_id)
        except Exception as exc:
            failures.append(f"workspace {workspace_id}: {exc}")
    if failures:
        raise RuntimeError("scenario cleanup failed: " + "; ".join(failures))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "shared image archive survival")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    endpoint = profile.resolved_endpoint()
    admin = _AdminApi(
        HttpChannel(endpoint=endpoint, token=profile.token, timeout_seconds=ADMIN_TIMEOUT_SECONDS)
    )
    suffix = secrets.token_hex(6)
    marker = f"image-archive-{secrets.token_hex(8)}"
    instances: list[SandboxInstance] = []
    workspace_ids: list[str] = []
    try:
        try:
            doomed_workspace = admin.create_workspace(f"e2e-archive-deleted-{suffix}")
        except HttpApiError as exc:
            if exc.status_code not in REJECTED_STATUS_CODES:
                raise
            return blocked(
                LivePrerequisiteError(
                    "the active lazycloud profile may not create workspaces; this scenario "
                    "requires an administrator token"
                )
            )
        workspace_ids.append(doomed_workspace.id)
        surviving_workspace = admin.create_workspace(f"e2e-archive-surviving-{suffix}")
        workspace_ids.append(surviving_workspace.id)
        account_token = admin.create_account_token(name=f"e2e-archive-{suffix}")
        doomed = _WorkspaceAccess(
            id=doomed_workspace.id,
            name=doomed_workspace.name,
            token=account_token,
        )
        surviving = _WorkspaceAccess(
            id=surviving_workspace.id,
            name=surviving_workspace.name,
            token=account_token,
        )

        doomed_image_id = _build_shared_image(marker, endpoint=endpoint, access=doomed)
        surviving_image_id = _build_shared_image(marker, endpoint=endpoint, access=surviving)
        if doomed_image_id != surviving_image_id:
            raise RuntimeError("one image spec resolved to two different ids across workspaces")
        image_id = doomed_image_id

        doomed_app = App(f"image_archive_doomed_{suffix}")
        doomed_instance = _bound_sandbox(
            doomed_app,
            _shared_image(marker),
            name="shared-image",
            endpoint=endpoint,
            access=doomed,
        ).create(timeout_seconds=SANDBOX_CREATE_TIMEOUT_SECONDS)
        instances.append(doomed_instance)
        _assert_marker(doomed_instance, marker, stage="workspace awaiting deletion")

        surviving_app = App(f"image_archive_surviving_{suffix}")
        surviving_instance = _bound_sandbox(
            surviving_app,
            _shared_image(marker),
            name="shared-image",
            endpoint=endpoint,
            access=surviving,
        ).create(timeout_seconds=SANDBOX_CREATE_TIMEOUT_SECONDS)
        instances.append(surviving_instance)
        _assert_marker(surviving_instance, marker, stage="surviving workspace before deletion")

        _terminate(doomed_instance, stage="workspace awaiting deletion")
        _terminate(surviving_instance, stage="surviving workspace before deletion")

        admin.delete_workspace(doomed.id)
        deleted_record = _await_workspace_deleted(admin, doomed.id)

        # `Image.from_id` cannot fall back to a rebuild, so mounting it is the
        # proof that the one shared archive still holds the image's bytes.
        restored_instance = _bound_sandbox(
            surviving_app,
            Image.from_id(image_id),
            name="shared-image-after-deletion",
            endpoint=endpoint,
            access=surviving,
        ).create(timeout_seconds=SANDBOX_CREATE_TIMEOUT_SECONDS)
        instances.append(restored_instance)
        _assert_marker(restored_instance, marker, stage="surviving workspace after deletion")

        rejected_status = _assert_deleted_workspace_resolves_nothing(
            image_id,
            endpoint=endpoint,
            access=doomed,
        )
        print(
            json.dumps(
                {
                    "capability": "workspace.deletion-preserves-shared-image-archive",
                    "deleted_workspace": doomed.name,
                    "deleted_workspace_request_status": rejected_status,
                    "deleted_workspace_status": deleted_record.status.value,
                    "image_id": image_id,
                    "surviving_container_id": restored_instance.container_id,
                    "surviving_workspace": surviving.name,
                },
                sort_keys=True,
            )
        )
    finally:
        _cleanup(admin, instances, workspace_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
