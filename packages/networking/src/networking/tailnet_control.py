from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from shared.app_identity import AGENT_NAME

DEFAULT_TAILSCALE_API_URL = "https://api.tailscale.com"
DEFAULT_TAILNET_AUTH_KEY_TTL_SECONDS = 300
DEFAULT_TAILNET_CONTROL_TIMEOUT_SECONDS = 10.0
TAILSCALE_OAUTH_SCOPES = "auth_keys devices:core"
TOKEN_REFRESH_SKEW_SECONDS = 30


class TailnetControlErrorCode(StrEnum):
    InvalidConfiguration = "invalid_configuration"
    AuthenticationFailed = "authentication_failed"
    PermissionDenied = "permission_denied"
    NotFound = "not_found"
    Conflict = "conflict"
    RateLimited = "rate_limited"
    UpstreamUnavailable = "upstream_unavailable"
    InvalidResponse = "invalid_response"
    VerificationFailed = "verification_failed"


class TailnetControlError(RuntimeError):
    def __init__(
        self,
        code: TailnetControlErrorCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class TailnetAuthKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    key: SecretStr
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def expiry_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("tailnet auth-key expiry must include a timezone")
        return value.astimezone(UTC)


class TailnetDevice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    hostname: str = Field(min_length=1)
    name: str = ""
    addresses: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    authorized: bool
    created_at: datetime | None = None
    last_seen_at: datetime | None = None


class TailnetControl(Protocol):
    def issue_auth_key(self, *, machine_id: str, hostname: str) -> TailnetAuthKey: ...

    def revoke_auth_key(self, key_id: str) -> None: ...

    def verify_device(
        self,
        node_id: str,
        *,
        expected_hostname: str,
    ) -> TailnetDevice: ...

    def find_devices(
        self,
        *,
        hostname: str,
    ) -> tuple[TailnetDevice, ...]: ...

    def remove_device(self, device_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class TailnetMachineCleanupResult:
    removed_device_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TailnetMachineIdentityReconciler:
    control: TailnetControl

    def cleanup(
        self,
        *,
        machine_id: str,
        generations: tuple[int, ...],
        auth_key_ids: tuple[str, ...] = (),
        device_ids: tuple[str, ...] = (),
    ) -> TailnetMachineCleanupResult:
        machine = _required_identifier(machine_id, "machine ID")
        normalized_generations = _unique_generations(generations)
        normalized_auth_key_ids = _unique_identifiers(auth_key_ids)
        normalized_device_ids = _unique_identifiers(device_ids)

        # Close every credential before discovery so a successful empty scan
        # cannot be followed by a new device from the generation being removed.
        for key_id in normalized_auth_key_ids:
            self.control.revoke_auth_key(key_id)

        removed_device_ids: set[str] = set()
        for device_id in normalized_device_ids:
            self.control.remove_device(device_id)
            removed_device_ids.add(device_id)
        for generation in normalized_generations:
            hostname = tailnet_machine_hostname(machine, generation)
            for device in self.control.find_devices(hostname=hostname):
                if device.id in removed_device_ids:
                    continue
                self.control.remove_device(device.id)
                removed_device_ids.add(device.id)
        return TailnetMachineCleanupResult(
            removed_device_ids=tuple(removed_device_ids),
        )


def tailnet_machine_hostname(machine_id: str, generation: int) -> str:
    machine = _required_identifier(machine_id, "machine ID")
    if generation < 1:
        raise TailnetControlError(
            TailnetControlErrorCode.InvalidConfiguration,
            "tailnet generation must be positive",
            retryable=False,
        )
    return f"{AGENT_NAME}-{machine}-g{generation}"


class TailscaleTailnetControlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_url: str = DEFAULT_TAILSCALE_API_URL
    oauth_client_id: str = Field(min_length=1)
    oauth_client_secret: SecretStr
    agent_tag: str = Field(min_length=5)
    auth_key_ttl_seconds: int = Field(
        default=DEFAULT_TAILNET_AUTH_KEY_TTL_SECONDS,
        ge=30,
        le=3600,
    )
    request_timeout_seconds: float = Field(
        default=DEFAULT_TAILNET_CONTROL_TIMEOUT_SECONDS,
        gt=0,
        le=120,
    )

    @field_validator("api_url")
    @classmethod
    def api_url_must_be_https_origin(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Tailscale API URL must be an HTTPS URL without query or fragment")
        return normalized

    @field_validator("oauth_client_id")
    @classmethod
    def client_id_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Tailscale OAuth client ID is required")
        return normalized

    @field_validator("oauth_client_secret")
    @classmethod
    def client_secret_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Tailscale OAuth client secret is required")
        return value

    @field_validator("agent_tag")
    @classmethod
    def agent_tag_must_be_valid(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.startswith("tag:") or len(normalized) == len("tag:"):
            raise ValueError("Tailscale agent tag must use the tag:<name> form")
        return normalized


class _TailscaleResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _OAuthTokenResponse(_TailscaleResponseModel):
    access_token: SecretStr = Field(min_length=1)
    token_type: str
    expires_in: int = Field(gt=0)
    scope: str = ""

    @field_validator("token_type")
    @classmethod
    def token_type_must_be_bearer(cls, value: str) -> str:
        if value.lower() != "bearer":
            raise ValueError("OAuth token type is not Bearer")
        return value


class _DeviceCreateCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reusable: bool = False
    ephemeral: bool = False
    preauthorized: bool = True
    tags: tuple[str, ...]


class _DeviceCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    create: _DeviceCreateCapability


class _AuthKeyCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    devices: _DeviceCapabilities


class _CreateAuthKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    capabilities: _AuthKeyCapabilities
    expiry_seconds: int = Field(alias="expirySeconds")
    description: str


class _CreateAuthKeyResponse(_TailscaleResponseModel):
    id: str = Field(min_length=1)
    key: SecretStr
    expires: datetime

    @field_validator("expires")
    @classmethod
    def expiry_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Tailscale auth-key response expiry has no timezone")
        return value.astimezone(UTC)


class _TailscaleDeviceResponse(_TailscaleResponseModel):
    id: str = Field(min_length=1)
    node_id: str = Field(alias="nodeId", min_length=1)
    hostname: str = Field(min_length=1)
    name: str = ""
    addresses: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    authorized: bool
    created: datetime | None = None
    last_seen: datetime | None = Field(default=None, alias="lastSeen")


class _TailscaleDeviceListResponse(_TailscaleResponseModel):
    devices: tuple[_TailscaleDeviceResponse, ...]


class _CachedOAuthToken(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: SecretStr
    refresh_at: datetime

    def usable_at(self, now: datetime) -> bool:
        return now < self.refresh_at


@dataclass(slots=True)
class TailscaleTailnetControl:
    config: TailscaleTailnetControlConfig
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    _token: _CachedOAuthToken | None = field(default=None, init=False, repr=False)
    _token_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def issue_auth_key(self, *, machine_id: str, hostname: str) -> TailnetAuthKey:
        machine = _required_identifier(machine_id, "machine ID")
        expected_hostname = _required_hostname(hostname)
        request = _CreateAuthKeyRequest(
            capabilities=_AuthKeyCapabilities(
                devices=_DeviceCapabilities(
                    create=_DeviceCreateCapability(tags=(self.config.agent_tag,))
                )
            ),
            expirySeconds=self.config.auth_key_ttl_seconds,
            description=f"machine {machine} {expected_hostname}",
        )
        response = self._authenticated_request(
            "POST",
            "/api/v2/tailnet/-/keys",
            content=request.model_dump_json(by_alias=True),
        )
        parsed = self._parse_response(response, _CreateAuthKeyResponse, "auth-key creation")
        return TailnetAuthKey(
            id=parsed.id,
            key=parsed.key,
            expires_at=parsed.expires,
        )

    def revoke_auth_key(self, key_id: str) -> None:
        normalized = _required_identifier(key_id, "auth-key ID")
        self._authenticated_request(
            "DELETE",
            f"/api/v2/tailnet/-/keys/{quote(normalized, safe='')}",
            allowed_status_codes=(200, 204, 404),
        )

    def get_device(self, device_id: str) -> TailnetDevice:
        normalized = _required_identifier(device_id, "device ID")
        response = self._authenticated_request(
            "GET",
            f"/api/v2/device/{quote(normalized, safe='')}?fields=all",
        )
        parsed = self._parse_response(response, _TailscaleDeviceResponse, "device lookup")
        return _tailnet_device(parsed)

    def find_devices(
        self,
        *,
        hostname: str,
        tag: str | None = None,
    ) -> tuple[TailnetDevice, ...]:
        expected_hostname = _required_hostname(hostname)
        expected_tag = self.config.agent_tag if tag is None else _required_tag(tag)
        response = self._authenticated_request("GET", "/api/v2/tailnet/-/devices?fields=all")
        parsed = self._parse_response(response, _TailscaleDeviceListResponse, "device list")
        return tuple(
            _tailnet_device(item)
            for item in parsed.devices
            if _device_name_matches(item, expected_hostname) and expected_tag in item.tags
        )

    def find_machine_devices(
        self,
        *,
        machine_id: str,
        tag: str | None = None,
    ) -> tuple[TailnetDevice, ...]:
        machine = _required_identifier(machine_id, "machine ID")
        expected_tag = self.config.agent_tag if tag is None else _required_tag(tag)
        response = self._authenticated_request("GET", "/api/v2/tailnet/-/devices?fields=all")
        parsed = self._parse_response(response, _TailscaleDeviceListResponse, "device list")
        return tuple(
            _tailnet_device(item)
            for item in parsed.devices
            if _machine_device_name_matches(item, machine) and expected_tag in item.tags
        )

    def verify_device(
        self,
        node_id: str,
        *,
        expected_hostname: str,
    ) -> TailnetDevice:
        normalized_node_id = _required_identifier(node_id, "node ID")
        normalized_hostname = _required_hostname(expected_hostname)
        response = self._authenticated_request("GET", "/api/v2/tailnet/-/devices?fields=all")
        parsed = self._parse_response(response, _TailscaleDeviceListResponse, "device list")
        matches = [device for device in parsed.devices if device.node_id == normalized_node_id]
        if not matches:
            raise TailnetControlError(
                TailnetControlErrorCode.NotFound,
                "tailnet node was not found",
                retryable=False,
            )
        if len(matches) > 1:
            raise TailnetControlError(
                TailnetControlErrorCode.Conflict,
                "tailnet node ID matched multiple devices",
                retryable=False,
            )
        device = _tailnet_device(matches[0])
        if device.node_id != normalized_node_id:
            raise TailnetControlError(
                TailnetControlErrorCode.VerificationFailed,
                "tailnet device response does not match the requested node",
                retryable=False,
            )
        if not _device_name_matches(device, normalized_hostname):
            raise TailnetControlError(
                TailnetControlErrorCode.VerificationFailed,
                "tailnet device hostname does not match the enrolled machine",
                retryable=False,
            )
        if self.config.agent_tag not in device.tags:
            raise TailnetControlError(
                TailnetControlErrorCode.VerificationFailed,
                "tailnet device does not have the configured machine tag",
                retryable=False,
            )
        if not device.authorized:
            raise TailnetControlError(
                TailnetControlErrorCode.VerificationFailed,
                "tailnet device is not authorized",
                retryable=False,
            )
        return device

    def remove_device(self, device_id: str) -> None:
        normalized = _required_identifier(device_id, "device ID")
        self._authenticated_request(
            "DELETE",
            f"/api/v2/device/{quote(normalized, safe='')}",
            allowed_status_codes=(200, 204, 404),
        )

    def _authenticated_request(
        self,
        method: str,
        path: str,
        *,
        content: str | None = None,
        allowed_status_codes: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        token = self._access_token()
        response = self._request(
            method,
            path,
            token=token,
            content=content,
        )
        if response.status_code == 401:
            self._invalidate_token(token)
            token = self._access_token()
            response = self._request(
                method,
                path,
                token=token,
                content=content,
            )
        if response.status_code not in allowed_status_codes:
            raise _http_error(method, path, response.status_code)
        return response

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: SecretStr,
        content: str | None,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token.get_secret_value()}",
        }
        if content is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self._client() as client:
                return client.request(method, path, content=content, headers=headers)
        except httpx.RequestError as exc:
            raise TailnetControlError(
                TailnetControlErrorCode.UpstreamUnavailable,
                f"Tailscale API request failed for {method} {path}",
                retryable=True,
            ) from exc

    def _access_token(self) -> SecretStr:
        now = datetime.now(UTC)
        cached = self._token
        if cached is not None and cached.usable_at(now):
            return cached.access_token
        with self._token_lock:
            now = datetime.now(UTC)
            cached = self._token
            if cached is not None and cached.usable_at(now):
                return cached.access_token
            response = self._request_oauth_token()
            parsed = self._parse_response(response, _OAuthTokenResponse, "OAuth token exchange")
            refresh_skew = min(TOKEN_REFRESH_SKEW_SECONDS, max(parsed.expires_in // 10, 1))
            token = _CachedOAuthToken(
                access_token=parsed.access_token,
                refresh_at=now + timedelta(seconds=parsed.expires_in - refresh_skew),
            )
            self._token = token
            return token.access_token

    def _request_oauth_token(self) -> httpx.Response:
        try:
            with self._client() as client:
                response = client.post(
                    "/api/v2/oauth/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.config.oauth_client_id,
                        "client_secret": self.config.oauth_client_secret.get_secret_value(),
                        "scope": TAILSCALE_OAUTH_SCOPES,
                        "tags": self.config.agent_tag,
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.RequestError as exc:
            raise TailnetControlError(
                TailnetControlErrorCode.UpstreamUnavailable,
                "Tailscale OAuth token exchange failed",
                retryable=True,
            ) from exc
        if response.status_code != 200:
            raise _http_error("POST", "/api/v2/oauth/token", response.status_code)
        return response

    def _invalidate_token(self, token: SecretStr) -> None:
        with self._token_lock:
            cached = self._token
            if cached is not None and cached.access_token == token:
                self._token = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.config.api_url,
            timeout=self.config.request_timeout_seconds,
            transport=self.transport,
        )

    @staticmethod
    def _parse_response[ResponseT: BaseModel](
        response: httpx.Response,
        response_type: type[ResponseT],
        operation: str,
    ) -> ResponseT:
        try:
            return response_type.model_validate_json(response.content)
        except ValueError as exc:
            raise TailnetControlError(
                TailnetControlErrorCode.InvalidResponse,
                f"Tailscale {operation} response was invalid",
                retryable=False,
            ) from exc


def _required_identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise TailnetControlError(
            TailnetControlErrorCode.InvalidConfiguration,
            f"{label} is required",
            retryable=False,
        )
    return normalized


def _required_hostname(value: str) -> str:
    normalized = value.strip().rstrip(".").lower()
    if not normalized:
        raise TailnetControlError(
            TailnetControlErrorCode.InvalidConfiguration,
            "machine hostname is required",
            retryable=False,
        )
    return normalized


def _required_tag(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized.startswith("tag:") or len(normalized) == len("tag:"):
        raise TailnetControlError(
            TailnetControlErrorCode.InvalidConfiguration,
            "tailnet device tag must use the tag:<name> form",
            retryable=False,
        )
    return normalized


def _unique_identifiers(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _unique_generations(values: tuple[int, ...]) -> tuple[int, ...]:
    if any(value < 1 for value in values):
        raise TailnetControlError(
            TailnetControlErrorCode.InvalidConfiguration,
            "tailnet generations must be positive",
            retryable=False,
        )
    return tuple(dict.fromkeys(values))


def _tailnet_device(response: _TailscaleDeviceResponse) -> TailnetDevice:
    return TailnetDevice(
        id=response.id,
        node_id=response.node_id,
        hostname=response.hostname,
        name=response.name,
        addresses=response.addresses,
        tags=response.tags,
        authorized=response.authorized,
        created_at=_as_utc(response.created),
        last_seen_at=_as_utc(response.last_seen),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise TailnetControlError(
            TailnetControlErrorCode.InvalidResponse,
            "Tailscale device timestamp has no timezone",
            retryable=False,
        )
    return value.astimezone(UTC)


def _device_name_matches(
    device: TailnetDevice | _TailscaleDeviceResponse,
    expected_hostname: str,
) -> bool:
    expected = expected_hostname.strip().rstrip(".").lower()
    hostname = device.hostname.strip().rstrip(".").lower()
    name = device.name.strip().rstrip(".").lower()
    name_matches_hostname = not name or name == hostname or name.startswith(f"{hostname}.")
    return name_matches_hostname and expected in (hostname, name)


def _machine_device_name_matches(
    device: TailnetDevice | _TailscaleDeviceResponse,
    machine_id: str,
) -> bool:
    hostname = device.hostname.strip().rstrip(".").lower()
    name = device.name.strip().rstrip(".").lower()
    prefix = f"{AGENT_NAME}-{machine_id}-g".lower()
    if not hostname.startswith(prefix):
        return False
    generation = hostname.removeprefix(prefix)
    if not generation.isdigit() or int(generation) < 1:
        return False
    return not name or name == hostname or name.startswith(f"{hostname}.")


def _http_error(method: str, path: str, status_code: int) -> TailnetControlError:
    if status_code == 401:
        code = TailnetControlErrorCode.AuthenticationFailed
        retryable = False
    elif status_code == 403:
        code = TailnetControlErrorCode.PermissionDenied
        retryable = False
    elif status_code == 404:
        code = TailnetControlErrorCode.NotFound
        retryable = False
    elif status_code == 409:
        code = TailnetControlErrorCode.Conflict
        retryable = False
    elif status_code == 429:
        code = TailnetControlErrorCode.RateLimited
        retryable = True
    elif status_code >= 500:
        code = TailnetControlErrorCode.UpstreamUnavailable
        retryable = True
    else:
        code = TailnetControlErrorCode.InvalidConfiguration
        retryable = False
    return TailnetControlError(
        code,
        f"Tailscale API returned status {status_code} for {method} {path}",
        retryable=retryable,
    )


__all__ = [
    "DEFAULT_TAILNET_AUTH_KEY_TTL_SECONDS",
    "DEFAULT_TAILSCALE_API_URL",
    "TailnetAuthKey",
    "TailnetControl",
    "TailnetControlError",
    "TailnetControlErrorCode",
    "TailnetDevice",
    "TailnetMachineCleanupResult",
    "TailnetMachineIdentityReconciler",
    "TailscaleTailnetControl",
    "TailscaleTailnetControlConfig",
    "tailnet_machine_hostname",
]
