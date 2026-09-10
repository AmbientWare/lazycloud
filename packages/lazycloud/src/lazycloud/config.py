from __future__ import annotations

import errno
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from secrets import token_hex

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.paths import state_home

from lazycloud.json_contracts import validate_json_object

DEFAULT_PROFILE = "default"

# The profile file stores bearer tokens, so it is owner-only and every write
# goes through `_write_config_document`. The directory is created owner-only for
# the same reason: a token file is only as private as the directory it can be
# renamed out of.
CONFIG_FILE_MODE = 0o600
CONFIG_DIRECTORY_MODE = 0o700

# Placeholder hosted control-plane endpoint shipped in the published `lazycloud`
# dist. It is the final fallback in the endpoint resolution order (flag > env >
# stored profile > this default), so plain `lazycloud login` targets the hosted
# platform with no configuration. Swapped for the real hosted domain at first
# publish.
PACKAGED_DEFAULT_ENDPOINT = "https://lazycloud.dev"

# `open(O_NOFOLLOW)` on a symlink reports `ELOOP` on Linux and macOS and
# `EMLINK` on the BSDs; both mean the same refusal here.
_SYMLINK_REFUSAL_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK})


class ConfigError(RuntimeError):
    pass


class ClientProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = DEFAULT_PROFILE
    endpoint: str = ""
    # Blank, not `default`. A client that fills in a name sends one on every
    # request, and the control plane never gets to resolve the account's own.
    workspace: str = ""
    # `repr=False` keeps the bearer token out of every rendering of a profile:
    # tracebacks, `--debug` output, assertion diffs, and anything that formats
    # the model or a container holding it. The stored value stays a plain string
    # because clients hand it straight to the transport layer.
    token: str = Field(default="", repr=False)
    tls: bool = False

    def resolved_endpoint(self) -> str:
        """The profile's endpoint, falling back to the packaged default.

        A blank profile endpoint is not an error: the published dist always has
        a hosted default to target, so this always yields a usable endpoint.
        """
        return self.endpoint.strip() or PACKAGED_DEFAULT_ENDPOINT


class ClientConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    active_profile: str = DEFAULT_PROFILE
    profiles: dict[str, ClientProfile] = Field(
        default_factory=lambda: {DEFAULT_PROFILE: ClientProfile()}
    )


class ClientSettings(BaseSettings):
    """Where the CLI is pointed: the environment, then the stored profile.

    A `.env` in the working directory is deliberately not read. It would make
    the target a property of whichever directory the operator is standing in,
    and it outranks an explicitly activated profile.
    """

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_",
        extra="ignore",
    )

    home: Path = Field(default_factory=state_home)
    config: Path | None = None
    profile: str = ""
    endpoint: str = ""
    workspace: str = ""
    token: str = ""
    tls: bool | None = None

    @property
    def home_path(self) -> Path:
        return self.home.expanduser().resolve()

    @property
    def config_path(self) -> Path:
        """The profile file, with its final component left unresolved.

        Only the containing directory is resolved. Resolving the whole path
        would silently follow a symlink planted at the profile file itself,
        which is exactly what the read and write paths have to see and refuse.
        """
        if self.config is not None:
            configured = self.config.expanduser()
            return configured.parent.resolve() / configured.name
        return self.home_path / "config.yaml"


@dataclass(frozen=True, slots=True)
class _ConfigFileIdentity:
    """The exact profile file a read observed.

    Compared again immediately before the atomic replace so an update never
    overwrites a profile another process wrote in the meantime.
    """

    device: int
    inode: int
    modified_ns: int
    size: int

    @classmethod
    def observed(cls, status: os.stat_result) -> _ConfigFileIdentity:
        return cls(
            device=status.st_dev,
            inode=status.st_ino,
            modified_ns=status.st_mtime_ns,
            size=status.st_size,
        )


@lru_cache(maxsize=1)
def settings() -> ClientSettings:
    return ClientSettings()


def reset_settings_cache() -> None:
    settings.cache_clear()


def empty_config() -> dict[str, JsonValue]:
    return _config_data(ClientConfig())


def load_config(*, replace_legacy: bool = False) -> dict[str, JsonValue]:
    return _config_data(_load_client_config(replace_legacy=replace_legacy))


def save_config(config: ClientConfig | Mapping[str, JsonValue]) -> None:
    """Replace the whole stored profile config."""
    normalized = _normalize_config(config)
    _write_config_document(
        _config_data(normalized),
        expected=_config_identity(settings().config_path),
    )


def list_profiles() -> list[ClientProfile]:
    return list(_load_client_config().profiles.values())


def active_profile_name() -> str:
    configured = settings().profile.strip()
    if configured:
        return configured
    return _load_client_config().active_profile


def get_profile(name: str | None = None, *, apply_env: bool = True) -> ClientProfile:
    config = _load_client_config()
    selected = name or settings().profile.strip() or config.active_profile
    profile = config.profiles.get(selected)
    if profile is None:
        if selected == DEFAULT_PROFILE:
            profile = ClientProfile()
        else:
            msg = f"profile not found: {selected}"
            raise KeyError(msg)
    if not apply_env:
        return profile
    return _apply_environment_overrides(profile)


def set_profile(
    profile: ClientProfile,
    *,
    activate: bool = True,
    replace_legacy: bool = False,
) -> ClientProfile:
    config, observed = _read_client_config(replace_legacy=replace_legacy)
    config.profiles[profile.name] = profile
    if activate:
        config.active_profile = profile.name
    _write_config_document(_config_data(config), expected=observed)
    return profile


def activate_profile(name: str) -> ClientProfile:
    config, observed = _read_client_config()
    profile = config.profiles.get(name)
    if profile is None:
        msg = f"profile not found: {name}"
        raise KeyError(msg)
    config.active_profile = profile.name
    _write_config_document(_config_data(config), expected=observed)
    return profile


def delete_profile(name: str) -> None:
    if name == DEFAULT_PROFILE:
        msg = "the default profile cannot be deleted"
        raise ValueError(msg)
    config, observed = _read_client_config()
    config.profiles.pop(name, None)
    if config.active_profile == name:
        config.active_profile = DEFAULT_PROFILE
    _write_config_document(_config_data(config), expected=observed)


def _load_client_config(*, replace_legacy: bool = False) -> ClientConfig:
    return _read_client_config(replace_legacy=replace_legacy)[0]


def _read_client_config(
    *,
    replace_legacy: bool = False,
) -> tuple[ClientConfig, _ConfigFileIdentity | None]:
    path = settings().config_path
    loaded, identity = _read_config_document(path)
    if _is_legacy_context_config(loaded):
        if replace_legacy:
            return ClientConfig(), identity
        msg = "legacy context config found; run `lazycloud login` to write a profile config"
        raise ConfigError(msg)
    raw_profiles = loaded.get("profiles")
    if raw_profiles is None:
        profiles_value = empty_config()["profiles"]
        if not isinstance(profiles_value, dict):
            raise ConfigError("default profiles must be an object")
        raw_profiles = profiles_value
    elif not isinstance(raw_profiles, dict):
        msg = "config profiles must be an object"
        raise ConfigError(msg)
    normalized_profiles: dict[str, ClientProfile] = {}
    for profile_name, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            msg = f"profile config must be an object: {profile_name}"
            raise ConfigError(msg)
        normalized_profiles[str(profile_name)] = _validated_profile(str(profile_name), raw_profile)
    normalized_profiles.setdefault(DEFAULT_PROFILE, ClientProfile())
    return _validated_config(loaded, normalized_profiles), identity


def _read_config_document(
    path: Path,
) -> tuple[dict[str, JsonValue], _ConfigFileIdentity | None]:
    """Read the profile file through a descriptor that cannot be swapped.

    `O_NOFOLLOW` refuses a symlink planted at the profile path, and every
    subsequent check runs against the opened inode rather than the name, so a
    replacement between checking and reading cannot redirect the read. A file
    left readable by other users is tightened on that same descriptor: the
    process has already proved it owns a regular file, so the repair cannot be
    aimed at anything else.
    """
    import yaml

    descriptor = _open_config_for_read(path)
    if descriptor is None:
        return {}, None
    try:
        status = os.fstat(descriptor)
        _require_private_regular_file(status, path)
        if stat.S_IMODE(status.st_mode) != CONFIG_FILE_MODE:
            os.fchmod(descriptor, CONFIG_FILE_MODE)
        identity = _ConfigFileIdentity.observed(status)
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        try:
            raw_loaded = yaml.safe_load(handle)
        except yaml.YAMLError:
            # Raised without the cause: PyYAML quotes the offending source line,
            # which for this file can be the token itself.
            msg = f"profile config is not valid YAML: {path}"
            raise ConfigError(msg) from None
    if raw_loaded is None:
        return {}, identity
    try:
        return validate_json_object(raw_loaded), identity
    except ValidationError:
        msg = f"config file must contain an object: {path}"
        raise ConfigError(msg) from None


def _open_config_for_read(path: Path) -> int | None:
    try:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in _SYMLINK_REFUSAL_ERRNOS:
            msg = f"profile config is a symlink; refusing to read it: {path}"
            raise ConfigError(msg) from None
        raise


def _config_identity(path: Path) -> _ConfigFileIdentity | None:
    """Identify the current profile file without following a symlink."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(status.st_mode):
        msg = f"profile config is a symlink; refusing to replace it: {path}"
        raise ConfigError(msg)
    _require_private_regular_file(status, path)
    return _ConfigFileIdentity.observed(status)


def _require_private_regular_file(status: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(status.st_mode):
        msg = f"profile config is not a regular file: {path}"
        raise ConfigError(msg)
    if status.st_uid != os.getuid():
        msg = f"profile config is owned by another user: {path}"
        raise ConfigError(msg)


def _write_config_document(
    data: dict[str, JsonValue],
    *,
    expected: _ConfigFileIdentity | None,
) -> None:
    """Publish the profile document as an owner-only atomic replacement.

    The document is written to a fresh same-directory file created `0600` with
    `O_EXCL | O_NOFOLLOW`, flushed to disk, then moved into place with
    `os.replace`. A reader therefore sees either the previous complete document
    or the new one, and the published file is never widened: a profile file that
    was already readable by other users is repaired by this replacement rather
    than by chmod-ing a path that could have been swapped underneath it.
    """
    import yaml

    path = settings().config_path
    directory = _prepared_config_directory(path.parent)
    if _config_identity(path) != expected:
        msg = f"profile config changed on disk during the update; re-run the command: {path}"
        raise ConfigError(msg)
    payload = yaml.safe_dump(data, sort_keys=True).encode("utf-8")
    staged = directory / f".{path.name}.{os.getpid()}.{token_hex(8)}"
    try:
        descriptor = os.open(
            staged,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            CONFIG_FILE_MODE,
        )
        try:
            # `os.open` masks the requested mode with the process umask, so set
            # it explicitly on the descriptor before anything is written.
            os.fchmod(descriptor, CONFIG_FILE_MODE)
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(staged, path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    _fsync_directory(directory)


def _prepared_config_directory(directory: Path) -> Path:
    try:
        status = directory.stat()
    except FileNotFoundError:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, CONFIG_DIRECTORY_MODE)
        status = directory.stat()
    if not stat.S_ISDIR(status.st_mode):
        msg = f"profile config directory is not a directory: {directory}"
        raise ConfigError(msg)
    if status.st_uid != os.getuid():
        msg = f"profile config directory is owned by another user: {directory}"
        raise ConfigError(msg)
    mode = stat.S_IMODE(status.st_mode)
    if mode & stat.S_IWOTH and not mode & stat.S_ISVTX:
        # Any user could rename a planted file over the profile file. Group
        # write is left alone deliberately: it is unreadable through a private
        # user group or a restrictive ancestor, and a bare mode check cannot
        # tell those apart from a genuinely shared directory.
        msg = f"profile config directory is writable by every user: {directory}"
        raise ConfigError(msg)
    return directory


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _normalize_config(config: ClientConfig | Mapping[str, JsonValue]) -> ClientConfig:
    if isinstance(config, ClientConfig):
        normalized = config
    else:
        raw_profiles = config.get("profiles", {})
        if not isinstance(raw_profiles, dict):
            msg = "config profiles must be an object"
            raise ConfigError(msg)
        profiles: dict[str, ClientProfile] = {}
        for name, raw in raw_profiles.items():
            if not isinstance(raw, dict):
                msg = f"profile config must be an object: {name}"
                raise ConfigError(msg)
            profiles[str(name)] = _validated_profile(str(name), raw)
        profiles.setdefault(DEFAULT_PROFILE, ClientProfile())
        normalized = _validated_config(config, profiles)
    normalized.profiles.setdefault(DEFAULT_PROFILE, ClientProfile())
    return normalized


def _validated_profile(name: str, raw: Mapping[str, JsonValue]) -> ClientProfile:
    try:
        return ClientProfile.model_validate({**raw, "name": name})
    except ValidationError as exc:
        msg = f"profile config is invalid: {name} ({_rejected_fields(exc)})"
        raise ConfigError(msg) from None


def _validated_config(
    document: Mapping[str, JsonValue],
    profiles: dict[str, ClientProfile],
) -> ClientConfig:
    try:
        return ClientConfig.model_validate({**document, "profiles": profiles})
    except ValidationError as exc:
        msg = f"profile config is invalid: {_rejected_fields(exc)}"
        raise ConfigError(msg) from None


def _rejected_fields(exc: ValidationError) -> str:
    """Name the fields a validation rejected, never the values it saw.

    Pydantic embeds the offending input in its own message, and one of these
    fields is the bearer token, so the chained cause is dropped and only field
    names reach the user.
    """
    names = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
    return ", ".join(names) or "unknown field"


def _config_data(config: ClientConfig) -> dict[str, JsonValue]:
    return validate_json_object(
        {
            "active_profile": config.active_profile,
            "profiles": {
                name: profile.model_dump(mode="json", exclude={"name"})
                for name, profile in config.profiles.items()
            },
        }
    )


def _is_legacy_context_config(config: Mapping[str, JsonValue]) -> bool:
    return "contexts" in config or "current_context" in config


def _apply_environment_overrides(profile: ClientProfile) -> ClientProfile:
    current_settings = settings()
    updates: dict[str, object] = {}
    if current_settings.endpoint.strip():
        updates["endpoint"] = current_settings.endpoint.strip()
    if current_settings.workspace.strip():
        updates["workspace"] = current_settings.workspace.strip()
    if current_settings.token.strip():
        updates["token"] = current_settings.token.strip()
    if current_settings.tls is not None:
        updates["tls"] = current_settings.tls
    if not updates:
        return profile
    return profile.model_copy(update=updates)


__all__ = [
    "CONFIG_DIRECTORY_MODE",
    "CONFIG_FILE_MODE",
    "DEFAULT_PROFILE",
    "PACKAGED_DEFAULT_ENDPOINT",
    "ClientConfig",
    "ClientProfile",
    "ClientSettings",
    "ConfigError",
    "activate_profile",
    "active_profile_name",
    "delete_profile",
    "empty_config",
    "get_profile",
    "list_profiles",
    "load_config",
    "reset_settings_cache",
    "save_config",
    "set_profile",
    "settings",
]
