"""Complete one CLI device login and revoke the account token it mints.

Requires an authenticated account profile. Uses an isolated CLI home, approves
the announced code as that account, and revokes only its minted token.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO
from urllib.parse import quote, urlsplit

import yaml
from lazycloud.cli.identity import device_login_client_name
from lazycloud.config import ClientConfig
from pydantic import BaseModel
from shared.http.device_auth import DeviceCodeResponse
from shared.http.system import AuthTokenResponse, TokenListResponse
from shared.http.workspaces import WorkspaceListResponse
from shared.http_transport import HttpChannel
from shared.identity import DeviceAuthorizationStatus, TokenStatus
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

ROOT = Path(__file__).resolve().parents[4]
USER_CODE = re.compile(r"\b[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}\b")


class LoginOutput(BaseModel):
    name: str
    token: str
    token_source: str
    workspace: str


def _tokens(channel: HttpChannel) -> list[AuthTokenResponse]:
    return TokenListResponse.model_validate(channel.get("/api/v1/tokens")).data


def _live_token_ids(channel: HttpChannel) -> set[str]:
    return {token.id for token in _tokens(channel) if token.status is TokenStatus.Active}


def _read_stream(stream: TextIO, received: queue.Queue[str | None]) -> None:
    for line in stream:
        received.put(line)
    received.put(None)


def _approve(
    process: subprocess.Popen[str],
    *,
    admin: HttpChannel,
    timeout_seconds: float,
) -> str:
    if process.stderr is None:
        raise RuntimeError("CLI login did not expose its device code stream")
    received: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(
        target=_read_stream,
        args=(process.stderr, received),
        name="e2e-device-login-stderr",
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    buffered = ""
    try:
        while time.monotonic() < deadline:
            try:
                line = received.get(timeout=0.25)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if line is None:
                break
            buffered += line
            match = USER_CODE.search(buffered)
            if match is None:
                continue
            user_code = match.group(0)
            # Approval names no workspace: it grants the CLI the approving account's
            # own reach, and the credential it claims covers every workspace that
            # account belongs to.
            approved = DeviceCodeResponse.model_validate(
                admin.post(f"/api/v1/device-codes/{quote(user_code, safe='')}/approve", {})
            )
            if approved.status is not DeviceAuthorizationStatus.Approved:
                raise RuntimeError("device login approval did not reach approved")
            process.wait(timeout=max(deadline - time.monotonic(), 0.1))
            return user_code
        raise RuntimeError("CLI login emitted no device code before timeout")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        reader.join(timeout=3)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(
            argv,
            description=__doc__ or "CLI device login",
        )
    except LivePrerequisiteError as exc:
        return blocked(exc)

    endpoint = profile.resolved_endpoint().rstrip("/")
    workspace = profile.workspace
    admin = HttpChannel(endpoint=endpoint, token=profile.token)
    workspaces = WorkspaceListResponse.model_validate(admin.get("/api/v1/workspaces")).workspaces
    matches = [item for item in workspaces if item.name == workspace]
    if len(matches) != 1:
        raise RuntimeError("device-login workspace is unavailable or ambiguous")
    before = _live_token_ids(admin)
    existing_ids = {token.id for token in _tokens(admin)}
    minted: AuthTokenResponse | None = None
    claimed_token = ""
    profile = f"e2e-device-{time.time_ns()}"
    try:
        with tempfile.TemporaryDirectory(prefix="lazycloud-device-login-") as home:
            child_environment = dict(os.environ)
            child_environment["LAZYCLOUD_HOME"] = home
            for name in ("LAZYCLOUD_CONFIG", "LAZYCLOUD_PROFILE"):
                child_environment.pop(name, None)
            # Emptied rather than removed: settings read a dotenv from the working
            # directory too, and a credential from there would send the CLI straight
            # past the device flow this scenario exists to exercise.
            child_environment["GATEWAY_TOKEN"] = ""
            child_environment["LAZYCLOUD_TOKEN"] = ""
            tls_flag = "--tls" if urlsplit(endpoint).scheme == "https" else "--no-tls"
            process = subprocess.Popen(
                (
                    str(Path(sys.executable).parent / "lazycloud"),
                    "--json",
                    "login",
                    "--profile",
                    profile,
                    "--endpoint",
                    endpoint,
                    "--workspace",
                    workspace,
                    tls_flag,
                ),
                cwd=ROOT,
                env=child_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _approve(
                process,
                admin=admin,
                timeout_seconds=60,
            )
            stdout = process.stdout.read() if process.stdout is not None else ""
            if process.returncode != 0:
                raise RuntimeError("public CLI device login failed")
            configured = ClientConfig.model_validate(
                yaml.safe_load(Path(home, "config.yaml").read_text())
            )
            claimed_token = configured.profiles[profile].token
            result = LoginOutput.model_validate_json(stdout)
            if (
                result.name != profile
                or result.workspace != workspace
                or result.token != "set"
                or result.token_source != "device"
            ):
                raise RuntimeError("public CLI device login returned an invalid outcome")
        created = [
            token
            for token in _tokens(admin)
            if token.id not in existing_ids
            and token.prefix
            and claimed_token.startswith(token.prefix)
        ]
        if len(created) != 1:
            raise RuntimeError("device login did not mint exactly one token")
        minted = created[0]
        # A device login mints an account credential, so the row names the person who
        # approved it and no workspace at all.
        if minted.name != device_login_client_name() or minted.workspace_id:
            raise RuntimeError("device login minted a token for the wrong owner")
        print(
            json.dumps(
                {
                    "capability": "authorization.device-login",
                    "token_id": minted.id,
                    "workspace": workspace,
                }
            )
        )
    finally:
        if minted is None and claimed_token:
            candidates = [
                token
                for token in _tokens(admin)
                if token.id not in existing_ids
                and token.prefix
                and claimed_token.startswith(token.prefix)
            ]
            if len(candidates) == 1:
                minted = candidates[0]
        if minted is not None:
            admin.request("POST", f"/api/v1/tokens/{quote(minted.id, safe='')}/revoke")
        # Revocation keeps the row, so the baseline is which credentials still work.
        after = _live_token_ids(admin)
        if not before.issubset(after) or (minted is not None and minted.id in after):
            raise RuntimeError("device-login token cleanup left a live credential behind")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
