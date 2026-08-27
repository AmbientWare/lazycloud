"""Create the API-only Tailnet used by the local development deployment."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError

_API_ORIGIN = "https://api.tailscale.com"
_DEFAULT_DISPLAY_NAME = "LazyCloud Development"
_TAILNETS_PATH = "/api/v2/organizations/-/tailnets"
_TOKEN_PATH = "/api/v2/oauth/token"


class TailnetBootstrapError(RuntimeError):
    pass


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class _ApiError(_ApiModel):
    message: str = ""


class _OAuthToken(_ApiModel):
    access_token: SecretStr = Field(min_length=1)


class _Tailnet(_ApiModel):
    id: str = Field(min_length=1)
    display_name: str = Field(alias="displayName", min_length=1)
    dns_name: str | None = Field(alias="dnsName", default=None)


class _TailnetList(_ApiModel):
    tailnets: tuple[_Tailnet, ...]


class _ManagementClient(_ApiModel):
    id: str = Field(min_length=1)
    secret: SecretStr


class _CreatedTailnet(_Tailnet):
    dns_name: str = Field(alias="dnsName", min_length=1)
    oauth_client: _ManagementClient = Field(alias="oauthClient")


_TAILNET_SEQUENCE = TypeAdapter(tuple[_Tailnet, ...])


def _request(
    method: str,
    path: str,
    *,
    token: SecretStr | None = None,
    form: dict[str, str] | None = None,
    document: dict[str, str] | None = None,
) -> bytes:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token.get_secret_value()}"
    try:
        with httpx.Client(base_url=_API_ORIGIN, timeout=20) as client:
            response = client.request(
                method,
                path,
                headers=headers,
                data=form,
                json=document,
            )
    except httpx.RequestError as exc:
        raise TailnetBootstrapError(f"Tailscale API request failed: {exc}") from None
    if response.is_success:
        return response.content
    try:
        detail = _ApiError.model_validate_json(response.content).message.strip()
    except ValidationError:
        detail = ""
    suffix = f": {detail}" if detail else ""
    raise TailnetBootstrapError(
        f"Tailscale API {method} {path} returned HTTP {response.status_code}{suffix}"
    )


def _oauth_token() -> SecretStr:
    client_id = os.environ.get("TAILSCALE_BOOTSTRAP_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TAILSCALE_BOOTSTRAP_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise TailnetBootstrapError(
            "TAILSCALE_BOOTSTRAP_OAUTH_CLIENT_ID and "
            "TAILSCALE_BOOTSTRAP_OAUTH_CLIENT_SECRET are required"
        )
    content = _request(
        "POST",
        _TOKEN_PATH,
        form={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "tailnets",
        },
    )
    try:
        return _OAuthToken.model_validate_json(content).access_token
    except ValidationError:
        raise TailnetBootstrapError("Tailscale OAuth response has no access token") from None


def _tailnets(token: SecretStr) -> tuple[_Tailnet, ...]:
    content = _request("GET", _TAILNETS_PATH, token=token)
    try:
        return _TailnetList.model_validate_json(content).tailnets
    except ValidationError:
        try:
            return _TAILNET_SEQUENCE.validate_json(content)
        except ValidationError:
            raise TailnetBootstrapError("Tailscale Tailnet list response has no tailnets") from None


def _credential_output() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME", "").strip()
    config_home = Path(configured).expanduser() if configured else Path.home() / ".config"
    return config_home / "lazycloud" / "tailnet-development-terraform.env"


def _recorded_tailnet_id(path: Path) -> str:
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        if line.startswith("export TAILSCALE_TAILNET="):
            _, value = line.split("=", 1)
            try:
                words = shlex.split(value)
            except ValueError:
                return ""
            return words[0] if len(words) == 1 else ""
    return ""


def _write_credentials(
    path: Path,
    *,
    tailnet_id: str,
    client: _ManagementClient,
) -> None:
    if path.exists():
        raise TailnetBootstrapError(f"refusing to overwrite credential file {path}")
    parent_existed = path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not parent_existed:
        path.parent.chmod(0o700)
    lines = (
        f"export TAILSCALE_TAILNET={shlex.quote(tailnet_id)}\n"
        f"export TF_VAR_tailnet_id={shlex.quote(tailnet_id)}\n"
        f"export TAILSCALE_OAUTH_CLIENT_ID={shlex.quote(client.id)}\n"
        "export TAILSCALE_OAUTH_CLIENT_SECRET="
        f"{shlex.quote(client.secret.get_secret_value())}\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(lines)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _create_tailnet(token: SecretStr, display_name: str) -> _CreatedTailnet:
    content = _request(
        "POST",
        _TAILNETS_PATH,
        token=token,
        document={"displayName": display_name},
    )
    try:
        return _CreatedTailnet.model_validate_json(content)
    except ValidationError:
        raise TailnetBootstrapError(
            "Tailscale Tailnet creation response has no management client"
        ) from None


def _bootstrap(display_name: str, credential_output: Path) -> None:
    token = _oauth_token()
    matching = tuple(
        tailnet for tailnet in _tailnets(token) if tailnet.display_name == display_name
    )
    if len(matching) > 1:
        raise TailnetBootstrapError(
            f"multiple API-only Tailnets are named {display_name!r}; refusing to choose one"
        )
    if matching:
        tailnet_id = matching[0].id
        if _recorded_tailnet_id(credential_output) != tailnet_id:
            raise TailnetBootstrapError(
                f"API-only Tailnet {display_name!r} already exists as {tailnet_id}, but "
                f"{credential_output} does not contain its management credentials"
            )
        print(f"API-only Tailnet {display_name!r} already exists as {tailnet_id}")
        print(f"Terraform credentials remain in {credential_output}")
        return

    created = _create_tailnet(token, display_name)
    _write_credentials(
        credential_output,
        tailnet_id=created.id,
        client=created.oauth_client,
    )
    print(f"created API-only Tailnet {display_name!r} as {created.id} ({created.dns_name})")
    print(f"wrote Terraform credentials to {credential_output}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the API-only Tailnet used by LazyCloud development."
    )
    parser.add_argument("--display-name", default=_DEFAULT_DISPLAY_NAME)
    parser.add_argument(
        "--credential-output",
        type=Path,
        default=_credential_output(),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    display_name = args.display_name.strip()
    if not display_name:
        print("error: display name cannot be blank", file=sys.stderr)
        return 2
    credential_output = args.credential_output.expanduser().resolve()
    repository = Path(__file__).resolve().parents[2]
    if credential_output == repository or credential_output.is_relative_to(repository):
        print("error: credential output must be outside the repository", file=sys.stderr)
        return 2
    try:
        _bootstrap(display_name, credential_output)
    except TailnetBootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
